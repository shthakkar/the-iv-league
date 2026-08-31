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

## Next up

- **Ranking-consistency gap between the two strategy sides** — surfaced
  during today's live run: the Analyst-driven directional selection and
  Risk Manager's own internal premium-side re-ranking are two
  independently-timed fetches, so the same ticker can appear as a
  candidate on both sides across a few minutes of live 0DTE drift.
  Didn't cause a double-order today only because the premium side
  happened to reject that ticker anyway on cost. Worth hardening (one
  shared ranking snapshot for both sides) before running unattended
  without a human reviewing each step.
- **Verify tomorrow's cron fires cleanly end-to-end unattended** — today's
  actual trades were placed manually (user explicitly chose to skip
  straight to the full pipeline once the greeks fix landed, rather than
  wait for a second automated cron fire). The cron-race fix (backup
  entries + lock files) was confirmed *not* to cause a double-run today,
  but the full unattended path — cron fires primary, decision run
  completes, execution hands off automatically, no manual step — hasn't
  been watched end-to-end yet.
- **Strategist Agent + HITL Review gate** (spec §24-27) — the "no trade
  history to analyze" blocker is gone as of today (3 real closed trades),
  but the user explicitly said hold off for now. Pick up once there's
  more than one day's worth of outcomes, or whenever asked. HITL itself
  has no code to build (manual review step by design), but the handoff
  contract (what the Strategist hands the reviewer, what
  "approve"/"reject" writes back) needs designing alongside the
  Strategist Agent.
- **Dashboard** (spec §29) — the page itself is done:
  `dashboard/index.html` + `dashboard/data.json` (sample) +
  `dashboard/README.md` (schema). No longer blocked on "no real trade
  data exists" — today produced exactly that. Still open: where the
  Alpaca-CLI export step lives (inside the Execution Agent's loop vs. a
  separate script it shells out to), and the GitHub Pages publish
  mechanism (commit-on-trade vs. scheduled rebuild) — see PROGRESS.md's
  Dashboard entry. Turning GitHub Pages on for this repo (Settings →
  Pages → serve from `/dashboard`) is still a 2-minute task, still not
  done.
- **Position recycling** (spec §10) — re-entering after an early
  profitable close. Cut from MVP scope. Today's two CSPs both hit TP
  early (TSLA at 11:44 ET, AAPL at 10:51 ET) and then sat idle the rest
  of the day per the current MVP design — a concrete, real example of the
  capital-left-idle cost this would address.
- **News/events for premium-selling side** — Analyst inputs (§12) are
  directional-strategy-specific; premium-selling ranking is pure IV skew
  math (§5) and doesn't need news.
- **Empirical bar-latency test** — how much lag between a 1-min bar's
  nominal close and it being queryable via the API. Still not explicitly
  measured — today's live run used real bars successfully (with
  `feed=iex` required explicitly, SIP 403's on this account) but didn't
  measure the lag itself. Do this before tightening the 9:41 trigger
  buffer.
- **Fix the REJECT reason string's stale wording** — minor: when a
  premium-selling candidate is rejected, the message still says "pooled
  leftover $X" — harmless now that there's no cap distorting that number,
  but worth double-checking the wording reads correctly given the cap
  removal.
