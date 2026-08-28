# ================================================================
# SHARED CONFIG — credentials + strategy constants
# Imported by every module so we don't duplicate keys/params.
# ================================================================
import os

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

# Expiration date to treat as "0DTE" for this run, YYYY-MM-DD. In production
# this should be computed as today's date (or skipped if no 0DTE chain
# exists for a ticker). Overridable here for testing when markets are
# closed or a same-day expiration isn't available — see strategy_engine.py.
EXPIRATION_OVERRIDE = os.environ.get("EXPIRATION_OVERRIDE", "")
