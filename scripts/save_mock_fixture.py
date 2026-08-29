#!/usr/bin/env python3
# ================================================================
# Regenerates the DETERMINISTIC pieces of a mock_cache/<date> fixture:
# the strategy-engine ranking/split and the directional-selection result.
#
# Deliberately does NOT re-run the Analyst subagents or re-fetch news --
# those are the expensive/LLM-driven parts (5 parallel subagent calls,
# each doing bars+news+reasoning) and are captured once, by hand, into
# analyst_candidates.json / news_cache.json. This script only touches the
# cheap-to-reproduce parts: pure Alpaca SDK calls (option chain, spot) and
# pure Python (selection rule) -- safe to re-run anytime without cost.
#
# Usage: venv/bin/python3 scripts/save_mock_fixture.py [fixture_dir]
#   (default fixture_dir: mock_cache/2026-08-28)
# Requires analyst_candidates.json to already exist in fixture_dir.
# ================================================================
import dataclasses
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategy_engine
from directional_selection import select_directional

FIXTURE_DIR = sys.argv[1] if len(sys.argv) > 1 else "mock_cache/2026-08-28"
UNIVERSE = ["SPY", "QQQ", "NVDA", "TSLA", "AAPL", "AMZN", "MSFT", "META"]
# Nearest available option chain when this fixture was captured (markets
# were closed for the weekend) -- a structural stand-in so the chain-fetch
# and ranking math could be exercised for real, NOT actual same-day 0DTE
# expiration data. See mock_cache/<date>/README.md.
EXPIRATION = "2026-08-31"


def main():
    os.makedirs(FIXTURE_DIR, exist_ok=True)
    candidates_path = os.path.join(FIXTURE_DIR, "analyst_candidates.json")
    if not os.path.exists(candidates_path):
        sys.exit(f"{candidates_path} not found -- copy the real Analyst output there first (this script won't re-run the subagents).")

    ranked, skipped = strategy_engine.rank_universe(universe=UNIVERSE, expiration=EXPIRATION)
    premium_sell, directional = strategy_engine.split_candidates(ranked)

    ranking_payload = {
        "universe": UNIVERSE,
        "expiration_used": EXPIRATION,
        "expiration_note": (
            "Nearest available option chain when markets were closed -- a "
            "structural stand-in for a real 0DTE chain, not actual "
            "same-day expiration data."
        ),
        "ranked": [dataclasses.asdict(c) for c in ranked],
        "skipped": [dataclasses.asdict(s) for s in skipped],
        "premium_sell": [c.ticker for c in premium_sell],
        "directional": [c.ticker for c in directional],
    }
    with open(os.path.join(FIXTURE_DIR, "strategy_ranking.json"), "w") as f:
        json.dump(ranking_payload, f, indent=2)
    print(f"Wrote {FIXTURE_DIR}/strategy_ranking.json")

    with open(candidates_path) as f:
        candidates = json.load(f)
    selection = select_directional(candidates)
    with open(os.path.join(FIXTURE_DIR, "selection_result.json"), "w") as f:
        json.dump(selection, f, indent=2)
    print(f"Wrote {FIXTURE_DIR}/selection_result.json")


if __name__ == "__main__":
    main()
