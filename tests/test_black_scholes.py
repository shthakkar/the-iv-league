import datetime
import unittest
from zoneinfo import ZoneInfo

import black_scholes as bs

ET = ZoneInfo("America/New_York")


class PriceAndPutCallParityTests(unittest.TestCase):
    def test_put_call_parity_holds(self):
        """C - P = S - K*e^(-rT) for the same inputs -- a strong, model-independent
        correctness check on the price formulas themselves."""
        S, K, T, r, sigma = 100.0, 105.0, 0.25, 0.04, 0.30
        call = bs.bs_price(S, K, T, r, sigma, "C")
        put = bs.bs_price(S, K, T, r, sigma, "P")
        self.assertAlmostEqual(call - put, S - K * pow(2.718281828459045, -r * T), places=4)

    def test_call_price_increases_with_volatility(self):
        S, K, T, r = 100.0, 100.0, 0.1, 0.045
        low = bs.bs_price(S, K, T, r, 0.10, "C")
        high = bs.bs_price(S, K, T, r, 0.50, "C")
        self.assertGreater(high, low)


class DeltaTests(unittest.TestCase):
    def test_call_delta_in_zero_one_range(self):
        for K in (80.0, 100.0, 120.0):
            d = bs.bs_delta(100.0, K, 0.1, 0.045, 0.25, "C")
            self.assertGreater(d, 0.0)
            self.assertLess(d, 1.0)

    def test_put_delta_in_negative_one_zero_range(self):
        for K in (80.0, 100.0, 120.0):
            d = bs.bs_delta(100.0, K, 0.1, 0.045, 0.25, "P")
            self.assertGreater(d, -1.0)
            self.assertLess(d, 0.0)

    def test_call_and_put_delta_differ_by_one(self):
        """delta_call - delta_put == 1 for the same S/K/T/r/sigma (no dividends) --
        direct consequence of put-call parity, independent of the IV solver."""
        d_call = bs.bs_delta(100.0, 95.0, 0.05, 0.045, 0.35, "C")
        d_put = bs.bs_delta(100.0, 95.0, 0.05, 0.045, 0.35, "P")
        self.assertAlmostEqual(d_call - d_put, 1.0, places=6)


class ImpliedVolatilityRoundTripTests(unittest.TestCase):
    """Price a contract with a known sigma, then solve IV back out of that price --
    the actual thing we depend on, since real usage always starts from an observed
    market price, not a known sigma."""

    def test_roundtrip_call_normal_dte(self):
        S, K, T, r, true_sigma = 100.0, 102.0, 0.25, 0.045, 0.28
        price = bs.bs_price(S, K, T, r, true_sigma, "C")
        solved = bs.implied_volatility(price, S, K, T, r, "C")
        self.assertIsNotNone(solved)
        self.assertAlmostEqual(solved, true_sigma, places=4)

    def test_roundtrip_put_normal_dte(self):
        S, K, T, r, true_sigma = 100.0, 98.0, 0.25, 0.045, 0.22
        price = bs.bs_price(S, K, T, r, true_sigma, "P")
        solved = bs.implied_volatility(price, S, K, T, r, "P")
        self.assertIsNotNone(solved)
        self.assertAlmostEqual(solved, true_sigma, places=4)

    def test_roundtrip_near_atm_0dte_scale_time(self):
        """The actual use case: a few hours of T, not a full year -- this is where
        vega is smallest and the solve is most delicate."""
        S, K, T, r, true_sigma = 765.95, 770.0, 0.000706, 0.045, 0.2381
        price = bs.bs_price(S, K, T, r, true_sigma, "C")
        solved = bs.implied_volatility(price, S, K, T, r, "C")
        self.assertIsNotNone(solved)
        self.assertAlmostEqual(solved, true_sigma, places=3)

    def test_roundtrip_deep_otm_small_time_still_converges_or_declines_gracefully(self):
        """Deep OTM + tiny T is the worst-conditioned corner (vega near zero) --
        the literature's caveat region. Must not crash or return garbage; either a
        plausible IV or a clean None is acceptable."""
        S, K, T, r, true_sigma = 100.0, 150.0, 0.000706, 0.045, 0.30
        price = bs.bs_price(S, K, T, r, true_sigma, "C")
        solved = bs.implied_volatility(price, S, K, T, r, "C")
        if solved is not None:
            self.assertGreater(solved, 0.0)
            self.assertLess(solved, 10.0)

    def test_implausible_price_returns_none_not_garbage(self):
        """A price above the S-K forward bound is a pure arbitrage violation --
        no sigma reproduces it. Must fail closed (None), never raise or return a
        nonsense value."""
        solved = bs.implied_volatility(price=200.0, S=100.0, K=100.0, T=0.1, r=0.045, right="C")
        self.assertIsNone(solved)

    def test_zero_or_negative_price_returns_none(self):
        self.assertIsNone(bs.implied_volatility(price=0.0, S=100.0, K=100.0, T=0.1, r=0.045, right="C"))
        self.assertIsNone(bs.implied_volatility(price=-1.0, S=100.0, K=100.0, T=0.1, r=0.045, right="C"))


class TimeToExpirationTests(unittest.TestCase):
    def test_normal_intraday_gap_matches_manual_calc(self):
        now = datetime.datetime(2026, 8, 31, 9, 49, tzinfo=ET)
        expiration = datetime.date(2026, 8, 31)
        T = bs.time_to_expiration_years(now, expiration)
        expected_hours = 6.183333  # 9:49 -> 16:00
        self.assertAlmostEqual(T * 365 * 24, expected_hours, places=2)

    def test_floors_instead_of_hitting_zero_right_at_close(self):
        now = datetime.datetime(2026, 8, 31, 15, 59, 59, tzinfo=ET)
        expiration = datetime.date(2026, 8, 31)
        T = bs.time_to_expiration_years(now, expiration)
        self.assertGreaterEqual(T, bs.MIN_T_YEARS)

    def test_floors_instead_of_going_negative_after_close(self):
        """Querying after the close (e.g. a delayed/late run) must never produce a
        negative or zero T -- that's exactly Alpaca's own division-by-zero bug."""
        now = datetime.datetime(2026, 8, 31, 16, 5, 0, tzinfo=ET)
        expiration = datetime.date(2026, 8, 31)
        T = bs.time_to_expiration_years(now, expiration)
        self.assertEqual(T, bs.MIN_T_YEARS)


if __name__ == "__main__":
    unittest.main()
