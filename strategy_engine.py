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

import datetime
from dataclasses import dataclass

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import OptionChainRequest, StockLatestTradeRequest

import black_scholes as bs
import config

_stock_client = StockHistoricalDataClient(config.API_KEY, config.API_SECRET)
_option_client = OptionHistoricalDataClient(config.API_KEY, config.API_SECRET)


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


@dataclass
class SkippedTicker:
    ticker: str
    reason: str


def _today_expiration() -> str:
    """Today's date as YYYY-MM-DD — the real "0DTE" expiration in production."""
    return datetime.date.today().isoformat()


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


def _fetch_liquid_chain(ticker: str, spot: float, expiration: str) -> tuple[dict, dict]:
    """Return (calls, puts) dicts of {strike: (delta, iv, symbol, bid, ask)}, liquid
    contracts only. Prefers Alpaca's own computed delta/IV when present (works
    fine for any non-0DTE expiration); falls back to a local Black-Scholes solve
    (_local_greeks) when they're null -- true for every 0DTE contract, which is
    what this project actually trades. Either way, a strike with no usable
    delta/IV (thin/no-interest, or a solve that isn't plausible) is dropped, not
    forced. bid/ask come from the same snapshot/same call — Risk Manager's
    sizing inputs (credit received, premium to pay) piggyback on this fetch
    rather than re-fetching."""
    req = OptionChainRequest(
        underlying_symbol=ticker,
        expiration_date=expiration,
        strike_price_gte=round(spot * (1 - config.STRIKE_RANGE_PCT)),
        strike_price_lte=round(spot * (1 + config.STRIKE_RANGE_PCT)),
    )
    chain = _option_client.get_option_chain(req)

    calls, puts = {}, {}
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

        bucket = calls if right == "C" else puts
        bucket[strike] = (delta, iv, symbol, bid, ask)
    return calls, puts


def _get_spot_price(ticker: str) -> float:
    """Last trade price. UNIVERSE names are all liquid enough that this is a
    fine spot proxy, and it sidesteps the bid/ask quote occasionally coming
    back with one leg at 0 near/after the close (seen live on AAPL/TSLA/MSFT
    — a naive (bid+ask)/2 there silently halves the price)."""
    trade = _stock_client.get_stock_latest_trade(
        StockLatestTradeRequest(symbol_or_symbols=ticker)
    )[ticker]
    return trade.price


def rank_ticker(ticker: str, expiration: str) -> RankedCandidate | SkippedTicker:
    spot = _get_spot_price(ticker)
    if spot <= 0:
        return SkippedTicker(ticker, "no valid spot quote")

    calls, puts = _fetch_liquid_chain(ticker, spot, expiration)
    if not calls or not puts:
        return SkippedTicker(ticker, f"no liquid chain for {expiration} (calls={len(calls)}, puts={len(puts)})")

    # ATM strike: closest to spot among strikes with BOTH a liquid call and put
    # (need both to average into ATM IV per spec section 4).
    common_strikes = set(calls) & set(puts)
    if not common_strikes:
        return SkippedTicker(ticker, "no strike with both liquid call and put for ATM IV")
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
    )


def rank_universe(
    universe: list[str] | None = None,
    expiration: str | None = None,
) -> tuple[list[RankedCandidate], list[SkippedTicker]]:
    """Rank the universe by IV skew, descending. Returns (ranked, skipped)."""
    universe = universe or config.UNIVERSE
    expiration = expiration or config.EXPIRATION_OVERRIDE or _today_expiration()

    ranked, skipped = [], []
    for ticker in universe:
        result = rank_ticker(ticker, expiration)
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
    # mock_cache/<date>/strategy_ranking.json. Meant to be captured to a
    # cache file ONCE per run (e.g. logs/cache/ranking-<date>.json) and fed
    # to both the Analyst-dispatch/directional-selection step and
    # risk_manager.py's ranking_result argument, instead of each side re-fetching
    # live minutes apart -- see PROGRESS.md's "Ranking-consistency gap"
    # entry for why a second independently-timed fetch is a real problem
    # (0DTE IV skew moves fast enough that the top-3 split can shift
    # between two fetches a few minutes apart).
    import argparse
    import dataclasses
    import json

    parser = argparse.ArgumentParser(description="Strategy Engine -- IV-skew ranking for the 0DTE universe.")
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON instead of the human-readable table")
    args = parser.parse_args()

    expiration = config.EXPIRATION_OVERRIDE or _today_expiration()
    ranked, skipped = rank_universe()
    premium_sell, directional = split_candidates(ranked)

    if args.json:
        payload = {
            "universe": config.UNIVERSE,
            "expiration_used": expiration,
            "ranked": [dataclasses.asdict(c) for c in ranked],
            "skipped": [dataclasses.asdict(s) for s in skipped],
            "premium_sell": [c.ticker for c in premium_sell],
            "directional": [c.ticker for c in directional],
        }
        print(json.dumps(payload, indent=2))
    else:
        print(f"Ranking {config.UNIVERSE} for expiration {expiration}...\n")
        _print_table(ranked, skipped)
        print(f"\nPremium-selling candidates (top {config.PREMIUM_SELL_COUNT}): {[c.ticker for c in premium_sell]}")
        print(f"Directional candidates (rest): {[c.ticker for c in directional]}")
