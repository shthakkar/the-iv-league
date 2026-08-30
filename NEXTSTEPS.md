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

## Immediate next: Risk Manager (Component 5) — covers BOTH sides

**Correction from earlier planning**: contract selection and sizing are
not a separate step before the Risk Manager — spec §22 has the Risk
Manager's own output include contract and quantity, since it needs the
option chain as an input anyway (to check a candidate's actual max loss
against the daily limit). So this one module does the whole thing per
candidate, for both premium-selling and directional:

**Premium-selling side** (top 3 from `strategy_engine.split_candidates()`):
1. Already have the 15Δ put (from the ranking). Pick the protective
   further-OTM put — spread width as a new `config.SPREAD_WIDTH` constant,
   not hardcoded.
2. Size toward the ~95%/3 ≈ $31,667 per-name target (spec §8) — Risk
   Manager decides actual contract quantity from spread width/max
   loss/buying power, not a forced full allocation.
3. Check against spec §23 hard limits (below).
4. Output APPROVE (ticker, contract legs, quantity, max risk, TP/SL
   levels) or REJECT + reason.

**Directional side** (the `selected` list from
`directional_selection.py` — already built, tested against
`mock_cache/2026-08-28/selection_result.json`):
1. BULLISH → 0DTE call, BEARISH → 0DTE put, ATM or slightly ITM (spec
   §15 — avoid far-OTM lottery strikes).
2. Size within $5,000 total directional premium, split across however
   many were selected (spec §16).
3. Check against spec §23 hard limits.
4. Output APPROVE (ticker, contract, quantity, capital allocation) or
   REJECT + reason. No stop-loss/take-profit fields for this side — exit
   is purely time-based (§18).

**Hard risk controls, spec §23** (apply to both sides):
- Max daily loss (configurable %, e.g. 2% = $2,000) — no new positions
  once hit; existing positions still follow their own exit rules.
- Max exposure per underlying.
- Max number of positions.
- Max number of re-entries (relevant once position recycling exists —
  see Deferred below).

**Testing**: directional side can be fully tested right now against the
mock fixture, no live calls needed. Premium-selling side needs a real (or
Aug-31-proxy, same pattern as before) chain fetch via `strategy_engine`
to get real strikes/prices to size against.

## Then: Execution Agent — places the approved orders

Once Risk Manager approves something:
1. Place the order via the raw `alpaca-py` SDK (`TradingClient`), **not**
   the MCP server — a deterministic script can't invoke an MCP tool
   without building an MCP client from scratch, and the "must use
   Alpaca's MCP server" requirement is already satisfied by the Analyst's
   read-only usage (see PROGRESS.md's handoff design note). Multi-leg
   support needed for the premium-selling credit spread.
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
