# Progress

Full spec: [SPEC.md](SPEC.md). This file tracks what's actually been built and
validated against it. Build approach: one component at a time (design →
implement → test on real paper-account data → commit), not a single upfront
plan — see git log for the granular history.

## Architecture decisions made

- **Deterministic vs. LLM split**: only the Analyst genuinely needs judgment
  (per spec §30, "AI agents should not replace deterministic risk and
  execution logic"). Strategy Engine, Risk Manager, and Execution Agent are
  plain Python — fast, unambiguous, no LLM latency on the hot path.
- **Agent runtime**: Claude Code subagents (`.claude/agents/*.md`), not a
  standalone Python service calling the Anthropic API directly. Fastest to
  build, and naturally exercises Alpaca's MCP server as the agent's tool
  interface.
- **Alpaca's three required surfaces, mapped**:
  - **Trading API** (direct `alpaca-py` SDK) → Strategy Engine's chain
    fetch/ranking math, deterministic order placement/monitoring (once
    built).
  - **MCP server** (`alpaca-spike`, project-scoped in `.mcp.json`) →
    Analyst subagent's tool interface (bars, news, clock).
  - **CLI** (`alpacahq/cli`, installed via `go install`, not brew — see
    Gotchas) → intended for ops/demo status checks, the EOD
    force-liquidation safety net, and (as of the Dashboard design, spec
    §29) the account-snapshot pull behind the Dashboard's data export
    (none of these wired in yet).
- **Universe bumped to the real 8, 2026-08-31**: `config.py`'s `UNIVERSE`
  was `[SPY, QQQ, NVDA, TSLA]` (4, not the spec's 8) as a dev-speed
  default — every ticker in the spec's 8 had already been tested via
  `UNIVERSE_OVERRIDE` and worked, but the default itself was never bumped
  before cron started firing live runs. Caught by the decision run itself
  on its first live morning (flagged the mismatch in its own output), then
  fixed same-day: default is now the real 8 (`SPY QQQ NVDA TSLA AAPL AMZN
  MSFT META`), `UNIVERSE_OVERRIDE` still available for a one-off smaller
  run. Verified: all 28 unit tests still pass, `config.UNIVERSE` reports
  the 8, `prefetch_news.py` pulled real news for all 8 (previously only
  the 4), `strategy_engine.py`'s ranking header now lists all 8 (still
  zero candidates today — same greeks gap as before, now confirmed across
  the full universe rather than just the 4).
- **PREMIUM_SELL_COUNT = 3**, matching the spec's top-3 split (was
  temporarily 1 during early testing, corrected).
- **News source**: Alpaca's own News API (`NewsClient`, Benzinga-sourced) —
  reinforces the "use Alpaca's API" requirement rather than diluting it with
  a third-party source, and it's free/already-authenticated.
- **Strategy changes require human approval (HITL)**: added spec §26 — the
  Strategist Agent's proposed changes go through an explicit human
  approve/reject gate before any backtest work starts, not straight into
  the Backtest → Paper Trading → Risk Approval pipeline. Mirrors the
  Analyst's no-execution-authority rule (§12): the Strategist can *propose*,
  never advance its own proposal. Decided 2026-08-30, before either the
  Strategist or the gate itself is built — see Component 6 below.
- **Premium-selling switched from defined-risk credit spread to plain
  cash-secured put (CSP)**: changed spec §6, decided 2026-08-30 while
  designing the Risk Manager, before any of it is built. No protective
  leg — buying power required per contract is now the full strike × 100
  (was just the spread width), so far fewer contracts fit the same
  per-name budget (checked against the real NVDA/AMZN/AAPL run below: 1
  contract each at the $31,667 target, ~83% budget utilization, no room
  to size up). Knowingly accepted trade-off: risk is now uncapped below
  the strike instead of structurally floored by a protective put.
  **Correction, 2026-08-30, later same session**: originally recorded
  here as "Alpaca has no broker-side stop-loss for options" — checked
  their OrderClass API spec (`bracket`/`oco`/`oto` are equity-only) and
  concluded from that alone that no stop-loss mechanism existed for
  options at all. That was wrong: `bracket`/`oco`/`oto` (atomically
  bundling TP+SL as child orders) really are equity-only, but a
  standalone `type: "stop"` order (`order_class: "simple"`) works fine
  on a single option leg and genuinely holds/fires server-side —
  confirmed by checking the sibling `alpacabot` project (per the user's
  prompt, since it's already placed real option trades): its
  `trade_manager.py` submits exactly this via `submit_stop_market_sell`,
  and its real logs show it firing (`🛑 STOP FILLED @ 4.14`,
  `🛑 STOP FILLED @ 3.12`, both on live SPY option positions). So the §9
  stop-loss can be a real standing order at Alpaca, not just a
  polling-loop check — see Component 6 below for how the Execution Agent
  uses this. `SPREAD_WIDTH` config constant and the protective-put fetch
  step are still dropped from the Risk Manager design, independent of
  this correction — that call was about capital efficiency and buying
  power (spec §8), not about stop-loss reliability.
- **Local Black-Scholes greeks fallback for 0DTE (`black_scholes.py`), 2026-08-31**:
  Alpaca's own greeks/IV are structurally unavailable for 0DTE contracts
  (see `SPEC.md` §4's "Changed 2026-08-31" note for the full root-cause
  story). Rather than switch the whole strategy to 1DTE (considered and
  rejected — a much bigger change: calendar logic, possible overnight
  position persistence, re-tuned exit timing, and a real deviation from
  what the spec's own title says the system is), built a local
  Black-Scholes IV/delta solver instead — the actual root-cause fix, not a
  workaround. Researched first (background web-research agent, sourced):
  using real hours-to-close as T instead of literal zero is standard
  practice (a QuantConnect user hit the identical symptom and fixed it the
  same way); real caveats (vega collapse, whippy delta/gamma) concentrate
  in deep OTM/ITM strikes as T→0, not the ATM/~15Δ region this project
  targets. Built TDD (`tests/test_black_scholes.py`, 14 tests: put-call
  parity, delta bounds, delta relationship, IV round-trip at both normal
  and 0DTE-scale T, graceful `None` on implausible/arbitrage-violating
  prices, T-floor behavior). Integrated into `strategy_engine.py`'s
  `_fetch_liquid_chain()` as a fallback — Alpaca's own fields are used
  when present (non-0DTE expirations, unaffected), local calc only kicks
  in when they're null. Proof-of-concept validated against real live 0DTE
  data before building: hand-solved IV/delta for a real SPY 0DTE quote
  matched the *real* Alpaca-provided delta for the same strike one day
  later almost exactly (0.2045 local vs. 0.2051 real 1DTE) — sanity
  check confirmed by the research (delta is moneyness-dominated, not
  time-dominated, at these horizons). New `config.RISK_FREE_RATE = 0.045`
  constant (negligible effect at these T's, not pulled live). This is what
  actually unblocked the account: strategy_engine.py went from "all 8
  tickers skipped, zero candidates" to a full clean 8-ticker ranking with
  real numbers the same session.
- **Premium-selling per-name concentration cap removed, 2026-08-31**:
  `MAX_EXPOSURE_PER_UNDERLYING_PCT` (0.35) was never spec-fixed (spec §8
  says "roughly equal allocation... never force a trade merely to use all
  available capital" — nothing about a per-name ceiling) and was actively
  fighting `allocate_premium_positions()`'s own leftover-pooling pass:
  pooling exists to rescue a candidate that couldn't afford its own equal
  share, and the cap then re-blocked that same rescue once the pool was
  big enough to help. Caught live: a real run rejected every single
  premium-selling candidate (TSLA/META/SPY, all individually too
  expensive for the $100k account's 35%-of-budget ceiling) purely on this
  tension, not genuine unaffordability — confirmed by re-running the exact
  same data without the cap, which approved a real position. Removed from
  `risk_manager.py`'s `evaluate()` call; `allocate_premium_positions()`
  still accepts the parameter (tested) if a cap is ever wanted again, it's
  just not applied by the live path. Trade-off accepted knowingly: a
  single name can now take a much larger share of the premium budget when
  others are too expensive to use the leftover (seen same day: TSLA took
  $35,750, ~38% of the $95k budget, in one name).
- **Daily loss limit (spec §23) dropped for V1**: decided 2026-08-30. Its
  "once hit, no new positions" phrasing is a circuit breaker for a
  *second wave* of trades after an early loss — but V1 never has one
  (position recycling is deferred, directional only fires once at 9:40
  AM), so there's nothing later in the day for it to ever block. Not
  reintroducing it as a pre-trade sizing cap either, since that would
  directly conflict with §8's ~$31,667/name allocation target (a
  pre-trade worst-case-loss reservation would shrink premium-side
  positions by an order of magnitude versus what §8 describes). Revisit
  once position recycling exists and a real second wave of trades is
  possible. `MAX_DAILY_LOSS_PCT` is not going into `risk_manager.py`.
- **Ranking-consistency gap closed, 2026-08-31**: `risk_manager.py`'s CLI
  used to call `strategy_engine.rank_universe()` itself — a second,
  independently-timed live chain fetch minutes after the one the
  morning-decision prompt already did for Analyst dispatch/directional
  selection. 0DTE IV skew moves fast enough that the two fetches could
  disagree on which tickers were premium-selling vs. directional (a real
  live example: the top-3 split shifted four times in ~10 minutes on
  2026-08-31 — see that day's "First live trading day" entry below). Not
  fixed same-day since it didn't cause an actual double-order that time
  (the premium side happened to reject the overlapping ticker on cost
  anyway) — logged in `NEXTSTEPS.md` and picked up as its own task.
  Fixed by making `strategy_engine.py` the single ranking fetch for the
  whole run: it gained a `--json` CLI mode (same
  `{universe, expiration_used, ranked, skipped, premium_sell,
  directional}` shape `scripts/save_mock_fixture.py` already wrote to
  `mock_cache/<date>/strategy_ranking.json`); `risk_manager.py`'s CLI now
  loads that cached file instead of re-fetching
  (`risk_manager.py <ranking_result.json> <selection_result.json>`,
  replacing the old `<expiration>` positional). `evaluate()` and the rest
  of the sizing logic are unchanged. Verified: all 42 unit tests still
  pass (CLI/`__main__` was never unit-tested, same convention as the rest
  of this file); the chain was exercised both offline against
  `mock_cache/2026-08-28/` (had to regenerate that fixture via
  `save_mock_fixture.py` first — it predated `RankedCandidate`'s
  Risk-Manager fields and no longer matched the dataclass) and live
  against real Alpaca data, both producing correct APPROVE/REJECT
  batches end to end. See `NEXTSTEPS.md`'s matching entry.
- **"Stale REJECT reason" note checked, no change needed, 2026-08-31**:
  `NEXTSTEPS.md` flagged the premium-side REJECT reason string's "pooled
  leftover $X" wording as possibly stale after the concentration-cap
  removal. Traced it by hand before editing anything: the confusing
  example (`"pooled leftover $95,000"` identical across three different
  rejections, in `logs/cache/risk-decisions-2026-08-31-manual.txt`) is a
  frozen artifact from *before* the cap removal — the string reported the
  raw uncapped leftover while the real decision used the capped amount,
  which really was misleading back then. On the real post-fix run
  (`logs/cache/risk-decisions-2026-08-31.json`), hand-tracing the pool
  (target share $31,667 → TSLA consumes part of it on retry → META
  correctly rejected against the true remaining $28,000) confirms the
  string is accurate as-is. No code change made; the frozen `-manual.txt`
  log is left untouched as a historical record.
- **`no-0dte-fallback-policy` decided and built, 2026-09-02**: left open
  since the 2026-09-01 anomalous day (only SPY/QQQ had a same-day chain).
  Decided: fall back to 1DTE, not skip. `strategy_engine.py`'s
  `rank_ticker()` now retries once against the next real trading day's
  chain (`_next_trading_day()`, via `TradingClient.get_calendar()` — no
  local weekday/holiday math) before giving up, at the universe-ranking
  step so a substituted ticker can land on either side of the
  premium/directional split. Deliberately scoped small: **position
  lifecycle is unchanged**, a 1DTE-substituted position is still
  force-closed same-day like every other position (spec §9/§18,
  `execution_agent.py`) — this is what keeps it from being the "switch the
  whole strategy to 1DTE" idea already rejected 2026-08-31 (which would
  have needed real overnight position persistence). `RankedCandidate`
  already had a per-ticker `expiration` field from day one, just always
  fed the same value — no interface change needed downstream in
  `risk_manager.py`/`execution_agent.py`, both build entirely off the OCC
  symbols `strategy_engine.py` returns. TDD, 8 new tests
  (`tests/test_strategy_engine.py`: `NextTradingDayTests`,
  `RankTicker1DTEFallbackTests`, `RankUniverseFallbackWiringTests`),
  101/101 tests passing across the full suite. Live-sanity-checked against
  real Alpaca data (`_next_trading_day('2026-09-02')` correctly returned
  `'2026-09-03'` via the real calendar API; normal ranking unaffected)
  without touching today's real `logs/cache/ranking-2026-09-02.json`. Not
  yet live-validated against an actual no-0DTE ticker (today had a full
  8-ticker chain) — see `NEXTSTEPS.md`. See `SPEC.md` §2 and
  `STRATEGY_CHANGELOG.md`'s matching 2026-09-02 entry.
- **Naked long vs. defined-risk spread for directional trades — decided,
  2026-09-02: keep the naked long.** Closes the structural question
  deferred in the 2026-09-02 directional-risk-cut entry below. No code
  change.

## Component 1: Strategy Engine — ✅ DONE, tested

Files: `config.py`, `strategy_engine.py`.

Ranks the universe by IV skew (spec §5): fetches spot (last trade price) +
option chain per ticker, finds the ATM strike and ~15Δ put among liquid
(greeks-populated) contracts, computes `IV Skew = 15Δ Put IV - ATM IV`, ranks
descending, splits into premium-sell (top `PREMIUM_SELL_COUNT`) vs.
directional (the rest). Tickers with no liquid chain for the target
expiration are skipped with a logged reason, not a crash.

**Run it**: `EXPIRATION_OVERRIDE=YYYY-MM-DD python3 strategy_engine.py`
(override only needed when markets are closed / testing a non-today date —
in production it defaults to today's date, i.e. the real 0DTE expiration).
`UNIVERSE_OVERRIDE=SPY,QQQ,...` to test an ad-hoc ticker list.

**Validated**: full 8-ticker spec universe ranks cleanly with zero skips
(latest run, Aug 31 expiry used as a same-day-0DTE proxy since markets were
closed):

```
Ticker       Spot ATM Strike   ATM IV 15dP Strike   15dP Δ  15dP IV     Skew
NVDA       217.54      217.5   0.2498       212.5  -0.1541   0.2773  +0.0275
AMZN       266.39      267.5   0.1899       260.0  -0.1039   0.2073  +0.0174
AAPL       319.92      320.0   0.1460       315.0  -0.1383   0.1633  +0.0173
QQQ        716.91      717.0   0.0942       709.0  -0.1502   0.1113  +0.0171
SPY        769.28      769.0   0.0654       764.0  -0.1505   0.0779  +0.0125
META       577.90      577.5   0.2382       565.0  -0.1442   0.2390  +0.0008
MSFT       513.67      512.5   0.1636       505.0  -0.1464   0.1644  +0.0008
TSLA       348.18      347.5   0.2502       340.0  -0.1483   0.2458  -0.0044

Premium-selling (top 3): NVDA, AMZN, AAPL
Directional (rest 5):    QQQ, SPY, META, MSFT, TSLA
```

**Bug found and fixed** (see git log `1a2f9dd`, `b58bbb6`): spot price was
originally `(bid+ask)/2`. Near/after market close, AAPL/TSLA/MSFT's quotes
came back with `ask=0.0`, silently halving the computed spot price — which
then poisoned the ATM strike search window and made those three look like
they had no liquid chain. Root-caused by comparing computed spot against
real daily-bar closes (~47-48% ratio on exactly the affected three tickers).
Fixed by using last trade price as the spot source (simpler than a
bid/ask-with-fallback — these are all liquid names).

## Component 2: Premium-selling execution — ✅ SUPERSEDED, no action needed

This entry's original design (credit spread: sell the 15Δ put, buy a
further-OTM protective put) was superseded 2026-08-30 by the CSP switch (see
"Architecture decisions made" above and `SPEC.md` §6) before it was ever
built — premium-selling execution is live and covered by Component 6
(`execution_agent.py`)'s `sell_to_open`/standing-stop CSP flow instead.
Confirmed 2026-09-02 (explicit human decision): no defined-risk-spread work
is needed here: this component is done via the different path Component 6
already took, not a gap.

## Component 3: Analyst Agent — ✅ DONE, tested live

Files: `.claude/agents/analyst.md`.

Claude Code subagent, tools restricted to
`mcp__alpaca-spike__{get_stock_bars,get_stock_latest_trade,get_news,get_clock}`.
Pulls today's first-10-minute (09:30–09:40 ET) 1-min bars, gap % vs. prior
close, and last-24h news; reasons over it with explicit calibration rules
(clean trend beats spike-and-fade; high volume + flat net move is its own
risk signal, not a weak trend; price action outranks conflicting news;
defaults to UNDECIDED rather than forcing a side). No execution authority —
output only, matches spec §13's "Analyst does not have authority to place
trades."

**Validated live** (dispatched via the `Agent` tool, `subagent_type:
"analyst"`, one per ticker, in parallel) against all 5 directional
candidates from the Component 1 run above, using today's real completed
first-10-minute session:

| Ticker | Direction | Confidence | Key driver |
|---|---|---|---|
| META | BULLISH | 62 | Clean +1.34% climb, closed near window high; Rosenblatt Buy/$886 PT raise |
| MSFT | BULLISH | 62 | Steady +0.68% climb; momentum off Thursday's AI-partnership rally |
| QQQ | UNDECIDED | 30 | Gap-fill; real bullish flow data offset by pending Fed speech |
| SPY | UNDECIDED | 30 | Mild grind; hawkish Fed commentary + weak PMI conflict with tape |
| TSLA | UNDECIDED | 30 | Spike-and-fade; gap-up vs. intraday-fade disagree, no real catalyst |

Applying spec §14 (select up to 3, confidence-gated, skip UNDECIDED) →
**META and MSFT → Buy Call.** Every UNDECIDED call cites a specific
conflicting-signal reason, not a generic hedge.

## Component 4 (partial): Directional Selection — ✅ DONE, tested

Files: `directional_selection.py`. Config additions: `config.py`'s
`MIN_DIRECTIONAL_CONFIDENCE` (55) and `MAX_DIRECTIONAL_SELECTED` (3, spec
§14's fixed cap).

Deterministic (spec §30 — no LLM here on purpose): takes the Analyst's 5
structured reads, drops any `UNDECIDED` regardless of confidence, drops
anything below `MIN_DIRECTIONAL_CONFIDENCE`, sorts the rest by confidence
descending, keeps the top `MAX_DIRECTIONAL_SELECTED`. Every rejection
carries a concrete `reject_reason` (which rule it failed), never a silent
drop.

**Handoff contract established**: the Analyst returns a fixed markdown
template (Ticker/Direction/Confidence/Reason/Catalysts/Risks); the
orchestrating session converts that into a JSON list
(`{ticker, direction, confidence, reason, catalysts, risks}`) before
calling this script — keeps the actual selection 100% code, with JSON
conversion as the only LLM-touched step, and that step is low-risk
transcription against a rigid template, not a judgment call.

**Validated two ways**:
1. **Real data, full 8-ticker universe** (SPY QQQ NVDA TSLA AAPL AMZN MSFT
   META, spec §2) — ranked by IV skew against the Aug 31 chain (nearest
   available, markets closed for the weekend — see the mock fixture's
   README for why), split top-3/bottom-5, all 5 directional candidates
   given real Analyst subagent reads against today's (2026-08-28) actual
   completed first-10-minute session + the prefetched news cache:

   | Ticker | Direction | Confidence | Selected? |
   |---|---|---|---|
   | MSFT | BULLISH | 65 | ✅ |
   | META | BULLISH | 60 | ✅ |
   | QQQ  | BULLISH | 55 | ✅ |
   | SPY  | UNDECIDED | 30 | ❌ (UNDECIDED) |
   | TSLA | UNDECIDED | 30 | ❌ (UNDECIDED) |

   Premium-selling top 3 (same run): NVDA, AAPL, AMZN.
2. **Synthetic edge cases** — a candidate with confidence 95 but
   `UNDECIDED` correctly rejected regardless of score; a 4th candidate
   that clears the confidence bar correctly bumped for exceeding the
   top-3 cap (`"ranked #4 of 4 eligible, exceeds max_selected (3)"`).

**Mock fixture**: this real run is captured to `mock_cache/2026-08-28/`
(news, Analyst reads, ranking, selection result — see its README) so
`directional_selection.py` and future sizing/Risk Manager work can be
tested against it instantly without re-hitting live data or re-running
the (slow, 5-way-parallel) Analyst subagents. `scripts/save_mock_fixture.py`
regenerates the cheap-to-reproduce pieces (ranking + selection) on demand;
the Analyst/news pieces are captured by hand from a real run only (spec
§31 has the general policy).

**Not yet built**: turning a selected candidate into an actual contract
(ATM/slightly-ITM 0DTE call or put, spec §15), sizing within the $5,000
directional cap (§16), and the Risk Manager approve/reject step (§22) —
this component only covers the selection step, not the trade itself.

## Component 5: Risk Manager — ✅ DONE, tested (unit + live)

Files: `risk_manager.py`, `tests/test_risk_manager.py`. Config additions:
`PREMIUM_SELL_ALLOCATION_PCT` (0.95), `DIRECTIONAL_ALLOCATION_PCT` (0.05),
`MAX_POSITIONS` (6), `MAX_EXPOSURE_PER_UNDERLYING_PCT` (0.35) — the latter
two not spec-fixed, same "documented default, revisit with real trade
history" status as `MIN_DIRECTIONAL_CONFIDENCE`.

Covers both strategy sides (spec §22), built TDD (17 unit tests,
`tests/test_risk_manager.py`, stdlib `unittest` — no new dependency):

- `get_account_snapshot()` / `compute_budgets()`: one live `TradingClient.
  get_account()` call per batch, budgets = 95%/5% of `options_buying_power`
  (falls back to `cash`) — dynamic replacement for the spec's hardcoded
  $100k account. See the "Architecture decisions made" section above for
  why `options_buying_power` specifically, the CSP-over-spread switch, and
  why max daily loss was dropped.
- `allocate_premium_positions()`: CSP sizing (`strike × 100` per contract)
  across a shared budget pool, two passes — equal target share per
  candidate first, then pools whatever's left (unused shares +
  couldn't-afford-even-one shares) and retries anyone who missed their own
  share against that pool. REJECTs (never forces, per spec §8) a candidate
  the pool still can't cover, naming its own cost in the reason.
  `max_exposure_per_underlying` caps any single name's share of the pool.
  Also derives TP (50%)/SL (3×) price levels from the put's credit price
  (spec §9).
- `size_directional_positions()`: equal split of the (much smaller)
  directional budget across whatever was selected (spec §16, literal —
  no leftover-pooling needed here, an ATM 0DTE premium is a small fraction
  of the underlying's price so the "too expensive for its own share" case
  isn't realistic on this side). No TP/SL — time-based exit only (§18).
- `apply_max_positions()`: trims approved decisions to `MAX_POSITIONS`,
  keeping priority order.
- `strategy_engine.RankedCandidate` extended (not re-fetched) with
  `atm_call_symbol`/`atm_call_ask`, `atm_put_symbol`/`atm_put_ask`,
  `put_15d_bid` — Risk Manager's sizing inputs piggyback on the chain
  fetch `strategy_engine` already does, rather than a second network call.

**Validated live** (`venv/bin/python3 risk_manager.py 2026-09-02`, full
8-ticker universe, real account, real chain — 2026-08-31 itself had null
greeks pre-market when checked, a data-availability quirk unrelated to
this code, so a few-days-out expiration was used to exercise the mechanics
for real instead): premium-selling top 3 came back NVDA/QQQ/AAPL, and QQQ
(strike 704 → $70,400/contract) was correctly REJECTed against the
$31,667 equal share plus $42,750 pooled leftover from NVDA/AAPL — still
short, so REJECTed with the actual numbers named rather than forced.
Directional side (MSFT/META/QQQ, from the real `mock_cache/2026-08-28`
selection) all sized and approved correctly. `options_buying_power` came
back $100,000 = `cash` on this paper account, confirming the earlier
finding live.

**Not yet built**: turning an APPROVE into an actual order (Execution
Agent, next).

## Component 6: Execution Agent — ✅ LIVE-VALIDATED, 2026-08-31 (see full trading-day entry below)

Files: `execution_agent.py`, `tests/test_execution_agent.py` (11 unit
tests, TDD, same stdlib `unittest` convention as Risk Manager).

Built off the sibling `alpacabot` project's proven live patterns
(`orders.py`/`trade_manager.py`/`bot.py`) rather than reinvented from
scratch — checked at the user's prompt specifically because it's already
placed real option trades. What was carried over deliberately: plain
MARKET orders for entries and force-closes (its marketable-limit helpers
exist but its live bot never actually calls them); take-profit as a
poll-based check + market-close, never a standing limit order; and — the
one that corrected an earlier mistake in this project (see Component 5's
entry above and `SPEC.md` §6) — a standalone `type: "stop"` order
(`order_class: "simple"`) really does work as a genuine broker-side
stop-loss on a single option leg, confirmed from `alpacabot`'s own fill
logs (`🛑 STOP FILLED @ 4.14`, `🛑 STOP FILLED @ 3.12`, both real SPY
option positions).

- **Premium-selling (CSP)**: `sell_to_open` entry → standing stop
  (`buy_to_close` at 3× the *actual fill price*, not Risk Manager's
  pre-trade credit estimate) submitted immediately after fill → poll loop
  checks the stop's status (SL), the bid against 50% of the real fill
  (TP), and `PREMIUM_EOD_CLOSE_TIME` (EOD) every `MONITOR_POLL_SECS`
  (60s, spec §19-20's "~every 1 minute"). `tick_premium_position()`
  handles the stop/poll-loop race the same way `alpacabot`'s
  `_force_close` does: if a market-close is racing a stop that already
  filled underneath it, recover the stop's real fill price instead of
  treating it as a second close.
- **Directional**: `buy_to_open` entry → no TP/SL at all → pure
  `DIRECTIONAL_CLOSE_TIME` (2:30 PM ET, spec §18) time-exit. Much
  simpler than `alpacabot`'s side (no avg-ups/scaling — not in our spec).
- **Position recycling (spec §10) is NOT implemented**, per the
  2026-08-30 MVP-scope decision: a position that closes mid-day (only
  possible on the premium-selling side — directional never closes early)
  is terminal. Logged, and that capital sits idle for the rest of the
  day; no re-evaluation, no new position opened in its place.
- Logging (spec §27): append-only `logs/<date>-execution.log`, one line
  per entry/exit.
- New config: `PREMIUM_EOD_CLOSE_TIME` (15:45 ET, 15-min buffer, same
  convention as `alpacabot`'s `EOD_EXIT_TIME`), `DIRECTIONAL_CLOSE_TIME`
  (14:30 ET, spec-fixed not tunable), `MONITOR_POLL_SECS` (60), `ET`
  (stdlib `zoneinfo`, not `pytz` like `alpacabot` — no new dependency).

**Tested**: the pure decision logic only — `check_premium_exit`/
`check_directional_exit` (TP/SL/EOD/time-exit triggers, priority when
multiple could apply, missing-quote handling) and `build_premium_position`/
`build_directional_position` (TP/SL correctly derived from the real fill,
not the pre-trade estimate). The order-submission wrappers and the
entry/tick/main-loop I/O are un-mocked thin SDK calls, same split as
Risk Manager's `get_account_snapshot()` — not unit-tested, meant to be
validated live.

**Live-validated 2026-08-31** — see the full "First live trading day"
entry below for the complete run (real fills, a real stop order, two real
take-profit exits, a real time-based exit, and a clean process exit).

**Update, 2026-08-31 (before market open)**: user explicitly decided to
skip the isolated 1-contract validation run and wire straight into the
full automated pipeline for today's real morning run instead — paper
account only, user monitoring manually, so a failed live-validate-first
step buys little extra safety here. Wired in as follows:

- **`scripts/run_morning_trigger.sh`** now has a second stage, plain bash,
  after `claude -p` exits: reads `logs/cache/risk-decisions-<date>.json`
  and — mechanically, no LLM judgment — launches `execution_agent.py`
  detached (`nohup ... &`, PID written to
  `logs/execution-agent-<date>.pid`) only if at least one decision has
  `approved: true`. This is the actual "loosen `--disallowedTools`
  deliberately" moment predicted in `NEXTSTEPS.md` — except the loosening
  turned out to be moving execution out of the LLM's tool-calling loop
  entirely rather than adding order-placing MCP tools to its allowlist,
  which keeps spec §30's LLM/deterministic split intact:
  `run_morning_trigger.sh`'s own header comment explains the reasoning.
  `set -e` means a non-zero exit from `claude -p` aborts before this stage
  ever runs — fails closed, never executes off a stale/partial decisions
  file.
- **`prompts/morning_decision.md`** updated to state explicitly that the
  LLM session writes the decisions file and stops — it never reads it back
  to decide whether to execute, and never invokes `execution_agent.py`
  itself. The old "NO ORDERS PLACED" closing line is gone (no longer true
  when a real order gets placed downstream); replaced with a pointer to
  where the actual outcome shows up.
- **Whole-day `caffeinate` gap closed**: the shared morning wake-bridge
  (`com.alpacabot.morning-caffeinate`, see the scheduling entry below)
  only covers a 30-min window (6:30–7:00 AM PT), nowhere near
  `PREMIUM_EOD_CLOSE_TIME` (3:45 PM ET = 12:45 PM PT). Fixed the same way
  alpacabot's `run_bot.sh` does it: `run_morning_trigger.sh` ties a
  `caffeinate -dimsu -w $EXEC_PID` to `execution_agent.py`'s own PID right
  after launching it — holds the Mac awake for exactly as long as it's
  actually monitoring positions, releases itself automatically on exit.
- **Heartbeat logging added** to `execution_agent.py`'s `run()` loop (3
  `print()` calls: on open, each poll tick, on all-closed) — stdout,
  captured into `logs/<date>-execution-run.out` by the trigger script's
  redirect. Pure addition, no logic changed; not covered by the unit
  tests (which test the pure decision functions, not `run()`'s I/O) —
  reran all 28 Execution Agent + Risk Manager tests after adding it,
  still green. Added because the user's plan is to monitor today's run
  manually and the loop previously had no visible sign of life between
  entry/exit log lines.
- **EOD force-liquidation safety net built** (`NEXTSTEPS.md` item 5,
  previously undone): `scripts/eod_force_liquidate.sh`, deliberately
  separate from `execution_agent.py` and using the **Alpaca CLI** (not the
  SDK/MCP) per the spec's "use all three surfaces" intent and so it shares
  no code path with whatever else might be hung or broken. Lists open
  positions (`alpaca position list --csv`); no-ops if empty; otherwise logs
  a warning with the actual position list and force-liquidates via
  `alpaca position close-all --cancel-orders` (cancels standing orders —
  e.g. a stop — before liquidating, one call). Ran it for real against the
  live paper account this session (zero open positions right now, so it
  hit the no-op path) — confirmed the CLI auth/connectivity work end to
  end, not just that the script parses. Stated limitation directly in its
  own header: this only helps if the Mac is actually awake at fire time:
  it's a net for "the loop is stuck/wrong", not for "the whole machine
  went to sleep".
- **Scheduling**: added to the same `crontab` as yesterday's two entries —
  `scripts/eod_force_liquidate.sh` at 12:55 PM PT (3:55 PM ET, 10 min
  after `PREMIUM_EOD_CLOSE_TIME`), Mon–Fri. Verified via `crontab -l`.

## HITL Review Gate — design decision only, 2026-08-30

Not a "component" in the build sense yet — no code exists for either side
of it (the Strategist Agent isn't built, so there's nothing to gate).
Recorded here because it changes the shape of spec §26-27's evolution
pipeline before that work starts, not after.

**What it is**: a human approve/reject checkpoint between the Strategist
Agent's proposed strategy change and any backtest work (spec §26). Reject
→ discard + log the reason, nothing else happens. Approve → the proposal
enters the existing Backtest → Compare against V1 → Paper Trading → Risk
Approval → Production pipeline (§27), unchanged.

**Not yet decided**: the actual review surface (a CLI prompt? a file the
reviewer edits? a Claude Code session?) and the handoff contract between
the Strategist's output and whatever the reviewer sees — deferred to
whenever the Strategist Agent itself gets built (see NEXTSTEPS.md).

## First live trading day — 2026-08-31 (paper account)

The system's first full run against real live-market data, start to
finish, with real fills — not a validation test, an actual trading
decision the user reviewed and approved. Run **manually**, not through the
automated cron trigger: the 6:30/6:41 AM cron jobs fired but hit the 0DTE
greeks gap (see below) and produced zero candidates before the fix
existed; once `black_scholes.py` was built and integrated mid-morning,
the user explicitly chose to skip the planned isolated 1-contract
validation step and run the full pipeline by hand instead (paper account,
manually monitored) rather than wait for the next scheduled cron fire.

**Pipeline, run by hand, each step's output shown to the user before
proceeding**:
1. `strategy_engine.py` — real 8-ticker ranking, zero skips (the local
   Black-Scholes fallback's first real production use).
2. 5 `analyst` subagents dispatched in parallel for the directional
   candidates (SPY, META, MSFT, NVDA, AAPL) — real reads against real
   first-10-minute price action and real news. Only SPY cleared the bar
   (BEARISH, confidence 58 — Chicago PMI miss, US-Iran tension headlines,
   hawkish Fed commentary); the other 4 came back UNDECIDED, each with a
   real, specific two-sided-price-action reason, not a generic hedge.
3. `directional_selection.py` — selected SPY, deterministically.
4. `risk_manager.py` — run **twice**, a few minutes apart, against live
   data: the top-3 IV-skew names shifted both times (TSLA/META/QQQ →
   TSLA/META/SPY → TSLA/QQQ/AMZN → TSLA/META/AAPL across four consecutive
   fetches within ~10 minutes) — real, fast-moving 0DTE IV, not a bug
   (matches the earlier research: delta/gamma get genuinely whippy
   intraday). The concentration-cap removal (see "Architecture decisions
   made" above) was discovered and fixed *during* this step, live,
   because the capped version rejected every premium-selling candidate on
   a real run.
5. **Final decisions, shown to the user, explicit go-ahead given**:
   - SELL PUT (CSP) `TSLA260831P00357500` x1 — $35,750
   - SELL PUT (CSP) `AAPL260831P00312500` x1 — $31,250
   - BUY PUT (directional) `SPY260831P00765000` x44 — $4,972
6. `execution_agent.py` launched detached (`nohup` + PID-tied `caffeinate`,
   same pattern as `run_morning_trigger.sh`'s automated path) — all 3
   orders placed and filled for real, both CSP stop orders confirmed
   standing at Alpaca (TSLA stop $1.50 = 3× the $0.50 real fill, AAPL
   stop $0.81 = 3× the $0.27 real fill — both computed from the actual
   fill, not the pre-trade estimate, confirming that design point live for
   the first time).

**Full outcome, all 3 positions closed by end of day**:

| Symbol | Side | Entry | Exit | Exit reason | Result |
|---|---|---|---|---|---|
| TSLA260831P00357500 | SHORT (CSP) | $0.50 | $0.28 | TP (11:44 ET) | **+$22** |
| AAPL260831P00312500 | SHORT (CSP) | $0.27 | $0.16 | TP (10:51 ET) | **+$11** |
| SPY260831P00765000 (x44) | LONG (directional) | $1.04 | $0.14 | TIME (14:30:58 ET, exact) | **-$3,960** |

**Net: -$3,927.** Both premium-selling CSPs hit take-profit as designed —
a real, if small, positive signal for that side's mechanics. The
directional SPY put lost most of its value: the BEARISH thesis (macro
risk-off catalysts, gap-down continuing into the window) didn't hold up
through the session, and since directional positions have no stop-loss by
design (spec §18 — max loss is already capped at the premium paid), it
rode the full adverse move to the 2:30 PM time-exit with no early out.
Nothing here indicates a code problem — this is a normal single-day
outcome for a system that just had its first live trades, not evidence
either strategy side is miscalibrated on one sample.

**Everything downstream also confirmed live, for the first time**:
- The `execution_agent.py` process exited cleanly on its own once both
  positions were closed (`0 premium + 0 directional... All positions
  closed. Exiting.`) — no lingering process, no manual cleanup needed.
- `eod_force_liquidate.sh` fired at 12:55 PM PT, found zero open
  positions, logged a clean no-op — the full safety-net chain worked
  end-to-end on a day it wasn't actually needed.
- The backup cron entries (6:35/6:50 AM) correctly no-op'd via their lock
  files once the primaries had already run — first real confirmation the
  cron-race fix's backup mechanism doesn't cause a double-run on a day the
  primary succeeds.

**Known rough edges surfaced, not yet fixed**: the premium-side vs.
directional-side rankings come from two independently-timed re-fetches
(the Analyst-driven directional selection happens minutes before Risk
Manager's own internal re-ranking for premium sizing), so the same ticker
can appear as a candidate on both sides across a few minutes of live 0DTE
drift — didn't cause a double-order today only because the premium side
happened to reject that ticker anyway. Worth hardening (a single ranking
snapshot shared by both sides) before this runs unattended without a
human reviewing each step.

## Component 7: Strategist Agent + HITL review — ✅ DONE, validated live, first review cycle complete, 2026-08-31

Files: `.claude/agents/strategist.md`, `STRATEGY_CHANGELOG.md`.

**Design pivots during brainstorming** (spec §24-27 as originally written
assumes a numeric-parameter-tuning Strategist and a formal
review-surface + Backtest → Compare → Paper → Risk Approval → Production
pipeline; both were deliberately simplified for V1 with only one day of
data):
- **Scope for n=1 trading day**: post-mortem + qualitative process-fix
  proposals only. Explicitly forbidden from proposing numeric parameter
  tuning (e.g. "15Δ → 10Δ") from a single day's sample — no statistical
  basis for that yet.
- **HITL interface**: considered a published Artifact with comments, then a
  local HTML review page + tiny stdlib server (Claude-agnostic, diff
  auto-applied via a headless `claude -p` "Implementor" agent on approval)
  — both dropped in favor of plain interactive review in a Claude Code
  session. Simpler, and matches how the rest of this project already gets
  built; no new always-on process, no separate Implementor agent needed
  since the interactive session just implements approved changes directly.
- **Post-mortem structure**: leads with "what went well" / "what could be
  better" (both evidence-cited, no generic claims), then an
  inputs-completeness audit (did the day's Analyst/Risk Manager actually
  use everything spec §5/§12 call for?), then a **live** end-of-day
  re-check — real `get_news` + hourly `get_stock_bars` for traded tickers
  only, comparing against what was known at the 9:40 ET decision point,
  specifically hunting for anything that emerged later that would explain
  a losing trade. Proposals come last, after all of that.

**Validated live, twice**, dispatched via the Agent tool
(`subagent_type: "strategist"`) against real 2026-08-31 trade data (the
first live trading day's 3 closed trades, net -$3,927). Second run added
hourly bars to the EOD check per request. Both runs correctly joined
`logs/<date>-execution.log` + `logs/cache/risk-decisions-<date>.json` +
`logs/cache/analyst-candidates-<date>.json` + `logs/cache/selection-
result-<date>.json`, and the second run's live EOD check correctly
explained the losing SPY trade (bearish thesis stalled almost exactly as
the Analyst's own risk list warned, later news was a wash — Hormuz
headlines vs. Goldman's Solomon calling the consumer "resilient").

**First real review cycle, same day** — all 5 Strategist proposals +
1 human-suggested change reviewed interactively in chat, approved, then
implemented via 3 parallel `general-purpose` agents on disjoint files
(no worktree isolation needed — zero file overlap by design), each
following TDD, none committing (human reviewed diffs and committed
after):

- **Proposal 1** (persist the IV-skew ranking snapshot): `strategy_engine.
  py`'s `--json` mode now also persists to `logs/cache/ranking-
  <expiration>.json`, gains a `run_id`.
- **Proposal 5** (log greeks-feed source): `RankedCandidate`/`SkippedTicker`
  gain `greeks_source` (`alpaca` | `black_scholes_fallback` |
  `unavailable`), logged per ticker to stderr.
- **Proposal 3** (partition inconsistency) — **traced to a real, confirmed
  bug**, not just a process gap: `build_directional_candidates()` resolved
  selected tickers against the FULL ranked lookup (undiscriminated between
  premium/directional sides), so a stale `selection_result.json` could
  silently pull in a ticker that had since shifted to the premium side of
  a later ranking run — exactly what happened live 2026-08-31 (META and
  AAPL ended up in both `premium_decisions` and `directional_decisions`).
  Fixed: now scoped to the directional-side lookup only; a mismatch is an
  explicit REJECT naming the mismatch, never a silent leak.
- **Proposal 4** (AAPL TP-price "anomaly") — **investigated and confirmed
  NOT a bug**: `build_premium_position` already derives TP from the real
  fill (0.27 × 0.5 = 0.135), not Risk Manager's pre-trade estimate (0.09,
  a different number, in `risk-decisions.json`) — a distinction this
  project corrected once before (Component 6). The logged exit (0.16) vs.
  the real threshold (0.135) is ordinary bid/ask slippage on the
  market-order close. The actual gap: nothing logged the threshold really
  armed, so an audit (including the Strategist's own first pass) ends up
  comparing against the wrong number. Fixed the legibility gap: premium
  `ENTRY` log lines now include the real `tp=`/`sl=` from the actual fill.
- **Proposal 2** (run provenance) — split across two files: `risk_manager.
  py`'s `RiskManagerResult` gains `generated_at`; `execution_agent.py`
  logs which decisions file (path + `generated_at`) it loaded at startup.
  Closes a real gap: today it took manual cross-referencing of exact
  contract symbols/quantities across 3 different same-day decision-file
  versions to determine which one was actually traded.
- **Human suggestion** (not from the Strategist Agent): directional
  capital allocation changed from a flat 5% of balance to **1% per
  selected directional candidate, capped at 3% total** — a single-name day
  (like 2026-08-31's actual SPY-only pick) was risking the same dollar
  amount as a full 3-name day. `config.DIRECTIONAL_ALLOCATION_PCT` →
  `DIRECTIONAL_PCT_PER_STOCK` (0.01) + `DIRECTIONAL_MAX_PCT` (0.03);
  `compute_budgets()` now takes `num_directional_selected`. On a $100k
  account, today's actual case (1 selected) would have sized at $1,000
  instead of $5,000.

4 commits (`5af31c2`, `a1a3f99`, `b8d6633`, `fe02c1e`), 65/65 tests passing
across the full suite after all four. Full before/after table and AI-vs-
human attribution recorded in `STRATEGY_CHANGELOG.md`, which
`strategist.md` now reads as an input on every run so it doesn't
re-propose something already decided.

**Same-day addendum**: user asked whether the directional 1%/2%/3%
formula scales with the account's real (live) balance — confirmed yes,
`compute_budgets()` already used `AccountSnapshot.available_balance`
(live `get_account()`, not a hardcoded $100k) before this cycle even
started. Follow-up ask: should premium's 95% also flex with how many
directional stocks were selected, rather than sitting fixed while
directional varies? Yes — `config.PREMIUM_SELL_ALLOCATION_PCT` (fixed
95%) removed; `compute_budgets()` now computes premium as the complement
of the directional percentage (`1.0 - directional_pct`), so the two sides
always sum to the full available balance instead of leaving 2-4% idle.
0 directional selected -> 100% premium, 1 -> 99%, 2 -> 98%, 3 -> 97%.
TDD, 9 tests in `ComputeBudgetsTests` including an explicit sum-to-100%
invariant check. 66/66 tests pass across the full suite.

**Not built in this cycle**: spec §26-27's formal Backtest → Compare
against V1 → Paper Trading → Risk Approval → Production pipeline — an
approved change here goes straight into what cron runs tomorrow, no
separate backtest/paper-trading stage. Accepted trade-off for V1 (paper
account only, human reviews every proposal interactively before
anything's implemented) — revisit if this ever needs to run with less
human oversight per cycle.

**Extended 2026-09-02 — dedicated profitability analysis + WebSearch**:
prior cycles focused almost entirely on process correctness ("were the
decisions made right"), not profitability ("did they make money, and
what would"). `.claude/agents/strategist.md` gains:
- A new §6 "Profitability Analysis" section (proposals renumbered to §7)
  — grounds itself in real per-trade P&L plus the cross-day trend now
  read from `dashboard/data.json`'s precomputed `strategy_stats`/
  `daily_summaries` (no need to re-derive cross-day aggregates from raw
  logs), looks for concrete loss/gain drivers (side, exit reason, ticker),
  and produces 0-N profitability suggestions tagged `reduce-losses` /
  `improve-profits` / `both` — same confidence discipline as regular
  proposals (no numeric tuning claimed as reliable from a small sample).
- **`WebSearch` added to its tool list** — can check a pattern it finds
  against external research (holding-period effects, decay mechanics,
  directional-vs-premium performance, etc.), with explicit guidance to
  weigh an academic/data-backed source over an unsourced blog post and
  cite every source's URL it actually uses. This mirrors what the human
  did manually for the 2026-09-02 allocation/exit-timing changes (see
  `STRATEGY_CHANGELOG.md`'s entry) — now built into the agent itself
  going forward instead of being a one-off human research pass.
- Not yet live-validated — same restart caveat as the 2026-09-01
  `analyst.md` edit (subagent definition changes need a Claude Code
  restart to actually load in a running session; see `PROGRESS.md`'s
  gotchas section and `NEXTSTEPS.md`).

## Dashboard — ✅ REBUILT with real data, branded "The IV League," 2026-08-31

Files: `dashboard/index.html`, `dashboard/data.json`, `dashboard/README.md`,
`dashboard/assets/` (banner/crest), `export_dashboard_data.py`,
`tests/test_export_dashboard_data.py`. Spec §29. Design doc:
`docs/superpowers/specs/2026-08-31-hackathon-dashboard-design.md`
(brainstormed and approved same day as the first live trading day).

Rebuilt from the sample-data scaffold into a single hackathon-facing page,
branded **The IV League** (banner/crest assets supplied by the user,
navy/gold/green/red palette taken directly from them). Four tabs, client-side
routed with URL-hash deep-links (`#overview`, `#journal`, `#strategy`,
`#architecture`) so any section can be linked to directly:

- **Overview**: stat tiles (balance, total realized P&L, overall win rate,
  trades today) + two SVG bar charts (daily P&L, win-rate by strategy),
  built by hand (no chart library) following the dataviz skill's mark specs
  (4px rounded data-ends, hairline baseline, native `<title>` hover
  tooltips, direct value labels). Colors: categorical blue/orange
  (`#2a78d6`/`#d95926`) for strategy identity, status green/red
  (`#0ca30c`/`#d03b3b`) for P&L polarity — the skill's validated defaults,
  re-validated (`validate_palette.js`) against this page's navy surface
  specifically since the palette was retargeted from the skill's own
  light/dark surfaces to the brand's navy.
- **Trade Journal**: the original scaffold's table, extended with
  date/strategy filter dropdowns and a Date column (was single-day-only
  before).
- **Strategy Evolution**: fetches and renders `../STRATEGY_CHANGELOG.md`
  client-side via `marked` (CDN) — no separate content to maintain, the
  changelog file already written for the Strategist Agent's own input.
- **Architecture**: a condensed prose summary of spec §30's pipeline plus
  "Open full diagram ↗" links out to `diagrams/agent-flow.html` and
  `diagrams/agent-roster.html` (not embedded/iframed — those pages keep
  their own full-page polish).

**Real data, not sample, for the first time**: `export_dashboard_data.py`
(TDD, 11 unit tests against the real `logs/2026-08-31-execution.log` as
fixture — its known real outcome, net -$3,927, is the correctness check,
not a synthetic shape check) parses every `logs/<date>-execution.log`,
reconstructs trades by joining `ENTRY`/`EXIT` lines by option symbol
(strategy/side read straight off the entry — `SELL_TO_OPEN` is always a
short put in V1, `BUY_TO_OPEN`'s call-vs-put comes from the OCC symbol's
own C/P flag — no cross-reference against `risk-decisions-*.json`
needed), and precomputes `daily_summaries[]`/`strategy_stats[]` alongside
the raw `trades[]`. Calls `risk_manager.get_account_snapshot()` for the
live balance. Run manually for now (`venv/bin/python3
export_dashboard_data.py`); a GitHub Actions workflow to run it on a
schedule is a separate, later task (see `NEXTSTEPS.md`) — this design only
produces the script such a workflow would call.

**Placement decision**: `export_dashboard_data.py` lives at repo root, not
`scripts/` as the design doc named it — every other unit-tested module
(`risk_manager.py`, `strategy_engine.py`, `execution_agent.py`,
`black_scholes.py`) lives at root so tests `import <module>` with no
sys.path hack; `scripts/` is shell orchestration plus one untested CLI
utility. Matched that convention since this module's parsing/aggregation
logic needed the same test treatment.

**GitHub Pages source corrected to repo root, not `/dashboard`**: the
Architecture tab links to `/diagrams/*.html` and Strategy Evolution fetches
`/STRATEGY_CHANGELOG.md` — both outside `dashboard/`. Serving Pages from
`/dashboard` (as originally suggested in `NEXTSTEPS.md`) would 404 both,
since Pages never serves files outside its chosen publish folder. Caught
during design brainstorming, before anything was built against the wrong
assumption. Local dev now matches production: serve the whole repo root
(`python3 -m http.server` from repo root, not from inside `dashboard/`),
open `/dashboard/`.

**Verified live in Chrome** (`claude-in-chrome`, served from repo root):
all four tabs render against the real `data.json`; hash deep-links work;
both charts render correct real values with working hover tooltips; the
Strategy Evolution tab correctly renders the real `STRATEGY_CHANGELOG.md`
including its tables; both Architecture diagram links open the real
`diagrams/*.html` pages in a new tab; zero console errors. Caught and
fixed two real bugs this way, not just by reading the code: (1) the daily
P&L chart's negative-value label overlapped its axis date label when the
bar filled the full plot height — fixed by widening `padBottom` and the
label offset; (2) the Strategy Evolution tab's wide changelog tables
initially forced the *whole page* to scroll horizontally instead of
scrolling within their own panel — fixed with `overflow-x:auto` on
`#changelogBody` plus `overflow-x:hidden` on `body` as a backstop.

**Not yet decided / deferred** (see `dashboard/README.md`'s matching
section): the GitHub Actions automation itself, unrealized P&L for OPEN
trades, and actually turning GitHub Pages on (Settings → Pages → serve
from `/` root — a 2-minute task, still not done).

## Also built this session: morning-trigger scaffolding (not yet installed)

Not a formal spec component, but real, working infra for Component 6's
eventual orchestrator:
- `prompts/morning_decision.md` — the headless decision-only prompt.
  **Extended 2026-08-30** once Risk Manager existed: ranks (`strategy_engine`),
  dispatches the Analyst, transcribes its reads to JSON, runs
  `directional_selection.py` as a real subprocess (not re-derived by the
  LLM — was previously described as "apply spec §14 yourself," fixed to
  actually shell out to the deterministic script), then runs
  `risk_manager.py --json` as a subprocess and logs its real APPROVE/REJECT
  batch. Still explicitly never places an order — every Alpaca
  order/cancel/mutate MCP tool is hard-blocked via `--disallowedTools`, not
  just prompt instruction; Risk Manager's output is logged as this run's
  final result, not acted on further, since Component 6 (Execution Agent)
  doesn't exist yet. `risk_manager.py`'s own CLI was tightened alongside
  this: the selection-result JSON path is now a required argument (no
  silent fallback to the `mock_cache` fixture — that was fine for manual
  testing, dangerously wrong for an automated run to default to), and it
  gained a `--json` mode so the prompt can capture its output reliably
  instead of parsing the human-readable table. Verified end-to-end this
  session with real data: `directional_selection.py` fed the mock
  fixture's real Analyst JSON, piped into `risk_manager.py --json`, same
  correct output as the direct validation run above.
- `scripts/run_morning_trigger.sh` — wrapper invoking `claude -p` with that
  prompt, absolute `claude` binary path (launchd's PATH is minimal).
- `prefetch_news.py` + `scripts/run_news_prefetch.sh` — deterministic
  (no LLM) news prefetch for the full 8-ticker universe in one API call,
  meant to fire at 9:30 ET so it's warm before the 9:41 decision run;
  `.claude/agents/analyst.md` updated to read this cache first and only
  fall back to a live `get_news` call if its ticker's bucket is empty.
- **Scheduling installed, 2026-08-30**: re-checked alpacabot's actual live
  setup (not just the earlier researched summary) before installing
  anything — it turns out actual bot start/stop is fired by plain
  `crontab`, not launchd; launchd's only job is the wake-bridge
  (`~/Library/LaunchAgents/com.alpacabot.morning-caffeinate.plist`, fires
  `caffeinate -dimsu -t 1800` at 6:30 AM PT Mon–Fri via a `pmset repeat
  wake at 6:30AM` weekday schedule, confirmed live via `pmset -g sched`
  and `launchctl list`). That 6:30–7:00 AM PT window already covers both
  of ivleague's trigger times (9:30/9:41 AM ET = 6:30/6:41 AM PT, fixed
  3h offset through November since both zones DST-shift together), so
  nothing new went into pmset/launchd — just two additive `crontab` lines
  (`scripts/run_news_prefetch.sh` @ 6:30, `scripts/run_morning_trigger.sh`
  @ 6:41), appended alongside the existing schwabbot entries, no `sudo`,
  shared wake infra untouched. Verified via `crontab -l` after install.
  Both scripts' header comments corrected from "fired by launchd" (stale)
  to "fired by cron". The "does `-dimsu` survive a closed lid" open
  question is now indirectly de-risked — alpacabot's own `logs/cron.log`
  shows its 6:55 AM cron job firing every weekday since mid-July with zero
  misses, which requires the wake mechanism to have worked — but whether
  the lid was actually closed those mornings wasn't checked, so this is
  suggestive, not confirmed. Genuinely verify once ivleague's own
  `logs/cron.log` has a few real days in it.

## Infra notes / gotchas (read before continuing)

- **Alpaca CLI**: install via `go install github.com/alpacahq/cli/cmd/alpaca@latest`
  — `brew install alpacahq/tap/cli` fails on this machine (system git 2.17.1
  too old for a flag brew's git wrapper uses; unrelated to Alpaca). Binary
  lands at `$(go env GOPATH)/bin/alpaca`. Auth via env vars:
  `ALPACA_API_KEY` + `ALPACA_SECRET_KEY` (note: `_SECRET_KEY`, not
  `_API_SECRET` like our `.env`/`config.py` convention).
- **MCP server registration is fragile across restarts**: local scope
  (`claude mcp add` default) silently didn't survive a session restart once
  (empty `mcpServers` in `~/.claude.json` afterward, cause not fully
  understood). **Project scope** (`.mcp.json` in this repo) is more durable
  — but see below, it's no longer committed.
- **`.mcp.json` uses literal credentials, not `${VAR}` references**: tried
  `${ALPACA_API_KEY}`/`${ALPACA_SECRET_KEY}` substitution first (cleaner,
  keeps secrets out of git) but it kept resolving to empty even with
  confirmed-correct exports in the launching shell (verified 3 separate
  ways) — every real MCP data call 401'd. Root cause not fully nailed down
  (looks like Claude Code resolving `.mcp.json` env-refs against a different
  process environment than the interactive shell), but reverting to literal
  values immediately fixed it. Worth a clean investigation if reproducible,
  since env-var refs are the right long-term answer for a real secret.
- **Corrected, 2026-08-31, before making this repo public**: the literal
  credentials above were actually committed (2 commits, `8492449`/`90070d5`)
  — a real exposure once the repo went public, even at paper-trading-only
  stakes. Scrubbed from **all** git history with `git filter-branch`
  (`git-filter-repo` wasn't installable in this environment; the repo was
  small enough — 30 commits — that `filter-branch --index-filter` plus
  `reflog expire` + `gc --prune=now` was verified clean by scanning every
  blob object in the rewritten history for the leaked key string before
  pushing anywhere). `.mcp.json` is now gitignored (real literal
  credentials, local-only); `.mcp.json.example` is the committed template
  with placeholder values — copy it to `.mcp.json` and fill in real values
  on a fresh clone.
- **Any new/changed MCP server registration needs a Claude Code restart** to
  load its tools into a session — registering mid-session never works, even
  if the server itself connects fine (confirmed via raw JSON-RPC probes
  bypassing Claude Code entirely).
- **Subagent (`.claude/agents/*.md`) definition changes also need a
  restart, same as MCP registrations — confirmed live, 2026-09-01**: edited
  `analyst.md` mid-session (added a `Write` tool + a new persistence
  instruction), then dispatched a live validation run. The subagent
  reported its actual configured instructions did **not** include the new
  Write tool or persistence step — and correctly refused to treat the task
  prompt's description of "your newly-added step" as authoritative when
  its own instructions disagreed (the right defensive instinct, just
  triggered by a stale cache here rather than an injection attempt).
  Confirmed no shadowing file exists (only one `analyst.md` in the repo,
  edit correctly saved) — this session's subagent-definition cache
  predates the edit. Re-validate after a restart before trusting any
  subagent-prompt change made mid-session.
- **Volume from the MCP/SDK bars endpoints is IEX-feed only**, not
  consolidated SIP tape — the Analyst subagent has been told to flag this
  when reasoning about whether volume looks elevated, since it may
  understate true participation.
- **No OPRA entitlement on this paper account** (`feed="opra"` →
  `403: "OPRA agreement is not signed"`) — greeks/IV come from Alpaca's free
  "indicative" feed. This was initially misdiagnosed as an AAPL/TSLA/MSFT
  data gap (see the spot-price bug above) — turned out to be unrelated once
  that bug was fixed; the indicative feed is fine for all 8 tickers.
