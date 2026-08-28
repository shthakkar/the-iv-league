# Next Steps

Read [PROGRESS.md](PROGRESS.md) first for what's done and why. Read
[SPEC.md](SPEC.md) for the full target design. Build approach stays the
same: one component at a time — brief design in chat, implement, test
against real paper-account data, commit — not a big upfront plan.

## Resuming a session

```bash
cd /Users/manalithakkar/Documents/ivleague
source venv/bin/activate          # or venv is already set up, just needs activating
```

`.mcp.json` (project-scoped `alpaca-spike` MCP server) has literal
credentials committed — no env vars need exporting for it to work (see
PROGRESS.md's gotchas section for why it's not `${VAR}`-based). `.env` has
the same credentials for `config.py`/`strategy_engine.py`'s direct SDK use.

## Immediate next component: #2, Premium-selling execution

Design already agreed (see PROGRESS.md Component 2 section) — just not
built yet because market was closed when we got here. When markets are open
again:

1. Take `strategy_engine.rank_universe()` → `split_candidates()`'s
   premium-sell list (top 3).
2. For each: build a credit spread — sell the already-found 15Δ put, buy a
   further-OTM put for protection. Spread width should be a config constant
   (e.g. `config.SPREAD_WIDTH`), not hardcoded.
3. Place via the MCP server's multi-leg option order tool
   (`mcp__alpaca-spike__place_option_order` — confirmed during the spike to
   support multi-leg, e.g. bull/bear spreads). This is the piece that
   actually exercises "use Alpaca's MCP server" for something other than
   read-only data, so don't quietly fall back to the raw SDK for this part.
4. Apply spec §9 exit rules: close at 50% of original premium (profit), or
   3× original premium (stop), or end-of-day — whichever hits first. This
   will need a monitoring loop (see Component 6 below — Execution Agent).
5. Test on paper with 1 contract before trusting it with real position
   sizing logic.

## Then #4: Directional execution

Takes the Analyst's output (already validated — see PROGRESS.md's
META/MSFT BULLISH example) and:
1. BULLISH → buy 0DTE call, BEARISH → buy 0DTE put, at ATM or slightly ITM
   (spec §15 — avoid far OTM lottery tickets).
2. Size within the $5,000 total directional premium cap (spec §16), split
   across however many trades actually got selected.
3. Hold to 2:30 PM ET, close regardless of P&L (spec §18 — no profit target,
   no trailing stop, deliberately). This is a clean, dumb time-based exit —
   resist the urge to add cleverness the spec explicitly says not to.

## Then #5: Risk Manager

Deterministic (not an LLM call — see PROGRESS.md's architecture decision on
why). Wraps both Component 2 and Component 4's order placement:
- Buying power check.
- Max daily loss cap (spec §23 — e.g. 2% of account = $2,000 → no new
  positions once hit, existing positions still follow their own exit rules).
- Max exposure per underlying / max total positions / max re-entries
  (all configurable, spec §23).
- Can REJECT what the Strategy Engine or Analyst proposed (spec §22's
  worked example: Analyst says BUY TSLA CALL confidence 88, Risk Manager
  REJECTs because daily risk is already at the limit).

## Then #6: Orchestrator + EOD safety net

- Ties the daily timeline together (spec §3): 9:30 collect-only, 9:30-9:40
  observation, ~9:40 rank → analyze → risk-check → execute, ongoing
  ~1-minute Execution Agent monitoring loop, hard EOD liquidation.
- **Run-now / demo flag**: since real-world testing keeps running into
  "market's closed right now," the orchestrator should support triggering
  the full pipeline on-demand against live data, not just at real 9:40 AM
  ET — same pattern `EXPIRATION_OVERRIDE` already uses in
  `strategy_engine.py`.
- **EOD liquidation via the Alpaca CLI**, not the SDK/MCP — deliberately a
  separate, dumb, unmissable safety net independent of whatever else may be
  hung or broken (spec's own philosophy: deterministic execution shouldn't
  depend on the fancier layers working).

## Deferred / not yet decided

- **Strategist Agent** (spec §24-26, post-day backtesting/analysis loop) —
  out of scope until there's actual trade history to analyze. Don't build
  ahead of having data.
- **Position recycling** (spec §10) — re-entering after an early profitable
  close. Explicitly cut from MVP scope; add once the base execution loop
  (#2/#4/#6) is solid.
- **News/events for premium-selling side** — spec's Analyst inputs (§12)
  are directional-strategy-specific; premium-selling ranking is pure IV
  skew math (§5) and doesn't need news. Don't add it there unless the
  Strategist Agent later finds it's predictive.
- **Data logging** (spec §27) — nothing persists trade/decision history
  yet. Worth adding alongside Component 6 (the orchestrator is the natural
  place to log each decision as it's made) rather than bolting on later.
