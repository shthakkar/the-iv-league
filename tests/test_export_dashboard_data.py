# ================================================================
# DASHBOARD EXPORT — unit tests for the pure log-parsing/aggregation
# logic (spec §29 / docs/superpowers/specs/2026-08-31-hackathon-
# dashboard-design.md §3.1).
#
# Uses the real logs/2026-08-31-execution.log as its fixture rather
# than a synthetic one, on purpose: that log's known real outcome
# (net -$3,927 = +$22 TSLA, +$11 AAPL, -$3,960 SPY -- see PROGRESS.md's
# "First live trading day" entry) is exactly the correctness check for
# the P&L formula, not just a shape check. The live account-snapshot
# call (get_account_snapshot(), reused from risk_manager.py) isn't
# covered here -- same convention as risk_manager.py's own tests.
# ================================================================
import pathlib
import unittest

import export_dashboard_data as ex

REAL_LOG = pathlib.Path(__file__).resolve().parent.parent / "logs" / "2026-08-31-execution.log"


class OptionTypeFromSymbolTests(unittest.TestCase):
    def test_put_symbol(self):
        self.assertEqual(ex.option_type_from_symbol("SPY260831P00765000"), "P")

    def test_call_symbol(self):
        self.assertEqual(ex.option_type_from_symbol("TSLA260831C00357500"), "C")


class ParseExecutionLogTests(unittest.TestCase):
    def test_parses_real_log(self):
        entries, exits = ex.parse_execution_log(REAL_LOG)
        self.assertEqual(len(entries), 3)
        self.assertEqual(len(exits), 3)

        tsla_entry = next(e for e in entries if e["ticker"] == "TSLA")
        self.assertEqual(tsla_entry["symbol"], "TSLA260831P00357500")
        self.assertEqual(tsla_entry["side"], "SELL_TO_OPEN")
        self.assertEqual(tsla_entry["quantity"], 1)
        self.assertEqual(tsla_entry["price"], 0.50)

        spy_exit = next(e for e in exits if e["ticker"] == "SPY")
        self.assertEqual(spy_exit["reason"], "TIME")
        self.assertEqual(spy_exit["price"], 0.14)


class ReconstructTradesTests(unittest.TestCase):
    def setUp(self):
        entries, exits = ex.parse_execution_log(REAL_LOG)
        self.trades = ex.reconstruct_trades(entries, exits, date="2026-08-31")
        self.by_ticker = {t["ticker"]: t for t in self.trades}

    def test_three_trades_reconstructed(self):
        self.assertEqual(len(self.trades), 3)

    def test_tsla_short_put_pnl(self):
        t = self.by_ticker["TSLA"]
        self.assertEqual(t["strategy"], "premium-selling")
        self.assertEqual(t["side"], "SHORT_PUT")
        self.assertEqual(t["status"], "CLOSED")
        self.assertEqual(t["exit_reason"], "take_profit")
        self.assertAlmostEqual(t["pnl"], 22.00, places=2)

    def test_aapl_short_put_pnl(self):
        t = self.by_ticker["AAPL"]
        self.assertEqual(t["strategy"], "premium-selling")
        self.assertAlmostEqual(t["pnl"], 11.00, places=2)

    def test_spy_long_put_pnl(self):
        t = self.by_ticker["SPY"]
        self.assertEqual(t["strategy"], "directional")
        self.assertEqual(t["side"], "LONG_PUT")
        self.assertEqual(t["exit_reason"], "time_exit_1430")
        self.assertAlmostEqual(t["pnl"], -3960.00, places=2)

    def test_trade_id_is_date_plus_symbol(self):
        t = self.by_ticker["TSLA"]
        self.assertEqual(t["id"], "2026-08-31-TSLA260831P00357500")

    def test_open_position_has_no_exit(self):
        entries = [{
            "timestamp": "2026-08-31T10:00:00-04:00", "ticker": "NVDA",
            "symbol": "NVDA260831P00200000", "quantity": 1, "price": 1.00,
            "side": "SELL_TO_OPEN",
        }]
        trades = ex.reconstruct_trades(entries, [], date="2026-08-31")
        self.assertEqual(trades[0]["status"], "OPEN")
        self.assertIsNone(trades[0]["exit_price"])
        self.assertIsNone(trades[0]["pnl"])
        self.assertIsNone(trades[0]["exit_reason"])


class BuildDailySummaryTests(unittest.TestCase):
    def test_real_day_summary(self):
        entries, exits = ex.parse_execution_log(REAL_LOG)
        trades = ex.reconstruct_trades(entries, exits, date="2026-08-31")
        summary = ex.build_daily_summary(trades, date="2026-08-31")
        self.assertEqual(summary["date"], "2026-08-31")
        self.assertAlmostEqual(summary["pnl"], -3927.00, places=2)
        self.assertEqual(summary["trades_count"], 3)
        # 2 of 3 closed trades (TSLA, AAPL) were profitable.
        self.assertAlmostEqual(summary["win_rate"], 2 / 3, places=4)


class BuildStrategyStatsTests(unittest.TestCase):
    def test_real_day_stats_by_strategy(self):
        entries, exits = ex.parse_execution_log(REAL_LOG)
        trades = ex.reconstruct_trades(entries, exits, date="2026-08-31")
        stats = {s["strategy"]: s for s in ex.build_strategy_stats(trades)}

        premium = stats["premium-selling"]
        self.assertEqual(premium["trades"], 2)
        self.assertEqual(premium["wins"], 2)
        self.assertEqual(premium["losses"], 0)
        self.assertAlmostEqual(premium["win_rate"], 1.0, places=4)
        self.assertAlmostEqual(premium["total_pnl"], 33.00, places=2)

        directional = stats["directional"]
        self.assertEqual(directional["trades"], 1)
        self.assertEqual(directional["wins"], 0)
        self.assertEqual(directional["losses"], 1)
        self.assertAlmostEqual(directional["win_rate"], 0.0, places=4)
        self.assertAlmostEqual(directional["total_pnl"], -3960.00, places=2)


if __name__ == "__main__":
    unittest.main()
