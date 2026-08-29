# ================================================================
# NEWS PREFETCH — warms the last-24h news cache during the 9:30-9:40 ET
# observation window (spec section 3), while no trades can be opened
# anyway. Deterministic, no LLM involved.
#
# Fetches once for the full 8-ticker universe in a single API call (the
# 9:30 trigger doesn't yet know which 5 will end up directional — that
# split only happens after Strategy Engine ranks at ~9:40), buckets each
# article under every universe ticker it's tagged with, and writes a
# cache file. The analyst subagent reads this instead of calling
# get_news live when the cache is fresh — see .claude/agents/analyst.md.
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
    """Fetch last-24h news for the universe in one call, bucketed per ticker.
    An article mentioning multiple tickers (e.g. a sector ETF roundup) lands
    in every relevant bucket — matches what per-ticker get_news filtering
    would return anyway, just as one request instead of eight."""
    universe = universe or config.UNIVERSE
    client = NewsClient(config.API_KEY, config.API_SECRET)

    end = datetime.datetime.now(datetime.timezone.utc)
    start = end - datetime.timedelta(hours=24)

    req = NewsRequest(
        symbols=",".join(universe),
        start=start,
        end=end,
        limit=50,
        include_content=False,
        exclude_contentless=True,
    )
    result = client.get_news(req)
    articles = result.data.get("news", [])

    buckets: dict[str, list[dict]] = {t: [] for t in universe}
    for a in articles:
        symbols = a.symbols or []
        headline = a.headline or ""
        summary = a.summary or ""
        source = a.source or ""
        url = a.url or ""
        created_at = a.created_at
        created_at = created_at.isoformat() if hasattr(created_at, "isoformat") else created_at
        entry = {
            "headline": headline,
            "summary": summary,
            "source": source,
            "url": url,
            "created_at": created_at,
            "symbols": symbols,
        }
        for t in universe:
            if t in symbols:
                buckets[t].append(entry)

    return buckets


def write_cache(buckets: dict[str, list[dict]], date: str | None = None) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(date)
    payload = {
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "window_hours": 24,
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
