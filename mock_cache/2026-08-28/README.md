# Mock fixture — 2026-08-28

A captured, real (not synthetic) run of the pipeline through directional
selection, saved to disk so the deterministic pieces downstream of the
Analyst (selection, and later sizing/Risk Manager) can be iterated on and
re-tested without re-fetching data or re-running Claude Code subagents.

**Why this exists**: the Analyst step is 5 parallel subagent calls, each
doing real MCP-backed bars/news/reasoning — slow and not free to re-run
just to check whether a change to `directional_selection.py` still picks
the right 3 tickers. This fixture freezes one real morning's worth of
output so that check is instant.

## What's real vs. what's a stand-in

- **`news_cache.json`**, **`analyst_candidates.json`**: 100% real. News
  fetched live via `prefetch_news.py`; Analyst reads are real subagent
  output (`subagent_type: "analyst"`) against today's (2026-08-28) actual
  completed first-10-minute session and the prefetched news — see
  PROGRESS.md's "Directional Selection" section for the full transcript.
- **`strategy_ranking.json`**: real IV-skew math against a **real option
  chain**, but for expiration **2026-08-31**, not 2026-08-28 — markets
  were closed (weekend) when this was captured, so the nearest available
  chain was used as a structural stand-in to exercise the ranking/split
  logic. This is NOT what an actual same-day 0DTE chain would look like;
  treat the ranking numbers as "the code works," not as a trading signal.
- **`selection_result.json`**: 100% real — pure function of
  `analyst_candidates.json` + `config.MIN_DIRECTIONAL_CONFIDENCE`, no
  stand-in involved.

## Regenerating

```bash
venv/bin/python3 scripts/save_mock_fixture.py
```

Regenerates `strategy_ranking.json` and `selection_result.json` only
(cheap: pure SDK chain fetch + pure Python). Never touches
`news_cache.json` / `analyst_candidates.json` — those require live
subagent runs to refresh and must be captured by hand when a real test
run is needed (copy from `logs/cache/` after such a run, same as this
fixture was built).

## Using it to test downstream pieces

```bash
venv/bin/python3 directional_selection.py mock_cache/2026-08-28/analyst_candidates.json
```

reproduces `selection_result.json` exactly — the same pattern will apply
to sizing/Risk Manager once built: point them at this fixture's
`selection_result.json` instead of a live morning run.

## Result summary (for quick reference)

Universe: SPY QQQ NVDA TSLA AAPL AMZN MSFT META (all 8, spec §2).
Premium-selling top 3 (by IV skew, Aug 31 chain): **NVDA, AAPL, AMZN**.
Directional 5: QQQ, SPY, META, MSFT, TSLA.

| Ticker | Direction | Confidence | Selected? |
|---|---|---|---|
| MSFT | BULLISH | 65 | ✅ |
| META | BULLISH | 60 | ✅ |
| QQQ  | BULLISH | 55 | ✅ |
| SPY  | UNDECIDED | 30 | ❌ (UNDECIDED) |
| TSLA | UNDECIDED | 30 | ❌ (UNDECIDED) |
