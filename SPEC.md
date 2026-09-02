# 0DTE Multi-Agent Options Trading System — Strategy & Architecture Specification (V1)

*This is the living spec for the system, updated as the design evolves (see
git history for changes). See PROGRESS.md for what's actually been built
against it and where the MVP has intentionally deviated (scope cuts,
universe size, etc.) — this file is the reference target, not a description of
current state.*

## 1. Objective

Build an automated 0DTE options trading system using a **$100,000 account**.

The system has two independent trading components:

1. **Premium-selling strategy**
   * Select the best 3 underlyings based on option-chain IV/skew.
   * Sell 0DTE puts.
   * Continuously monitor and manage positions.

2. **Directional strategy**
   * Select 2–3 opportunities from the remaining 5 underlyings.
   * Use the first 10 minutes of market data plus news/events to determine direction.
   * Buy 0DTE calls or puts.
   * Enter at approximately **9:40 AM ET**.
   * Close at **2:30 PM ET**, regardless of profit or loss.

The system should prioritize **simple, deterministic rules in V1**. Strategy
optimization comes later through backtesting and the Strategist Agent.

## 2. Trading Universe

The system evaluates these 8 underlyings: SPY, QQQ, NVDA, TSLA, AAPL, AMZN, MSFT, META.

The system should dynamically check whether a valid 0DTE expiration exists for
each underlying on the current trading day. If no 0DTE chain is available,
that ticker is skipped for the day.

**Changed 2026-09-02 (no-0dte-fallback-policy, `strategy_engine.py`'s
`rank_ticker()`)**: "skipped" is now a fallback, not a dead end. A ticker with
no usable 0DTE chain is retried once against the next real trading day's
chain (via Alpaca's own calendar, no local weekday/holiday math) before being
skipped outright — surfaced live 2026-09-01, when only SPY/QQQ had a same-day
chain and the other 6 tickers had genuinely none. Applies at the
universe-ranking step, before the premium/directional split, so a
1DTE-substituted ticker can land on either side. **Position lifecycle is
unchanged**: this only changes which expiration gets ranked/sized/traded —
the resulting position is still force-closed same-day like every other
position (spec §9/§18's exit rules, `execution_agent.py`), never held
overnight. That's what keeps this a small, contained change rather than the
"switch the whole strategy to 1DTE" idea rejected on 2026-08-31 (which would
have needed real overnight position persistence, calendar logic, and
re-tuned exit timing — see PROGRESS.md's architecture-decisions entry).

## 3. Daily Timeline

**9:30 AM ET — Market Open.** Do not immediately trade. Begin collecting price,
volume, high, low, bid/ask, option-chain data, news, events, market conditions.

**9:30–9:40 AM ET — Observation Period.** No directional trades opened. The
Analyst Agent receives the first 10-minute market data.

**~9:40 AM ET — Initial Decision.** The system:
1. Retrieves the latest option chains.
2. Calculates IV/skew metrics.
3. Ranks all 8 underlyings.
4. Selects the top 3 for premium selling.
5. Sends the remaining 5 to the Analyst Agent.
6. Analyst determines directional opportunities.
7. Risk Manager approves positions and sizing.
8. Execution Agent submits orders.

## 4. Option Chain Analysis

**ATM Strike** = the strike price closest to the current underlying price.

```
ATM IV = (ATM Call IV + ATM Put IV) / 2
```

**15Δ Put** = the put whose absolute delta is closest to 0.15.

**Changed 2026-08-31**: Alpaca's own option-snapshot delta/IV fields are
**never populated for 0DTE contracts** — confirmed via their Market Data
FAQ (a literal division-by-zero in their Black-Scholes calc at T=0) and
empirically (20/20 identical near-ATM liquid SPY strikes had greeks at
1DTE, 0/20 had them at 0DTE, checked the same moment). Not a feed/
subscription gap — structural, every live trading day, for exactly the
0DTE contracts this spec requires. Fixed by computing delta/IV locally
(`black_scholes.py`) whenever Alpaca's own fields are null: standard
Black-Scholes, inverted via Newton-Raphson (falling back to bisection) off
the contract's own mid quote, using real hours-remaining-to-close as T
(never literal zero). Researched before building (see PROGRESS.md): this
is standard practice for near-zero-but-nonzero T, not a workaround of
questionable soundness — real caveats exist as T→0 (vega collapse, whippy
delta/gamma in the final 30-60 min) but concentrate in deep OTM/ITM
strikes, not the ATM/~15Δ region this spec actually targets. First proved
against real 0DTE market data live, then used for the system's actual
first live trades the same day.

## 5. Premium-Selling Ranking

```
IV Skew = 15Δ Put IV - ATM IV
```

Example: ATM IV 22%, 15Δ Put IV 28% → IV Skew = 6 percentage points. Higher
skew means the downside put carries a larger IV premium relative to ATM.

Calculate IV skew for all 8, sort descending. The **top 3** become
premium-selling candidates.

## 6. Premium-Selling Strategy

Sell the approximately **15Δ 0DTE put**, **cash-secured** (full strike × 100
secured per contract) — not a defined-risk credit spread. **Changed
2026-08-30** from the original defined-risk-spread design (sell 15Δ put, buy
a lower-strike put for protection); no protective leg in V1. Trade-off,
accepted knowingly: buying power required per contract is now the full
strike (versus just the spread width), so fewer contracts fit the same
budget and capital utilization is lower — but risk is uncapped below the
strike rather than structurally floored by a protective put. A standalone
`type: "stop"` order (`order_class: "simple"`) **is** supported for a
single option leg and genuinely holds/fires server-side — confirmed both
from Alpaca's own OrderType spec and from the sibling `alpacabot` project's
real fill logs (`🛑 STOP FILLED @ 4.14` on an actual SPY option position) —
so the §9 stop-loss can be a real standing order, not just a polling-loop
check. What's still equity-only is bundling TP+SL atomically as child
orders (`bracket`/`oco`/`oto` order classes); the Execution Agent submits
the stop as its own standalone order instead (see `alpacabot/trade_manager.py`'s
proven pattern) — the take-profit and EOD close still go through the
polling loop, with no backstop under it if that loop
lags or a move gaps through.

## 7. Capital Allocation

Account: $100,000. Target: **95% → premium-selling**, **5% → directional**.
Premium selling budget ≈ $95,000. Directional option budget ≈ $5,000 —
this is the maximum total premium spent on directional options, NOT $5,000
per position.

**Changed 2026-08-31**: both percentages made dynamic, both explicit
human/product decisions (not Strategist Agent proposals — see
`STRATEGY_CHANGELOG.md`). Directional is no longer a flat 5% regardless of
how many candidates were actually selected that day — it's **1% of
balance per selected directional candidate, capped at 3% total** (1
selected → 1%, 2 → 2%, 3 → 3%), so a low-conviction/low-breadth day risks
proportionally less. Premium is no longer an independent fixed 95% either
— it's the **complement** of whatever directional took (100% - directional
%), so the two always sum to the full available balance instead of a
fixed split leaving 2-4% permanently idle: 0 directional selected → 100%
premium, 1 → 99%, 2 → 98%, 3 → 97%. Both computed off the account's real
live balance (`options_buying_power`, falling back to `cash`), not the
hardcoded $100k above — see PROGRESS.md's Risk Manager entry. Directional
option budget is still a total across whatever's selected, not per
position, as originally specified.

**Changed 2026-09-02**: the 1%/3% figures above are now **0.5%/1.5%** —
halved, a human decision after 3 real trading days showed directional
losing on 5 of 6 trades (net -$5,671), corroborated by external research
that long ATM/near-ATM 0DTE directional structures show negative median
PNL on average (see `STRATEGY_CHANGELOG.md`'s 2026-09-02 entry for the
full evidence and sources). Premium's complement formula is unchanged,
just now summing against the smaller directional side: 0 selected → 100%
premium, 1 → 99.5%, 2 → 99%, 3 → 98.5%.

## 8. Premium-Selling Position Allocation

Target roughly equal allocation across the top 3: $95,000 / 3 ≈ $31,667. The
Risk Manager determines actual contract quantity based on the CSP's buying
power requirement (strike × 100 × contracts), portfolio exposure, existing
positions, daily risk limits. Never force a trade merely to use all
available capital.

## 9. Short-Put Exit Rules

- **Take Profit**: close at ~**50%** of original premium (sold $1.00 → close ~$0.50).
- **Stop Loss**: close at **3×** original premium (sold $1.00 → stop ~$3.00).
  Account for the position's executable market price and bid/ask.
- **End-of-Day**: all short option positions must be closed before expiration.
  Hard EOD liquidation time. No position intentionally left to expire in V1.

## 10. Position Recycling

When a short-put position hits its profit target and closes intraday: mark
capital available, recalculate chains/rankings, re-evaluate candidates, Risk
Manager decides whether to open another position. Don't auto-re-enter the
same underlying if already held. Consider a configurable cooldown.

**Built 2026-09-02** (see PROGRESS.md's Component 6 entry and
`STRATEGY_CHANGELOG.md`): implemented for the premium-selling side, spec-literal
— triggers on **either** a short put's take-profit **or** stop-loss close (not
just profit target as this section's opening sentence says), re-ranks the
universe fresh, excludes any ticker currently held on either side, and sizes
the top eligible candidate against one slot's worth of the current premium
budget. Gated by a hard entry cutoff (2:30 PM ET / `config.
NEW_ENTRY_CUTOFF_TIME`) — no new position, recycled or otherwise, opens after
that time. No configurable cooldown implemented; re-entering the same ticker
that just closed is allowed if it re-ranks eligible (not "already held" once
closed). Directional side does **not** recycle — see §17's 2026-09-02 note.

## 11. Directional Strategy

Operates on the remaining 5 underlyings after the top-3 premium-selling
candidates are selected.

## 12. Analyst Agent

Evaluates the remaining 5. Receives:
- **News**: current-day, overnight, company announcements, regulatory,
  upgrades/downgrades, product announcements, relevant macro.
- **Events**: earnings, investor events, product launches, economic events,
  other known catalysts.
- **First 10-minute market data**: open, current price, first-10-min return,
  high/low, volume, relative volume, gap %, price vs VWAP, market/sector
  performance.
- **Option data**: ATM IV, put IV, call IV, delta, bid/ask, premium, liquidity.

## 13. Analyst Output

Per candidate, structured:
```
Ticker:
Direction: BULLISH | BEARISH | UNDECIDED
Confidence: 0-100
Reason: <short explanation>
Catalysts: <list>
Risks: <list>
```

The Analyst **does not have authority to place trades**.

## 14. Selecting Directional Trades

From the remaining 5: select up to 3, only candidates with sufficient
confidence, undecided candidates skipped. Maximum total premium: $5,000.

**Changed 2026-09-02**: "sufficient confidence" (`MIN_DIRECTIONAL_CONFIDENCE`)
raised from 55 to **60** — a human decision after 3 real trading days showed
two directional trades clearing the old bar only narrowly (55, 56) and both
losing; see `STRATEGY_CHANGELOG.md`'s 2026-09-02 entry.

## 15. Directional Option Selection

BULLISH → buy 0DTE call. BEARISH → buy 0DTE put. UNDECIDED → no trade. Strike
selection configurable, ATM or slightly ITM preferred for the initial
backtest. Avoid far OTM lottery-style options.

## 16. Directional Capital Allocation

Maximum $5,000 total premium, split across however many trades are taken (1
trade → up to $5,000; 2 → ~$2,500 each; 3 → ~$1,667 each). Risk Manager may
use less than the max. No requirement to trade without a strong signal.

## 17. Directional Entry

~9:40 AM ET, after Analyst + Risk Manager complete their decisions. Still
fires only once per morning — **unchanged 2026-09-02**: only §18's exit
duration changed, not entry frequency. Directional does not recycle
(contrast §10, built for the premium side only).

## 18. Directional Exit

No fixed profit target, no trailing stop, no exit-when-ITM rule, no dynamic
cap. **Close all directional positions at 2:30 PM ET**, regardless of
profit/loss/ITM/IV/momentum. Clean research question: does the 9:40 AM
directional signal predict price movement through 2:30 PM?

**Changed 2026-09-02**: exit changed from the fixed 2:30 PM ET clock time to
a fixed **30-minute duration from each position's own entry**
(`config.DIRECTIONAL_HOLD_MINUTES`) — a human decision, informed by external
research (the first-half-hour-predicts-last-half-hour finding concentrates
predictive power at the END of the session, which a 2:30 PM exit never
reached anyway; practitioner 0DTE backtests generally favor shorter holds
once decay/spread costs are counted) and live evidence (a 2026-09-02 TSLA
loss where the adverse move happened within the first hour of a ~4-hour
hold). This changes what the "clean research question" above actually tests
— no longer "does the signal predict price through a fixed 2:30 PM," but
"does the signal predict price 30 minutes out." See `STRATEGY_CHANGELOG.md`'s
2026-09-02 entry for the full sourcing.

## 19. Execution Agent

Runs ~every minute during market hours. Monitors open positions, current
option price, bid/ask, underlying price, P/L, stop-loss/take-profit
conditions, time-based exits, buying power, daily risk limits.

## 20. Execution Rules

**Short Put**: 50% premium reduction → CLOSE, or 3× premium → CLOSE, or
end-of-day → CLOSE.
**Long Option**: 2:30 PM ET → CLOSE. No profit target or stop loss in V1.

## 21. Re-analysis After Position Closure

Position closed intraday → recalculate market/option data → Analyst → Risk
Manager → possible new position → Execution Agent. Enforce: max entries per
ticker, max total trades, max daily loss, max portfolio exposure (Risk
Manager controlled).

## 22. Risk Manager Agent

Final authority before execution. Inputs: account balance, buying power,
current positions, daily P/L, option chain, IV/skew, Analyst signals, market
conditions, trade history. Output: APPROVE or REJECT (+ ticker, direction,
contract, quantity, max risk, stop, take profit, capital allocation if
approved). Can override the Analyst.

## 23. Risk Controls

Hard limits independent of the AI agents:
- **Max daily loss**: configurable % of account (e.g. 2% = $2,000). Once hit:
  no new positions (existing positions still follow their exit rules unless
  a separate emergency liquidation rule applies).
- **Max exposure**: prevent excessive concentration in one underlying.
- **Max number of positions**: configurable.
- **Max number of re-entries**: configurable, prevents excessive churn.

## 24. Strategist Agent

Runs primarily post-trading-day. Analyzes premium-selling performance
(ticker-by-ticker, IV skew at entry, premium collected, delta, strike
distance, expected move, entry/exit timing, TP vs SL, re-entry performance)
and directional performance (Analyst prediction/confidence, news/event
category, first-10-min movement, call vs put, premium, entry/exit price,
P/L).

**Changed 2026-08-31 (built, see PROGRESS.md's Component 7 entry)**: V1
scope narrowed for a single trading day of history (n=1) — post-mortem
(what went well / what could be better, evidence-cited) + an
inputs-completeness audit + a live end-of-day news/price re-check, plus
qualitative process-fix proposals only. Explicitly forbidden from
proposing numeric parameter tuning (this section's "is 15Δ optimal"-style
questions, §25) from a single day's sample — no statistical basis yet.
Built as a `strategist` subagent, manually invoked (no cron).

## 25. Strategist Questions

Is 15Δ optimal? 10Δ or 20Δ better? Is IV skew actually predictive? Which
underlying works best? Treat SPY/QQQ differently? Does the first-10-min
signal add value? Does confidence correlate with returns? Are news-driven
trades better? Does re-entry improve returns? Is 9:40 the optimal entry? Is
2:30 the optimal exit? Are stop-loss rules appropriate?

## 26. HITL Review (Human-in-the-Loop)

Sits between the Strategist Agent's proposed change and any backtest work.
A human reviews the proposal and returns **APPROVE** or **REJECT**:

- **Reject**: the proposal is discarded and logged with the reviewer's
  reason. No backtest, no further action.
- **Approve**: the proposal proceeds into the pipeline below (§27) —
  Backtest → Compare against V1 → Paper Trading → Risk Approval →
  Production.

This is a separate gate from **Risk Approval** later in that same pipeline:
HITL judges whether the *idea* is worth testing at all; the Risk Manager
(§22) still separately gates real capital before anything goes live. The
Strategist has no authority to advance its own proposals past this point —
mirrors the Analyst's no-execution-authority rule (§12).

**Changed 2026-08-31 (built, see PROGRESS.md's Component 7 entry)**: review
happens interactively in a Claude Code chat session, not a dedicated
review surface — a published Artifact with comments and a local
HTML-page-plus-server were both considered and dropped during design in
favor of just reviewing and implementing in-session (simpler, no new
always-on process). Every accepted proposal from the first review cycle
was implemented directly (see below, §27's note on the pipeline) — there
is no separate machine-readable APPROVE/REJECT record beyond the
conversation itself and `STRATEGY_CHANGELOG.md`'s dated summary.

## 27. Strategy Evolution

Strategist Agent does **not** directly modify production strategy:
Trading Results → Strategist Agent → Proposed Strategy Change → HITL
Review (§26) → Backtest → Compare against V1 → Paper Trading → Risk
Approval → Production. Every strategy version tracked (V1.0, V1.1, ...).

**Changed 2026-08-31**: the Backtest → Compare against V1 → Paper Trading
→ Risk Approval → Production pipeline is **not built** — none of that
infrastructure exists yet (no backtester at all). V1's actual pipeline is
shorter: Strategist proposal → human review (§26, interactive) → approved
changes implemented directly against what cron runs next, on the paper
account, with a human reviewing every proposal first. Accepted trade-off
for now (see `STRATEGY_CHANGELOG.md`'s 2026-08-31 entry) — revisit once
this needs to run with less human oversight per cycle, or once there's
enough trade history to make backtesting worthwhile. No version numbers
(V1.0, V1.1, ...) are tracked yet either — changes are tracked by
`STRATEGY_CHANGELOG.md` entry + git commit instead.
Never allow the system to silently change live trading rules.

## 28. Data Logging

Every decision logged: market snapshot (timestamp, ticker, price, volume,
VWAP, gap %, first-10-min return, market return), option snapshot
(expiration, strike, bid/ask/mid, delta/gamma/theta/vega, IV, volume, OI),
derived metrics (ATM IV, 10/15/20Δ put IV, IV skew/ratio, expected move, put
distance/expected move), Analyst output (direction, confidence, news,
catalysts, reason), trade (entry/exit timestamp/price, quantity, P/L, exit
reason).

## 29. Dashboard (Trade Journal & P&L)

A simple static HTML page — hosted on GitHub Pages — giving a
human-readable view of trading state: current P&L, account balance, the
active strategy version (V1.0, V1.1, ...), and a chronological trade
journal.

Updated on each trade taken, not live/streaming. When the Execution Agent
logs a trade (§28), a lightweight export step pulls the current account
snapshot via the **Alpaca CLI** (balance, positions) and appends the trade
to a JSON file the static page fetches client-side. GitHub Pages serves
static files only — the JSON snapshot is the sync mechanism, there's no
backend to query live.

Also reflects strategy-version changes from the evolution loop (§26-27):
once a HITL-approved change reaches Production, the dashboard shows the
new active version.

Not itself an agent — a passive observer of the other six components, not
part of the daily call sequence.

## 30. High-Level System Architecture

```
                    MARKET DATA
                         │
             ┌───────────┴───────────┐
        PRICE DATA              OPTION DATA
             └───────────┬───────────┘
                  STRATEGY ENGINE
                  Rank 8 Underlyings
             ┌───────────┴───────────┐
          TOP 3                  BOTTOM 5
       SELL 15Δ PUT             ANALYST AGENT
                                Bullish/Bearish + Confidence
                                     │
                              RISK MANAGER
                         ┌───────────┴───────────┐
                    SHORT PUTS             LONG OPTIONS
                     ~95%                    ≤5%
                         └───────────┬───────────┘
                              EXECUTION AGENT
                              Every ~1 minute
                         ┌───────────┴───────────┐
                      Position                Position
                       Open                   Closed
                    Monitor                Re-run analysis
                         └───────────┬───────────┘
                              END OF DAY
                              TRADE LOGS
                              STRATEGIST AGENT
                              Analyze → Propose → HITL Approve/Reject
                                     → Backtest → New Strategy
```

## 31. V1 Strategy Summary

**At 9:40 AM**, Universe: SPY QQQ NVDA TSLA AAPL AMZN MSFT META.
**Rank**: 15Δ Put IV − ATM IV. **Top 3**: sell 0DTE ~15Δ puts, ~95% capital,
exit at 50% profit / 3× stop / EOD. **Bottom 5**: Analyst evaluates
news/events/first-10-min/price action/market context, select up to 3
(Bullish→Call, Bearish→Put, Undecided→no trade), max $5,000 total premium,
entry ~9:40, exit 2:30 PM, no directional profit cap in V1.

## 32. Core Philosophy

Responsibilities intentionally separated:
- **Analyst Agent**: "What direction does the market appear to favor?"
- **Strategy Engine**: "Where is option premium relatively attractive?"
- **Risk Manager**: "Can we afford to take this trade?"
- **Execution Agent**: "Execute and manage the predefined rules."
- **Strategist Agent**: "What did we learn, and what should we test next?"
- **HITL Review**: "Is this proposed change worth testing?"
- **Dashboard**: not a decision-maker — the human-readable record of what
  the other six did.

AI agents should **not replace deterministic risk and execution logic**. The
goal of V1 is not the perfect strategy — it's a clean, measurable system
whose components can be evaluated independently. Once enough historical data
exists, the Strategist Agent can determine whether 15Δ, IV skew, 9:40
entry, 2:30 exit, 50% capture, 3× stop, top-3 selection, and $5K directional
allocation actually improve the strategy.

## 33. Testing / Mock Data Strategy

Later pipeline stages (selection, sizing, Risk Manager, Execution Agent)
are deterministic code and shouldn't need a live market or a fresh Analyst
subagent run every time they're touched. Practice: capture one real
morning's output to `mock_cache/<date>/` (news, Analyst reads, ranking,
selection) once, then reuse it as a fixture — deterministic code gets
built/re-tested against a frozen real morning instantly, no cost or agent
latency. When markets are closed, a nearby real option-chain expiration
may stand in for ranking/chain-fetch testing (never synthetic/faked data),
clearly labeled as a stand-in and never treated as a live trading signal.
Judgment-driven output (Analyst reads, news) is never synthesized this
way — only captured from a real run.

## Constraint: must use Alpaca's Trading API, MCP server, and CLI

All three Alpaca surfaces — the [Trading API](https://docs.alpaca.markets/us/docs/alpaca-mcp-server)
(REST/SDK), the [official MCP server](https://github.com/alpacahq/alpaca-mcp-server),
and the [official CLI](https://github.com/alpacahq/cli) — must be used
somewhere in the system (hackathon/sponsor requirement). See PROGRESS.md for
how each has been mapped to a component.
