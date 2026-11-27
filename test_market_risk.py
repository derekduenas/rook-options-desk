import sqlite3
import unittest
from decimal import Decimal
from math import sqrt

from advanced_risk import option_greeks
from market_risk import implied_vol, option_value, portfolio_report, surface_report, years_between
from optionsdesk import Journal, contract, utc

AS_OF = "2024-05-07T19:50:00Z"
NEAR = "2024-05-17"
FAR = "2024-06-21"
EXPIRY_AT = "2024-05-17T20:00:00Z"


class VolatilitySurfaceTests(unittest.TestCase):
    def setUp(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        self.journal = Journal(connection)
        self.config = {"symbol": "TEST", "as_of": AS_OF, "quote_source": "alpaca", "quote_feed": "opra",
                       "underlying_source": "alpaca", "underlying_feed": "sip", "rate": 0, "dividend_yield": 0,
                       "max_quote_age_seconds": 1, "max_quote_skew_seconds": 0.5, "exercise_style": "European"}
        self.journal.record({"source": "alpaca", "feed": "sip", "received_at": AS_OF, "observations": [
            {"kind": "underlying_trade", "instrument": "TEST", "price": 100, "session": "regular",
             "observed_at": AS_OF}]})

    def quotes(self, prices, *, feed="opra", observed=AS_OF, received=AS_OF):
        self.journal.record({"source": "alpaca", "feed": feed, "received_at": received, "observations": [
            {"kind": "option_quote", "instrument": contract("TEST", expiry, strike, "C"),
             "observed_at": observed, "bid": f"{mid - .001:.8f}", "ask": f"{mid + .001:.8f}",
             "bid_size": 10, "ask_size": 10, "multiplier": 100} for expiry, strike, mid in prices]})
        return [{"expiry": expiry, "strike": strike, "right": "C"} for expiry, strike, _ in prices]

    def test_known_black_scholes_prices_and_inverse(self):
        for right, expected in (("C", 10.45058357), ("P", 5.57352602)):
            with self.subTest(right=right):
                price = option_value(100, 100, 1, .05, 0, .20, right)
                self.assertAlmostEqual(price, expected, places=7)
                self.assertAlmostEqual(implied_vol(price, 100, 100, 1, .05, 0, right), .20, places=10)
        self.assertIsNone(implied_vol(1, 100, 80, 1, .05, 0, "C"))

    def test_surface_keeps_cutoff_evidence_and_finds_calendar_inversion(self):
        prices = []
        for expiry, expiry_at, iv in ((NEAR, EXPIRY_AT, .60), (FAR, "2024-06-21T20:00:00Z", .20)):
            years = years_between(utc(expiry_at), utc(AS_OF))
            for strike in (95, 100, 105):
                prices.append((expiry, strike, option_value(100, strike, years, 0, 0, iv, "C")))
        contracts = self.quotes(prices)
        self.quotes([(NEAR, 95, 20)], received="2024-05-07T19:50:00.000000001Z")
        report = surface_report(self.journal, {**self.config, "contracts": contracts})
        self.assertEqual(report["underlying_status"], "fresh")
        node = report["nodes"][0]
        self.assertEqual(node["quote"]["received_at"], utc(AS_OF))
        self.assertLess(node["iv"]["bid"], node["iv"]["mid"])
        self.assertLess(node["iv"]["mid"], node["iv"]["ask"])
        self.assertAlmostEqual(node["iv"]["mid"], .60, places=6)
        self.assertFalse(report["diagnostics"]["strike"]["findings"])
        self.assertEqual(report["diagnostics"]["calendar"]["checks"], 3)
        self.assertTrue(all(row["quote_band_violation"] for row in report["diagnostics"]["calendar"]["findings"]))
        self.assertEqual(len(report["diagnostics"]["calendar"]["findings"]), 3)

    def test_smile_flags_quote_band_monotonicity_and_convexity(self):
        contracts = self.quotes([(NEAR, 90, 11), (NEAR, 100, 9), (NEAR, 110, 1), (NEAR, 120, 2)])
        report = surface_report(self.journal, {**self.config, "contracts": contracts})
        findings = report["diagnostics"]["strike"]["findings"]
        self.assertEqual({row["kind"] for row in findings}, {"strike_monotonicity", "strike_convexity"})
        self.assertTrue(all(row["quote_band_violation"] for row in findings))

    def test_indicative_and_asynchronous_quotes_are_not_inverted(self):
        contracts = self.quotes([(NEAR, 100, 3)], feed="indicative")
        report = surface_report(self.journal, {**self.config, "contracts": contracts, "quote_feed": "indicative"})
        self.assertEqual(report["nodes"][0]["model_status"], "indicative")
        self.assertIsNone(report["nodes"][0]["iv"])
        self.quotes([(NEAR, 100, 3)], observed="2024-05-07T19:49:59.499999999Z")
        report = surface_report(self.journal, {**self.config, "contracts": contracts})
        self.assertEqual(report["nodes"][0]["model_status"], "unsynchronized_underlying")

    def test_event_variance_estimates_known_jump_and_rejects_mismatched_nodes(self):
        expiries = (NEAR, FAR, "2024-07-19")
        event_var, background_var = .005, .04
        prices = []
        for index, expiry in enumerate(expiries):
            t = years_between(utc(f"{expiry}T20:00:00Z"), utc(AS_OF))
            iv = sqrt(background_var + (event_var / t if index else 0))
            prices.append((expiry, 100, option_value(100, 100, t, 0, 0, iv, "C")))
        contracts = self.quotes(prices + [(FAR, 105, 3)])
        selection = dict(zip(("before", "after", "farther"), (contract("TEST", e, 100, "C") for e in expiries)))
        selection.update(event_at="2024-05-29T20:30:00Z", max_log_moneyness_gap=.01, max_quote_skew_seconds=.5)
        spec = {**self.config, "contracts": contracts, "event_variance": selection}
        result = surface_report(self.journal, spec)["event_variance"]
        self.assertEqual(result["status"], "estimated")
        self.assertAlmostEqual(result["event_variance"], event_var, places=7)
        self.assertLess(result["event_variance_bid_ask_bounds"][0], event_var)
        self.assertGreater(result["event_variance_bid_ask_bounds"][1], event_var)
        selection["after"] = contract("TEST", FAR, 105, "C")
        self.assertEqual(surface_report(self.journal, spec)["event_variance"]["reason"], "moneyness_mismatch")


class GreekTests(unittest.TestCase):
    def test_analytic_greeks_match_finite_differences_for_calls_and_puts(self):
        s, k, t, r, q, vol = 110, 100, .35, .04, .02, .25
        h = .00001
        for right in ("C", "P"):
            g = option_greeks(s, k, t, r, q, vol, right)
            def central(fn, x):
                return (fn(x + h) - fn(x - h)) / (2 * h)
            def risk(key, s=s, t=t, vol=vol):
                return option_greeks(s, k, t, r, q, vol, right)[key]
            def price(s=s, t=t, vol=vol):
                return option_value(s, k, t, r, q, vol, right)
            checks = {
                "delta_shares": central(lambda x: price(s=x), s),
                "gamma_delta_per_dollar": central(lambda x: risk("delta_shares", s=x), s),
                "theta_dollars_per_calendar_day": -central(lambda x: price(t=x), t) / 365,
                "vega_dollars_per_iv_point": central(lambda x: price(vol=x), vol) / 100,
                "vanna_delta_per_iv_point": central(lambda x: risk("delta_shares", vol=x), vol) / 100,
                "volga_dollars_per_iv_point_squared": central(lambda x: risk("vega_dollars_per_iv_point", vol=x), vol) / 100,
                "charm_delta_per_calendar_day": -central(lambda x: risk("delta_shares", t=x), t) / 365,
                "speed_gamma_per_dollar": central(lambda x: risk("gamma_delta_per_dollar", s=x), s),
                "zomma_gamma_per_iv_point": central(lambda x: risk("gamma_delta_per_dollar", vol=x), vol) / 100,
                "color_gamma_per_calendar_day": -central(lambda x: risk("gamma_delta_per_dollar", t=x), t) / 365,
            }
            for key, expected in checks.items():
                with self.subTest(right=right, greek=key):
                    self.assertAlmostEqual(g[key], expected, places=8)


class PortfolioTests(unittest.TestCase):
    def setUp(self):
        self.base = {"as_of": AS_OF, "underlyings": {"TEST": {"spot": 100, "rate": 0, "dividend_yield": 0}}}
        self.long_call = {"kind": "option", "symbol": "TEST", "quantity": 1, "right": "C", "strike": 100,
                          "expiry_at": EXPIRY_AT, "iv": ".40", "entry_price": 2,
                          "entry_fee_dollars": ".65", "exit_fee_dollars": ".65", "exit_slippage_per_unit": ".05",
                          "bid": "1.90", "ask": "2.10", "bid_size": 1, "ask_size": 1}
        self.short_call = {**self.long_call, "quantity": -1, "strike": 105, "entry_price": 1,
                           "bid": ".90", "ask": "1.10", "exit_fee_dollars": 0}
        self.stock = {"kind": "stock", "symbol": "TEST", "quantity": 10, "entry_price": 100,
                      "entry_fee_dollars": 1, "exit_fee_dollars": 1, "exit_slippage_per_unit": ".05"}

    def test_advanced_attribution_reconciles_joint_shock_and_skips_expiration(self):
        spec = {**self.base, "advanced_greeks": True, "positions": [self.stock, self.long_call, self.short_call],
                "scenarios": [{"name": "shock", "at": "2024-05-08T19:50:00Z", "spots": {"TEST": 108},
                               "iv_shifts": {"TEST": "-.1"}, "iv_shifts_by_position": {"3": ".02"}},
                              {"name": "expiry", "at": EXPIRY_AT, "spots": {"TEST": 108}}]}
        result = portfolio_report(spec)
        self.assertEqual(Decimal(result["scenarios"][0]["positions"][2]["iv_used"]), Decimal(".32"))
        details = result["advanced_risk"]
        self.assertGreater(details["positions"][1]["greeks"]["gamma_delta_per_dollar"], 0)
        self.assertLess(details["positions"][2]["greeks"]["gamma_delta_per_dollar"], 0)
        effect = details["attribution_before_exit_cost_dollars"]["shock"]
        self.assertEqual(sum(Decimal(effect[k]) for k in ("spot", "implied_volatility", "time")),
                         Decimal(effect["mark_change"]))
        expected = (Decimal(result["scenarios"][0]["change_from_current_mark_dollars"])
                    + Decimal(result["scenarios"][0]["exit_friction_dollars"]))
        self.assertAlmostEqual(Decimal(effect["mark_change"]), expected, delta=Decimal(".01"))
        self.assertEqual(details["attribution_before_exit_cost_dollars"]["expiry"]["unattributed_positions"], [2, 3])

    def test_expiry_intrinsic_uses_exact_signed_math_and_exposes_assignment(self):
        report = portfolio_report({**self.base, "positions": [self.stock, self.long_call, self.short_call],
                                   "scenarios": [{"name": "Expiry rally", "at": EXPIRY_AT, "spots": {"TEST": 108}}]})
        scenario = report["scenarios"][0]
        self.assertEqual(scenario["gross_pnl_dollars"], "480.00")
        self.assertEqual(scenario["pnl_dollars"], "475.55")
        self.assertEqual(scenario["positions"][1]["exit_friction_dollars"], "0.65")
        self.assertEqual(scenario["itm_assignment_exposure"]["TEST"], {
            "shares_to_deliver": 100, "shares_to_receive": 0,
            "cash_to_pay_dollars": "0.00", "cash_to_receive_dollars": "10500.00"})

    def test_a_later_horizon_requires_expiry_fixing_instead_of_reusing_spot(self):
        horizon = {"name": "After expiry", "at": "2024-05-20T20:00:00Z", "spots": {"TEST": 112}}
        with self.assertRaisesRegex(ValueError, "needs the TEST fixing"):
            portfolio_report({**self.base, "positions": [self.stock, self.long_call], "scenarios": [horizon]})
        horizon["expiry_spots"] = {"TEST": {"2024-05-17T16:00:00-04:00": 108}}
        report = portfolio_report({**self.base, "positions": [self.stock, self.long_call], "scenarios": [horizon]})
        legs = report["scenarios"][0]["positions"]
        self.assertEqual([position["spot_used"] for position in legs], ["112", "108"])
        self.assertEqual(legs[1]["gross_pnl_dollars"], "600.00")

    def test_interim_volatility_and_transaction_friction_change_signed_pnl(self):
        def scenario(name, change):
            return {"name": name, "at": AS_OF, "spots": {}, "iv_shifts": {"TEST": change}}

        report = portfolio_report({**self.base, "positions": [self.long_call],
                                   "scenarios": [scenario("Unchanged", 0), scenario("Crush", "-.20")]})
        unchanged, crush = report["scenarios"]
        self.assertEqual(unchanged["change_from_current_mark_dollars"], "-5.65")
        self.assertEqual(crush["positions"][0]["iv_used"], "0.20")
        self.assertEqual(report["worst_scenario"]["name"], "Crush")
        with self.assertRaisesRegex(ValueError, "negative volatility"):
            portfolio_report({**self.base, "positions": [self.long_call], "scenarios": [scenario("Invalid", "-.41")]})

    def test_multi_underlying_shocks_and_american_short_put_cash_obligation(self):
        basis = {**self.base, "underlyings": {**self.base["underlyings"],
                 "OTHER": {"spot": 50, "rate": 0, "dividend_yield": 0}}}
        short_stock = {"kind": "stock", "symbol": "OTHER", "quantity": -2, "entry_price": 50}
        short_put = {**self.short_call, "right": "P", "quantity": -2}
        report = portfolio_report({**basis, "positions": [short_stock, short_put],
                                   "scenarios": [{"name": "Multi-market shock", "at": AS_OF,
                                                  "spots": {"TEST": 80, "OTHER": 65}}]})
        scenario = report["scenarios"][0]
        self.assertEqual(scenario["positions"][0]["gross_pnl_dollars"], "-30.00")
        self.assertEqual(scenario["positions"][1]["assignment"]["timing"], "early_assignment")
        exposure = scenario["itm_assignment_exposure"]["TEST"]
        self.assertEqual(exposure["shares_to_receive"], 200)
        self.assertEqual(exposure["cash_to_pay_dollars"], "21000.00")

    def test_synthetic_leg_crossing_and_depth_of_shared_instruments(self):
        request = {**self.base, "positions": [self.long_call, self.short_call],
                   "scenarios": [{"name": "Base", "at": AS_OF, "spots": {}}]}
        cost = portfolio_report(request)["execution_sensitivity"]
        self.assertEqual(cost["status"], "synthetic_quoted_sides")
        self.assertEqual(cost["entry_net_debit_mid_dollars"], "100.00")
        self.assertEqual(cost["entry_net_debit_cross_dollars"], "120.00")
        self.assertEqual(cost["exit_net_credit_cross_dollars"], "80.00")
        self.assertEqual(cost["round_trip_cross_spread_cost_dollars"], "40.00")
        request["positions"].append(self.long_call)
        cost = portfolio_report(request)["execution_sensitivity"]
        self.assertEqual(cost["status"], "insufficient_displayed_size")

    def test_risk_limits_distinguish_funding_breaches_and_unknown_liquidity(self):
        short_put = {key: value for key, value in self.short_call.items()
                     if key not in {"bid", "ask", "bid_size", "ask_size"}}
        book = {**self.base, "positions": [{**short_put, "right": "P", "quantity": -2}],
                "scenarios": [{"name": "Gap down", "at": AS_OF, "spots": {"TEST": 80}}],
                "risk_limits": {"max_gross_assignment_cash_dollars": 20000,
                                "max_synthetic_round_trip_cost_dollars": 100}}
        result = portfolio_report(book)["risk_limits"]
        self.assertEqual(result["status"], "breach")
        self.assertEqual([row["status"] for row in result["checks"]], ["breach", "unknown"])
        self.assertEqual(result["checks"][0]["value_dollars"], "21000.00")


if __name__ == "__main__":
    unittest.main()
