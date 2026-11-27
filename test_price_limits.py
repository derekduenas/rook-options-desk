import json
from decimal import Decimal as D
from pathlib import Path
import unittest

from price_limits import price_limit_report, score_distribution


class PriceLimitTests(unittest.TestCase):
    def setUp(self):
        self.plan = json.loads(Path(__file__).with_name("examples").joinpath("price-limits.json").read_text())

    def test_lowest_price_can_fail_and_largest_passing_price_can_be_suboptimal(self):
        result = price_limit_report(self.plan)
        self.assertEqual(result["highest_passing_tested_limit"], "1.20")
        self.assertEqual(result["tested_limit_with_best_worst_model_expectancy"], "1.10")
        self.assertEqual(result["prices"][0]["worst_model_expected_net_dollars"], "-5.50")
        self.assertIn("tail_limit", result["prices"][-1]["reasons"])

    def test_partial_fills_and_unfilled_mass_do_not_hide_tail_risk(self):
        scenarios = {"basis": "synthetic", "outcomes": [
            {"probability": ".90", "filled_contracts": 0, "exit_dollars_per_contract": 0},
            {"probability": ".02", "filled_contracts": 2, "exit_dollars_per_contract": 50},
            {"probability": ".08", "filled_contracts": 1, "exit_dollars_per_contract": 150},
        ]}
        report = score_distribution(scenarios, D("1.00"), 2, D(0), D(0), None, D(".4"))
        self.assertEqual(report["expected_net_dollars"], D(2))
        self.assertEqual(report["expected_filled_contracts"], D(".12"))
        self.assertEqual(report["expected_net_dollars_given_fill"], D(20))
        self.assertEqual(report["tail_loss_dollars_given_fill"], D(25))

    def test_late_received_quote_and_insufficient_ask_depth(self):
        self.plan["quote"]["received_at"] = "2026-11-27T15:40:00.000000001Z"
        report = price_limit_report(self.plan)
        self.assertIsNone(report["highest_passing_tested_limit"])
        self.assertIn("quote_unavailable_at_cutoff", report["prices"][1]["reasons"])

        self.plan["quote"]["received_at"] = self.plan["as_of"]
        self.plan["quote"]["ask_size"] = 0
        report = price_limit_report(self.plan)
        self.assertEqual(report["highest_passing_tested_limit"], "1.10")
        self.assertIn("insufficient_ask_depth", report["prices"][2]["reasons"])

    def test_synthetic_spread_book_and_future_models(self):
        self.plan["contract"]["short_strike"] = "105"
        self.plan["quote"]["basis"] = "synthetic_legs"
        report = price_limit_report(self.plan)
        self.assertIsNone(report["highest_passing_tested_limit"])
        self.assertIn("no_package_price", report["prices"][1]["reasons"])

        self.plan["price_grid"][0]["models"]["baseline"]["known_at"] = "2026-11-27T15:41:00Z"
        with self.assertRaisesRegex(ValueError, "unavailable at the decision cutoff"):
            price_limit_report(self.plan)

    def test_same_day_expiration_uses_explicit_contract_cutoff(self):
        self.plan["contract"]["expiry"] = "2026-11-27"
        with self.assertRaisesRegex(ValueError, "same-day expiration"):
            price_limit_report(self.plan)
        self.plan["contract"]["expiry_at"] = self.plan["as_of"]
        with self.assertRaisesRegex(ValueError, "must follow the decision"):
            price_limit_report(self.plan)
        self.plan["contract"]["expiry_at"] = "2026-11-27T18:00:00Z"
        self.assertEqual(price_limit_report(self.plan)["highest_passing_tested_limit"], "1.20")
