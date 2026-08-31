# ================================================================
# EXECUTION AGENT — unit tests for the pure decision/builder logic.
#
# Covers exit-trigger decisions (TP/SL/EOD/time) and position
# construction from a fill. Order submission wrappers are thin SDK
# calls (mirroring alpacabot/orders.py's proven pattern) validated live
# once the market is open, not here -- same split as risk_manager.py's
# get_account_snapshot().
# ================================================================
import datetime
import unittest

from alpaca.trading.enums import OrderStatus

import execution_agent as ea
import risk_manager as rm


def _dt(hour, minute):
    return datetime.datetime(2026, 8, 31, hour, minute, tzinfo=datetime.timezone.utc)


class CheckPremiumExitTests(unittest.TestCase):
    def setUp(self):
        self.position = ea.PremiumPosition(
            ticker="NVDA", symbol="NVDA260831P00212500", qty=1,
            credit=2.00, entered_at=_dt(9, 41),
            take_profit_price=1.00, stop_loss_price=6.00,
        )

    def test_no_exit_when_nothing_triggers(self):
        reason = ea.check_premium_exit(self.position, now=_dt(10, 0), current_bid=1.50, stop_order_status=None)
        self.assertIsNone(reason)

    def test_take_profit_when_bid_at_or_below_target(self):
        reason = ea.check_premium_exit(self.position, now=_dt(10, 0), current_bid=1.00, stop_order_status=None)
        self.assertEqual(reason, "TP")

    def test_stop_loss_when_standing_stop_order_filled(self):
        reason = ea.check_premium_exit(
            self.position, now=_dt(10, 0), current_bid=5.50, stop_order_status=OrderStatus.FILLED,
        )
        self.assertEqual(reason, "SL")

    def test_eod_when_past_close_time_even_if_nothing_else_triggered(self):
        reason = ea.check_premium_exit(self.position, now=_dt(15, 46), current_bid=1.50, stop_order_status=None)
        self.assertEqual(reason, "EOD")

    def test_stop_loss_takes_priority_over_take_profit_if_somehow_both_true(self):
        # Shouldn't happen in practice (bid can't be both <= TP and the stop
        # simultaneously filled at a much higher price), but priority order
        # should still be deterministic: a filled stop is a fact already
        # happened, so it wins over a bid reading.
        reason = ea.check_premium_exit(
            self.position, now=_dt(10, 0), current_bid=1.00, stop_order_status=OrderStatus.FILLED,
        )
        self.assertEqual(reason, "SL")

    def test_missing_quote_does_not_crash_or_falsely_trigger(self):
        reason = ea.check_premium_exit(self.position, now=_dt(10, 0), current_bid=None, stop_order_status=None)
        self.assertIsNone(reason)


class CheckDirectionalExitTests(unittest.TestCase):
    def setUp(self):
        self.position = ea.DirectionalPosition(
            ticker="META", symbol="META260831C00577500", qty=2,
            entry_price=4.00, entered_at=_dt(9, 41),
        )

    def test_no_exit_before_time_cutoff(self):
        reason = ea.check_directional_exit(self.position, now=_dt(14, 0))
        self.assertIsNone(reason)

    def test_time_exit_at_cutoff(self):
        reason = ea.check_directional_exit(self.position, now=_dt(14, 30))
        self.assertEqual(reason, "TIME")

    def test_time_exit_after_cutoff(self):
        reason = ea.check_directional_exit(self.position, now=_dt(15, 0))
        self.assertEqual(reason, "TIME")


class BuildPremiumPositionTests(unittest.TestCase):
    def test_tp_sl_derived_from_actual_fill_not_the_pretrade_estimate(self):
        # Risk Manager estimated credit_price=1.20 pre-trade; the real fill
        # came back at 1.35 -- TP/SL must be computed off the real fill.
        decision = rm.Decision(
            ticker="NVDA", approved=True, reason="approved", symbol="NVDA260831P00212500",
            strike=212.5, quantity=1, capital_allocated=21_250,
            take_profit_price=0.60, stop_loss_price=3.60,  # Risk Manager's pre-trade estimate
        )
        position = ea.build_premium_position(decision, fill_price=1.35, entered_at=_dt(9, 41))
        self.assertEqual(position.credit, 1.35)
        self.assertAlmostEqual(position.take_profit_price, 0.675)  # 50% of the REAL fill
        self.assertAlmostEqual(position.stop_loss_price, 4.05)     # 3x the REAL fill
        self.assertEqual(position.symbol, "NVDA260831P00212500")
        self.assertEqual(position.qty, 1)


class BuildDirectionalPositionTests(unittest.TestCase):
    def test_builds_from_decision_and_fill(self):
        decision = rm.Decision(
            ticker="META", approved=True, reason="approved", symbol="META260831C00577500",
            quantity=2, capital_allocated=890,
        )
        position = ea.build_directional_position(decision, fill_price=4.15, entered_at=_dt(9, 41))
        self.assertEqual(position.ticker, "META")
        self.assertEqual(position.entry_price, 4.15)
        self.assertEqual(position.qty, 2)


class FormatEntryLineTests(unittest.TestCase):
    # AAPL TP mismatch investigation (2026-08-31 post-mortem): the
    # pre-trade risk-decisions.json estimate ($0.09 TP) is a different
    # number from the real TP threshold execution_agent.py actually
    # armed off the real fill ($0.27 -> $0.135 TP). That real threshold
    # was never logged anywhere, so an auditor had no way to tell the two
    # apart. This locks the entry log line down so it now is.
    def test_includes_real_tp_and_sl_for_a_premium_entry(self):
        line = ea._format_entry_line(
            "AAPL", "AAPL260831P00312500", 1, 0.27, _dt(10, 35), "SELL_TO_OPEN",
            take_profit_price=0.135, stop_loss_price=0.81,
        )
        self.assertEqual(
            line,
            "ENTRY 2026-08-31T10:35:00+00:00 AAPL AAPL260831P00312500 "
            "qty=1 price=0.27 side=SELL_TO_OPEN tp=0.14 sl=0.81",
        )

    def test_omits_tp_sl_fields_for_a_directional_entry(self):
        # Directional positions have no TP/SL at all (spec section 18) --
        # the log line must not gain stray fields for that side.
        line = ea._format_entry_line(
            "META", "META260831C00577500", 2, 4.15, _dt(9, 41), "BUY_TO_OPEN",
        )
        self.assertEqual(
            line,
            "ENTRY 2026-08-31T09:41:00+00:00 META META260831C00577500 "
            "qty=2 price=4.15 side=BUY_TO_OPEN",
        )


class FormatDecisionsLoadedLineTests(unittest.TestCase):
    # Second gap from the same post-mortem: with no record of which
    # risk-decisions-<date>.json file (and version) was actually
    # consumed, matching a live trade back to its decision file took
    # manual cross-referencing of contract symbols/quantities across
    # multiple file versions.
    def test_includes_path_and_generated_at_when_present(self):
        line = ea._format_decisions_loaded_line(
            _dt(10, 30), "risk-decisions-2026-08-31.json",
            {"generated_at": "2026-08-31T10:29:03-04:00"},
        )
        self.assertEqual(
            line,
            "[2026-08-31T10:30:00+00:00] Loaded decisions from "
            "risk-decisions-2026-08-31.json (generated_at=2026-08-31T10:29:03-04:00)",
        )

    def test_defaults_generated_at_when_missing_from_older_files(self):
        line = ea._format_decisions_loaded_line(
            _dt(10, 30), "risk-decisions-2026-08-31.json", {},
        )
        self.assertEqual(
            line,
            "[2026-08-31T10:30:00+00:00] Loaded decisions from "
            "risk-decisions-2026-08-31.json (generated_at=unknown)",
        )


if __name__ == "__main__":
    unittest.main()
