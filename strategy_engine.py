# ================================================================
# STRATEGY ENGINE — ranks the universe by IV skew (spec section 5).
#
# For each ticker: pull spot price + option chain for the target
# expiration, find the ATM strike and the ~15-delta put among LIQUID
# (greeks-populated) contracts, compute ATM IV and IV skew, then rank
# the whole universe descending by skew.
#
# Deterministic, no LLM involved — talks to the Trading API directly.
# Run standalone (`python strategy_engine.py`) to print a ranked table.
# ================================================================
from __future__ import annotations

import dataclasses
import datetime
import json
import os
import sys
from dataclasses import dataclass

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import OptionChainRequest, StockLatestTradeRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetCalendarRequest

import black_scholes as bs
import config

_stock_client = StockHistoricalDataClient(config.API_KEY, config.API_SECRET)
_option_client = OptionHistoricalDataClient(config.API_KEY, config.API_SECRET)
_trading_client = TradingClient(config.API_KEY, config.API_SECRET, paper=config.PAPER)

# Where --json ranking snapshots get persisted (Change 1) -- same
# CACHE_DIR/write-cache convention as prefetch_news.py.
CACHE_DIR = "logs/cache"


@dataclass
class RankedCandidate:
    ticker: str
    spot: float
    expiration: str
    atm_strike: float
    atm_call_iv: float
    atm_put_iv: float
    atm_iv: float
    atm_call_symbol: str
    atm_call_ask: float
    atm_put_symbol: str
    atm_put_ask: float
    put_15d_symbol: str
    put_15d_strike: float
    put_15d_delta: float
    put_15d_iv: float
    put_15d_bid: float  # credit received selling this put -- Risk Manager's CSP sizing input
    iv_skew: float  # 15d put IV - ATM IV, in decimal (0.01 = 1 pct pt)
    # "alpaca" | "black_scholes_fallback" | "unavailable" -- which greeks
    # source _fetch_liquid_chain actually used for this ticker (Change 2).
    # Defaulted so existing callers building a RankedCandidate without this
    # field (e.g. tests/test_risk_manager.py's fixtures) keep working.
    greeks_source: str = "alpaca"


@dataclass
class SkippedTicker:
    ticker: str
    reason: str
    # Same greeks_source convention as RankedCandidate above; None when the
    # ticker was skipped before a chain fetch even happened (e.g. no spot
    # quote), since there's no fetch to report a source for.
    greeks_source: str | None = None


def _today_expiration() -> str:
    """Today's date as YYYY-MM-DD — the real "0DTE" expiration in production."""
    return datetime.date.today().isoformat()


def _next_trading_day(after: str) -> str | None:
    """First real market day strictly after `after` (YYYY-MM-DD), per Alpaca's
    own calendar -- no local weekday/holiday math, the calendar API is
    authoritative on weekends/market holidays. Used for the no-0dte-fallback-
    policy decision (2026-09-02, see NEXTSTEPS.md/STRATEGY_CHANGELOG.md):
    when a ticker has no usable same-day (0DTE) chain, rank_ticker() retries
    against this date's chain instead of skipping the ticker outright. Returns
    None if the calendar has nothing later (shouldn't happen in practice,
    defensive only)."""
    after_date = datetime.date.fromisoformat(after)
    calendar = _trading_client.get_calendar(
        GetCalendarRequest(start=after_date, end=after_date + datetime.timedelta(days=7))
    )
    for day in calendar:
        if day.date > after_date:
            return day.date.isoformat()
    return None


def _parse_occ_symbol(symbol: str) -> tuple[float, str]:
    """OCC option symbol -> (strike, 'C'|'P'). E.g. SPY260831P00764000 -> (764.0, 'P')."""
    strike = int(symbol[-8:]) / 1000.0
    right = symbol[-9]
    return strike, right


def _local_greeks(spot: float, strike: float, right: str, bid: float, ask: float,
                   expiration: str) -> tuple[float, float] | None:
    """Fallback when Alpaca's own greeks/IV are missing -- true for every 0DTE
    contract (Alpaca computes greeks via Black-Scholes with a literal T=0 for
    same-day expirations, a division-by-zero on their end; confirmed via their
    own Market Data FAQ and empirically, see black_scholes.py's module
    docstring and PROGRESS.md). Solves IV from the quote's own mid price using
    real hours-remaining-to-close as T, then derives delta from that IV.
    Returns None if there's no usable quote or no plausible solve -- caller
    treats that exactly like Alpaca returning nulls (skip this strike)."""
    if bid <= 0 or ask <= 0:
        return None
    mid = (bid + ask) / 2
    exp_date = datetime.date.fromisoformat(expiration)
    now = datetime.datetime.now(config.ET)
    T = bs.time_to_expiration_years(now, exp_date)
    iv = bs.implied_volatility(mid, spot, strike, T, config.RISK_FREE_RATE, right)
    if iv is None:
        return None
    delta = bs.bs_delta(spot, strike, T, config.RISK_FREE_RATE, iv, right)
    return delta, iv


def _fetch_liquid_chain(ticker: str, spot: float, expiration: str) -> tuple[dict, dict, str]:
    """Return (calls, puts, greeks_source). calls/puts are dicts of
    {strike: (delta, iv, symbol, bid, ask)}, liquid contracts only. Prefers
    Alpaca's own computed delta/IV when present (works fine for any non-0DTE
    expiration); falls back to a local Black-Scholes solve (_local_greeks)
    when they're null -- true for every 0DTE contract, which is what this
    project actually trades. Either way, a strike with no usable delta/IV
    (thin/no-interest, or a solve that isn't plausible) is dropped, not
    forced. bid/ask come from the same snapshot/same call — Risk Manager's
    sizing inputs (credit received, premium to pay) piggyback on this fetch
    rather than re-fetching.

    greeks_source records, per ticker, which source actually got used this
    run: "alpaca" if every usable contract came from Alpaca's own fields,
    "black_scholes_fallback" if any contract needed the local solver (flags
    a partial-or-total Alpaca greeks outage for this ticker/run), or
    "unavailable" if nothing usable came back at all -- an audit trail for
    exactly the kind of feed outage that went unrecorded on the real
    2026-08-31 09:41 ET run (see PROGRESS.md / NEXTSTEPS.md)."""
    req = OptionChainRequest(
        underlying_symbol=ticker,
        expiration_date=expiration,
        strike_price_gte=round(spot * (1 - config.STRIKE_RANGE_PCT)),
        strike_price_lte=round(spot * (1 + config.STRIKE_RANGE_PCT)),
    )
    chain = _option_client.get_option_chain(req)

    calls, puts = {}, {}
    sources_used = set()
    for symbol, snapshot in chain.items():
        strike, right = _parse_occ_symbol(symbol)
        quote = snapshot.latest_quote
        bid = getattr(quote, "bid_price", None) or 0.0
        ask = getattr(quote, "ask_price", None) or 0.0

        delta = getattr(snapshot.greeks, "delta", None)
        iv = getattr(snapshot, "implied_volatility", None)
        if delta is None or iv is None:
            local = _local_greeks(spot, strike, right, bid, ask, expiration)
            if local is None:
                continue
            delta, iv = local
            sources_used.add("black_scholes_fallback")
        else:
            sources_used.add("alpaca")

        bucket = calls if right == "C" else puts
        bucket[strike] = (delta, iv, symbol, bid, ask)

    if "black_scholes_fallback" in sources_used:
        greeks_source = "black_scholes_fallback"
    elif "alpaca" in sources_used:
        greeks_source = "alpaca"
    else:
        greeks_source = "unavailable"
    # stderr, not stdout -- --json's stdout must stay pure JSON for callers
    # that redirect it straight to a file (directional_selection.py /
    # execution_agent.py already use this same file=sys.stderr convention
    # for output that isn't the machine-readable payload).
    print(f"GREEKS_FEED ticker={ticker} source={greeks_source}", file=sys.stderr)

    return calls, puts, greeks_source


def _get_spot_price(ticker: str) -> float:
    """Last trade price. UNIVERSE names are all liquid enough that this is a
    fine spot proxy, and it sidesteps the bid/ask quote occasionally coming
    back with one leg at 0 near/after the close (seen live on AAPL/TSLA/MSFT
    — a naive (bid+ask)/2 there silently halves the price)."""
    trade = _stock_client.get_stock_latest_trade(
        StockLatestTradeRequest(symbol_or_symbols=ticker)
    )[ticker]
    return trade.price


def _rank_at_expiration(ticker: str, spot: float, expiration: str) -> RankedCandidate | SkippedTicker:
    """One attempt at building a RankedCandidate for `ticker` at a single
    `expiration` -- the whole chain-fetch/ATM/15-delta pipeline, minus the
    spot-price fetch (expiration-independent, done once by the caller).
    Split out of rank_ticker() so it can be tried at the primary (0DTE)
    expiration and, on failure, retried at a fallback expiration (the
    no-0dte-fallback-policy decision, 2026-09-02) without duplicating this
    logic."""
    calls, puts, greeks_source = _fetch_liquid_chain(ticker, spot, expiration)
    if not calls or not puts:
        return SkippedTicker(
            ticker, f"no liquid chain for {expiration} (calls={len(calls)}, puts={len(puts)})",
            greeks_source=greeks_source,
        )

    # ATM strike: closest to spot among strikes with BOTH a liquid call and put
    # (need both to average into ATM IV per spec section 4).
    common_strikes = set(calls) & set(puts)
    if not common_strikes:
        return SkippedTicker(
            ticker, f"no strike with both liquid call and put for ATM IV at {expiration}",
            greeks_source=greeks_source,
        )
    atm_strike = min(common_strikes, key=lambda k: abs(k - spot))
    _, atm_call_iv, atm_call_symbol, _, atm_call_ask = calls[atm_strike]
    _, atm_put_iv, atm_put_symbol, _, atm_put_ask = puts[atm_strike]
    atm_iv = (atm_call_iv + atm_put_iv) / 2

    # 15-delta put: closest |delta| to TARGET_PUT_DELTA among liquid puts.
    put_15d_strike = min(puts, key=lambda k: abs(abs(puts[k][0]) - config.TARGET_PUT_DELTA))
    put_15d_delta, put_15d_iv, put_15d_symbol, put_15d_bid, _ = puts[put_15d_strike]

    return RankedCandidate(
        ticker=ticker,
        spot=spot,
        expiration=expiration,
        atm_strike=atm_strike,
        atm_call_iv=atm_call_iv,
        atm_put_iv=atm_put_iv,
        atm_iv=atm_iv,
        atm_call_symbol=atm_call_symbol,
        atm_call_ask=atm_call_ask,
        atm_put_symbol=atm_put_symbol,
        atm_put_ask=atm_put_ask,
        put_15d_symbol=put_15d_symbol,
        put_15d_strike=put_15d_strike,
        put_15d_delta=put_15d_delta,
        put_15d_iv=put_15d_iv,
        put_15d_bid=put_15d_bid,
        iv_skew=put_15d_iv - atm_iv,
        greeks_source=greeks_source,
    )


def rank_ticker(
    ticker: str, expiration: str, fallback_expiration: str | None = None,
) -> RankedCandidate | SkippedTicker:
    """Ranks `ticker` at `expiration` (production: today's date, the real
    0DTE expiration). If that chain isn't usable and `fallback_expiration`
    is given, retries once against it before giving up -- the
    no-0dte-fallback-policy decision (2026-09-02, see NEXTSTEPS.md/
    STRATEGY_CHANGELOG.md): a ticker with no same-day chain gets ranked off
    the next trading day's chain instead of being skipped outright. Position
    lifecycle is unchanged either way (same-day force-close still applies,
    per execution_agent.py) -- this only changes which expiration gets
    ranked/sized/traded. Spot-quote failure is never retried: it isn't
    expiration-related, so a different expiration can't fix it."""
    spot = _get_spot_price(ticker)
    if spot <= 0:
        return SkippedTicker(ticker, "no valid spot quote")

    primary_result = _rank_at_expiration(ticker, spot, expiration)
    if isinstance(primary_result, RankedCandidate) or fallback_expiration is None:
        return primary_result

    fallback_result = _rank_at_expiration(ticker, spot, fallback_expiration)
    if isinstance(fallback_result, RankedCandidate):
        print(
            f"EXPIRATION_FALLBACK ticker={ticker} primary={expiration} fallback={fallback_expiration}",
            file=sys.stderr,
        )
        return fallback_result

    return SkippedTicker(
        ticker,
        f"{primary_result.reason}; also tried fallback {fallback_expiration}: {fallback_result.reason}",
        greeks_source=primary_result.greeks_source,
    )


def rank_universe(
    universe: list[str] | None = None,
    expiration: str | None = None,
) -> tuple[list[RankedCandidate], list[SkippedTicker]]:
    """Rank the universe by IV skew, descending. Returns (ranked, skipped).
    Computes the no-0dte-fallback-policy's fallback expiration (next trading
    day) once per run and passes it to every ticker -- cheap (one calendar
    call), and rank_ticker() only actually uses it for tickers whose primary
    chain isn't usable."""
    universe = universe or config.UNIVERSE
    expiration = expiration or config.EXPIRATION_OVERRIDE or _today_expiration()
    fallback_expiration = _next_trading_day(expiration)

    ranked, skipped = [], []
    for ticker in universe:
        result = rank_ticker(ticker, expiration, fallback_expiration=fallback_expiration)
        if isinstance(result, SkippedTicker):
            skipped.append(result)
        else:
            ranked.append(result)

    ranked.sort(key=lambda c: c.iv_skew, reverse=True)
    return ranked, skipped


def split_candidates(
    ranked: list[RankedCandidate],
    premium_sell_count: int | None = None,
) -> tuple[list[RankedCandidate], list[RankedCandidate]]:
    """Top N by skew -> premium-selling candidates. Rest -> directional candidates."""
    n = premium_sell_count if premium_sell_count is not None else config.PREMIUM_SELL_COUNT
    return ranked[:n], ranked[n:]


def _ranking_cache_path(expiration: str, cache_dir: str = CACHE_DIR) -> str:
    return os.path.join(cache_dir, f"ranking-{expiration}.json")


def _write_ranking_snapshot(payload: dict, expiration: str, cache_dir: str = CACHE_DIR) -> str:
    """Persist the --json payload to logs/cache/ranking-<expiration>.json
    (Change 1) -- additive to stdout, not a replacement, so existing callers
    that redirect stdout keep working. Defensive os.makedirs: logs/cache/
    already exists in this repo today, but don't assume it always will."""
    os.makedirs(cache_dir, exist_ok=True)
    path = _ranking_cache_path(expiration, cache_dir)
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    return path


def _build_json_payload(
    ranked: list[RankedCandidate],
    skipped: list[SkippedTicker],
    expiration: str,
    premium_sell: list[RankedCandidate],
    directional: list[RankedCandidate],
    run_id: str,
) -> dict:
    """Same {universe, expiration_used, ranked, skipped, premium_sell,
    directional} shape as before, plus run_id (Change 1) -- lets downstream
    tooling (e.g. risk_manager.py) confirm which ranking snapshot a given
    selection/decision file was derived from."""
    return {
        "run_id": run_id,
        "universe": config.UNIVERSE,
        "expiration_used": expiration,
        "ranked": [dataclasses.asdict(c) for c in ranked],
        "skipped": [dataclasses.asdict(s) for s in skipped],
        "premium_sell": [c.ticker for c in premium_sell],
        "directional": [c.ticker for c in directional],
    }


def _print_table(ranked: list[RankedCandidate], skipped: list[SkippedTicker]) -> None:
    print(f"{'Ticker':7} {'Spot':>9} {'ATM Strike':>10} {'ATM IV':>8} {'15dP Strike':>11} {'15dP Δ':>8} {'15dP IV':>8} {'Skew':>8}")
    for c in ranked:
        print(
            f"{c.ticker:7} {c.spot:9.2f} {c.atm_strike:10.1f} {c.atm_iv:8.4f} "
            f"{c.put_15d_strike:11.1f} {c.put_15d_delta:8.4f} {c.put_15d_iv:8.4f} {c.iv_skew:+8.4f}"
        )
    if skipped:
        print("\nSkipped:")
        for s in skipped:
            print(f"  {s.ticker}: {s.reason}")


if __name__ == "__main__":
    # --json: machine-readable ranking snapshot, same shape
    # scripts/save_mock_fixture.py already writes to
    # mock_cache/<date>/strategy_ranking.json. Captured to a cache file ONCE
    # per run (logs/cache/ranking-<date>.json, via _write_ranking_snapshot)
    # and fed to both the Analyst-dispatch/directional-selection step and
    # risk_manager.py's ranking_result argument, instead of each side re-fetching
    # live minutes apart -- see PROGRESS.md's "Ranking-consistency gap"
    # entry for why a second independently-timed fetch is a real problem
    # (0DTE IV skew moves fast enough that the top-3 split can shift
    # between two fetches a few minutes apart). run_id is stamped once at
    # the very start of the run so downstream tooling (e.g. risk_manager.py)
    # can later confirm which ranking snapshot a decision was derived from
    # -- a real gap on the real 2026-08-31 09:41 ET run, flagged by a live
    # Strategist Agent post-mortem (see PROGRESS.md / NEXTSTEPS.md).
    import argparse

    run_id = datetime.datetime.now(config.ET).isoformat()

    parser = argparse.ArgumentParser(description="Strategy Engine -- IV-skew ranking for the 0DTE universe.")
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON instead of the human-readable table")
    args = parser.parse_args()

    expiration = config.EXPIRATION_OVERRIDE or _today_expiration()
    ranked, skipped = rank_universe()
    premium_sell, directional = split_candidates(ranked)

    if args.json:
        payload = _build_json_payload(ranked, skipped, expiration, premium_sell, directional, run_id=run_id)
        # Persist first (Change 1) -- stdout stays pure JSON below so a
        # caller doing `python3 strategy_engine.py --json > out.json` still
        # gets exactly the payload, nothing more.
        _write_ranking_snapshot(payload, expiration)
        print(json.dumps(payload, indent=2))
    else:
        print(f"Ranking {config.UNIVERSE} for expiration {expiration}...\n")
        _print_table(ranked, skipped)
        print(f"\nPremium-selling candidates (top {config.PREMIUM_SELL_COUNT}): {[c.ticker for c in premium_sell]}")
        print(f"Directional candidates (rest): {[c.ticker for c in directional]}")
