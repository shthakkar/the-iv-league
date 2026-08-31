# ================================================================
# RISK MANAGER — spec section 22, covers both strategy sides.
#
# Final authority before execution: turns a ranked/selected candidate into
# an APPROVE (contract, quantity, capital allocated, TP/SL if applicable)
# or REJECT (+ reason). Deterministic, no LLM (spec section 30) — same
# reasoning as strategy_engine.py and directional_selection.py.
#
# Premium-selling side sells a cash-secured put (CSP), not a defined-risk
# spread (spec section 6, changed 2026-08-30 — see PROGRESS.md). Buying
# power per contract is the full strike x 100, so a flat equal-N-way split
# of the premium budget (spec section 8) can silently strand an expensive
# underlying (SPY, QQQ) that doesn't fit its own equal share even though
# cheaper names in the same batch have leftover room. allocate_premium_
# positions() pools that leftover across two passes instead of rejecting
# outright — see its docstring.
#
# Directional side sizes an ATM/slightly-ITM 0DTE call or put toward an
# equal split of the (much smaller) directional budget, per spec section
# 16's literal wording -- no leftover-pooling needed there, since a single
# option contract's premium is a small fraction of the underlying's price
# (unlike a CSP's full-strike buying-power requirement), so the "too
# expensive for its own share" case is not a realistic problem on this
# side. No TP/SL fields — directional exit is purely time-based (section 18).
#
# Max daily loss (spec section 23) is deliberately NOT implemented here —
# dropped for V1, see PROGRESS.md.
# ================================================================
from __future__ import annotations

import math
from dataclasses import dataclass

from alpaca.trading.client import TradingClient

import config

_trading_client = TradingClient(config.API_KEY, config.API_SECRET, paper=config.PAPER)


@dataclass
class AccountSnapshot:
    cash: float
    options_buying_power: float
    equity: float

    @property
    def available_balance(self) -> float:
        """Balance basis for sizing options positions. options_buying_power,
        falling back to cash if that field is ever missing/zero -- margin
        doesn't extend to options on this account (spec section 7 amendment,
        see PROGRESS.md), so options_buying_power is the real ceiling, not
        the margin-inclusive buying_power field."""
        return self.options_buying_power or self.cash


@dataclass
class Budgets:
    premium_sell_budget: float
    directional_budget: float


@dataclass
class Decision:
    ticker: str
    approved: bool
    reason: str
    symbol: str | None = None
    strike: float | None = None
    quantity: int = 0
    capital_allocated: float = 0.0
    take_profit_price: float | None = None
    stop_loss_price: float | None = None


@dataclass
class PremiumCandidate:
    ticker: str
    symbol: str
    strike: float
    credit_price: float  # premium received per share (bid at entry)


@dataclass
class DirectionalCandidate:
    ticker: str
    symbol: str
    ask_price: float  # premium to pay per share


def get_account_snapshot() -> AccountSnapshot:
    """Live call to the raw alpaca-py SDK (not the MCP server -- deterministic
    risk logic talks to the Trading API directly, same split as strategy_engine.py)."""
    account = _trading_client.get_account()
    return AccountSnapshot(
        cash=float(account.cash),
        options_buying_power=float(account.options_buying_power),
        equity=float(account.equity),
    )


def compute_budgets(snapshot: AccountSnapshot) -> Budgets:
    balance = snapshot.available_balance
    return Budgets(
        premium_sell_budget=balance * config.PREMIUM_SELL_ALLOCATION_PCT,
        directional_budget=balance * config.DIRECTIONAL_ALLOCATION_PCT,
    )


def allocate_premium_positions(
    candidates: list[PremiumCandidate],
    budget: float,
    max_exposure_per_underlying: float | None = None,
) -> list[Decision]:
    """CSP sizing across a shared budget pool, two passes:

    1. Give each candidate an equal target share (budget / N), sized down to
       whole contracts. Anything left unused (a share that wasn't evenly
       divisible by that name's strike, or a whole share nobody could use at
       all) goes into a pooled leftover.
    2. Retry any candidate that couldn't afford even 1 contract on its own
       share against that pooled leftover.

    Never spends more than `budget` total. Never forces a trade a candidate
    can't afford even with the full pooled leftover -- REJECTs with a reason
    naming its own cost instead (spec section 8: "never force a trade merely
    to use all available capital")."""
    n = len(candidates)
    if n == 0:
        return []

    target_share = budget / n
    decisions: dict[str, Decision] = {}
    leftover = 0.0
    needs_retry: list[PremiumCandidate] = []

    def _cap(share: float) -> float:
        return min(share, max_exposure_per_underlying) if max_exposure_per_underlying is not None else share

    for c in candidates:
        cost_per_contract = c.strike * 100
        usable = _cap(target_share)
        qty = math.floor(usable / cost_per_contract)
        if qty >= 1:
            capital = qty * cost_per_contract
            leftover += target_share - capital
            decisions[c.ticker] = _approved_premium_decision(c, qty, capital)
        else:
            leftover += target_share
            needs_retry.append(c)

    for c in needs_retry:
        cost_per_contract = c.strike * 100
        usable = _cap(leftover)
        qty = math.floor(usable / cost_per_contract)
        if qty >= 1:
            capital = qty * cost_per_contract
            leftover -= capital
            decisions[c.ticker] = _approved_premium_decision(c, qty, capital)
        else:
            decisions[c.ticker] = Decision(
                ticker=c.ticker,
                approved=False,
                reason=(
                    f"cost per contract (strike {c.strike:.2f} x 100 = "
                    f"${cost_per_contract:,.0f}) exceeds available budget "
                    f"(equal share ${target_share:,.0f}, pooled leftover ${leftover:,.0f})"
                ),
            )

    return [decisions[c.ticker] for c in candidates]


def _approved_premium_decision(c: PremiumCandidate, qty: int, capital: float) -> Decision:
    return Decision(
        ticker=c.ticker,
        approved=True,
        reason="approved",
        symbol=c.symbol,
        strike=c.strike,
        quantity=qty,
        capital_allocated=capital,
        take_profit_price=c.credit_price * 0.5,
        stop_loss_price=c.credit_price * 3.0,
    )


def size_directional_positions(candidates: list[DirectionalCandidate], budget: float) -> list[Decision]:
    """Equal split of the directional budget across whatever was selected
    (spec section 16: "split across however many trades are taken"). No
    leftover-pooling like the premium side -- an ATM 0DTE option's premium
    is a small fraction of the underlying's price, so a name being too
    pricey for its own equal share isn't a realistic failure mode here.
    No TP/SL: directional exit is purely time-based (spec section 18)."""
    n = len(candidates)
    if n == 0:
        return []

    share = budget / n
    decisions = []
    for c in candidates:
        cost_per_contract = c.ask_price * 100
        qty = math.floor(share / cost_per_contract) if cost_per_contract > 0 else 0
        if qty >= 1:
            decisions.append(Decision(
                ticker=c.ticker,
                approved=True,
                reason="approved",
                symbol=c.symbol,
                quantity=qty,
                capital_allocated=qty * cost_per_contract,
            ))
        else:
            decisions.append(Decision(
                ticker=c.ticker,
                approved=False,
                reason=(
                    f"contract cost (ask {c.ask_price:.2f} x 100 = ${cost_per_contract:,.2f}) "
                    f"exceeds equal share ${share:,.2f}"
                ),
            ))
    return decisions


def build_premium_candidates(ranked: list) -> list[PremiumCandidate]:
    """ranked: strategy_engine.split_candidates()'s premium-selling side (top-N
    RankedCandidate by IV skew). Pulls the 15d put leg strategy_engine already
    identified -- no extra chain fetch."""
    return [
        PremiumCandidate(ticker=c.ticker, symbol=c.put_15d_symbol, strike=c.put_15d_strike,
                          credit_price=c.put_15d_bid)
        for c in ranked
    ]


def build_directional_candidates(selected: list[dict], ranked_lookup: dict) -> list[DirectionalCandidate]:
    """selected: directional_selection.select_directional()'s 'selected' list
    ({ticker, direction, confidence, ...} dicts -- UNDECIDED never appears here,
    already dropped upstream). ranked_lookup: {ticker: RankedCandidate} built
    from strategy_engine's directional split. BULLISH -> buy the ATM call,
    BEARISH -> buy the ATM put (spec section 15)."""
    candidates = []
    for s in selected:
        c = ranked_lookup[s["ticker"]]
        if s["direction"] == "BULLISH":
            candidates.append(DirectionalCandidate(ticker=c.ticker, symbol=c.atm_call_symbol, ask_price=c.atm_call_ask))
        elif s["direction"] == "BEARISH":
            candidates.append(DirectionalCandidate(ticker=c.ticker, symbol=c.atm_put_symbol, ask_price=c.atm_put_ask))
    return candidates


@dataclass
class RiskManagerResult:
    snapshot: AccountSnapshot
    budgets: Budgets
    premium_decisions: list[Decision]
    directional_decisions: list[Decision]


def evaluate(
    premium_ranked: list,
    directional_ranked_lookup: dict,
    directional_selected: list[dict],
    snapshot: AccountSnapshot | None = None,
) -> RiskManagerResult:
    """Top-level entry point: strategy_engine's premium-selling candidates +
    directional_selection's selected list in, a full APPROVE/REJECT batch out.
    One account snapshot for the whole batch (not re-fetched per candidate)."""
    snapshot = snapshot or get_account_snapshot()
    budgets = compute_budgets(snapshot)

    premium_candidates = build_premium_candidates(premium_ranked)
    # No per-name concentration cap -- spec section 8 is spec-literal here:
    # "target roughly equal allocation... never force a trade merely to use
    # all available capital." A 35%-of-budget cap lived here previously
    # (config.MAX_EXPOSURE_PER_UNDERLYING_PCT, never spec-fixed) but was
    # actively working against the leftover-pooling pass below: pooling
    # exists specifically to rescue a candidate that couldn't afford its own
    # equal share, and the cap then re-blocked that same rescue once the
    # pool was big enough to actually help. Removed 2026-08-31 after it
    # rejected every single premium-selling candidate on a live run purely
    # on cap-vs-pool tension, not genuine unaffordability -- see PROGRESS.md.
    premium_decisions = allocate_premium_positions(
        premium_candidates,
        budgets.premium_sell_budget,
    )

    directional_candidates = build_directional_candidates(directional_selected, directional_ranked_lookup)
    directional_decisions = size_directional_positions(directional_candidates, budgets.directional_budget)

    combined = apply_max_positions(premium_decisions + directional_decisions)
    n_premium = len(premium_decisions)
    return RiskManagerResult(
        snapshot=snapshot,
        budgets=budgets,
        premium_decisions=combined[:n_premium],
        directional_decisions=combined[n_premium:],
    )


def apply_max_positions(decisions: list[Decision], max_positions: int | None = None) -> list[Decision]:
    """Trims approved decisions down to MAX_POSITIONS, keeping priority
    (input) order and downgrading the overflow to REJECTED. Rejected
    decisions already in the list pass through untouched."""
    max_positions = max_positions if max_positions is not None else config.MAX_POSITIONS
    result = []
    approved_so_far = 0
    for d in decisions:
        if d.approved and approved_so_far >= max_positions:
            result.append(Decision(
                ticker=d.ticker,
                approved=False,
                reason=f"exceeds MAX_POSITIONS ({max_positions})",
                symbol=d.symbol,
                strike=d.strike,
            ))
            continue
        if d.approved:
            approved_so_far += 1
        result.append(d)
    return result


def _print_decisions(label: str, decisions: list[Decision]) -> None:
    print(f"\n{label}:")
    for d in decisions:
        if d.approved:
            tp_sl = (
                f" TP={d.take_profit_price:.2f} SL={d.stop_loss_price:.2f}"
                if d.take_profit_price is not None else ""
            )
            print(f"  APPROVE {d.ticker:6} {d.symbol} qty={d.quantity} capital=${d.capital_allocated:,.0f}{tp_sl}")
        else:
            print(f"  REJECT  {d.ticker:6} {d.reason}")


if __name__ == "__main__":
    # Usage: risk_manager.py <ranking_result.json> <selection_result.json> [--json]
    #   ranking_result.json: strategy_engine.py --json's stdout (same shape
    #     scripts/save_mock_fixture.py writes to
    #     mock_cache/<date>/strategy_ranking.json -- that fixture works here
    #     directly for offline testing). Loaded, not re-fetched: this used to
    #     call strategy_engine.rank_universe() itself, a SECOND independent
    #     live chain fetch minutes after the one the morning-decision prompt
    #     already did for Analyst dispatch/directional selection -- 0DTE IV
    #     skew moves fast enough that the two fetches could disagree on the
    #     premium-selling/directional split (surfaced live 2026-08-31, see
    #     PROGRESS.md's "Ranking-consistency gap" entry). Now both sides of
    #     one run consume the exact same ranking snapshot.
    #   selection_result.json: directional_selection.py's stdout, i.e. a file
    #     containing {"selected": [...], ...}. No default/fallback path here
    #     on purpose -- an automated run must pass today's real selection
    #     result, never silently fall back to a stale fixture. For manual
    #     testing against the frozen real morning, pass
    #     mock_cache/2026-08-28/{strategy_ranking,selection_result}.json
    #     explicitly.
    import argparse
    import dataclasses
    import json

    import strategy_engine as se

    parser = argparse.ArgumentParser(description="Risk Manager -- APPROVE/REJECT batch for one day's candidates.")
    parser.add_argument("ranking_result", help="Path to strategy_engine.py --json's JSON output")
    parser.add_argument("selection_result", help="Path to directional_selection.py's JSON output")
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON instead of the human-readable table")
    args = parser.parse_args()

    with open(args.ranking_result) as f:
        ranking_payload = json.load(f)
    ranked = [se.RankedCandidate(**c) for c in ranking_payload["ranked"]]
    skipped = [se.SkippedTicker(**s) for s in ranking_payload["skipped"]]
    expiration_used = ranking_payload["expiration_used"]
    premium_ranked, directional_ranked = se.split_candidates(ranked)
    ranked_lookup = {c.ticker: c for c in ranked}

    with open(args.selection_result) as f:
        directional_selected = json.load(f)["selected"]

    result = evaluate(premium_ranked, ranked_lookup, directional_selected)

    if args.json:
        print(json.dumps(dataclasses.asdict(result), indent=2))
    else:
        print(f"Ranking {config.UNIVERSE} for expiration {expiration_used}...")
        print(f"Premium-selling candidates: {[c.ticker for c in premium_ranked]}")
        print(f"Directional candidates: {[c.ticker for c in directional_ranked]}")
        if skipped:
            print(f"Skipped: {[(s.ticker, s.reason) for s in skipped]}")
        print(f"Directional selected: {[c['ticker'] for c in directional_selected]}")
        print(f"\nAccount: cash=${result.snapshot.cash:,.0f} options_buying_power=${result.snapshot.options_buying_power:,.0f}")
        print(f"Budgets: premium=${result.budgets.premium_sell_budget:,.0f} directional=${result.budgets.directional_budget:,.0f}")
        _print_decisions("Premium-selling decisions", result.premium_decisions)
        _print_decisions("Directional decisions", result.directional_decisions)
