# ================================================================
# RISK MANAGER — unit tests for the pure sizing/allocation logic.
#
# Covers everything that doesn't touch the network: budget split,
# CSP position sizing (including the SPY-style "too expensive for its
# equal share" case), directional sizing, and the max-positions trim.
# get_account_snapshot()/chain-fetch/evaluate() glue is validated the
# same way strategy_engine.py and directional_selection.py already are
# (live run / mock_cache fixture), not here.
# ================================================================
import unittest
from unittest.mock import patch

import risk_manager as rm
import strategy_engine as se


def _ranked(ticker, atm_strike, atm_call_symbol, atm_call_ask, atm_put_symbol, atm_put_ask,
            put_15d_strike, put_15d_symbol, put_15d_bid):
    """Builds a real strategy_engine.RankedCandidate with only the fields these
    tests care about populated meaningfully -- everything else is a placeholder,
    since build_premium_candidates/build_directional_candidates don't read it."""
    return se.RankedCandidate(
        ticker=ticker, spot=atm_strike, expiration="2026-08-31", atm_strike=atm_strike,
        atm_call_iv=0.2, atm_put_iv=0.2, atm_iv=0.2,
        atm_call_symbol=atm_call_symbol, atm_call_ask=atm_call_ask,
        atm_put_symbol=atm_put_symbol, atm_put_ask=atm_put_ask,
        put_15d_symbol=put_15d_symbol, put_15d_strike=put_15d_strike,
        put_15d_delta=-0.15, put_15d_iv=0.2, put_15d_bid=put_15d_bid,
        iv_skew=0.01,
    )


class ComputeBudgetsTests(unittest.TestCase):
    def test_splits_95_5_off_options_buying_power(self):
        snapshot = rm.AccountSnapshot(cash=100_000, options_buying_power=100_000, equity=100_000)
        budgets = rm.compute_budgets(snapshot)
        self.assertAlmostEqual(budgets.premium_sell_budget, 95_000)
        self.assertAlmostEqual(budgets.directional_budget, 5_000)

    def test_falls_back_to_cash_when_options_buying_power_is_zero(self):
        snapshot = rm.AccountSnapshot(cash=50_000, options_buying_power=0, equity=50_000)
        budgets = rm.compute_budgets(snapshot)
        self.assertAlmostEqual(budgets.premium_sell_budget, 47_500)
        self.assertAlmostEqual(budgets.directional_budget, 2_500)


class AllocatePremiumPositionsTests(unittest.TestCase):
    def test_equal_split_when_all_affordable(self):
        # Real numbers from PROGRESS.md's validated run.
        candidates = [
            rm.PremiumCandidate(ticker="NVDA", symbol="NVDA260831P00212500", strike=212.5, credit_price=1.20),
            rm.PremiumCandidate(ticker="AMZN", symbol="AMZN260831P00260000", strike=260.0, credit_price=1.10),
            rm.PremiumCandidate(ticker="AAPL", symbol="AAPL260831P00315000", strike=315.0, credit_price=1.05),
        ]
        decisions = rm.allocate_premium_positions(candidates, budget=95_000)
        self.assertEqual([d.ticker for d in decisions], ["NVDA", "AMZN", "AAPL"])
        self.assertTrue(all(d.approved for d in decisions))
        self.assertEqual([d.quantity for d in decisions], [1, 1, 1])
        self.assertAlmostEqual(decisions[0].capital_allocated, 21_250)
        self.assertAlmostEqual(decisions[1].capital_allocated, 26_000)
        self.assertAlmostEqual(decisions[2].capital_allocated, 31_500)

    def test_too_expensive_candidate_rejected_gracefully_not_forced(self):
        # The SPY problem: equal share can't cover it, and neither can the
        # pooled leftover from the cheaper names that did fit.
        candidates = [
            rm.PremiumCandidate(ticker="NVDA", symbol="NVDA260831P00212500", strike=212.5, credit_price=1.20),
            rm.PremiumCandidate(ticker="AMZN", symbol="AMZN260831P00260000", strike=260.0, credit_price=1.10),
            rm.PremiumCandidate(ticker="SPY", symbol="SPY260831P00764000", strike=764.0, credit_price=3.98),
        ]
        decisions = rm.allocate_premium_positions(candidates, budget=95_000)
        by_ticker = {d.ticker: d for d in decisions}
        self.assertTrue(by_ticker["NVDA"].approved)
        self.assertEqual(by_ticker["NVDA"].quantity, 1)
        self.assertTrue(by_ticker["AMZN"].approved)
        self.assertEqual(by_ticker["AMZN"].quantity, 1)
        self.assertFalse(by_ticker["SPY"].approved)
        self.assertIn("764", by_ticker["SPY"].reason)  # names its own strike-driven cost, not a generic message

    def test_leftover_from_cheap_names_rescues_one_that_missed_its_own_share(self):
        # target share = 101,500 / 3 = 33,833.33 each.
        # NVDA: floor(33833/3000) = 11 contracts = 33,000, leftover += 833
        # QQQ:  same as NVDA, leftover += 833 (pooled leftover so far: 1,667)
        # AAPL: floor(33833/35000) = 0 on its own share -> retried against the
        #       pool: unused leftover (1,667) + AAPL's own unspent share
        #       (33,833) = 35,500, and floor(35500/35000) = 1 -> rescued.
        candidates = [
            rm.PremiumCandidate(ticker="NVDA", symbol="NVDA260831P00030000", strike=30.0, credit_price=1.0),
            rm.PremiumCandidate(ticker="QQQ", symbol="QQQ260831P00030000", strike=30.0, credit_price=1.0),
            rm.PremiumCandidate(ticker="AAPL", symbol="AAPL260831P00350000", strike=350.0, credit_price=1.0),
        ]
        decisions = rm.allocate_premium_positions(candidates, budget=101_500)
        by_ticker = {d.ticker: d for d in decisions}
        self.assertTrue(by_ticker["AAPL"].approved, by_ticker["AAPL"].reason)
        self.assertEqual(by_ticker["AAPL"].quantity, 1)

    def test_take_profit_and_stop_loss_derived_from_credit(self):
        candidates = [rm.PremiumCandidate(ticker="NVDA", symbol="NVDA260831P00212500", strike=212.5, credit_price=2.00)]
        decisions = rm.allocate_premium_positions(candidates, budget=95_000)
        d = decisions[0]
        self.assertAlmostEqual(d.take_profit_price, 1.00)  # 50% of credit
        self.assertAlmostEqual(d.stop_loss_price, 6.00)    # 3x credit

    def test_max_exposure_cap_limits_a_single_name(self):
        candidates = [rm.PremiumCandidate(ticker="NVDA", symbol="NVDA260831P00010000", strike=10.0, credit_price=0.5)]
        decisions = rm.allocate_premium_positions(candidates, budget=95_000, max_exposure_per_underlying=35_000)
        d = decisions[0]
        self.assertTrue(d.approved)
        self.assertLessEqual(d.capital_allocated, 35_000)
        self.assertEqual(d.quantity, 35)  # floor(35,000 / (10.0 * 100))

    def test_empty_candidates_returns_empty(self):
        self.assertEqual(rm.allocate_premium_positions([], budget=95_000), [])


class SizeDirectionalPositionsTests(unittest.TestCase):
    def test_equal_split_across_selected(self):
        candidates = [
            rm.DirectionalCandidate(ticker="META", symbol="META260828C00577500", ask_price=4.00),
            rm.DirectionalCandidate(ticker="MSFT", symbol="MSFT260828C00512500", ask_price=3.00),
        ]
        decisions = rm.size_directional_positions(candidates, budget=5_000)
        by_ticker = {d.ticker: d for d in decisions}
        # share = 2,500 each. META: floor(2500/400)=6. MSFT: floor(2500/300)=8.
        self.assertEqual(by_ticker["META"].quantity, 6)
        self.assertEqual(by_ticker["MSFT"].quantity, 8)
        self.assertIsNone(by_ticker["META"].take_profit_price)  # no TP/SL on the directional side (spec 18)
        self.assertIsNone(by_ticker["META"].stop_loss_price)

    def test_rejects_when_share_cant_cover_one_contract(self):
        candidates = [rm.DirectionalCandidate(ticker="SPY", symbol="SPY260828C00769000", ask_price=100.00)]
        decisions = rm.size_directional_positions(candidates, budget=5_000)
        self.assertFalse(decisions[0].approved)


class ApplyMaxPositionsTests(unittest.TestCase):
    def test_trims_overflow_by_priority_order(self):
        approved = [
            rm.Decision(ticker=t, approved=True, reason="approved", quantity=1)
            for t in ["NVDA", "AMZN", "AAPL", "META", "MSFT", "QQQ", "TSLA"]
        ]
        trimmed = rm.apply_max_positions(approved, max_positions=6)
        self.assertEqual(sum(1 for d in trimmed if d.approved), 6)
        self.assertFalse(trimmed[-1].approved)
        self.assertIn("MAX_POSITIONS", trimmed[-1].reason)

    def test_no_trim_when_under_the_limit(self):
        approved = [rm.Decision(ticker="NVDA", approved=True, reason="approved", quantity=1)]
        trimmed = rm.apply_max_positions(approved, max_positions=6)
        self.assertTrue(trimmed[0].approved)


class BuildPremiumCandidatesTests(unittest.TestCase):
    def test_pulls_the_15d_put_leg(self):
        ranked = [_ranked("NVDA", 217.5, "NVDA260831C00217500", 3.0, "NVDA260831P00217500", 3.1,
                           212.5, "NVDA260831P00212500", 1.20)]
        candidates = rm.build_premium_candidates(ranked)
        self.assertEqual(candidates, [
            rm.PremiumCandidate(ticker="NVDA", symbol="NVDA260831P00212500", strike=212.5, credit_price=1.20)
        ])


class BuildDirectionalCandidatesTests(unittest.TestCase):
    def setUp(self):
        self.lookup = {
            "META": _ranked("META", 577.5, "META260828C00577500", 4.00, "META260828P00577500", 4.10,
                             565.0, "META260828P00565000", 1.50),
            "MSFT": _ranked("MSFT", 512.5, "MSFT260828C00512500", 3.00, "MSFT260828P00512500", 3.10,
                             505.0, "MSFT260828P00505000", 1.10),
        }

    def test_bullish_picks_the_atm_call(self):
        selected = [{"ticker": "META", "direction": "BULLISH", "confidence": 60}]
        candidates = rm.build_directional_candidates(selected, self.lookup)
        self.assertEqual(candidates, [
            rm.DirectionalCandidate(ticker="META", symbol="META260828C00577500", ask_price=4.00)
        ])

    def test_bearish_picks_the_atm_put(self):
        selected = [{"ticker": "MSFT", "direction": "BEARISH", "confidence": 60}]
        candidates = rm.build_directional_candidates(selected, self.lookup)
        self.assertEqual(candidates, [
            rm.DirectionalCandidate(ticker="MSFT", symbol="MSFT260828P00512500", ask_price=3.10)
        ])


class EvaluateTests(unittest.TestCase):
    def test_ties_budgets_sizing_and_max_positions_together(self):
        premium_ranked = [
            _ranked("NVDA", 217.5, "NVDA260831C00217500", 3.0, "NVDA260831P00217500", 3.1,
                    212.5, "NVDA260831P00212500", 1.20),
        ]
        directional_lookup = {
            "META": _ranked("META", 577.5, "META260828C00577500", 4.00, "META260828P00577500", 4.10,
                             565.0, "META260828P00565000", 1.50),
        }
        directional_selected = [{"ticker": "META", "direction": "BULLISH", "confidence": 60}]
        snapshot = rm.AccountSnapshot(cash=100_000, options_buying_power=100_000, equity=100_000)

        result = rm.evaluate(premium_ranked, directional_lookup, directional_selected, snapshot=snapshot)

        self.assertAlmostEqual(result.budgets.premium_sell_budget, 95_000)
        self.assertAlmostEqual(result.budgets.directional_budget, 5_000)
        self.assertEqual(len(result.premium_decisions), 1)
        self.assertTrue(result.premium_decisions[0].approved)
        self.assertEqual(result.premium_decisions[0].ticker, "NVDA")
        self.assertEqual(len(result.directional_decisions), 1)
        self.assertTrue(result.directional_decisions[0].approved)
        self.assertEqual(result.directional_decisions[0].ticker, "META")

    def test_uses_live_account_snapshot_when_none_passed(self):
        with patch.object(rm, "get_account_snapshot", return_value=rm.AccountSnapshot(
            cash=100_000, options_buying_power=100_000, equity=100_000,
        )) as mock_snapshot:
            result = rm.evaluate([], {}, [])
            mock_snapshot.assert_called_once()
            self.assertAlmostEqual(result.budgets.premium_sell_budget, 95_000)


if __name__ == "__main__":
    unittest.main()
