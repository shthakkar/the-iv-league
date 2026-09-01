# The IV League — Hackathon Dashboard Design

Status: approved by user 2026-08-31, pending implementation plan.

## Purpose

Replace the sample-data dashboard scaffold (`dashboard/index.html` +
hand-written `dashboard/data.json`) with a single hackathon-facing page
that shows real trading results, the strategy's evolution history, and
the system's architecture — everything a judge needs, one URL, no
navigation required to find any of it. Branded as **The IV League**.

Out of scope for this design (explicitly deferred):
- The GitHub Actions workflow that runs the export script on a schedule.
  This design only produces the script it will eventually call.
- Turning on GitHub Pages itself (a separate 2-minute repo-settings task,
  tracked in `NEXTSTEPS.md`).
- Unrealized P&L for OPEN positions (still deferred per the existing
  `dashboard/README.md` note — out of scope here too).

## 1. Branding

Assets (currently in `~/Downloads`, to be copied into `dashboard/assets/`
at implementation time):
- `iv_league_banner.png` — wide hero banner: crest, "The IV League",
  "Est. 2026", tagline "Volatility, harvested nightly".
- `iv_league_crest_v2.png` / `iv_league_crest.svg` — crest alone, used as
  a small logo mark and favicon (prefer the `.svg` for crispness at small
  sizes).

Palette taken from the banner: navy background (`#0d1b3e`-ish), gold
accents (`#c9a227`-ish), green/red for gains/losses (already the
project's implicit P&L convention). Typography: serif display face for
headings (matches the banner's collegiate feel — e.g. a Google Fonts
serif like Playfair Display or IBM Plex Serif), IBM Plex Mono/Sans for
data, tables, and numbers (matches `diagrams/agent-flow.html` and
`diagrams/agent-roster.html`'s existing typography, so the linked-out
diagram pages feel like part of the same site).

## 2. Page structure

Single file, `dashboard/index.html` (replaces the current scaffold in
place — same filename, same GitHub Pages serving path).

**Persistent hero header** (not a tab, always visible at the top):
banner image, title, tagline, "Est. 2026", and directly beneath it the
one-paragraph About text:

> IV League is an AI-powered 0DTE options trading system that ranks
> eight liquid stocks and ETFs using option-chain IV/skew, sells puts on
> the strongest premium opportunities, and takes selective directional
> trades on the remaining candidates. Specialized Analyst, Risk Manager,
> Execution, and Strategist agents work together to analyze, trade,
> monitor, and continuously improve the strategy.

Footer: "Built by Sakshar Thakkar" + a link to the GitHub repo.

**Tab nav** below the hero, client-side switching (no page reload), each
tab addressable via a URL hash for deep-linking (`index.html#architecture`):

1. Overview
2. Trade Journal
3. Strategy Evolution
4. Architecture

Reused/kept as-is, not touched by this design: `diagrams/agent-flow.html`
and `diagrams/agent-roster.html` stay separate standalone pages — the
Architecture tab links out to them (`target="_blank"`) rather than
iframing or rebuilding their content, so they keep their own full-page
polish and there's no duplicated diagram-maintenance burden.

## 3. Data pipeline

### 3.1 New script: `scripts/export_dashboard_data.py`

Run manually for now (`venv/bin/python3 scripts/export_dashboard_data.py`);
this is the script a future GitHub Actions workflow will call on a
schedule (not built in this design).

**Inputs**:
- Live account snapshot — reuses the same call `risk_manager.py`'s
  `get_account_snapshot()` already makes (`options_buying_power` falling
  back to `cash`), for current balance.
- Every `logs/<date>-execution.log` file present in `logs/` — one per
  trading day, growing over time with no schema change needed.

**Trade reconstruction algorithm** (per log file / date), validated by
hand against the real 2026-08-31 log and its known real outcome
(net −$3,927 = +$22 TSLA, +$11 AAPL, −$3,960 SPY — see `PROGRESS.md`):

1. Parse each `ENTRY`/`EXIT` line (fixed format:
   `ENTRY <iso-ts> <ticker> <symbol> qty=<n> price=<p> side=<SELL_TO_OPEN|BUY_TO_OPEN>`,
   `EXIT  <iso-ts> <ticker> <symbol> qty=<n> price=<p> reason=<TP|SL|EOD|TIME>`).
2. Join `ENTRY`/`EXIT` pairs by `symbol` within the same day's file.
3. Strategy side comes straight from the entry's `side`, no cross-file
   lookup needed: `SELL_TO_OPEN` → `premium-selling`, always a short put
   in V1 (spec §6 — no calls are ever sold), so `side: "SHORT_PUT"`.
   `BUY_TO_OPEN` → `directional`, a long call or put — distinguished by
   parsing the OCC `symbol` itself (the character immediately before the
   8-digit strike is `C` or `P`, e.g. `SPY260831P00765000` → put), giving
   `side: "LONG_CALL"` or `"LONG_PUT"`.
4. P&L, confirmed against the real numbers above:
   - premium-selling (short): `pnl = (entry_price - exit_price) * qty * 100`
   - directional (long): `pnl = (exit_price - entry_price) * qty * 100`
5. A symbol with an `ENTRY` but no matching `EXIT` yet is `OPEN` (`pnl:
   null`, `exit_price: null`, `exit_reason: null`) — matches the existing
   `dashboard/README.md` schema convention.

### 3.2 Output schema — `dashboard/data.json`

Extends the current schema (raw `trades[]` kept, in the existing shape)
with precomputed aggregates:

```jsonc
{
  "updated_at": "2026-08-31T16:45:00-04:00",
  "strategy_version": "v1.0",
  "account": {
    "starting_balance": 100000.00,
    "balance": 96073.00,
    "daily_pnl": -3927.00          // today's realized P&L only
  },
  "daily_summaries": [
    {
      "date": "2026-08-31",
      "pnl": -3927.00,
      "trades_count": 3,
      "win_rate": 0.6667            // 2 of 3 trades closed profitably
    }
  ],
  "strategy_stats": [
    {
      "strategy": "premium-selling",
      "trades": 2, "wins": 2, "losses": 0,
      "win_rate": 1.0, "total_pnl": 33.00
    },
    {
      "strategy": "directional",
      "trades": 1, "wins": 0, "losses": 1,
      "win_rate": 0.0, "total_pnl": -3960.00
    }
  ],
  "trades": [
    {
      "id": "2026-08-31-TSLA260831P00357500",
      "date": "2026-08-31",
      "timestamp": "2026-08-31T10:35:37.611971-04:00",
      "ticker": "TSLA",
      "strategy": "premium-selling",
      "side": "SHORT_PUT",
      "entry_price": 0.50, "exit_price": 0.28,
      "quantity": 1, "pnl": 22.00,
      "status": "CLOSED", "exit_reason": "take_profit"
    }
    // ... AAPL, SPY
  ]
}
```

`daily_summaries[]` and `strategy_stats[]` are computed once by the
script and written alongside the raw trades — the page itself does no
aggregation math, only rendering (per the earlier decision to precompute
rather than compute client-side).

## 4. Tab content

- **Overview**: stat tiles (current balance, total realized P&L, overall
  win rate, trades today) + a per-day P&L bar chart (from
  `daily_summaries[]`) + a win-rate-by-strategy chart (from
  `strategy_stats[]`), green/red coded.
- **Trade Journal**: sortable table over `trades[]` — date, ticker,
  strategy, side, entry/exit price, quantity, P&L, status, exit reason.
  Same table the current scaffold already renders, extended with a
  date/strategy filter now that multiple days will eventually exist.
- **Strategy Evolution**: `STRATEGY_CHANGELOG.md`'s content rendered as
  a proposals table — proposal, root cause, decision, before/after,
  AI-vs-human attribution, matching that file's existing structure
  exactly rather than inventing a new one.
- **Architecture**: a short prose summary of the agent pipeline (spec
  §30's architecture, condensed) plus two "Open full diagram ↗" links to
  `diagrams/agent-flow.html` and `diagrams/agent-roster.html`.

## 5. Testing

- `scripts/export_dashboard_data.py`: unit-test the log-parsing and
  aggregation functions against a small fixture log (reuse today's real
  `logs/2026-08-31-execution.log` as the fixture — its known real
  aggregate result, net −$3,927, is exactly the correctness check).
  The live account-snapshot call is a thin SDK call, smoke-tested live
  (same convention as `risk_manager.py`'s `get_account_snapshot()`).
- `dashboard/index.html`: serve locally
  (`python3 -m http.server` from `dashboard/`), verify all four tabs
  render, hash deep-links jump to the right tab on load, and both charts
  and the trade table render correctly against the real generated
  `data.json`.

## Decisions log (from brainstorming)

- Data pipeline: manual script now; GitHub Actions automation is a
  separate, later task.
- Aggregates are precomputed by the script, not computed client-side.
- Page structure: single page, tab nav with hash deep-links; the
  existing diagram pages stay separate and are linked out to, not
  embedded.
- Branding: "The IV League," Est. 2026, tagline "Volatility, harvested
  nightly," About paragraph, attribution "Built by Sakshar Thakkar" —
  all sourced from `iv_league_banner.png` and the user's own description.
