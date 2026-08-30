# Dashboard

Spec: [`../SPEC.md`](../SPEC.md) §29. Static HTML page, meant to be hosted
on GitHub Pages, showing account P&L/balance, the active strategy version,
and a trade journal — updated on each trade, not live/streaming.

## Status: scaffolded, sample data only

`index.html` and the rendering logic are done and were smoke-tested by
serving this folder locally. **`data.json` is hand-written sample data**,
not real trading output — nothing generates it yet. See
[`../PROGRESS.md`](../PROGRESS.md)'s Dashboard entry for the full picture
of what's real vs. not.

## Viewing it

Browsers block `fetch()` of local files opened via `file://`, so don't
double-click `index.html`. Serve the folder instead:

```bash
cd dashboard
python3 -m http.server 8000
# then open http://localhost:8000/
```

On GitHub Pages this isn't an issue — pages are always served over
http(s).

## `data.json` schema

```jsonc
{
  "updated_at": "2026-08-30T14:32:00-04:00",   // ISO 8601, when this file was last written
  "strategy_version": "v1.0",                   // spec §27's tracked version (V1.0, V1.1, ...)
  "account": {
    "starting_balance": 100000.00,
    "balance": 101240.00,                       // starting_balance + sum of realized trade pnl
    "daily_pnl": 1240.00                         // sum of CLOSED trades' pnl only (not unrealized)
  },
  "trades": [
    {
      "id": "t-001",                             // unique, any stable string
      "timestamp": "2026-08-30T09:41:05-04:00",  // entry time, ISO 8601
      "ticker": "NVDA",
      "strategy": "premium-selling",              // "premium-selling" | "directional"
      "side": "SHORT_PUT_SPREAD",                  // SHORT_PUT_SPREAD | LONG_CALL | LONG_PUT
      "entry_price": 1.05,
      "exit_price": 0.52,                          // null while OPEN
      "quantity": 10,
      "pnl": 530.00,                               // null while OPEN (no unrealized P&L calc here)
      "status": "CLOSED",                          // "OPEN" | "CLOSED"
      "exit_reason": "take_profit"                 // take_profit | stop_loss | time_exit_1430 | eod | null
    }
  ]
}
```

The page sorts trades newest-first and doesn't otherwise validate the
file — malformed JSON just shows the fetch-error state.

## Not yet decided (picked up when the Execution Agent is built)

- **Where the export step lives**: inside the Execution Agent's own
  ~1-minute loop (spec §19) right after it logs a trade, vs. a separate
  script it shells out to. Either way it calls the **Alpaca CLI** for the
  account snapshot (balance) — see `PROGRESS.md`'s Alpaca-surfaces
  mapping.
- **Publish mechanism**: simplest is committing the updated `data.json`
  straight to the branch GitHub Pages serves (matches "simple html," no
  extra infra) — but that means the export step needs git credentials in
  whatever environment runs it. A scheduled rebuild is the alternative if
  that turns out to be awkward. Not chosen yet.
- **Unrealized P&L for OPEN trades**: currently just shown as "—" in the
  journal and excluded from `daily_pnl`. Computing a live mark would need
  a current option quote per open position at export time — deferred.
