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
# Demo default — full V1 spec universe is SPY QQQ NVDA TSLA AAPL AMZN MSFT META.
# Kept small here so chain-fetch/ranking stays fast to iterate on and cheap to
# test against; add tickers freely, nothing else needs to change.
# Override for a one-off run without editing this file, e.g.:
#   UNIVERSE_OVERRIDE=SPY,QQQ,NVDA,TSLA,AAPL,AMZN,MSFT,META python3 strategy_engine.py
_universe_override = os.environ.get("UNIVERSE_OVERRIDE", "")
UNIVERSE = (
    [t.strip().upper() for t in _universe_override.split(",") if t.strip()]
    if _universe_override
    else ["SPY", "QQQ", "NVDA", "TSLA"]
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
# Spec section 7's 95%/5% split, applied dynamically to the account's real
# options_buying_power (not the historical spec's hardcoded $100k account) —
# decided 2026-08-30, see PROGRESS.md's Risk Manager entry for why
# options_buying_power specifically (margin doesn't extend to options on
# this account, confirmed via Alpaca's account-field docs).
PREMIUM_SELL_ALLOCATION_PCT = 0.95
DIRECTIONAL_ALLOCATION_PCT = 0.05

# Spec section 23 hard limits. Max daily loss is deliberately NOT here —
# dropped for V1, see PROGRESS.md (its "halt new positions" behavior is
# inert with no position recycling this version).
# Max total open positions across both strategies (3 premium + up to 3
# directional under current PREMIUM_SELL_COUNT/MAX_DIRECTIONAL_SELECTED).
MAX_POSITIONS = 6

# Max capital committed to any single underlying, as a fraction of the
# available balance — prevents concentration if a name's sizing would
# otherwise dominate the budget (spec section 23). 0.35 sits just above the
# natural ~1/3 three-way premium-selling split so it doesn't bind in the
# common case, only when something unusual would concentrate exposure.
MAX_EXPOSURE_PER_UNDERLYING_PCT = 0.35

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
