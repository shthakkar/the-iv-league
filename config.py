# ================================================================
# SHARED CONFIG — credentials + strategy constants
# Imported by every module so we don't duplicate keys/params.
# ================================================================
import datetime
import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

# ---------- CREDENTIALS ----------
API_KEY    = os.environ.get("ALPACA_API_KEY", "")
API_SECRET = os.environ.get("ALPACA_API_SECRET", "")
PAPER      = os.environ.get("ALPACA_PAPER", "true").lower() != "false"

if not API_KEY or not API_SECRET:
    raise RuntimeError(
        "Missing Alpaca credentials. Set ALPACA_API_KEY and ALPACA_API_SECRET "
        "in .env before running."
    )
# ---------------------------------

# ---------- TRADING UNIVERSE ----------
# Full V1 spec universe (spec §2). Was a 4-ticker demo default
# (SPY QQQ NVDA TSLA) during early dev for faster chain-fetch/ranking
# iteration — every one of the 8 below was already tested via
# UNIVERSE_OVERRIDE and works (see PROGRESS.md). Bumped to the real
# default 2026-08-31 once live cron runs started, after today's first
# live run (and its news prefetch) silently ran on only 4 names —
# flagged by the decision run itself as a prompt/code mismatch.
# Override for a one-off run without editing this file, e.g.:
#   UNIVERSE_OVERRIDE=SPY,QQQ python3 strategy_engine.py
_universe_override = os.environ.get("UNIVERSE_OVERRIDE", "")
UNIVERSE = (
    [t.strip().upper() for t in _universe_override.split(",") if t.strip()]
    if _universe_override
    else ["SPY", "QQQ", "NVDA", "TSLA", "AAPL", "AMZN", "MSFT", "META"]
)

# How many of the top-ranked-by-skew tickers become premium-selling candidates.
# Spec default: top 3 of 8.
PREMIUM_SELL_COUNT = 3

# ---------- OPTION CHAIN / RANKING ----------
# Target delta for the short put leg (spec section 4).
TARGET_PUT_DELTA = 0.15

# Strike range to pull around spot, as a fraction of spot price. Wide enough
# to comfortably contain the ATM strike and a 0.15-delta put on any of the
# UNIVERSE names without dragging in the whole deep ITM/OTM chain.
STRIKE_RANGE_PCT = 0.20

# Risk-free rate for the local Black-Scholes fallback (black_scholes.py) --
# used only when Alpaca's own greeks/IV are missing, which is every 0DTE
# contract (see black_scholes.py's module docstring and PROGRESS.md). At
# these short time-to-expirations the discounting effect is negligible, so
# this is a stable approximate constant, not something pulled live.
RISK_FREE_RATE = 0.045

# ---------- DIRECTIONAL SELECTION ----------
# Minimum Analyst confidence (0-100) for a directional candidate to be
# selected for a trade (spec section 14: "only candidates with sufficient
# confidence"). Not spec-fixed — chosen from the one validated live run in
# PROGRESS.md, where decisive reads landed at 62 and UNDECIDED/conflicted
# reads landed at 30; 55 sits clearly above the noise floor without being
# tuned to that single sample. Revisit once the Strategist Agent has real
# trade history to check it against (spec section 25).
MIN_DIRECTIONAL_CONFIDENCE = 55

# Max directional trades selected per day — spec-fixed at 3 ("select up to
# 3", spec section 14), unlike MIN_DIRECTIONAL_CONFIDENCE above.
MAX_DIRECTIONAL_SELECTED = 3

# Expiration date to treat as "0DTE" for this run, YYYY-MM-DD. In production
# this should be computed as today's date (or skipped if no 0DTE chain
# exists for a ticker). Overridable here for testing when markets are
# closed or a same-day expiration isn't available — see strategy_engine.py.
EXPIRATION_OVERRIDE = os.environ.get("EXPIRATION_OVERRIDE", "")

# ---------- RISK MANAGER / CAPITAL ALLOCATION ----------
# Spec section 7's 95%/5% split was originally two independent fixed
# percentages, applied dynamically to the account's real options_buying_
# power (not the historical spec's hardcoded $100k account) -- decided
# 2026-08-30, see PROGRESS.md's Risk Manager entry for why options_buying_
# power specifically (margin doesn't extend to options on this account,
# confirmed via Alpaca's account-field docs).
#
# Both halves changed 2026-08-31, both explicit human/product decisions
# (not Strategist Agent proposals):
#
# 1. Directional side used to be a flat 5% of balance regardless of how many
#    candidates actually cleared MIN_DIRECTIONAL_CONFIDENCE that day -- but
#    that risks the same dollar amount whether 1 name or 3 names made the
#    cut, so a single-name day (breadth/conviction was low -- exactly what
#    happened 2026-08-31, where only one ticker cleared the confidence bar)
#    got exposed for the SAME capital as a full 3-name day. Replaced with a
#    per-selected-stock formula: 1% of balance per selected directional
#    candidate, capped at 3% total. 1 selected -> 1%, 2 -> 2%, 3 (spec
#    section 14's max) -> 3% -- a deliberate reduction from the old flat 5%
#    ceiling.
# 2. Premium side used to be a fixed 95% independent of the directional
#    side, which meant 2-4% of the account sat permanently idle (100% -
#    95% premium - up-to-3% directional never quite closed the gap once
#    directional stopped being a flat 5%). Made complementary instead --
#    PREMIUM_SELL_ALLOCATION_PCT is gone; premium_sell_budget is now
#    `1.0 - directional_pct` of balance, so the two sides always sum to the
#    full available balance. 1 directional selected -> 99% premium, 2 ->
#    98%, 3 -> 97%, 0 -> 100%. See risk_manager.compute_budgets().
DIRECTIONAL_PCT_PER_STOCK = 0.01  # 1% of balance per selected directional candidate
DIRECTIONAL_MAX_PCT = 0.03        # cap on total directional allocation regardless of count

# Spec section 23 hard limits. Max daily loss is deliberately NOT here —
# dropped for V1, see PROGRESS.md (its "halt new positions" behavior is
# inert with no position recycling this version).
# Max total open positions across both strategies (3 premium + up to 3
# directional under current PREMIUM_SELL_COUNT/MAX_DIRECTIONAL_SELECTED).
MAX_POSITIONS = 6

# MAX_EXPOSURE_PER_UNDERLYING_PCT (0.35) removed 2026-08-31 — was never
# spec-fixed (spec section 8 says "roughly equal allocation... never force
# a trade merely to use all available capital", nothing about a per-name
# ceiling), and was actively fighting allocate_premium_positions()'s own
# leftover-pooling pass: pooling exists to rescue a candidate that couldn't
# afford its own equal share, and the cap then re-blocked that same rescue
# once the pool was big enough to help. Rejected every premium-selling
# candidate on a live run for exactly this reason before being removed —
# see PROGRESS.md and risk_manager.py's evaluate(). allocate_premium_
# positions() still accepts max_exposure_per_underlying as an optional
# param (tested in tests/test_risk_manager.py) if a cap is ever wanted
# again — it's just no longer passed by the live call.

# ---------- EXECUTION AGENT ----------
# Hard EOD liquidation time for short option positions (spec section 9 --
# "all short option positions must be closed before expiration"). 15:45 ET
# gives a 15-min buffer before the close, same convention as the sibling
# alpacabot project's EOD_EXIT_TIME.
PREMIUM_EOD_CLOSE_TIME = datetime.time(15, 45)

# Directional exit time -- spec section 18, fixed at 2:30 PM ET regardless
# of P&L. Not configurable; it's the thing V1 is testing (does the 9:40 AM
# signal predict price movement through 2:30 PM), not a tunable knob.
DIRECTIONAL_CLOSE_TIME = datetime.time(14, 30)

# Monitoring loop poll interval, spec section 19-20 ("~every 1 minute").
MONITOR_POLL_SECS = 60

# Wall-clock timezone all exit-time comparisons are made in (market hours,
# EOD/directional close times above are all ET).
ET = ZoneInfo("America/New_York")
