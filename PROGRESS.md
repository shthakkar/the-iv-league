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
- **MVP universe**: `config.py`'s `UNIVERSE` defaults to `[SPY, QQQ, NVDA,
  TSLA]` (4, not the spec's 8) for faster iteration, but every ticker in the
  spec's 8 has been tested via `UNIVERSE_OVERRIDE` and works.
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
  the strike instead of structurally floored by a protective put —
  material because Alpaca has **no broker-side stop-loss for options**
  (checked their OrderClass API spec: `bracket`/`oco`/`oto` order classes
  are equity-only; options only support `simple`/`mleg`), so the §9 3×
  stop-loss is enforced entirely by the Execution Agent's own polling
  loop with nothing backstopping a gap or a lagging loop. `SPREAD_WIDTH`
  config constant and the protective-put fetch step are dropped from the
  Risk Manager design as a result.
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

## Component 2: Premium-selling execution — ⛔ NOT STARTED

Deliberately skipped for now: market was closed, so live order
placement/fills can't be meaningfully tested. Design (agreed, not yet
built): take the top-`PREMIUM_SELL_COUNT` tickers from the Strategy Engine,
build a credit spread (sell the 15Δ put already found, buy a further-OTM put
for protection, width configurable), place via the MCP server's multi-leg
option order tool (`place_option_order` — confirmed to support multi-leg
during the building-block spike), apply spec §9 exit rules (50% TP / 3× SL /
EOD close).

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

## Dashboard — 🟡 SCAFFOLDED, sample data only, 2026-08-30

Files: `dashboard/index.html`, `dashboard/data.json`, `dashboard/README.md`.
Spec §29. The page itself is done and smoke-tested (served locally via
`python3 -m http.server`, verified it fetches `data.json` and renders the
stat tiles + trade journal correctly, including OPEN-vs-CLOSED and
positive/negative P&L styling). **`data.json` is hand-written sample
data** — six illustrative trades across both strategies, nothing real —
because nothing downstream of the Execution Agent exists yet to generate
it.

**What it is**: a static HTML page, hosted on GitHub Pages, showing
current P&L, account balance, active strategy version, and a trade
journal. Updated on each trade — an export step (fired from wherever the
Execution Agent logs a trade, §28) pulls the account snapshot via the
**Alpaca CLI** and writes/appends a JSON file the page fetches
client-side. No backend, so no live query — the JSON snapshot is the only
sync mechanism GitHub Pages allows.

**Why the CLI specifically**: this finally gives the CLI a concrete job.
It was registered for the hackathon's "use all three Alpaca surfaces"
requirement but sat unused (see the surfaces bullet above) — the
Dashboard's account pull is real, ongoing usage rather than a one-off demo
call.

**Decided** (see `dashboard/README.md` for the full schema): the
`data.json` shape — `updated_at`, `strategy_version`, `account
{starting_balance, balance, daily_pnl}`, and a `trades[]` array (id,
timestamp, ticker, strategy, side, entry/exit price, quantity, pnl,
status, exit_reason). Realized P&L only; OPEN trades show no P&L rather
than a computed unrealized mark.

**Not yet decided**: where the export step lives (inside the Execution
Agent's own loop vs. a separate script it shells out to), the GitHub
Pages publish mechanism (commit-on-trade vs. a scheduled rebuild), and
whether OPEN trades ever get an unrealized-P&L column. Deferred to
whenever the Execution Agent gets built.

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
- Researched (via a background agent) how the sibling `alpacabot` project
  handles unattended scheduling: `pmset repeat wake` + a bridging
  caffeinate-only LaunchAgent (`StartCalendarInterval` as 5 weekday dicts,
  no native Mon–Fri shorthand in launchd XML) + a per-process
  `caffeinate -w $PID` tied to the run script's own lifetime. Plan is to
  adapt this for our two triggers, but **nothing is installed yet** —
  `launchctl load`/`pmset` changes are pending explicit go-ahead (system
  state, not just repo files). Open question: whether `-dimsu` actually
  holds through a closed lid on this Mac without external power/display —
  untested in alpacabot itself either, so don't assume it works.

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
  understood). **Project scope is what's committed** (`.mcp.json` in this
  repo) — more durable, and shareable if this goes on GitHub.
- **`.mcp.json` uses literal credentials, not `${VAR}` references**: tried
  `${ALPACA_API_KEY}`/`${ALPACA_SECRET_KEY}` substitution first (cleaner,
  keeps secrets out of git) but it kept resolving to empty even with
  confirmed-correct exports in the launching shell (verified 3 separate
  ways) — every real MCP data call 401'd. Root cause not fully nailed down
  (looks like Claude Code resolving `.mcp.json` env-refs against a different
  process environment than the interactive shell), but reverting to literal
  values immediately fixed it. Low-stakes tradeoff since this is a
  paper-trading-only key. If picking this back up: worth a clean
  investigation if reproducible, since env-var refs are the right long-term
  answer for a real secret.
- **Any new/changed MCP server registration needs a Claude Code restart** to
  load its tools into a session — registering mid-session never works, even
  if the server itself connects fine (confirmed via raw JSON-RPC probes
  bypassing Claude Code entirely).
- **Volume from the MCP/SDK bars endpoints is IEX-feed only**, not
  consolidated SIP tape — the Analyst subagent has been told to flag this
  when reasoning about whether volume looks elevated, since it may
  understate true participation.
- **No OPRA entitlement on this paper account** (`feed="opra"` →
  `403: "OPRA agreement is not signed"`) — greeks/IV come from Alpaca's free
  "indicative" feed. This was initially misdiagnosed as an AAPL/TSLA/MSFT
  data gap (see the spot-price bug above) — turned out to be unrelated once
  that bug was fixed; the indicative feed is fine for all 8 tickers.
