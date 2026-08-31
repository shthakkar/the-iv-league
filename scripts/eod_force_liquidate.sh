#!/usr/bin/env bash
# EOD force-liquidation safety net (spec §, NEXTSTEPS.md item 5).
#
# Deliberately separate from execution_agent.py, and uses the Alpaca CLI
# instead of the SDK/MCP -- an independent, dumb, unmissable check that
# doesn't share a code path (or a process) with whatever else may be hung
# or broken. Fires 10 min after execution_agent.py's own
# PREMIUM_EOD_CLOSE_TIME (config.py: 3:45 PM ET), so on a normal day it
# should find nothing open and do nothing.
#
# Limitation, stated plainly: this only helps if the Mac is actually awake
# at 3:55 PM ET to run it. Normally it will be -- execution_agent.py's own
# `caffeinate -w $PID` (see run_morning_trigger.sh) holds the Mac awake for
# as long as it's monitoring positions, which covers this. But if the Mac
# went to sleep because execution_agent.py's process (and its caffeinate
# assertion) already died, this cron job may not fire at all. It's a net
# for "the loop is stuck/wrong", not for "the whole machine is asleep".
set -uo pipefail  # no -e: keep logging even if one CLI call fails
cd "$(dirname "$0")/.."

mkdir -p logs
TODAY="$(date +%Y-%m-%d)"
LOGFILE="logs/${TODAY}-eod-safety.log"
ALPACA_BIN="/Users/manalithakkar/go/bin/alpaca"

# .env has ALPACA_API_SECRET (this project's convention); the CLI wants
# ALPACA_SECRET_KEY (see PROGRESS.md's gotchas section).
if [ -f .env ]; then
    set -a; source .env; set +a
fi
export ALPACA_SECRET_KEY="${ALPACA_API_SECRET:-}"

echo "[$(date)] EOD safety net firing." >> "$LOGFILE"

OPEN_POSITIONS="$("$ALPACA_BIN" position list --csv 2>>"$LOGFILE" | tail -n +2)"

if [ -z "$OPEN_POSITIONS" ]; then
    echo "[$(date)] No open positions -- execution_agent.py already closed everything on schedule. Nothing to do." >> "$LOGFILE"
    exit 0
fi

echo "[$(date)] WARNING: open positions found past EOD close time:" >> "$LOGFILE"
echo "$OPEN_POSITIONS" >> "$LOGFILE"
echo "[$(date)] Force-liquidating via 'alpaca position close-all --cancel-orders'..." >> "$LOGFILE"

"$ALPACA_BIN" position close-all --cancel-orders >> "$LOGFILE" 2>&1

echo "[$(date)] close-all issued. Verify manually -- this logs the attempt, not a confirmed clean result." >> "$LOGFILE"
