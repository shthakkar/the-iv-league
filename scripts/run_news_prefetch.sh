#!/usr/bin/env bash
# Fired by cron at market open (~9:30 AM ET / 6:30 AM PT), with a backup
# firing a few minutes later (see crontab) in case the primary loses the
# sleep/wake-boundary race -- confirmed real on 2026-08-31: a job
# scheduled essentially at the exact pmset wake instant can get silently
# skipped by cron, while the same daemon fires everything else normally
# once the system's been awake even a minute. Idempotency guard below
# makes the backup a no-op if the primary already ran. Pure Python, no
# LLM involved -- just warms logs/cache/news-<date>.json for the analyst
# subagent to read during the 9:41 AM decision run. See prefetch_news.py.
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p logs
TODAY="$(date +%Y-%m-%d)"
OUT="logs/prefetch-${TODAY}.out"

if [ -s "$OUT" ]; then
    echo "[$(date)] logs/prefetch-${TODAY}.out already has content -- already ran today, skipping." >> logs/cron.log
    exit 0
fi

./venv/bin/python3 prefetch_news.py >> "$OUT" 2>&1
