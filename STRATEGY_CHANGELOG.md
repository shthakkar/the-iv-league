# Strategy Changelog

Dated record of every strategy/process change reviewed via the Strategist
Agent + human review cycle (spec §24-27's intent, done interactively rather
than through the formal Backtest → Compare → Paper → Risk Approval →
Production pipeline — see PROGRESS.md's "First live trading day" and
NEXTSTEPS.md for why V1 skips that pipeline for now). Every entry names
which suggestions came from the Strategist Agent (AI) vs. directly from the
human, what was decided, and — for anything actually implemented — the
before/after and the commit(s).

This file is a **read input for the Strategist Agent itself** (see
`.claude/agents/strategist.md`) so future runs know what's already been
tried or changed, and don't re-propose something already decided.

---

## 2026-08-31 — First review cycle (after the first live trading day)

**Trigger**: Strategist Agent post-mortem of 2026-08-31's 3 real trades
(net -$3,927: +$22 TSLA, +$11 AAPL, -$3,960 SPY), run twice (dispatched via
the Agent tool, `subagent_type: "strategist"`) — second run added hourly
bars for the live end-of-day news/price re-check.

### AI suggestions (Strategist Agent, all LOW confidence, process-fix — n=1 day)

| # | Proposal | Root cause found | Decision |
|---|---|---|---|
| 1 | Persist the IV-skew ranking snapshot used for premium sizing | Never written to a file for any non-empty run that day — `--json` only printed to stdout | **Approved as-is** |
| 2 | Make it unambiguous which decision-file version was actually traded | 3 different decision states existed for the same date with no provenance record | **Approved as-is** |
| 3 | Investigate the premium/directional partition inconsistency (META, AAPL on both sides) | **Confirmed real bug**: `build_directional_candidates()` resolved selected tickers against the full (undiscriminated) ranked lookup, so a stale `selection_result.json` could pull in a ticker that had since shifted to the premium side of a later ranking run | **Approved, fixed** — now scoped to the directional-side lookup only; a mismatch is an explicit REJECT, not a silent leak |
| 4 | Investigate the AAPL TP-price mismatch (stored 0.09 vs. actual exit 0.16) | **Confirmed NOT a bug** — TP is correctly derived from the real fill (0.27×0.5=0.135, not the pre-trade 0.09 estimate); the 0.16 exit is ordinary bid/ask slippage on the market-order close. The real gap: nothing logged the actual armed threshold, so the audit compared against the wrong number | **Approved** — fixed the legibility gap (now logged at entry), no logic change needed |
| 5 | Log presence/absence of Alpaca's greeks fields per fetch | The real 09:41 ET feed outage that day had no record of when/how it "recovered" by the later manual run | **Approved as-is** |

### Human suggestion (not from the Strategist Agent)

**Directional capital allocation formula.** Flat 5% of balance regardless of
how many directional candidates were actually selected → **1% per selected
candidate, capped at 3% total** (1 selected → 1%, 2 → 2%, 3 → 3%).

Rationale (human): a single-name day (low breadth/conviction — exactly
what happened 2026-08-31, where only SPY cleared the confidence bar) was
risking the *same* dollar amount as a full 3-name day. Scale exposure with
conviction/breadth instead.

### Before / after

| | Before | After |
|---|---|---|
| Directional allocation | flat 5% of balance | `min(1% × selected_count, 3%)` |
| Directional budget, 1 selected (today's actual case, $100k account) | $5,000 | $1,000 |
| Directional budget, 3 selected | $5,000 | $3,000 |
| `build_directional_candidates()` on a stale/mismatched selection | silently resolved via the full ranked lookup (real bug: could leak a premium-side ticker into directional) | explicit REJECT naming the mismatch |
| `strategy_engine.py --json` | printed to stdout only | also persists to `logs/cache/ranking-<expiration>.json`, gains `run_id` |
| Ranked/skipped tickers | no record of greeks source | `greeks_source` field (`alpaca` \| `black_scholes_fallback` \| `unavailable`) + stderr log line per ticker |
| `risk_manager.py` output | no provenance | gains `generated_at` (ISO timestamp) |
| Premium `ENTRY` log lines | no TP/SL recorded | includes real `tp=`/`sl=` from the actual fill |
| `execution_agent.py` startup | no record of which decisions file/version was loaded | logs the path + `generated_at` |

### Implementation

All 5 AI proposals + the 1 human change implemented in the same review
cycle, dispatched as 3 parallel agents on disjoint files (no file overlap,
so no worktree isolation needed), each following TDD, none committing —
reviewed and committed by the human afterward as 4 separate commits:

- `5af31c2` — Strategist Agent subagent itself
- `a1a3f99` — `strategy_engine.py` (proposals 1, 5)
- `b8d6633` — `risk_manager.py` / `config.py` (proposal 3, generated_at half of 2, human's allocation change)
- `fe02c1e` — `execution_agent.py` (proposal 4, decisions-file-provenance half of 2)

65/65 tests passing across the full suite after all four commits.

**Not done in this cycle**: no numeric parameter changes were proposed by
the Strategist Agent itself (n=1 day, no statistical basis) — the only
numeric change (directional allocation formula) came from the human, not
the AI. Revisit `MIN_DIRECTIONAL_CONFIDENCE`, `TARGET_PUT_DELTA`, and
similar tunables once there's more than one day of trade history.
