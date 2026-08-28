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
    Gotchas) → intended for ops/demo status checks and the EOD
    force-liquidation safety net (not yet wired in).
- **MVP universe**: `config.py`'s `UNIVERSE` defaults to `[SPY, QQQ, NVDA,
  TSLA]` (4, not the spec's 8) for faster iteration, but every ticker in the
  spec's 8 has been tested via `UNIVERSE_OVERRIDE` and works.
- **PREMIUM_SELL_COUNT = 3**, matching the spec's top-3 split (was
  temporarily 1 during early testing, corrected).
- **News source**: Alpaca's own News API (`NewsClient`, Benzinga-sourced) —
  reinforces the "use Alpaca's API" requirement rather than diluting it with
  a third-party source, and it's free/already-authenticated.

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
