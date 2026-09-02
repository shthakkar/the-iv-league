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

### Same-day addendum: premium allocation made complementary (human)

Follow-up human suggestion, same review cycle: the fixed 95% premium
allocation left 2-4% of the account permanently idle once directional
stopped being a flat 5% (95% premium + up to 3% directional never quite
summed to 100%). Changed `compute_budgets()` so premium is the
**complement** of the directional percentage — `premium_pct = 1.0 -
directional_pct` — rather than an independent fixed constant.
`config.PREMIUM_SELL_ALLOCATION_PCT` removed entirely.

| Directional selected | Directional % | Premium % (before → after) |
|---|---|---|
| 0 | 0% | 95% → **100%** |
| 1 | 1% | 95% → **99%** |
| 2 | 2% | 95% → **98%** |
| 3 | 3% | 95% → **97%** |

Premium + directional now always sum to exactly the full available
balance (`options_buying_power`, falling back to `cash`) — no gap.
TDD (9 tests in `ComputeBudgetsTests`, including an explicit
`premium_plus_directional_always_sums_to_full_balance` invariant check
across 0-4 selected). 66/66 tests pass across the full suite.

---

## 2026-09-01 — Anomalous trading day: no-0DTE fallback, second review cycle

**Trigger**: a genuinely abnormal day, not a standard sample. The real,
unmodified `strategy_engine.py` run found only SPY and QQQ had any same-day
(0DTE) option chain at all — NVDA/TSLA/AAPL/AMZN/MSFT/META had no
2026-09-01 expiration, confirmed directly (not a bug). The automated cron
run also failed earlier that morning on a headless-auth issue and wasn't
validly re-run in time. A human-directed, explicitly modified/throwaway
scenario was run instead: real 15-min price action (market open to ~noon
ET) + real news stood in for the Analyst's first-10-minute design, picking
META/AAPL/NVDA BULLISH; since none had a same-day chain, their calls were
bought at the **next day's (1DTE)** expiration instead, still force-closed
at the normal 2:30 PM ET. Real paper trades placed: QQQ short put (0DTE,
normal) + META/AAPL/NVDA long calls (1DTE substitute). Net: **-$796**
(-$46 premium, -$750 directional). Strategist Agent post-mortem run
against this day (dispatched via the Agent tool), explicitly told this was
not a normal sample.

### AI suggestions (Strategist Agent, all LOW confidence, process-fix — n=1 anomalous day)

| # | Proposal | Decision |
|---|---|---|
| 1 | `persist-directional-observation-window` — persist the raw price bars/news backing a directional read, mirroring `ranking-<date>.json` on the premium side | **Approved, implemented** — see below. Not yet live-validated (see "Known gap") |
| 2 | `no-0dte-fallback-policy` — define and spec a tested policy for no-0DTE days instead of ad hoc human substitution | **Deferred, still open.** Discussed at length (skip entirely vs. substitute), no policy decided yet |
| 3 | `news-cache-timing-and-completeness-gap` — reconcile news-cache fetch timing with actual decision time | **Approved, investigated, and fixed** — root cause turned out more serious than proposed: see below |

### Investigation: news-cache gap was a real bug, not just timing

Traced concretely: `prefetch_news.py` made **one combined API call for the
full 8-ticker universe with `limit=50`** — a shared cap across all 8, not
per ticker. Confirmed live: today's cache held exactly 50 unique articles
total; a real NVDA story from 06:06 ET ("NVIDIA To Purchase $1.50B... In
Private Placement") was silently crowded out of that shared cap before the
09:30:48 ET prefetch ran, despite being well within the nominal 24h
window. Fixed: `prefetch_news.py` now makes one request **per ticker**,
each capped at `config.NEWS_ARTICLES_PER_TICKER` (10) — one ticker's news
volume can no longer affect another's coverage. TDD (6 new tests,
including a direct regression test for the crowding failure mode).
Verified live by replaying the exact historical 09:30:48 ET cutoff: the
previously-missing NVDA story now appears.

### Implemented: `persist-directional-observation-window` (proposal 1)

`.claude/agents/analyst.md` gains the `Write` tool and a new instruction
step: before reasoning, persist every raw input it gathered (first-10-min
bars, gap %, news actually read) to
`logs/cache/analyst-observation-<date>-<ticker>.json` — one file per
ticker to avoid write races across the parallel per-ticker dispatches.
Mirrors what `strategy_engine.py` already does for the premium side.

**Known gap, not yet resolved**: a live validation dispatch (Analyst on
META, post-edit) reported it did **not** have the new instructions or the
`Write` tool in its actual configured behavior — it correctly refused to
treat the task prompt's description of "your newly-added persistence
step" as authoritative when its own instructions didn't contain it. The
file on disk is correct; this session's cached subagent definition
predates the edit. Same class of issue as the documented MCP-server
registration gotcha (`PROGRESS.md`) — needs a Claude Code restart, then
re-validation, before this is considered actually live.

### Discussed, explicitly NOT implemented this cycle (human-directed, not Strategist proposals)

Time-decay mitigation for directional trades, prompted by AAPL's real
result today (spot -0.3%, but its 1DTE call lost 42% of value — a
decay-dominated loss, not a directional miss). Three options discussed:
(1) skip the ticker entirely when no true 0DTE chain exists, (2) if
substituting, buy deeper ITM instead of ATM to reduce the extrinsic-value
share of the premium, (3) use a defined-risk debit spread instead of a
naked long for any substitute trade. A fourth idea (shortening the 2:30 PM
close specifically for substitute trades) was discussed and recommended
**against** as a primary fix — it trades one risk for another (cuts the
thesis's time to play out) and the AAPL loss likely wasn't theta-dominated
anyway (rough math: ~$10-15/contract from raw theta vs. the ~$60/contract
actual move — the rest is probably IV compression/slippage, not yet
logged/provable). **No numeric or structural change made** — explicit
human instruction this cycle was "don't make any changes now." Revisit
once IV is logged per-trade (closes the theta-vs-vega-vs-slippage
ambiguity) and proposal 2 (no-0dte-fallback-policy) is actually decided.

### Before / after

| | Before | After |
|---|---|---|
| News prefetch | 1 combined call, `limit=50` shared across all 8 tickers | 1 call per ticker, `limit=10` each — no cross-ticker crowding |
| `config.py` | no news-fetch tunable | `NEWS_ARTICLES_PER_TICKER = 10` |
| Analyst subagent | no persisted raw-input trail | persists `analyst-observation-<date>-<ticker>.json` per dispatch (pending restart validation) |
| No-0DTE-day policy | none — ad hoc human call each time | still none; explicitly deferred |
| Directional decay mitigation | none | discussed, not yet decided/implemented |

### Implementation

`config.py`, `prefetch_news.py`, `.claude/agents/analyst.md`,
`tests/test_prefetch_news.py` — see git log for the commit. 83/83 tests
passing.

**Not done in this cycle**: no numeric parameter changes (consistent with
n=1/anomalous-day discipline); the no-0DTE-fallback-policy and
decay-mitigation questions remain open for a future cycle with more
evidence (per-trade IV logging in particular).

---

## 2026-09-02 — Directional risk cut after 3 days of live evidence + external research

**Trigger**: not a Strategist Agent post-mortem cycle — a direct human
decision made mid-conversation after reviewing 3 real trading days'
combined track record (2026-08-31 through 2026-09-02) plus targeted web
research on 0DTE holding periods and directional-vs-premium option
strategy performance.

### The evidence

| | Trades | Wins | Net P&L |
|---|---|---|---|
| Premium-selling (short puts) | 5 | 4 | +$15 (~breakeven) |
| Directional (long calls/puts) | 6 | 1 (META, +$130) | **-$5,671** |

Directional lost on 5 of 6 trades across 3 days, including a severe one
(SPY, -$3,960, an 86% loss) and two 2026-09-02 trades that cleared the
then-55 confidence bar only narrowly (55, 56) and both lost. A live
underlying-move check on the 2026-09-02 pair showed both moved <0.35% net
over the full hold, yet the options lost 68% (AMZN) and 82% (TSLA) of
premium — TSLA's put was hurt by a mid-session spike *against* the thesis
(to $356.56, +1.08% above entry) within the first hour, not by the net
end-of-day move.

**External research** (human-directed, sources below):
- Long ATM/near-ATM 0DTE directional structures show negative median PNL
  on average; short-premium structures (selling slightly OTM
  calls/puts) are positive in up to 75% of observations — a direct match
  to this system's own premium-vs-directional split in the table above.
  ([Vilkov et al., 0dte-strategies paper](https://github.com/vilkovgr/0dte-strategies/blob/main/docs/paper/paper-annotated.md))
- Entry *clock time* (10:00/13:00/15:00/prior-day-close ET, all held to
  the same close) barely changes the qualitative result — the structural
  finding above holds regardless of exactly when you enter. This argues
  the earlier 10-min-vs-30-min observation-window debate (see this file's
  2026-09-01 entry) is probably not the lever that matters.
  ([same source](https://github.com/vilkovgr/0dte-strategies/blob/main/docs/paper/paper-annotated.md))
- The well-cited first-half-hour-predicts-last-half-hour intraday momentum
  finding (Gao/Han/Xie/Zhou) is specifically about the FIRST 30 minutes
  predicting the LAST 30 minutes of the session (a small effect, R²≈1.6%,
  rising to ~3% on high-volatility days) — not about predicting the
  middle of the day. A 2:30 PM exit never reaches that window at all.
  ([SSRN paper](https://www.smallake.kr/wp-content/uploads/2015/01/SSRN-id2440866.pdf))
- Practitioner 0DTE backtests generally favor short holds once real
  option costs (theta, spread, slippage) are counted — one cited backtest
  found a 5-minute opening-range strategy nearly doubled returns vs.
  15/30-minute ranges while reducing drawdown; another successful
  approach averaged ~52 minutes in the market per day, not hours.
  ([SpotGamma 0DTE strategy guide](https://spotgamma.com/0dte-options-strategy/); general practitioner-blog sourcing, less rigorous than the two academic papers above)

### Decisions (human, not Strategist Agent proposals)

| # | Change | Rationale |
|---|---|---|
| 1 | Directional exit: fixed 2:30 PM ET clock time → **30 minutes after each position's own entry** (`config.DIRECTIONAL_HOLD_MINUTES`) | Multi-hour holds accumulate decay cost without reaching the session window (last 30 min) where the one well-documented early-signal predictive edge actually concentrates; today's TSLA adverse move happened in the first hour regardless |
| 2 | `MIN_DIRECTIONAL_CONFIDENCE`: 55 → **60** | Both 2026-09-02 trades cleared the old bar only narrowly (55, 56) and both lost — a stricter bar means fewer, more selective directional trades |
| 3 | `DIRECTIONAL_PCT_PER_STOCK`: 1% → **0.5%** of balance per selected stock (`DIRECTIONAL_MAX_PCT`: 3% → **1.5%** correspondingly) | A long option's max loss is already hard-capped at the premium paid — no additional stop-loss mechanism was needed to bound risk further. The lever that matters given directional's persistent negative tilt is position size, not a new exit rule |
| 4 | **Position recycling (spec §10), built for the first time** — premium-selling side only. When a short put closes (TP or SL), re-rank fresh, exclude tickers held elsewhere, size the top eligible candidate against one slot's worth of the current premium budget, subject to a hard cutoff (no new entries after 2:30 PM ET / `config.NEW_ENTRY_CUTOFF_TIME`). Directional does NOT recycle — still enters once per morning only | Previously cut from MVP scope; capital freed by an early TP/SL sat idle for the rest of the day (a concrete, real cost — e.g. 2026-08-31's two early CSP closes). Scoped to premium only since that's the side with the demonstrated positive track record |

### Before / after

| | Before | After |
|---|---|---|
| Directional exit rule | fixed 2:30 PM ET clock time, all positions | `entered_at + 30 min`, per position |
| `MIN_DIRECTIONAL_CONFIDENCE` | 55 | 60 |
| `DIRECTIONAL_PCT_PER_STOCK` / `DIRECTIONAL_MAX_PCT` | 1% / 3% | 0.5% / 1.5% |
| Directional budget, 1 selected ($100k account) | $1,000 | $500 |
| Directional budget, 3 selected | $3,000 | $1,500 |
| Premium-selling position recycling | not implemented (spec §10) | implemented — TP/SL-triggered, re-ranked, cutoff-gated |
| `config.DIRECTIONAL_CLOSE_TIME` | existed, fixed clock time | removed; replaced by `DIRECTIONAL_HOLD_MINUTES` (duration) + `NEW_ENTRY_CUTOFF_TIME` (same clock value, new purpose: gates recycling entries, not directional exit) |

### Implementation

`config.py`, `execution_agent.py` (`check_directional_exit` changed to a
duration check; new `decide_premium_recycle()`/`attempt_premium_recycle()`;
`run()`'s main loop detects premium closures and attempts recycling before
the cutoff), `risk_manager.py` (docstring only — `compute_budgets()`'s logic
was already parameterized off `config.py`, no code change needed), `SPEC.md`
(§7/§10/§14/§17/§18 all get dated "Changed 2026-09-02" notes). TDD:
`tests/test_execution_agent.py` gains `DecidePremiumRecycleTests` (5 tests)
and rewrites `CheckDirectionalExitTests` for the new duration-based
semantics (4 tests); `tests/test_risk_manager.py`'s `ComputeBudgetsTests`
and one `EvaluateTests` case updated for the new 0.5%/1.5% figures. 91/91
tests passing across the full suite.

**Not done in this cycle**: the bigger structural question raised alongside
this research (whether a naked long call/put is even the right instrument
for testing a short-horizon directional signal, vs. e.g. a defined-risk
spread or risk-reversal structure) is explicitly deferred — flagged as an
open question for a future cycle, not something to change without more
deliberation. Per-trade IV logging (to separate theta from vega/IV-crush in
future post-mortems) also remains undone, tracked in `NEXTSTEPS.md`.

---

## 2026-09-02 (same day, later) — Two open questions closed

Two items left open by the entries above, closed by direct human decision
mid-conversation (not a Strategist Agent cycle):

### `no-0dte-fallback-policy` — decided: fall back to 1DTE, same-day close unchanged

Left open in the 2026-09-01 entry above (proposal 2, "deferred, still
open") and in the 2026-09-02 entry's evidence section. Decided: when a
ticker has no usable same-day (0DTE) chain, `strategy_engine.py`'s
`rank_ticker()` retries once against the next real trading day's chain
(`_next_trading_day()`, via Alpaca's own calendar) instead of skipping the
ticker outright. Applies at the universe-ranking step, before the
premium/directional split — a 1DTE-substituted ticker can land on either
side, same as any other candidate. **Position lifecycle is unchanged**:
the resulting position is still force-closed same-day like every other
position, never held overnight — this was the specific design choice that
kept the change small (no new calendar/position-persistence logic),
deliberately avoiding the "switch the whole strategy to 1DTE" idea already
rejected on 2026-08-31. See `SPEC.md` §2's matching "Changed 2026-09-02"
note. TDD, 8 new tests in `tests/test_strategy_engine.py`
(`NextTradingDayTests`, `RankTicker1DTEFallbackTests`,
`RankUniverseFallbackWiringTests`); 101/101 tests passing across the full
suite after. Live-sanity-checked against real Alpaca data (today's
universe still ranks normally, `_next_trading_day('2026-09-02')` correctly
returned `'2026-09-03'` via the real calendar API) without touching
today's actual `logs/cache/ranking-2026-09-02.json` (a real trading day's
provenance record, left untouched).

### Naked long vs. defined-risk spread — decided: keep the naked long

The structural question deferred in the entry above (whether a naked long
call/put is the right instrument at all vs. a defined-risk spread or
risk-reversal). Decided: **no change** — naked long stays. No code or
config change; this just closes the open question in `NEXTSTEPS.md`.
