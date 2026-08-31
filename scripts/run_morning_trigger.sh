#!/usr/bin/env bash
# Fired by cron on weekday mornings (~9:41 AM ET / 6:41 AM PT), with a
# backup firing a few minutes later (see crontab) in case the primary
# loses the sleep/wake-boundary race -- confirmed real on 2026-08-31: the
# 6:30 AM prefetch job silently never fired (Mac woke at exactly 6:30:00.7
# via pmset; a job scheduled essentially at that exact instant can be
# missed by cron), while a throwaway job added minutes later on the
# already-awake system fired perfectly. The lock file below (claimed
# immediately, before the slow `claude -p` call even starts) makes the
# backup a safe no-op whenever the primary did fire -- this is the
# safety-critical script, so it gets the guard even though prefetch's own
# consequence of a miss is much lower stakes.
#
# Two stages, deliberately split across the LLM/deterministic boundary
# (spec §30 — "AI agents should not replace deterministic risk and
# execution logic"):
#   1. `claude -p` (prompts/morning_decision.md) — decision-only, same as
#      before Component 6 existed. Ranks, dispatches the Analyst, sizes,
#      and writes logs/cache/risk-decisions-<date>.json. Its own tool
#      scope below still hard-blocks every order-placing/mutating MCP
#      tool — it NEVER places an order itself, whether or not Component 6
#      exists.
#   2. This script, plain bash, reads that JSON *after* claude exits and
#      decides — mechanically, no LLM judgment involved — whether to
#      launch execution_agent.py. That's the actual "loosen deliberately,
#      not by accident" moment from NEXTSTEPS.md: the loosening is moving
#      execution out of the LLM's tool-calling loop entirely, not adding
#      order-placing MCP tools to its allowlist.
#
# If `claude -p` exits non-zero, `set -e` aborts before stage 2 runs —
# fail closed: a broken decision run should never fall through to
# launching orders off a stale or partial decisions file.
#
# execution_agent.py is launched detached (nohup + disown), same pattern
# as the sibling alpacabot project's run_bot.sh: it runs until every
# position closes (up to PREMIUM_EOD_CLOSE_TIME, 3:45 PM ET), long after
# this trigger script and the cron job that fired it have exited. A
# `caffeinate -w $PID` tied to its exact lifetime keeps the Mac awake for
# as long as it's actually monitoring positions — see PROGRESS.md's
# scheduling entry for why the shared morning wake-bridge (30 min) isn't
# enough on its own once real positions are being held into the afternoon.
set -euo pipefail
cd "$(dirname "$0")/.."

ALLOWED="Bash,Agent,Write,Read,mcp__alpaca-spike__get_clock"
DISALLOWED="mcp__alpaca-spike__place_option_order,mcp__alpaca-spike__place_stock_order,mcp__alpaca-spike__place_crypto_order,mcp__alpaca-spike__cancel_order_by_id,mcp__alpaca-spike__cancel_all_orders,mcp__alpaca-spike__close_position,mcp__alpaca-spike__close_all_positions,mcp__alpaca-spike__replace_order_by_id,mcp__alpaca-spike__exercise_options_position,mcp__alpaca-spike__do_not_exercise_options_position,mcp__alpaca-spike__create_locate,mcp__alpaca-spike__create_watchlist,mcp__alpaca-spike__update_watchlist_by_id,mcp__alpaca-spike__delete_watchlist_by_id,mcp__alpaca-spike__add_asset_to_watchlist_by_id,mcp__alpaca-spike__remove_asset_from_watchlist_by_id,mcp__alpaca-spike__update_account_config"

mkdir -p logs logs/cache

TODAY="$(date +%Y-%m-%d)"
CLAUDE_BIN="/Users/manalithakkar/.local/bin/claude"
DECISIONS_FILE="logs/cache/risk-decisions-${TODAY}.json"
LOCK_FILE="logs/trigger-${TODAY}.out"

# Claimed immediately, before the slow claude -p call -- a backup firing
# a few minutes later sees this and skips instead of starting a second,
# concurrent decision run (which could cascade into a second, concurrent
# Execution Agent launch and duplicate real orders).
if [ -f "$LOCK_FILE" ]; then
    echo "[$(date)] ${LOCK_FILE} already exists -- today's decision run already started (or ran). Skipping duplicate trigger." >> logs/cron.log
    exit 0
fi
touch "$LOCK_FILE"

# ---- Stage 1: decision-only, unchanged scope ----
"$CLAUDE_BIN" -p "$(cat prompts/morning_decision.md)" \
  --allowedTools "$ALLOWED" \
  --disallowedTools "$DISALLOWED" \
  >> "$LOCK_FILE" 2>&1

# ---- Stage 2: deterministic execution handoff, no LLM involved ----
if [ ! -f "$DECISIONS_FILE" ]; then
    echo "[$(date)] No decisions file for ${TODAY} (market closed, or the decision run stopped early) — Execution Agent not launched." >> logs/cron.log
    exit 0
fi

HAS_APPROVED=$(./venv/bin/python3 -c "
import json
with open('${DECISIONS_FILE}') as f:
    d = json.load(f)
approved = any(x['approved'] for x in d.get('premium_decisions', [])) or \
           any(x['approved'] for x in d.get('directional_decisions', []))
print('yes' if approved else 'no')
")

if [ "$HAS_APPROVED" != "yes" ]; then
    echo "[$(date)] Decisions file for ${TODAY} has zero APPROVEd entries — Execution Agent not launched." >> logs/cron.log
    exit 0
fi

echo "[$(date)] Approved decisions found for ${TODAY} — launching Execution Agent." >> logs/cron.log

EXEC_LOG="logs/${TODAY}-execution-run.out"
nohup ./venv/bin/python3 execution_agent.py "$DECISIONS_FILE" >> "$EXEC_LOG" 2>&1 &
EXEC_PID=$!
echo "$EXEC_PID" > "logs/execution-agent-${TODAY}.pid"

# Held awake only as long as execution_agent.py itself is running --
# releases itself automatically once it exits (every position closed).
nohup caffeinate -dimsu -w "$EXEC_PID" >> "$EXEC_LOG" 2>&1 &
disown -a

echo "[$(date)] Execution Agent launched, PID ${EXEC_PID} (caffeinated for its lifetime). See ${EXEC_LOG} and logs/${TODAY}-execution.log." >> logs/cron.log
