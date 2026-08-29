#!/usr/bin/env bash
# Fired by launchd at market open (~9:30 AM ET). Pure Python, no LLM
# involved — just warms logs/cache/news-<date>.json for the analyst
# subagent to read during the 9:41 AM decision run. See prefetch_news.py.
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p logs
./venv/bin/python3 prefetch_news.py >> "logs/prefetch-$(date +%Y-%m-%d).out" 2>&1
