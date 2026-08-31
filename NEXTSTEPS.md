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

## Immediate next: Execution Agent — places the approved orders

Once Risk Manager approves something:
1. Place the order via the raw `alpaca-py` SDK (`TradingClient`), **not**
   the MCP server — a deterministic script can't invoke an MCP tool
   without building an MCP client from scratch, and the "must use
   Alpaca's MCP server" requirement is already satisfied by the Analyst's
   read-only usage (see PROGRESS.md's handoff design note). Premium-selling
   is a plain single-leg CSP order now (spec §6 changed 2026-08-30, see
   PROGRESS.md) — no multi-leg/`mleg` order needed for that side.
2. Log the entry (ticker, contract, qty, price, timestamp, side) — spec
   §27 data logging starts here, not bolted on later.
3. Monitoring loop, ~every 1 minute (spec §19-20):
   - Short puts: close at 50% TP, 3× SL, or EOD — whichever first (§9).
   - Long options: close at 2:30 PM ET regardless of P&L, no TP/SL (§18).
   This loop is plain Python — no LLM, no Claude Code invocation needed,
   which also means it doesn't have to run inside a `claude -p` session
   (see the launchd note below).
4. Test on paper with 1 contract before trusting real position sizing.

## Then: Orchestrator (Component 6) + EOD safety net

- Ties the full daily timeline together (spec §3).
- **The morning-trigger scaffolding already exists and is ready to
  extend** (built this session, see PROGRESS.md): `prompts/morning_decision.md`
  + `scripts/run_morning_trigger.sh` (9:41 ET, dispatches Analyst,
  currently decision-only/no orders) and `scripts/run_news_prefetch.sh`
  (9:30 ET, warms the news cache). Once Risk Manager + Execution exist,
  extend the prompt to call them instead of stopping at the log file —
  and loosen the `--disallowedTools` order-blocking list deliberately,
  not by accident.
- **launchd/pmset install is still pending a go-ahead** — this can
  actually happen independently/anytime, since the decision-only prompt
  is already safe to run unattended. Researched pattern (via alpacabot,
  see PROGRESS.md): `pmset repeat wake` + a bridging caffeinate
  LaunchAgent + per-script `caffeinate -w $PID`. Open question: whether
  `-dimsu` survives a closed lid on this Mac — untested, verify for real
  rather than assuming.
- **EOD liquidation via the Alpaca CLI**, not the SDK/MCP — deliberately
  a separate, dumb, unmissable safety net independent of whatever else
  may be hung or broken.

## Deferred / not yet decided

- **Strategist Agent + HITL Review gate** (spec §24-27 — Strategist's
  post-day backtesting/analysis loop, now gated by a human approve/reject
  step (§26) before any proposal reaches Backtest) — out of scope until
  there's actual trade history to analyze. HITL itself has no code to
  build (it's a manual review step by design), but the handoff contract
  (what the Strategist hands the reviewer, what "approve"/"reject" writes
  back) needs designing alongside the Strategist Agent when that's picked
  up.
- **Dashboard** (spec §29) — the page itself is done:
  `dashboard/index.html` + `dashboard/data.json` (sample) +
  `dashboard/README.md` (schema). What's left is blocked on the Execution
  Agent existing, since there's no real trade data to export yet: where
  the Alpaca-CLI export step lives (inside the Execution Agent's loop vs.
  a separate script it shells out to), and the GitHub Pages publish
  mechanism (commit-on-trade vs. scheduled rebuild) — both still open, see
  PROGRESS.md's Dashboard entry. Turning GitHub Pages on for this repo
  (Settings → Pages → serve from `/dashboard`) is a 2-minute task whenever
  there's something worth looking at live — hasn't been done yet.
- **Position recycling** (spec §10) — re-entering after an early
  profitable close. Cut from MVP scope; add once the base execution loop
  is solid.
- **News/events for premium-selling side** — Analyst inputs (§12) are
  directional-strategy-specific; premium-selling ranking is pure IV skew
  math (§5) and doesn't need news.
- **Empirical bar-latency test** — how much lag between a 1-min bar's
  nominal close and it being queryable via the API. Blocked on market
  being open (was closed all of this session); do it live at the next
  market open before tightening the 9:41 trigger buffer.
