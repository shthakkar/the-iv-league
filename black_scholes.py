# ================================================================
# BLACK-SCHOLES — local IV/delta calc, stdlib only.
#
# Why this exists: Alpaca's own option-snapshot greeks/IV are computed
# server-side via Black-Scholes with a literal T=0 for same-day-expiration
# (0DTE) contracts -- a division-by-zero, confirmed both by their own
# Market Data FAQ and empirically (checked 20 identical near-ATM, liquid
# SPY strikes: 0/20 had greeks at 0DTE, 20/20 had greeks one day later).
# Not a feed/subscription gap, not a timing-at-open thing -- structural,
# every live trading day, for as long as this project trades 0DTE per
# spec's design.
#
# The fix isn't "Black-Scholes doesn't work for 0DTE" -- it's "don't use
# a literal zero". time_to_expiration_years() uses the real hours
# remaining until market close as T, which sidesteps the division-by-zero
# entirely (T is never truly zero until the literal expiration instant).
# Researched before building this (see PROGRESS.md): a QuantConnect user
# hit the identical symptom and traced it to computing T in days instead
# of minutes-to-close; switching granularity fixed it. Real caveats do
# exist as T->0 (vega collapses, delta/gamma get genuinely whippy in the
# final 30-60 min -- a market property, not a solver bug) but they
# concentrate in the wings (deep ITM/OTM), not the ATM/~15-delta region
# this project actually targets.
#
# Deliberately pure stdlib (math.erf for the normal CDF) -- no new
# dependency, matching the rest of the project's minimal-dependency
# convention (stdlib unittest, no test framework, etc.).
# ================================================================
from __future__ import annotations

import datetime
import math

# Never treat time-to-expiration as literal zero -- that's Alpaca's own
# bug. Floored at 15 minutes: small enough to not distort a real
# multi-hour intraday IV solve, large enough to keep vega from collapsing
# to the point Newton-Raphson can't make progress at all.
MIN_T_YEARS = 15 / (365 * 24 * 60)

# Options expire at the close; that's when time value hits zero.
MARKET_CLOSE_TIME = datetime.time(16, 0)

_SQRT_2PI = math.sqrt(2 * math.pi)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_pdf(x: float) -> float:
    return math.exp(-x * x / 2) / _SQRT_2PI


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float) -> tuple[float, float]:
    d1 = (math.log(S / K) + (r + sigma**2 / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return d1, d2


def bs_price(S: float, K: float, T: float, r: float, sigma: float, right: str) -> float:
    """European Black-Scholes price. `right` is 'C' or 'P'. SPY/QQQ/etc. are
    American-style, but early-exercise premium is rare in practice and
    negligible for near-ATM contracts with real time value -- the caveat is
    on record (see module docstring / PROGRESS.md), not silently ignored."""
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    if right == "C":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def bs_delta(S: float, K: float, T: float, r: float, sigma: float, right: str) -> float:
    d1, _ = _d1_d2(S, K, T, r, sigma)
    return _norm_cdf(d1) if right == "C" else _norm_cdf(d1) - 1.0


def _vega(S: float, K: float, T: float, r: float, sigma: float) -> float:
    d1, _ = _d1_d2(S, K, T, r, sigma)
    return S * _norm_pdf(d1) * math.sqrt(T)


def implied_volatility(
    price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    right: str,
    *,
    tol: float = 1e-6,
    max_newton_iter: int = 50,
    max_bisect_iter: int = 100,
    lo: float = 0.005,
    hi: float = 5.0,
) -> float | None:
    """Solve for the sigma that reproduces `price` under Black-Scholes. Newton-
    Raphson first (fast, but unreliable when vega is small); falls back to
    bisection over [lo, hi] if Newton stalls, diverges, or vega collapses --
    the standard robust-implementation pattern for IV solvers (researched, see
    PROGRESS.md). Fails closed (returns None) rather than a garbage value: on
    a non-convergent solve, an implausible/arbitrage-violating price, or a
    non-positive price -- there's no sigma that reproduces those, and the
    caller (strategy_engine.py) already skips a strike it can't price rather
    than forcing one, same convention as the rest of this codebase."""
    if price is None or price <= 0:
        return None

    # Arbitrage bounds: no sigma reproduces a price outside the model's
    # achievable range as sigma -> 0 or sigma -> infinity.
    intrinsic = max(S - K, 0.0) if right == "C" else max(K - S, 0.0)
    upper_bound = S if right == "C" else K
    if price < intrinsic - tol or price > upper_bound + tol:
        return None

    sigma = 0.30  # reasonable starting guess across most equity/ETF options
    for _ in range(max_newton_iter):
        vega = _vega(S, K, T, r, sigma)
        if vega < 1e-8:
            break  # Newton can't make progress -- hand off to bisection below
        price_est = bs_price(S, K, T, r, sigma, right)
        diff = price_est - price
        if abs(diff) < tol:
            return sigma if lo <= sigma <= hi else None
        sigma -= diff / vega
        if sigma <= 0 or sigma > hi * 2:
            break  # diverged -- hand off to bisection below

    return _bisect_implied_vol(price, S, K, T, r, right, lo, hi, tol, max_bisect_iter)


def _bisect_implied_vol(
    price: float, S: float, K: float, T: float, r: float, right: str,
    lo: float, hi: float, tol: float, max_iter: int,
) -> float | None:
    f_lo = bs_price(S, K, T, r, lo, right) - price
    f_hi = bs_price(S, K, T, r, hi, right) - price
    if f_lo * f_hi > 0:
        return None  # can't bracket a root in [lo, hi] -- no plausible IV here

    for _ in range(max_iter):
        mid = (lo + hi) / 2
        f_mid = bs_price(S, K, T, r, mid, right) - price
        if abs(f_mid) < tol or (hi - lo) < tol:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def time_to_expiration_years(now: datetime.datetime, expiration: datetime.date) -> float:
    """Real hours remaining until market close on `expiration`, as a year
    fraction -- never literal zero (see MIN_T_YEARS). `now` must be
    timezone-aware (ET); `expiration` is a plain date."""
    close = datetime.datetime.combine(expiration, MARKET_CLOSE_TIME, tzinfo=now.tzinfo)
    seconds_remaining = (close - now).total_seconds()
    years = seconds_remaining / (365 * 24 * 3600)
    return max(years, MIN_T_YEARS)
