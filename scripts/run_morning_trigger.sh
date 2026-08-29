#!/usr/bin/env bash
# Fired by launchd on weekday mornings (~9:41 AM ET). Headless, decision-only
# — see prompts/morning_decision.md for exactly what this does and does not
# do (no order placement; Components 2/4/5/6 aren't built yet).
#
# Tool access is allowlisted rather than run with
# --dangerously-skip-permissions, so an unattended run can't place, cancel,
# or mutate anything even if the prompt were somehow subverted.
set -euo pipefail
cd "$(dirname "$0")/.."

ALLOWED="Bash,Agent,Write,Read,mcp__alpaca-spike__get_clock"
DISALLOWED="mcp__alpaca-spike__place_option_order,mcp__alpaca-spike__place_stock_order,mcp__alpaca-spike__place_crypto_order,mcp__alpaca-spike__cancel_order_by_id,mcp__alpaca-spike__cancel_all_orders,mcp__alpaca-spike__close_position,mcp__alpaca-spike__close_all_positions,mcp__alpaca-spike__replace_order_by_id,mcp__alpaca-spike__exercise_options_position,mcp__alpaca-spike__do_not_exercise_options_position,mcp__alpaca-spike__create_locate,mcp__alpaca-spike__create_watchlist,mcp__alpaca-spike__update_watchlist_by_id,mcp__alpaca-spike__delete_watchlist_by_id,mcp__alpaca-spike__add_asset_to_watchlist_by_id,mcp__alpaca-spike__remove_asset_from_watchlist_by_id,mcp__alpaca-spike__update_account_config"

mkdir -p logs

CLAUDE_BIN="/Users/manalithakkar/.local/bin/claude"

"$CLAUDE_BIN" -p "$(cat prompts/morning_decision.md)" \
  --allowedTools "$ALLOWED" \
  --disallowedTools "$DISALLOWED" \
  >> "logs/trigger-$(date +%Y-%m-%d).out" 2>&1
