# ================================================================
# NEWS PREFETCH — warms the last-24h news cache during the 9:30-9:40 ET
# observation window (spec section 3), while no trades can be opened
# anyway. Deterministic, no LLM involved.
#
# Fetches the full 8-ticker universe (the 9:30 trigger doesn't yet know
# which 5 will end up directional — that split only happens after Strategy
# Engine ranks at ~9:40) and writes a cache file. The analyst subagent
# reads this instead of calling get_news live when the cache is fresh —
# see .claude/agents/analyst.md.
#
# One request PER TICKER, not one combined call for the whole universe --
# see fetch_news()'s docstring for why (a real 2026-09-01 bug where a
# shared limit crowded out individual tickers' coverage).
#
# Run standalone: `venv/bin/python3 prefetch_news.py`
# ================================================================
from __future__ import annotations

import datetime
import json
import os

from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest

import config

CACHE_DIR = "logs/cache"


def _today() -> str:
    return datetime.date.today().isoformat()


def _cache_path(date: str | None = None) -> str:
    return os.path.join(CACHE_DIR, f"news-{date or _today()}.json")


def fetch_news(universe: list[str] | None = None) -> dict[str, list[dict]]:
    """Fetch last-24h news, one request PER TICKER (not one combined call
    for the whole universe). Changed 2026-09-01: the original single call
    (symbols=<all 8>, limit=50) shared one 50-article cap across the whole
    universe, which silently crowded out older-but-relevant articles for
    individual tickers on a high-news morning -- confirmed live (a real
    NVDA story from 06:06 ET, well within the 24h window, was missing from
    the 09:30:48 ET cache; see STRATEGY_CHANGELOG.md's 2026-09-01 entry).
    Fetching each ticker separately, capped at config.NEWS_ARTICLES_PER_TICKER
    each, means one busy name can never crowd out another's coverage --
    the trade-off is 8 API calls instead of 1, once a day, pre-market."""
    universe = universe or config.UNIVERSE
    client = NewsClient(config.API_KEY, config.API_SECRET)

    end = datetime.datetime.now(datetime.timezone.utc)
    start = end - datetime.timedelta(hours=24)

    buckets: dict[str, list[dict]] = {}
    for ticker in universe:
        req = NewsRequest(
            symbols=ticker,
            start=start,
            end=end,
            limit=config.NEWS_ARTICLES_PER_TICKER,
            include_content=False,
            exclude_contentless=True,
        )
        result = client.get_news(req)
        articles = result.data.get("news", [])

        entries = []
        for a in articles:
            created_at = a.created_at
            created_at = created_at.isoformat() if hasattr(created_at, "isoformat") else created_at
            entries.append({
                "headline": a.headline or "",
                "summary": a.summary or "",
                "source": a.source or "",
                "url": a.url or "",
                "created_at": created_at,
                "symbols": a.symbols or [],
            })
        buckets[ticker] = entries

    return buckets


def write_cache(buckets: dict[str, list[dict]], date: str | None = None) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(date)
    payload = {
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "window_hours": 24,
        "articles_per_ticker": config.NEWS_ARTICLES_PER_TICKER,
        "tickers": buckets,
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    return path


if __name__ == "__main__":
    buckets = fetch_news()
    path = write_cache(buckets)
    print(f"Wrote {path}")
    for ticker, articles in buckets.items():
        print(f"  {ticker}: {len(articles)} article(s)")
