import json
from pathlib import Path
import unittest

from candidate_sizing import size_report


class CandidateSizingTests(unittest.TestCase):
    def setUp(self):
        self.case = json.loads(Path(__file__).with_name("examples").joinpath("candidate-sizing.json").read_text())

    def test_fractional_budget_never_rounds_up_to_whole_lot(self):
        report = size_report(self.case)
        self.assertEqual((report["reference_contracts"], report["selected_contracts"]), (3, 1))
        self.assertEqual(report["fractional_loss_budget_dollars"], "122.85")
        self.case["net_liquidation_value_dollars"] = "5000"
        report = size_report(self.case)
        self.assertEqual((report["reference_contracts"], report["selected_contracts"]), (1, 0))
        self.assertIn("loss_budget", report["sizes"][1]["reasons"])

    def test_adverse_size_specific_fill_model_changes_choice(self):
        self.case["fractional_risk_scale"] = "1"
        self.case["sizes"][2]["models"]["adverse_selection"]["outcomes"][0]["exit_dollars_per_contract"] = 130
        report = size_report(self.case)
        self.assertEqual(report["selected_contracts"], 2)
        self.assertIn("modeled_log_growth_does_not_beat_cash", report["sizes"][2]["reasons"])

    def test_expected_profit_can_be_positive_while_expected_log_growth_is_negative(self):
        self.case.update(net_liquidation_value_dollars="120", max_order_loss_fraction="1",
                         max_combined_stress_fraction="1", fractional_risk_scale="1")
        self.case["sizes"] = self.case["sizes"][:1]
        self.case["sizes"][0]["models"]["adverse_selection"]["outcomes"] = [
            {"probability": ".6", "filled_contracts": 1, "exit_dollars_per_contract": 230},
            {"probability": ".4", "filled_contracts": 1, "exit_dollars_per_contract": -5},
        ]
        report = size_report(self.case)
        self.assertEqual(report["selected_contracts"], 0)
        trial, = report["sizes"]
        self.assertEqual(trial["worst_expected_net_dollars"], "24.00")
        self.assertEqual(trial["reasons"], ["modeled_log_growth_does_not_beat_cash"])

    def test_strict_operational_context_blocks_all_sizes_when_missing(self):
        report = size_report(self.case, require_operational_context=True)
        self.assertEqual(report["selected_contracts"], 0)
        self.assertTrue(all("missing_operational_context" in row["reasons"] for row in report["sizes"]))
