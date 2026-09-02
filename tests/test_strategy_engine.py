# ================================================================
# STRATEGY ENGINE — unit tests for the deterministic bits added on
# 2026-08-31 in response to two real audit gaps (see PROGRESS.md /
# NEXTSTEPS.md and the Strategist Agent post-mortems that flagged them):
#
#  1. The --json ranking snapshot used to only go to stdout -- nothing
#     forced it to a file, so a real live run's numbers were lost.
#     _write_ranking_snapshot/_build_json_payload cover the persistence
#     + run_id behavior.
#  2. _fetch_liquid_chain silently fell back to the local Black-Scholes
#     solver with no record of which source (Alpaca vs. local) was
#     actually used per ticker on a given run. greeks_source tests cover
#     all three outcomes: alpaca / black_scholes_fallback / unavailable.
#
# Live network calls (chain fetch, spot price) are mocked the same way
# tests/test_risk_manager.py mocks module-level functions/clients --
# patch.object() on the module, not a new mocking framework. No
# live-network tests here, same split as strategy_engine.py itself
# (validated live / via mock_cache fixture, not in this file).
# ================================================================
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

import strategy_engine as se


class WriteRankingSnapshotTests(unittest.TestCase):
    def test_writes_payload_to_ranking_dated_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = {"run_id": "2026-08-31T09:41:00-04:00", "ranked": []}
            path = se._write_ranking_snapshot(payload, "2026-08-31", cache_dir=tmp)
            self.assertEqual(path, os.path.join(tmp, "ranking-2026-08-31.json"))
            with open(path) as f:
                written = json.load(f)
            self.assertEqual(written, payload)

    def test_creates_cache_dir_if_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = os.path.join(tmp, "nested", "cache")
            self.assertFalse(os.path.isdir(nested))
            se._write_ranking_snapshot({"run_id": "x"}, "2026-08-31", cache_dir=nested)
            self.assertTrue(os.path.isfile(os.path.join(nested, "ranking-2026-08-31.json")))

    def test_uses_the_ranking_cache_path_naming_convention(self):
        self.assertEqual(
            se._ranking_cache_path("2026-09-01", cache_dir="logs/cache"),
            "logs/cache/ranking-2026-09-01.json",
        )


class BuildJsonPayloadTests(unittest.TestCase):
    def test_includes_run_id(self):
        payload = se._build_json_payload([], [], "2026-08-31", [], [], run_id="RUN-123")
        self.assertEqual(payload["run_id"], "RUN-123")

    def test_includes_ranked_and_skipped_with_greeks_source(self):
        ranked_candidate = se.RankedCandidate(
            ticker="SPY", spot=600.0, expiration="2026-08-31", atm_strike=600.0,
            atm_call_iv=0.2, atm_put_iv=0.2, atm_iv=0.2,
            atm_call_symbol="SPY260831C00600000", atm_call_ask=1.0,
            atm_put_symbol="SPY260831P00600000", atm_put_ask=1.0,
            put_15d_symbol="SPY260831P00590000", put_15d_strike=590.0,
            put_15d_delta=-0.15, put_15d_iv=0.25, put_15d_bid=0.5,
            iv_skew=0.05, greeks_source="alpaca",
        )
        skipped_ticker = se.SkippedTicker(ticker="TSLA", reason="no liquid chain", greeks_source="unavailable")

        payload = se._build_json_payload(
            [ranked_candidate], [skipped_ticker], "2026-08-31",
            [ranked_candidate], [], run_id="RUN-123",
        )

        self.assertEqual(payload["ranked"][0]["ticker"], "SPY")
        self.assertEqual(payload["ranked"][0]["greeks_source"], "alpaca")
        self.assertEqual(payload["skipped"][0]["ticker"], "TSLA")
        self.assertEqual(payload["skipped"][0]["greeks_source"], "unavailable")
        self.assertEqual(payload["premium_sell"], ["SPY"])
        self.assertEqual(payload["directional"], [])


def _snapshot(bid, ask, delta=None, iv=None):
    """Fakes the shape of an Alpaca OptionsSnapshot enough for
    _fetch_liquid_chain's attribute access (latest_quote.bid_price/
    ask_price, greeks.delta, implied_volatility)."""
    import types
    return types.SimpleNamespace(
        latest_quote=types.SimpleNamespace(bid_price=bid, ask_price=ask),
        greeks=types.SimpleNamespace(delta=delta),
        implied_volatility=iv,
    )


class FetchLiquidChainGreeksSourceTests(unittest.TestCase):
    def test_source_is_alpaca_when_alpaca_greeks_present(self):
        chain = {
            "SPY260831C00600000": _snapshot(1.0, 1.2, delta=0.5, iv=0.20),
            "SPY260831P00600000": _snapshot(1.0, 1.2, delta=-0.5, iv=0.22),
        }
        with patch.object(se, "_option_client") as mock_client, \
             patch.object(se, "_local_greeks") as mock_local:
            mock_client.get_option_chain.return_value = chain
            calls, puts, source = se._fetch_liquid_chain("SPY", 600.0, "2026-08-31")

        mock_local.assert_not_called()
        self.assertEqual(source, "alpaca")
        self.assertEqual(calls[600.0], (0.5, 0.20, "SPY260831C00600000", 1.0, 1.2))
        self.assertEqual(puts[600.0], (-0.5, 0.22, "SPY260831P00600000", 1.0, 1.2))

    def test_source_is_black_scholes_fallback_when_alpaca_greeks_null(self):
        chain = {
            "SPY260831C00600000": _snapshot(1.0, 1.2, delta=None, iv=None),
        }
        with patch.object(se, "_option_client") as mock_client, \
             patch.object(se, "_local_greeks", return_value=(0.48, 0.21)) as mock_local:
            mock_client.get_option_chain.return_value = chain
            calls, puts, source = se._fetch_liquid_chain("SPY", 600.0, "2026-08-31")

        mock_local.assert_called_once()
        self.assertEqual(source, "black_scholes_fallback")
        self.assertEqual(calls[600.0], (0.48, 0.21, "SPY260831C00600000", 1.0, 1.2))

    def test_source_is_unavailable_when_neither_alpaca_nor_local_solve_works(self):
        chain = {
            "SPY260831C00600000": _snapshot(0.0, 0.0, delta=None, iv=None),
        }
        with patch.object(se, "_option_client") as mock_client, \
             patch.object(se, "_local_greeks", return_value=None):
            mock_client.get_option_chain.return_value = chain
            calls, puts, source = se._fetch_liquid_chain("SPY", 600.0, "2026-08-31")

        self.assertEqual(source, "unavailable")
        self.assertEqual(calls, {})
        self.assertEqual(puts, {})

    def test_logs_a_greeks_feed_line_per_ticker_to_stderr(self):
        # stderr, not stdout -- --json's stdout must stay pure JSON for a
        # caller that redirects it straight to a file (Change 1).
        chain = {
            "QQQ260831C00500000": _snapshot(1.0, 1.2, delta=0.5, iv=0.20),
        }
        buf = io.StringIO()
        with patch.object(se, "_option_client") as mock_client:
            mock_client.get_option_chain.return_value = chain
            with redirect_stderr(buf):
                se._fetch_liquid_chain("QQQ", 500.0, "2026-08-31")

        self.assertIn("GREEKS_FEED ticker=QQQ source=alpaca", buf.getvalue())


class RankTickerGreeksSourcePropagationTests(unittest.TestCase):
    def test_ranked_candidate_carries_greeks_source(self):
        with patch.object(se, "_get_spot_price", return_value=600.0), \
             patch.object(se, "_fetch_liquid_chain", return_value=(
                 {600.0: (0.5, 0.20, "SPY260831C00600000", 1.0, 1.2)},
                 {600.0: (-0.5, 0.22, "SPY260831P00600000", 1.0, 1.2),
                  590.0: (-0.15, 0.25, "SPY260831P00590000", 0.4, 0.5)},
                 "black_scholes_fallback",
             )):
            result = se.rank_ticker("SPY", "2026-08-31")

        self.assertIsInstance(result, se.RankedCandidate)
        self.assertEqual(result.greeks_source, "black_scholes_fallback")

    def test_skipped_ticker_carries_greeks_source_when_chain_empty(self):
        with patch.object(se, "_get_spot_price", return_value=600.0), \
             patch.object(se, "_fetch_liquid_chain", return_value=({}, {}, "unavailable")):
            result = se.rank_ticker("SPY", "2026-08-31")

        self.assertIsInstance(result, se.SkippedTicker)
        self.assertEqual(result.greeks_source, "unavailable")


class NextTradingDayTests(unittest.TestCase):
    """_next_trading_day() -- the no-0dte-fallback-policy decision (2026-09-02):
    when a ticker has no same-day 0DTE chain, strategy_engine falls back to the
    next trading day's chain instead of skipping outright. Position lifecycle
    is unchanged (same-day force-close still applies, per execution_agent.py --
    this only changes which expiration gets ranked/sized/traded)."""

    def test_returns_first_calendar_date_after_the_given_date(self):
        import types
        calendar = [
            types.SimpleNamespace(date=__import__("datetime").date(2026, 9, 2)),
            types.SimpleNamespace(date=__import__("datetime").date(2026, 9, 3)),
        ]
        with patch.object(se, "_trading_client") as mock_client:
            mock_client.get_calendar.return_value = calendar
            result = se._next_trading_day("2026-09-02")

        self.assertEqual(result, "2026-09-03")

    def test_skips_weekend_via_whatever_the_calendar_api_returns(self):
        # 2026-09-04 is a Friday; calendar naturally omits the weekend, so
        # the next real trading day is Monday 2026-09-07 -- no local
        # weekday/holiday math needed, the Alpaca calendar is authoritative.
        import types
        calendar = [
            types.SimpleNamespace(date=__import__("datetime").date(2026, 9, 4)),
            types.SimpleNamespace(date=__import__("datetime").date(2026, 9, 7)),
        ]
        with patch.object(se, "_trading_client") as mock_client:
            mock_client.get_calendar.return_value = calendar
            result = se._next_trading_day("2026-09-04")

        self.assertEqual(result, "2026-09-07")

    def test_returns_none_when_calendar_has_no_later_date(self):
        import types
        calendar = [types.SimpleNamespace(date=__import__("datetime").date(2026, 9, 2))]
        with patch.object(se, "_trading_client") as mock_client:
            mock_client.get_calendar.return_value = calendar
            result = se._next_trading_day("2026-09-02")

        self.assertIsNone(result)


class RankTicker1DTEFallbackTests(unittest.TestCase):
    """rank_ticker(ticker, expiration, fallback_expiration=...) -- retries
    against the next trading day's chain when today's (0DTE) chain isn't
    usable, instead of skipping the ticker outright."""

    def test_no_fallback_given_behaves_exactly_as_before(self):
        with patch.object(se, "_get_spot_price", return_value=600.0), \
             patch.object(se, "_fetch_liquid_chain", return_value=({}, {}, "unavailable")):
            result = se.rank_ticker("SPY", "2026-09-02")

        self.assertIsInstance(result, se.SkippedTicker)

    def test_falls_back_to_next_trading_day_when_primary_chain_empty(self):
        primary_chain = ({}, {}, "unavailable")
        fallback_chain = (
            {600.0: (0.5, 0.20, "SPY260903C00600000", 1.0, 1.2)},
            {600.0: (-0.5, 0.22, "SPY260903P00600000", 1.0, 1.2),
             590.0: (-0.15, 0.25, "SPY260903P00590000", 0.4, 0.5)},
            "alpaca",
        )
        with patch.object(se, "_get_spot_price", return_value=600.0), \
             patch.object(se, "_fetch_liquid_chain", side_effect=[primary_chain, fallback_chain]):
            result = se.rank_ticker("SPY", "2026-09-02", fallback_expiration="2026-09-03")

        self.assertIsInstance(result, se.RankedCandidate)
        self.assertEqual(result.expiration, "2026-09-03")

    def test_falls_back_when_primary_chain_has_no_common_atm_strike(self):
        primary_chain = (
            {610.0: (0.5, 0.20, "SPY260902C00610000", 1.0, 1.2)},  # no matching put strike
            {600.0: (-0.5, 0.22, "SPY260902P00600000", 1.0, 1.2)},
            "alpaca",
        )
        fallback_chain = (
            {600.0: (0.5, 0.20, "SPY260903C00600000", 1.0, 1.2)},
            {600.0: (-0.5, 0.22, "SPY260903P00600000", 1.0, 1.2),
             590.0: (-0.15, 0.25, "SPY260903P00590000", 0.4, 0.5)},
            "alpaca",
        )
        with patch.object(se, "_get_spot_price", return_value=600.0), \
             patch.object(se, "_fetch_liquid_chain", side_effect=[primary_chain, fallback_chain]):
            result = se.rank_ticker("SPY", "2026-09-02", fallback_expiration="2026-09-03")

        self.assertIsInstance(result, se.RankedCandidate)
        self.assertEqual(result.expiration, "2026-09-03")

    def test_skipped_when_both_primary_and_fallback_chains_empty(self):
        with patch.object(se, "_get_spot_price", return_value=600.0), \
             patch.object(se, "_fetch_liquid_chain", return_value=({}, {}, "unavailable")):
            result = se.rank_ticker("SPY", "2026-09-02", fallback_expiration="2026-09-03")

        self.assertIsInstance(result, se.SkippedTicker)
        self.assertIn("2026-09-02", result.reason)
        self.assertIn("2026-09-03", result.reason)

    def test_no_retry_when_spot_price_itself_is_invalid(self):
        # Spot-quote failure isn't expiration-related -- retrying against a
        # different expiration can't fix it, so _fetch_liquid_chain should
        # never even be called.
        with patch.object(se, "_get_spot_price", return_value=0.0), \
             patch.object(se, "_fetch_liquid_chain") as mock_fetch:
            result = se.rank_ticker("SPY", "2026-09-02", fallback_expiration="2026-09-03")

        mock_fetch.assert_not_called()
        self.assertIsInstance(result, se.SkippedTicker)

    def test_logs_fallback_used_line_to_stderr(self):
        primary_chain = ({}, {}, "unavailable")
        fallback_chain = (
            {600.0: (0.5, 0.20, "SPY260903C00600000", 1.0, 1.2)},
            {600.0: (-0.5, 0.22, "SPY260903P00600000", 1.0, 1.2),
             590.0: (-0.15, 0.25, "SPY260903P00590000", 0.4, 0.5)},
            "alpaca",
        )
        buf = io.StringIO()
        with patch.object(se, "_get_spot_price", return_value=600.0), \
             patch.object(se, "_fetch_liquid_chain", side_effect=[primary_chain, fallback_chain]):
            with redirect_stderr(buf):
                se.rank_ticker("SPY", "2026-09-02", fallback_expiration="2026-09-03")

        self.assertIn("EXPIRATION_FALLBACK ticker=SPY primary=2026-09-02 fallback=2026-09-03", buf.getvalue())


class RankUniverseFallbackWiringTests(unittest.TestCase):
    def test_rank_universe_computes_and_passes_fallback_expiration_to_rank_ticker(self):
        with patch.object(se, "_next_trading_day", return_value="2026-09-03") as mock_next, \
             patch.object(se, "rank_ticker") as mock_rank:
            mock_rank.return_value = se.SkippedTicker("SPY", "no valid spot quote")
            se.rank_universe(universe=["SPY"], expiration="2026-09-02")

        mock_next.assert_called_once_with("2026-09-02")
        mock_rank.assert_called_once_with("SPY", "2026-09-02", fallback_expiration="2026-09-03")


if __name__ == "__main__":
    unittest.main()
