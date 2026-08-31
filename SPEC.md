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

## 15. Directional Option Selection

BULLISH → buy 0DTE call. BEARISH → buy 0DTE put. UNDECIDED → no trade. Strike
selection configurable, ATM or slightly ITM preferred for the initial
backtest. Avoid far OTM lottery-style options.

## 16. Directional Capital Allocation

Maximum $5,000 total premium, split across however many trades are taken (1
trade → up to $5,000; 2 → ~$2,500 each; 3 → ~$1,667 each). Risk Manager may
use less than the max. No requirement to trade without a strong signal.

## 17. Directional Entry

~9:40 AM ET, after Analyst + Risk Manager complete their decisions.

## 18. Directional Exit

No fixed profit target, no trailing stop, no exit-when-ITM rule, no dynamic
cap. **Close all directional positions at 2:30 PM ET**, regardless of
profit/loss/ITM/IV/momentum. Clean research question: does the 9:40 AM
directional signal predict price movement through 2:30 PM?

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

## 27. Strategy Evolution

Strategist Agent does **not** directly modify production strategy:
Trading Results → Strategist Agent → Proposed Strategy Change → HITL
Review (§26) → Backtest → Compare against V1 → Paper Trading → Risk
Approval → Production. Every strategy version tracked (V1.0, V1.1, ...).
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
