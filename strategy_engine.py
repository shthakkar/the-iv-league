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


def _fetch_liquid_chain(ticker: str, spot: float, expiration: str) -> tuple[dict, dict]:
    """Return (calls, puts) dicts of {strike: (delta, iv, symbol, bid, ask)}, liquid
    contracts only (i.e. Alpaca returned a computed delta and IV — thin/no-interest
    strikes come back null and are dropped, matching what the spike found). bid/ask
    come from the same snapshot/same call — Risk Manager's sizing inputs (credit
    received, premium to pay) piggyback on this fetch rather than re-fetching."""
    req = OptionChainRequest(
        underlying_symbol=ticker,
        expiration_date=expiration,
        strike_price_gte=round(spot * (1 - config.STRIKE_RANGE_PCT)),
        strike_price_lte=round(spot * (1 + config.STRIKE_RANGE_PCT)),
    )
    chain = _option_client.get_option_chain(req)

    calls, puts = {}, {}
    for symbol, snapshot in chain.items():
        delta = getattr(snapshot.greeks, "delta", None)
        iv = getattr(snapshot, "implied_volatility", None)
        if delta is None or iv is None:
            continue
        quote = snapshot.latest_quote
        bid = getattr(quote, "bid_price", None) or 0.0
        ask = getattr(quote, "ask_price", None) or 0.0
        strike, right = _parse_occ_symbol(symbol)
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
    expiration = config.EXPIRATION_OVERRIDE or _today_expiration()
    print(f"Ranking {config.UNIVERSE} for expiration {expiration}...\n")

    ranked, skipped = rank_universe()
    _print_table(ranked, skipped)

    premium_sell, directional = split_candidates(ranked)
    print(f"\nPremium-selling candidates (top {config.PREMIUM_SELL_COUNT}): {[c.ticker for c in premium_sell]}")
    print(f"Directional candidates (rest): {[c.ticker for c in directional]}")
