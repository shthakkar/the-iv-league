# Dashboard — The IV League

Spec: [`../SPEC.md`](../SPEC.md) §29. Design: [`../docs/superpowers/specs/2026-08-31-hackathon-dashboard-design.md`](../docs/superpowers/specs/2026-08-31-hackathon-dashboard-design.md).
Single branded page — Overview, Trade Journal, Strategy Evolution, and
Architecture tabs — meant to be hosted on GitHub Pages, showing account
P&L/balance, per-day and per-strategy win rate, the strategy changelog, and
links out to the system's architecture diagrams.

## Status: real data, generated manually

`index.html` is done. `data.json` is now generated from real trading output
by `../export_dashboard_data.py` — no longer hand-written sample data. Run it
again any time to refresh:

```bash
cd /Users/manalithakkar/Documents/ivleague
venv/bin/python3 export_dashboard_data.py
```

Automating that (GitHub Actions, on a schedule) is a separate, later task —
see `NEXTSTEPS.md`.

## Viewing it

Browsers block `fetch()` of local files opened via `file://`, so don't
double-click `index.html`. The page also fetches two files *outside* this
folder (`../STRATEGY_CHANGELOG.md` for the Strategy Evolution tab,
`../diagrams/*.html` linked from the Architecture tab) — so serve the whole
**repo root**, not just `dashboard/`:

```bash
cd /Users/manalithakkar/Documents/ivleague   # repo root, not dashboard/
python3 -m http.server 8000
# then open http://localhost:8000/dashboard/
```

This is also why **GitHub Pages must be set to serve from the repository
root** (`/`), not `/dashboard` — serving from `/dashboard` would 404 both of
those relative fetches, since Pages never serves files outside its chosen
publish folder. The live site's dashboard URL becomes `.../dashboard/`
instead of the bare repo URL; the diagrams and changelog stay reachable
either way.

## `data.json` schema

```jsonc
{
  "updated_at": "2026-08-31T17:05:31-07:00",   // ISO 8601, when export_dashboard_data.py last ran
  "strategy_version": "v1.0",                   // spec §27's tracked version (V1.0, V1.1, ...)
  "account": {
    "starting_balance": 100000.00,
    "balance": 96070.70,                        // live account equity (options_buying_power/cash basis, see risk_manager.py)
    "daily_pnl": -3927.00                        // realized P&L, all days combined (only one day exists so far)
  },
  "daily_summaries": [                           // one entry per logs/<date>-execution.log found
    {
      "date": "2026-08-31",
      "pnl": -3927.00,                           // sum of that day's CLOSED trades' pnl
      "pnl_pct": -3.927,                          // pnl / balance CARRIED INTO that day * 100 (compounds day-over-day, not always off the original $100k)
      "trades_count": 3,                          // all trades that day, OPEN or CLOSED
      "win_rate": 0.6667                          // wins / closed trades that day
    }
  ],
  "strategy_stats": [                            // aggregated across every day found
    {
      "strategy": "premium-selling",             // "premium-selling" | "directional"
      "trades": 2, "wins": 2, "losses": 0,
      "win_rate": 1.0, "total_pnl": 33.00
    }
  ],
  "trades": [
    {
      "id": "2026-08-31-TSLA260831P00357500",    // "<date>-<OCC option symbol>"
      "date": "2026-08-31",
      "timestamp": "2026-08-31T10:35:37.611971-04:00",  // entry time, ISO 8601
      "ticker": "TSLA",
      "strategy": "premium-selling",              // "premium-selling" | "directional"
      "side": "SHORT_PUT",                         // "SHORT_PUT" | "LONG_CALL" | "LONG_PUT"
      "entry_price": 0.50,
      "exit_price": 0.28,                          // null while OPEN
      "quantity": 1,
      "pnl": 22.00,                                 // null while OPEN; short: (entry-exit)*qty*100, long: (exit-entry)*qty*100
      "status": "CLOSED",                           // "OPEN" | "CLOSED"
      "exit_reason": "take_profit"                  // take_profit | stop_loss | time_exit_1430 | eod | null
    }
  ]
}
```

`daily_summaries` and `strategy_stats` are precomputed by
`export_dashboard_data.py`, not by the page — the page only renders. The page
sorts trades newest-first and doesn't otherwise validate the file — malformed
JSON just shows the fetch-error state.

## How `export_dashboard_data.py` builds this

Parses every `logs/<date>-execution.log` line (`ENTRY`/`EXIT`, fixed format —
see the script's docstring), joins `ENTRY`+`EXIT` by option symbol, and reads
strategy/side straight off the entry: `SELL_TO_OPEN` is always a short put in
V1 (spec §6 — no calls are ever sold); `BUY_TO_OPEN`'s call-vs-put comes from
the OCC symbol itself. No cross-reference against `risk-decisions-*.json` is
needed. Then calls `risk_manager.get_account_snapshot()` for the live balance.
See the design doc's §3.1 for the full algorithm, hand-verified against
2026-08-31's real known outcome (net -$3,927).

## Not yet decided / deferred

- **GitHub Actions automation** — running `export_dashboard_data.py` on a
  schedule and committing the result. Tracked in `NEXTSTEPS.md`.
- **Unrealized P&L for OPEN trades** — still shown as "—" / excluded from
  aggregates. Would need a live option quote per open position at export
  time.
- **Turning on GitHub Pages itself** — a repo-settings task (Settings → Pages
  → serve from `/` root, see above), not yet done.
