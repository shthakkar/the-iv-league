# Next Steps

Read [PROGRESS.md](PROGRESS.md) first for what's done and why. Read
[SPEC.md](SPEC.md) for the full target design. Build approach stays the
same: one component at a time — brief design in chat, implement, test
against real data, commit — not a big upfront plan.

## Resuming a session

```bash
cd /Users/manalithakkar/Documents/ivleague
source venv/bin/activate          # or venv is already set up, just needs activating
```

`.mcp.json` (project-scoped `alpaca-spike` MCP server) has literal
credentials committed — no env vars need exporting for it to work (see
PROGRESS.md's gotchas section for why it's not `${VAR}`-based). `.env` has
the same credentials for `config.py`/`strategy_engine.py`'s direct SDK use.

**Fast iteration without live calls/agent runs**: `mock_cache/2026-08-28/`
has a captured real morning (news, Analyst reads, ranking, selection
result) — point new deterministic code at it instead of re-fetching or
re-running the Analyst subagents. See its README. Regenerate the
cheap/deterministic parts of it anytime with
`venv/bin/python3 scripts/save_mock_fixture.py`.

## Done: Risk Manager (Component 5) — covers BOTH sides

Built, tested (17 unit tests + a live end-to-end run) — see `PROGRESS.md`'s
Component 5 entry for the full writeup. Two spec changes came out of
designing it, both logged in `PROGRESS.md`'s "Architecture decisions
made": premium-selling switched from a defined-risk credit spread to a
plain cash-secured put (spec §6), and the max-daily-loss hard limit (spec
§23) was dropped for V1 as inert dead code with no position recycling.

## Done: Execution Agent wired into the automated pipeline (2026-08-31)

Built + unit-tested Aug 30 (TDD, off the sibling `alpacabot` project's
proven live patterns — see `PROGRESS.md`'s Component 6 entry). On the
morning of 2026-08-31 (first trading day it could run for real), the user
explicitly decided to skip the isolated 1-contract manual validation step
and wire straight into the full automated pipeline instead — paper
account, monitored manually — since a separate validate-first run buys
little extra safety when nothing is real money. What actually got built,
all same-session, see `PROGRESS.md`'s Component 6 entry for the full
writeup:

1. ~~Live-validate on paper, 1 contract, in isolation~~ — **skipped by
   explicit user decision**, see above. Today's real 9:41 AM ET run *is*
   the first live validation, just via the full pipeline instead of a
   standalone script.
2. **Done**: `execution_agent.run()` wired in — not into
   `prompts/morning_decision.md` itself (the LLM session still never
   touches order-placing tools or reads its own decisions file back), but
   into `scripts/run_morning_trigger.sh` as a second, plain-bash stage
   that runs after the LLM session exits and launches
   `execution_agent.py` detached only if the decisions file has an
   approved entry. `--disallowedTools` was **not** loosened — the
   "loosening," per that script's header comment, turned out to be moving
   execution out of the LLM's tool-calling loop entirely, which keeps
   spec §30's LLM/deterministic split intact rather than trading it away.
3. **Done**: this *is* the orchestrator (spec §3) — `run_news_prefetch.sh`
   (9:30 ET) → `run_morning_trigger.sh`'s decision stage (9:41 ET) →
   its execution-handoff stage → `execution_agent.py` monitoring until
   EOD → `eod_force_liquidate.sh` (3:55 ET) as a last-resort backstop.
   No separate "Orchestrator" file was needed — the timeline lives across
   these scripts' cron schedule, matching spec §3's actual list of steps
   almost verbatim.
4. **Done (2026-08-30)**: scheduling installed via `crontab`, reusing the
   existing shared `pmset`/launchd wake-bridge — see `PROGRESS.md`'s
   scheduling entry for the full reasoning (why no new `pmset`/launchd
   was needed, the 6:30 prefetch race accepted as fine, the "`-dimsu`
   survives a closed lid" question still only indirectly de-risked).
   **Added same day (Aug 31)**: `run_morning_trigger.sh` now also ties a
   `caffeinate -dimsu -w $EXEC_PID` to `execution_agent.py`'s own PID once
   launched — the shared 30-min morning bridge alone doesn't reach
   `PREMIUM_EOD_CLOSE_TIME` (12:45 PM PT), so this closes that gap
   specifically for whatever `execution_agent.py`'s own runtime turns out
   to be.
5. **Done**: `scripts/eod_force_liquidate.sh` — separate from
   `execution_agent.py`, uses the **Alpaca CLI** (`position list` →
   `position close-all --cancel-orders`), scheduled 3:55 PM ET via
   `crontab`. Ran for real against the live paper account this session
   (found zero open positions, hit its no-op path) — confirms CLI
   auth/connectivity work, not just that the script parses. Its own
   header states the real limitation plainly: only helps if the Mac is
   actually awake at fire time.

**Resolved 2026-08-31**: fully live-validated, real orders placed and
closed — see `PROGRESS.md`'s "First live trading day" entry for the
complete run (3 real fills, a real standing stop confirmed at Alpaca, 2
real take-profit exits, 1 real time-based exit, clean process exit, and
the EOD safety net firing and correctly no-op'ing). Net P&L: -$3,927
(+$22 TSLA, +$11 AAPL, -$3,960 SPY) — a normal single-day outcome, not a
verdict on either strategy side from one sample.

## Done: 0DTE greeks gap root-caused and fixed (2026-08-31)

Was the actual blocker behind the first automated run producing zero
candidates. Root cause: Alpaca cannot compute greeks/IV for any 0DTE
contract at all (confirmed via their own FAQ + empirical A/B check), not
a feed/timing/subscription issue. Fixed with a local Black-Scholes
solver (`black_scholes.py`, TDD, 14 tests) used as a fallback whenever
Alpaca's own fields are null — see `PROGRESS.md`'s architecture-decisions
entry and `SPEC.md` §4's "Changed 2026-08-31" note for the full story,
including the web research done *before* building it (confirms this is
standard practice for near-zero T, not a shortcut). Considered and
rejected switching the strategy to 1DTE instead — a much bigger change
(calendar logic, possible overnight position persistence, re-tuned exit
timing) that would also deviate from what the spec's own title says the
system is.

## Also fixed live, 2026-08-31: premium-selling concentration cap removed

`MAX_EXPOSURE_PER_UNDERLYING_PCT` (35%, never spec-fixed) was caught
live rejecting every premium-selling candidate on a real run — see
`PROGRESS.md`'s architecture-decisions entry. Removed; `risk_manager.py`
is now spec-literal on §8 (equal split + pool leftover, no per-name
ceiling).

## Done: ranking-consistency gap closed, 2026-08-31

Surfaced during that day's live run: the Analyst-driven directional
selection and Risk Manager's own internal premium-side re-ranking were two
independently-timed fetches, so the same ticker could appear as a
candidate on both sides across a few minutes of live 0DTE drift. Didn't
cause a double-order that day only because the premium side happened to
reject that ticker anyway on cost. Fixed by making `strategy_engine.py`
the single fetch for the whole run: it gained a `--json` CLI mode
(`{universe, expiration_used, ranked, skipped, premium_sell,
directional}`, matching the shape `scripts/save_mock_fixture.py` already
wrote to `mock_cache/<date>/strategy_ranking.json`) whose output the
morning-decision prompt now caches to
`logs/cache/ranking-<date>.json`; `risk_manager.py`'s CLI no longer
calls `strategy_engine.rank_universe()` itself — it loads that cached
file instead (`risk_manager.py <ranking_result.json>
<selection_result.json>`, replacing the old `<expiration>` positional
arg). `evaluate()` and the rest of the Risk Manager's logic are
unchanged — this only touched the two CLIs and
`prompts/morning_decision.md`'s steps 2/6/7. Verified: all 42 unit tests
still pass (the CLI/`__main__` blocks were never unit-tested, same
convention as before); ran the new chain both offline against
`mock_cache/2026-08-28/` (had to regenerate that fixture via
`save_mock_fixture.py` first — it predated `RankedCandidate`'s
Risk-Manager-sizing fields and no longer matched the dataclass) and live
against real Alpaca data, both producing correct APPROVE/REJECT batches
end to end.

## Verified, no change needed: the "stale REJECT reason" note

Traced this before touching any code. The confusing example
(`"pooled leftover $95,000"` identical across three different
rejections, in `logs/cache/risk-decisions-2026-08-31-manual.txt`) is a
frozen artifact from **before** the concentration-cap removal — back
then the string reported the raw uncapped leftover while the actual
decision used the capped amount, which really was misleading. Hand-
traced the numbers on the real post-fix run
(`logs/cache/risk-decisions-2026-08-31.json`: target share $31,667 →
TSLA consumes part of the pool on retry → META correctly rejected
against the true remaining $28,000) and the reason string is accurate
as-is. No code change made; the frozen `-manual.txt` log is left
untouched as a historical record rather than "fixed" to match current
behavior.

## Next up
- **Verify tomorrow's cron fires cleanly end-to-end unattended** — today's
  actual trades were placed manually (user explicitly chose to skip
  straight to the full pipeline once the greeks fix landed, rather than
  wait for a second automated cron fire). The cron-race fix (backup
  entries + lock files) was confirmed *not* to cause a double-run today,
  but the full unattended path — cron fires primary, decision run
  completes, execution hands off automatically, no manual step — hasn't
  been watched end-to-end yet.
- ~~**Strategist Agent + HITL Review gate**~~ — **Done, 2026-08-31.** Built
  as a `strategist` subagent (`.claude/agents/strategist.md`), validated
  live twice against real 2026-08-31 trade data. HITL review happens
  interactively in a Claude Code session (not a web page/local server, not
  a separate headless Implementor agent — dropped both during design in
  favor of just implementing approved changes directly in-session) — this
  deviates from spec §26-27's more formal Backtest → Compare → Paper →
  Risk Approval → Production pipeline, none of which exists; see
  `STRATEGY_CHANGELOG.md` for why that's an accepted trade-off for now.
  First real review cycle done same day: all 5 of the Strategist's
  proposals + 1 human-suggested change (directional allocation: flat 5% →
  1% per selected stock capped at 3%) implemented via 3 parallel agents on
  disjoint files, TDD, 65/65 tests passing, 4 commits. Full before/after
  and AI-vs-human attribution tracked in `STRATEGY_CHANGELOG.md`, which
  the Strategist Agent itself now reads on every run so it doesn't
  re-propose already-decided things. See PROGRESS.md's Component 7 entry
  for the full writeup.
- ~~**Dashboard** (spec §29)~~ — **Rebuilt, 2026-08-31**, branded "The IV
  League," real data. `export_dashboard_data.py` generates
  `dashboard/data.json` from the real `logs/*-execution.log` files + a
  live account snapshot (no longer sample data); `dashboard/index.html` is
  a four-tab page (Overview/Trade Journal/Strategy Evolution/Architecture)
  with hash deep-links. See `PROGRESS.md`'s Dashboard entry and
  `docs/superpowers/specs/2026-08-31-hackathon-dashboard-design.md` for
  the full design. Verified live in Chrome. Still open:
  - **GitHub Actions automation** — running `export_dashboard_data.py` on
    a schedule and committing the result, so the dashboard updates itself
    instead of a manual re-run. Not built yet.
  - ~~**Turning GitHub Pages on**~~ — **Confirmed already live, 2026-09-02.**
    Checked via `gh api repos/.../pages`: already serving from `main` at
    repo root (exactly what was needed), verified both `/dashboard/` and
    `/STRATEGY_CHANGELOG.md` return 200 live at
    `https://shthakkar.github.io/the-iv-league/`. No action was needed.
  - ~~**Unrealized P&L for OPEN positions**~~ — **decided not needed,
    2026-09-02.** Dropped by explicit human decision.
- ~~**Position recycling** (spec §10)~~ — **Built, 2026-09-02**, premium-selling
  side only. See `STRATEGY_CHANGELOG.md`'s 2026-09-02 entry and
  `execution_agent.py`'s `decide_premium_recycle()`/`attempt_premium_recycle()`.
  Not yet live-validated (built same session as this note, next real
  trading day is the first live test of it firing for real).
- **News/events for premium-selling side** — Analyst inputs (§12) are
  directional-strategy-specific; premium-selling ranking is pure IV skew
  math (§5) and doesn't need news.
- **Empirical bar-latency test** — how much lag between a 1-min bar's
  nominal close and it being queryable via the API. Still not explicitly
  measured — today's live run used real bars successfully (with
  `feed=iex` required explicitly, SIP 403's on this account) but didn't
  measure the lag itself. Do this before tightening the 9:41 trigger
  buffer.

## Next up (from 2026-09-01)

- ~~**Re-validate `analyst.md`'s new observation-persistence step after a
  Claude Code restart.**~~ — **Confirmed live, 2026-09-02.** Post-restart,
  re-dispatched the `analyst` subagent for MSFT/2026-09-02 (standard 10-min
  window): returned a real structured read (UNDECIDED, 35, whipsaw-vs-BofA-
  catalyst reasoning) and correctly wrote
  `logs/cache/analyst-observation-2026-09-02-MSFT.json`. Separately, this
  morning's real live cron run had already exercised it for real at 7:11 AM
  (5 tickers, real bars/news persisted) — both confirm it independently.
- ~~**Re-validate `strategist.md`'s new §6 Profitability Analysis + WebSearch
  tool after a restart, too**~~ — **Confirmed live, 2026-09-02.**
  Post-restart re-dispatch against 2026-09-02 produced a real §6
  Profitability Analysis section (premium vs. directional P&L breakdown,
  cross-day trend read from `dashboard/data.json`) and correctly reasoned
  about *not* calling `WebSearch` this time (the pattern it found was
  already researched and sourced in that day's earlier `STRATEGY_CHANGELOG.md`
  entry — a fresh search would've been redundant, not a sign the tool isn't
  wired up). Note: the pre-restart run at 11:38 AM that morning lacked this
  section entirely, which is itself confirmation the edit hadn't been live
  yet at that point — backed up before overwriting with the re-validation run.
- ~~**`no-0dte-fallback-policy`**~~ — **Decided and built, 2026-09-02: fall
  back to 1DTE.** When a ticker has no same-day 0DTE chain,
  `strategy_engine.py` now retries once against the next real trading day's
  chain before skipping it. Position lifecycle unchanged — still
  force-closed same-day like every other position, never held overnight, so
  this stayed a small change (no calendar/position-persistence logic). See
  `STRATEGY_CHANGELOG.md`'s matching 2026-09-02 entry and `SPEC.md` §2.
  TDD, 101/101 tests passing. Not yet live-validated against a real no-0DTE
  day (today had a full 8-ticker chain, so the fallback path never actually
  triggered live) — watch for it the next time a ticker genuinely lacks a
  same-day chain.
- **Directional time-decay mitigation — partially acted on, 2026-09-02.**
  Three options were on the table (skip no-0DTE days entirely / buy deeper
  ITM on any substitute trade / use a defined-risk debit spread instead of
  a naked long) after a 2026-09-01 example (AAPL: spot -0.3%, option -42%).
  None of those three were implemented directly — instead, after 3 days'
  combined evidence (directional 1/6 win rate, net -$5,671) plus external
  research, a different set of changes landed: directional hold shortened
  to 30 min from entry, confidence bar raised to 60, allocation halved to
  0.5%/1.5%, and premium-side recycling built. See `STRATEGY_CHANGELOG.md`'s
  2026-09-02 entry for the full reasoning and sources. **Still open**:
  logging entry/exit IV per directional trade (to separate theta from
  vega/slippage in future post-mortems — still not done). ~~The bigger
  structural question of whether a naked long call/put is the right
  instrument at all vs. a defined-risk spread or risk-reversal~~ —
  **decided, 2026-09-02: keep the naked long, no change.** See
  `STRATEGY_CHANGELOG.md`'s matching entry.
- **Watch tomorrow's cron run all four 2026-09-02 changes live for the
  first time**: the shortened directional hold, the raised confidence bar,
  the smaller allocation, and — especially — premium recycling actually
  triggering a real re-rank + new order mid-morning. None of this has run
  unattended yet.
