# ================================================================
# NEWS PREFETCH — unit tests for fetch_news()'s per-ticker fetching.
#
# Regression test for the real 2026-09-01 bug: a single combined
# NewsRequest(symbols=<all 8>, limit=50) call shared one 50-article cap
# across the whole universe, so a high-news ticker (e.g. NVDA on an
# AI-heavy morning) could silently crowd a real, in-window article out
# of another ticker's bucket -- or its own. Fixed by fetching each
# ticker with its own request/limit, so one ticker's article volume can
# never affect another's coverage. See config.py's NEWS_ARTICLES_PER_TICKER
# and STRATEGY_CHANGELOG.md's 2026-09-01 entry.
# ================================================================
import unittest
from unittest.mock import MagicMock, patch

import prefetch_news as pn


def _article(headline, symbols):
    a = MagicMock()
    a.headline = headline
    a.summary = "summary"
    a.source = "benzinga"
    a.url = "https://example.com"
    a.created_at = "2026-09-01T12:00:00Z"
    a.symbols = symbols
    return a


class FetchNewsTests(unittest.TestCase):
    @patch("prefetch_news.NewsClient")
    def test_makes_one_request_per_ticker(self, mock_client_cls):
        mock_client = mock_client_cls.return_value
        mock_client.get_news.return_value = MagicMock(data={"news": []})

        pn.fetch_news(universe=["SPY", "QQQ", "NVDA"])

        self.assertEqual(mock_client.get_news.call_count, 3)
        requested_symbols = [
            call.args[0].symbols for call in mock_client.get_news.call_args_list
        ]
        self.assertEqual(sorted(requested_symbols), ["NVDA", "QQQ", "SPY"])

    @patch("prefetch_news.NewsClient")
    def test_each_request_capped_at_articles_per_ticker(self, mock_client_cls):
        mock_client = mock_client_cls.return_value
        mock_client.get_news.return_value = MagicMock(data={"news": []})

        pn.fetch_news(universe=["SPY", "NVDA"])

        for call in mock_client.get_news.call_args_list:
            self.assertEqual(call.args[0].limit, pn.config.NEWS_ARTICLES_PER_TICKER)

    @patch("prefetch_news.NewsClient")
    def test_a_busy_tickers_articles_never_crowd_another_tickers_bucket(self, mock_client_cls):
        # The actual 2026-09-01 failure mode: NVDA having plenty of news
        # must not reduce how much SPY's own request can return -- each
        # ticker's bucket is populated ONLY from that ticker's own
        # independent request/response, never a shared pool.
        mock_client = mock_client_cls.return_value

        def get_news_side_effect(request):
            if request.symbols == "NVDA":
                return MagicMock(data={"news": [_article(f"NVDA story {i}", ["NVDA"]) for i in range(10)]})
            return MagicMock(data={"news": [_article("SPY story", ["SPY"])]})

        mock_client.get_news.side_effect = get_news_side_effect

        buckets = pn.fetch_news(universe=["SPY", "NVDA"])

        self.assertEqual(len(buckets["NVDA"]), 10)
        self.assertEqual(len(buckets["SPY"]), 1)
        self.assertEqual(buckets["SPY"][0]["headline"], "SPY story")

    @patch("prefetch_news.NewsClient")
    def test_bucket_entry_shape(self, mock_client_cls):
        mock_client = mock_client_cls.return_value
        mock_client.get_news.return_value = MagicMock(
            data={"news": [_article("Some headline", ["AAPL"])]}
        )

        buckets = pn.fetch_news(universe=["AAPL"])

        entry = buckets["AAPL"][0]
        self.assertEqual(entry["headline"], "Some headline")
        self.assertEqual(entry["source"], "benzinga")
        self.assertIn("created_at", entry)
        self.assertIn("url", entry)
        self.assertEqual(entry["symbols"], ["AAPL"])

    @patch("prefetch_news.NewsClient")
    def test_ticker_with_zero_articles_gets_empty_bucket_not_missing_key(self, mock_client_cls):
        mock_client = mock_client_cls.return_value
        mock_client.get_news.return_value = MagicMock(data={"news": []})

        buckets = pn.fetch_news(universe=["SPY", "QQQ"])

        self.assertEqual(buckets["SPY"], [])
        self.assertEqual(buckets["QQQ"], [])


class WriteCacheTests(unittest.TestCase):
    def test_payload_includes_articles_per_ticker(self):
        import json
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmp:
            orig_cache_dir = pn.CACHE_DIR
            pn.CACHE_DIR = tmp
            try:
                path = pn.write_cache({"SPY": []}, date="2026-09-01")
                with open(path) as f:
                    payload = json.load(f)
                self.assertEqual(payload["articles_per_ticker"], pn.config.NEWS_ARTICLES_PER_TICKER)
                self.assertEqual(payload["window_hours"], 24)
                self.assertIn("fetched_at", payload)
            finally:
                pn.CACHE_DIR = orig_cache_dir


if __name__ == "__main__":
    unittest.main()
