# ================================================================
# DIRECTIONAL SELECTION — spec section 14.
#
# Deterministic: given the Analyst's structured reads for the 5 directional
# candidates, applies the fixed selection rule and returns who actually
# gets a trade this morning. No LLM judgment here on purpose (spec section
# 30) — the Analyst already made the judgment call (direction, confidence,
# reasoning); this just applies an auditable numeric rule to its output, so
# "why did we skip this one" is always a fact (a number below a threshold),
# never a re-litigated vibe.
#
# Rule: skip UNDECIDED, require confidence >= MIN_DIRECTIONAL_CONFIDENCE,
# take up to MAX_DIRECTIONAL_SELECTED by confidence descending.
#
# Usage: venv/bin/python3 directional_selection.py <analyst_candidates.json>
#   input file: JSON list of
#     {"ticker": str, "direction": "BULLISH"|"BEARISH"|"UNDECIDED",
#      "confidence": int, "reason": str, "catalysts": ..., "risks": ...}
#   stdout: JSON {selected: [...], rejected: [...], min_confidence_used,
#                 max_selected}
# ================================================================
from __future__ import annotations

import json
import sys

import config


def select_directional(
    candidates: list[dict],
    min_confidence: int | None = None,
    max_selected: int | None = None,
) -> dict:
    min_confidence = (
        min_confidence if min_confidence is not None else config.MIN_DIRECTIONAL_CONFIDENCE
    )
    max_selected = (
        max_selected if max_selected is not None else config.MAX_DIRECTIONAL_SELECTED
    )

    eligible, rejected = [], []
    for c in candidates:
        direction = c.get("direction")
        confidence = c.get("confidence", 0)
        if direction == "UNDECIDED":
            rejected.append({**c, "reject_reason": "UNDECIDED"})
        elif confidence < min_confidence:
            rejected.append({
                **c,
                "reject_reason": f"confidence {confidence} < MIN_DIRECTIONAL_CONFIDENCE ({min_confidence})",
            })
        else:
            eligible.append(c)

    # Ties broken by input order (stable sort) -- whichever candidate the
    # Analyst returned first among equal confidences. Good enough for V1;
    # revisit if ties turn out to matter once there's real trade history.
    eligible.sort(key=lambda c: c["confidence"], reverse=True)
    selected = eligible[:max_selected]
    overflow = eligible[max_selected:]
    for rank, c in enumerate(overflow, start=max_selected + 1):
        rejected.append({
            **c,
            "reject_reason": f"confidence {c['confidence']} qualified but ranked #{rank} of {len(eligible)} eligible, exceeds max_selected ({max_selected})",
        })

    return {
        "selected": selected,
        "rejected": rejected,
        "min_confidence_used": min_confidence,
        "max_selected": max_selected,
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: directional_selection.py <analyst_candidates.json>", file=sys.stderr)
        sys.exit(1)
    with open(sys.argv[1]) as f:
        candidates = json.load(f)
    print(json.dumps(select_directional(candidates), indent=2))
