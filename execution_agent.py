# ================================================================
# EXECUTION AGENT — spec section 19-20/27. Places Risk Manager's
# approved orders and manages their exits.
#
# Patterns below are adapted from the sibling alpacabot project's
# trade_manager.py/orders.py/bot.py -- proven live (its logs show real
# fills, including standing stop orders on option positions: "STOP
# FILLED @ 4.14"), not reinvented from scratch. Notable choices carried
# over deliberately:
#   - Entries are plain MARKET orders, not marketable limits -- alpacabot
#     defines a marketable-limit helper but its actual live bot never
#     calls it, always uses plain market for entries and force-closes.
#   - Take-profit is poll-based (check the bid, market-close when it's
#     hit), never a standing limit order -- alpacabot never trusted one
#     either. Only the stop-loss is a standing order at Alpaca.
#   - A force-close checks whether the standing stop already filled
#     before assuming failure -- the stop and the poll loop can race.
#
# Premium-selling (CSP, spec section 6): sell_to_open on entry, a
# standing stop (buy_to_close at 3x credit) submitted right after fill --
# real broker-side protection, not just the polling loop (see PROGRESS.md's
# correction on this, prompted by checking alpacabot's real fill logs).
# Take-profit (50% of credit) and EOD close are poll-based.
#
# Directional (spec section 15/17): buy_to_open on entry, once per
# morning only. No TP/SL at all -- purely time-based close, changed
# 2026-09-02 from a fixed 2:30 PM ET clock time to config.
# DIRECTIONAL_HOLD_MINUTES (30) after each position's own entry -- see
# config.py's comment and STRATEGY_CHANGELOG.md's 2026-09-02 entry for
# why. Much simpler than alpacabot's side (no avg-ups, no scaling).
#
# Position recycling (spec section 10), built 2026-09-02 -- see
# decide_premium_recycle()/attempt_premium_recycle() below. Premium-selling
# ONLY: when a short put closes (TP or SL) before config.
# NEW_ENTRY_CUTOFF_TIME, re-rank and open one replacement position with
# the freed capital. Directional never recycles -- spec section 17's
# once-per-morning entry is unchanged, only its hold DURATION changed.
# ================================================================
from __future__ import annotations

import datetime
import time
from dataclasses import dataclass

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderStatus, PositionIntent, TimeInForce
from alpaca.trading.requests import MarketOrderRequest, StopOrderRequest

import config
import risk_manager as rm

_trading_client = TradingClient(config.API_KEY, config.API_SECRET, paper=config.PAPER)
_option_data_client = OptionHistoricalDataClient(config.API_KEY, config.API_SECRET)

_TERMINAL_STATUSES = {
    OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED,
    OrderStatus.REJECTED, OrderStatus.DONE_FOR_DAY, OrderStatus.REPLACED,
}


# ----------------------------------------------------------------
# Order submission -- thin SDK wrappers, mirrors alpacabot/orders.py.
# Not unit-tested beyond request shape; validated live once the market
# is open, same split as risk_manager.py's get_account_snapshot().
# ----------------------------------------------------------------
def submit_sell_to_open(symbol: str, qty: int):
    """CSP entry -- sell the put."""
    req = MarketOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
        position_intent=PositionIntent.SELL_TO_OPEN,
    )
    return _trading_client.submit_order(req)


def submit_buy_to_open(symbol: str, qty: int):
    """Directional entry -- buy the call/put."""
    req = MarketOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
        position_intent=PositionIntent.BUY_TO_OPEN,
    )
    return _trading_client.submit_order(req)


def submit_stop_buy_to_close(symbol: str, qty: int, stop_price: float):
    """CSP stop-loss -- a standing order at Alpaca, buy-side since closing
    a short put means buying it back. Mirrors alpacabot's
    submit_stop_market_sell (same mechanism, opposite side -- that one
    protects a long by selling on a drop, this one protects a short by
    buying on a rise)."""
    req = StopOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
        stop_price=round(stop_price, 2), position_intent=PositionIntent.BUY_TO_CLOSE,
    )
    return _trading_client.submit_order(req)


def submit_market_buy_to_close(symbol: str, qty: int):
    """Force-close a short position (CSP hitting TP, or EOD)."""
    req = MarketOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
        position_intent=PositionIntent.BUY_TO_CLOSE,
    )
    return _trading_client.submit_order(req)


def submit_market_sell_to_close(symbol: str, qty: int):
    """Force-close a long position (directional time-exit)."""
    req = MarketOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
        position_intent=PositionIntent.SELL_TO_CLOSE,
    )
    return _trading_client.submit_order(req)


def wait_for_fill(order_id: str, timeout: float = 30.0, poll: float = 0.5):
    """Poll an order until it reaches a terminal status or `timeout` elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        o = _trading_client.get_order_by_id(order_id)
        if o.status in _TERMINAL_STATUSES:
            return o
        time.sleep(poll)
    raise TimeoutError(f"Order {order_id} did not reach terminal status within {timeout}s")


def get_order(order_id: str):
    return _trading_client.get_order_by_id(order_id)


def cancel_order(order_id: str) -> None:
    _trading_client.cancel_order_by_id(order_id)


# ----------------------------------------------------------------
# Positions
# ----------------------------------------------------------------
@dataclass
class PremiumPosition:
    ticker: str
    symbol: str
    qty: int
    credit: float  # actual fill price (premium received per share)
    entered_at: datetime.datetime
    take_profit_price: float
    stop_loss_price: float
    stop_order_id: str | None = None
    exit_reason: str | None = None
    exit_price: float | None = None


@dataclass
class DirectionalPosition:
    ticker: str
    symbol: str
    qty: int
    entry_price: float
    entered_at: datetime.datetime
    exit_reason: str | None = None
    exit_price: float | None = None


def build_premium_position(decision: rm.Decision, fill_price: float, entered_at: datetime.datetime) -> PremiumPosition:
    """TP/SL derived from the ACTUAL fill price, not Risk Manager's
    pre-trade credit_price estimate -- the real execution price is what
    matters once the position is actually open."""
    return PremiumPosition(
        ticker=decision.ticker,
        symbol=decision.symbol,
        qty=decision.quantity,
        credit=fill_price,
        entered_at=entered_at,
        take_profit_price=fill_price * 0.5,
        stop_loss_price=fill_price * 3.0,
    )


def build_directional_position(decision: rm.Decision, fill_price: float, entered_at: datetime.datetime) -> DirectionalPosition:
    return DirectionalPosition(
        ticker=decision.ticker,
        symbol=decision.symbol,
        qty=decision.quantity,
        entry_price=fill_price,
        entered_at=entered_at,
    )


# ----------------------------------------------------------------
# Exit decisions -- pure, no I/O. The actual close order is submitted by
# the caller once one of these returns a reason.
# ----------------------------------------------------------------
def check_premium_exit(
    position: PremiumPosition,
    now: datetime.datetime,
    current_bid: float | None,
    stop_order_status: OrderStatus | None,
) -> str | None:
    """Returns 'SL' / 'EOD' / 'TP' if this position should close now, else
    None. Priority: a filled stop is a fact that already happened, so it's
    checked first; EOD is a hard deadline independent of price; TP is the
    poll-based check (spec section 9)."""
    if stop_order_status == OrderStatus.FILLED:
        return "SL"
    if now.timetz().replace(tzinfo=None) >= config.PREMIUM_EOD_CLOSE_TIME:
        return "EOD"
    if current_bid is not None and current_bid <= position.take_profit_price:
        return "TP"
    return None


def check_directional_exit(position: DirectionalPosition, now: datetime.datetime) -> str | None:
    """Purely time-based (spec section 18) -- no TP/SL fields exist on
    this side at all. Changed 2026-09-02: relative to this position's OWN
    entry (config.DIRECTIONAL_HOLD_MINUTES after entered_at), not a fixed
    clock time -- see config.py's comment for why. Both position.entered_at
    and now must be timezone-aware and comparable (both config.ET in
    production)."""
    elapsed = now - position.entered_at
    if elapsed >= datetime.timedelta(minutes=config.DIRECTIONAL_HOLD_MINUTES):
        return "TIME"
    return None


def get_bid(symbol: str) -> float | None:
    """Latest bid for an option contract, or None if unavailable (thin
    enough it isn't worth unit-testing beyond what strategy_engine.py's
    own quote handling already covers)."""
    quote = _option_data_client.get_option_latest_quote(
        OptionLatestQuoteRequest(symbol_or_symbols=symbol)
    )[symbol]
    bid = getattr(quote, "bid_price", None)
    return float(bid) if bid else None


# ----------------------------------------------------------------
# Entry -- submit, wait for fill, build the position, (premium side)
# submit the standing stop.
# ----------------------------------------------------------------
def open_premium_position(decision: rm.Decision) -> PremiumPosition:
    order = submit_sell_to_open(decision.symbol, decision.quantity)
    filled = wait_for_fill(order.id)
    if filled.status != OrderStatus.FILLED:
        raise RuntimeError(f"{decision.symbol} entry did not fill: status={filled.status}")
    fill_price = float(filled.filled_avg_price)
    position = build_premium_position(decision, fill_price, entered_at=datetime.datetime.now(config.ET))
    stop_order = submit_stop_buy_to_close(position.symbol, position.qty, position.stop_loss_price)
    position.stop_order_id = stop_order.id
    log_entry(
        position.ticker, position.symbol, position.qty, fill_price, position.entered_at, "SELL_TO_OPEN",
        take_profit_price=position.take_profit_price, stop_loss_price=position.stop_loss_price,
    )
    return position


def open_directional_position(decision: rm.Decision) -> DirectionalPosition:
    order = submit_buy_to_open(decision.symbol, decision.quantity)
    filled = wait_for_fill(order.id)
    if filled.status != OrderStatus.FILLED:
        raise RuntimeError(f"{decision.symbol} entry did not fill: status={filled.status}")
    fill_price = float(filled.filled_avg_price)
    position = build_directional_position(decision, fill_price, entered_at=datetime.datetime.now(config.ET))
    log_entry(position.ticker, position.symbol, position.qty, fill_price, position.entered_at, "BUY_TO_OPEN")
    return position


# ----------------------------------------------------------------
# Tick -- one check-and-maybe-close cycle. Returns True once the position
# is closed. On close: cancels any still-open standing order, submits the
# close, and if the standing stop had actually already filled underneath
# the poll loop (a real race, not hypothetical -- alpacabot's own
# _force_close handles exactly this), recovers the real exit price from
# the stop order instead of treating it as a second, spurious close.
# ----------------------------------------------------------------
def tick_premium_position(position: PremiumPosition) -> bool:
    now = datetime.datetime.now(config.ET)
    stop_status = None
    if position.stop_order_id is not None:
        try:
            stop_status = get_order(position.stop_order_id).status
        except Exception:
            stop_status = None

    bid = None
    try:
        bid = get_bid(position.symbol)
    except Exception:
        bid = None

    reason = check_premium_exit(position, now, bid, stop_status)
    if reason is None:
        return False

    if reason == "SL":
        # The standing stop already filled -- nothing left to submit,
        # just record the real fill price.
        stop_order = get_order(position.stop_order_id)
        position.exit_reason = "SL"
        position.exit_price = float(stop_order.filled_avg_price)
        log_exit(position.ticker, position.symbol, position.qty, position.exit_price, now, position.exit_reason)
        return True

    # TP or EOD: cancel the standing stop first (it's still live and would
    # otherwise try to close a position we're about to close ourselves),
    # then market-close. If the cancel loses a race against a genuine
    # stop fill, recover that fill instead of double-closing.
    try:
        cancel_order(position.stop_order_id)
    except Exception:
        pass
    try:
        order = submit_market_buy_to_close(position.symbol, position.qty)
        filled = wait_for_fill(order.id)
        position.exit_reason = reason
        position.exit_price = float(filled.filled_avg_price)
    except Exception:
        stop_order = get_order(position.stop_order_id)
        if stop_order.filled_avg_price:
            position.exit_reason = "SL"
            position.exit_price = float(stop_order.filled_avg_price)
        else:
            raise
    log_exit(position.ticker, position.symbol, position.qty, position.exit_price, now, position.exit_reason)
    return True


# ----------------------------------------------------------------
# Position recycling (spec section 10), built 2026-09-02 -- see
# config.py's NEW_ENTRY_CUTOFF_TIME comment and STRATEGY_CHANGELOG.md's
# 2026-09-02 entry. Triggered when a premium position closes (TP or SL,
# not EOD -- EOD only fires once the day is already ending). Split the
# same way as the rest of this module: decide_premium_recycle() is pure
# and unit-tested; attempt_premium_recycle() is the thin live wrapper
# (fresh account snapshot + fresh ranking + real order placement).
# ----------------------------------------------------------------
def decide_premium_recycle(
    premium_ranked: list,
    held_tickers: set,
    total_premium_budget: float,
    now: datetime.datetime,
) -> "rm.Decision | None":
    """Pure decision logic. premium_ranked: a FRESH strategy_engine ranking's
    premium-selling side (already sorted by IV skew descending -- 0DTE skew
    moves fast, so recycling re-ranks rather than reusing the morning's
    stale snapshot, same reasoning as the 2026-08-31 ranking-consistency
    fix). held_tickers: every ticker currently open on EITHER side --
    never re-enter one already held (spec section 10, literal). Sizes the
    top eligible candidate against ONE slot's worth of the current total
    premium budget (total_premium_budget / config.PREMIUM_SELL_COUNT) --
    approximates the original equal N-way split for the one slot being
    replaced, rather than handing a single recycled position the entire
    currently-free budget. Returns None past the entry cutoff, if nothing
    is eligible, or if the top eligible candidate isn't affordable --
    never forces a trade (spec section 8's principle applies here too)."""
    if now.timetz().replace(tzinfo=None) >= config.NEW_ENTRY_CUTOFF_TIME:
        return None
    eligible = [c for c in premium_ranked if c.ticker not in held_tickers]
    if not eligible:
        return None
    top = eligible[0]
    slot_budget = total_premium_budget / config.PREMIUM_SELL_COUNT
    candidate = rm.PremiumCandidate(
        ticker=top.ticker, symbol=top.put_15d_symbol,
        strike=top.put_15d_strike, credit_price=top.put_15d_bid,
    )
    decision = rm.allocate_premium_positions([candidate], slot_budget)[0]
    return decision if decision.approved else None


def attempt_premium_recycle(held_tickers: set, num_directional_selected: int) -> PremiumPosition | None:
    """Live wrapper: fresh account snapshot + fresh universe ranking, then
    decide_premium_recycle(). Opens the position for real if approved.
    Thin I/O glue, not unit-tested beyond decide_premium_recycle()'s pure
    logic -- same split as get_account_snapshot()/rank_universe() elsewhere
    in this codebase."""
    import strategy_engine as se

    now = datetime.datetime.now(config.ET)
    snapshot = rm.get_account_snapshot()
    budgets = rm.compute_budgets(snapshot, num_directional_selected)
    ranked, _ = se.rank_universe()
    premium_ranked, _ = se.split_candidates(ranked)

    decision = decide_premium_recycle(premium_ranked, held_tickers, budgets.premium_sell_budget, now)
    if decision is None:
        return None
    return open_premium_position(decision)


def tick_directional_position(position: DirectionalPosition) -> bool:
    now = datetime.datetime.now(config.ET)
    reason = check_directional_exit(position, now)
    if reason is None:
        return False

    order = submit_market_sell_to_close(position.symbol, position.qty)
    filled = wait_for_fill(order.id)
    position.exit_reason = reason
    position.exit_price = float(filled.filled_avg_price)
    log_exit(position.ticker, position.symbol, position.qty, position.exit_price, now, position.exit_reason)
    return True


# ----------------------------------------------------------------
# Logging -- spec section 27. Append-only, one line per event.
# ----------------------------------------------------------------
def _format_entry_line(
    ticker, symbol, qty, price, timestamp, side,
    take_profit_price: float | None = None, stop_loss_price: float | None = None,
) -> str:
    """Premium entries pass the REAL take_profit_price/stop_loss_price just
    computed from the actual fill (build_premium_position) so the log is
    self-sufficient for later audits -- risk-decisions.json only has Risk
    Manager's pre-trade estimate, a different number (see the AAPL
    2026-08-31 post-mortem: pre-trade TP was $0.09, the real armed
    threshold off the $0.27 fill was $0.135 -- comparing an exit against
    the wrong one looks like a bug that isn't one). Directional entries
    have no TP/SL at all (spec section 18) and omit both fields."""
    line = f"ENTRY {timestamp.isoformat()} {ticker} {symbol} qty={qty} price={price:.2f} side={side}"
    if take_profit_price is not None:
        line += f" tp={take_profit_price:.2f}"
    if stop_loss_price is not None:
        line += f" sl={stop_loss_price:.2f}"
    return line


def log_entry(
    ticker, symbol, qty, price, timestamp, side,
    take_profit_price: float | None = None, stop_loss_price: float | None = None,
) -> None:
    _append_log(_format_entry_line(
        ticker, symbol, qty, price, timestamp, side, take_profit_price, stop_loss_price,
    ))


def log_exit(ticker, symbol, qty, price, timestamp, reason) -> None:
    _append_log(f"EXIT  {timestamp.isoformat()} {ticker} {symbol} qty={qty} price={price:.2f} reason={reason}")


def _format_decisions_loaded_line(now: datetime.datetime, risk_decisions_path: str, data: dict) -> str:
    """Heartbeat-style line (matches run()'s other print()s) recording
    which decisions file was actually consumed, and its generated_at if
    present. Closes the other gap the same post-mortem surfaced: with no
    record of which risk-decisions-<date>.json version was traded, tying
    a live fill back to its decision file took manual cross-referencing
    of contract symbols/quantities across versions. generated_at is read
    defensively via .get() -- older decision files predate the field."""
    generated_at = data.get("generated_at", "unknown")
    return f"[{now.isoformat()}] Loaded decisions from {risk_decisions_path} (generated_at={generated_at})"


def _append_log(line: str) -> None:
    import os
    os.makedirs("logs", exist_ok=True)
    today = datetime.datetime.now(config.ET).date().isoformat()
    with open(f"logs/{today}-execution.log", "a") as f:
        f.write(line + "\n")


# ----------------------------------------------------------------
# Main loop -- opens every APPROVE decision, then polls until every
# position is closed. Position recycling (spec section 10), built
# 2026-09-02: when a premium position closes, attempt_premium_recycle()
# tries to open one replacement with the freed capital before the day's
# entry cutoff (config.NEW_ENTRY_CUTOFF_TIME) -- see that function's
# docstring. Directional never recycles -- it enters once in the morning
# only (spec section 17, unchanged) and each position now exits
# config.DIRECTIONAL_HOLD_MINUTES after its own entry instead of at a
# fixed clock time (see config.py's 2026-09-02 comment).
# ----------------------------------------------------------------
def run(risk_decisions_path: str) -> None:
    import json

    with open(risk_decisions_path) as f:
        data = json.load(f)

    print(_format_decisions_loaded_line(datetime.datetime.now(config.ET), risk_decisions_path, data), flush=True)

    premium_positions = [
        open_premium_position(rm.Decision(**d))
        for d in data["premium_decisions"] if d["approved"]
    ]
    directional_positions = [
        open_directional_position(rm.Decision(**d))
        for d in data["directional_decisions"] if d["approved"]
    ]
    # Every directional TICKER Risk Manager evaluated (approved or
    # rejected-for-cost/mismatch) equals the day's original selected count
    # -- size_directional_positions()/build_directional_candidates() both
    # return one Decision per input candidate, so this is recoverable from
    # the decisions file alone without re-reading selection-result.json.
    num_directional_selected = len(data["directional_decisions"])

    print(f"[{datetime.datetime.now(config.ET).isoformat()}] Opened "
          f"{len(premium_positions)} premium + {len(directional_positions)} "
          f"directional position(s). Monitoring...", flush=True)

    while premium_positions or directional_positions:
        still_open_premium = []
        closed_count = 0
        for p in premium_positions:
            if tick_premium_position(p):
                closed_count += 1
            else:
                still_open_premium.append(p)
        premium_positions = still_open_premium

        directional_positions = [p for p in directional_positions if not tick_directional_position(p)]

        for _ in range(closed_count):
            held = ({p.ticker for p in premium_positions} | {p.ticker for p in directional_positions})
            new_position = attempt_premium_recycle(held, num_directional_selected)
            if new_position is not None:
                premium_positions.append(new_position)

        # Heartbeat for manual monitoring (stdout, captured by
        # run_morning_trigger.sh into logs/<date>-execution-run.out) --
        # entries/exits themselves are logged separately by log_entry/
        # log_exit into logs/<date>-execution.log.
        print(f"[{datetime.datetime.now(config.ET).isoformat()}] "
              f"{len(premium_positions)} premium + {len(directional_positions)} "
              f"directional position(s) still open.", flush=True)
        if premium_positions or directional_positions:
            time.sleep(config.MONITOR_POLL_SECS)

    print(f"[{datetime.datetime.now(config.ET).isoformat()}] All positions closed. Exiting.", flush=True)


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: execution_agent.py <risk-decisions.json>", file=sys.stderr)
        sys.exit(1)
    run(sys.argv[1])
