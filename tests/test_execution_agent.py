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


if __name__ == "__main__":
    unittest.main()
