from datetime import date, timedelta
from json import loads
from pathlib import Path
import unittest

from exit_replay import exit_report
from expression_review import expression_report
from overnight_variance import compare
from regime_diagnostics import correlation_report, report as regime_report


EXAMPLES = Path(__file__).with_name("examples")


class FrontierUpgradeTests(unittest.TestCase):
    def test_compares_spreads_under_same_horizon_and_keeps_cash(self):
        raw = loads((EXAMPLES / "expression-review.json").read_text())
        result = expression_report(raw)
        self.assertEqual(result["selected_expression"], "100/105 call vertical")
        self.assertEqual(expression_report(raw, require_operational_context=True)["status"], "cash")
        raw["expressions"][0]["sizes"][0]["models"]["baseline"]["exit_at"] = "2026-12-02T20:15:00Z"
        with self.assertRaisesRegex(ValueError, "common exit"):
            expression_report(raw)

    def test_exit_requires_a_later_quote_with_enough_depth(self):
        raw = loads((EXAMPLES / "exit-replay.json").read_text())
        scheduled, target = exit_report(raw)["policies"]
        self.assertEqual((scheduled["net_pnl_dollars"], target["net_pnl_dollars"]), ("113.00", "268.00"))
        self.assertEqual(target["modeled_credit"], "3.80")
        raw["package_quotes"][1]["bid_size"] = 0
        self.assertEqual(exit_report(raw)["policies"][1]["net_pnl_dollars"], "113.00")
        raw["package_quotes"][2]["received_at"] = "2026-12-03T20:20:00.000000001Z"
        self.assertEqual([row["status"] for row in exit_report(raw)["policies"]], ["unresolved_exit"] * 2)

    def sample_variance(self):
        rows = []
        previous = None
        for offset in range(85):
            day = date(2024, 1, 2) + timedelta(days=offset)
            if day.weekday() >= 5:
                continue
            day = day.isoformat()
            opening_price = 100 + (offset % 11) * .15
            prices = [opening_price + ((bar + offset) % 8 - 4) * .01 * (1 + offset % 5)
                      for bar in range(79)]
            rows.append({"open_at": day + "T14:30:00Z", "close_at": day + "T21:00:00Z",
                         "received_at": day + "T21:00:01Z", "previous_session": previous,
                         "five_minute_prices": prices})
            previous = day
        return {"horizon_sessions": 1, "min_training_windows": 8, "ridge_penalty": .1, "sessions": rows}

    def test_outcome_can_change_without_altering_its_frozen_forecasts(self):
        raw = self.sample_variance()
        first = compare(raw)
        self.assertGreater(first["complete_test_windows"], 15)
        raw["sessions"][-1]["five_minute_prices"][-1] *= 1.03
        second = compare(raw)
        self.assertEqual(first["outcomes"][-1]["forecasts"], second["outcomes"][-1]["forecasts"])
        self.assertNotEqual(first["outcomes"][-1]["target"], second["outcomes"][-1]["target"])

    def test_late_session_cannot_be_used_for_an_already_open_target(self):
        raw = self.sample_variance()
        expected = compare(raw)["outcomes"][-1]["session"]
        raw["sessions"][-2]["received_at"] = raw["sessions"][-1]["open_at"]
        self.assertNotIn(expected, {row["session"] for row in compare(raw)["outcomes"]})
        raw["sessions"][-2]["five_minute_prices"].pop()
        with self.assertRaisesRegex(ValueError, "complete regular"):
            compare(raw)

    def test_archive_assumptions_remain_separate_from_observed_receipts(self):
        raw = self.sample_variance()
        raw["archive"] = {"source": "fixture", "downloaded_at": "2024-05-01T00:00:00Z", "availability_lag_seconds": 60}
        with self.assertRaisesRegex(ValueError, "no prospective receipt"):
            compare(raw)
        for row in raw["sessions"]:
            del row["received_at"]
        raw["incumbent_power_law"] = {"lookback_sessions": 25, "exponent": "-.4"}
        replay = compare(raw)
        self.assertEqual(replay["basis"], "archived bars with assumed availability")
        self.assertTrue(all("21:01:00" in row["decision_at"] for row in replay["outcomes"]))
        self.assertTrue(all("power_law" in row["qlike"] for row in replay["outcomes"]))

    def test_clustered_jumps_raise_hawkes_probability_at_equal_event_count(self):
        def run(jump_at):
            return regime_report({"window_start": "2026-11-27T16:00:00Z", "as_of": "2026-11-27T16:10:00Z",
                                  "forecast_seconds": 60, "decay_seconds": 60, "branching_ratio": ".5",
                                  "background_jumps_per_second": ".001", "assets": ["A", "B"], "aligned_returns": [],
                                  "jumps": [{"observed_at": jump_at, "received_at": jump_at}]})
        recent, distant = run("2026-11-27T16:09:59Z"), run("2026-11-27T16:00:01Z")
        self.assertEqual(recent["poisson_count_baseline_probability"], distant["poisson_count_baseline_probability"])
        self.assertGreater(recent["fixed_hawkes_probability_of_jump"], distant["fixed_hawkes_probability_of_jump"])

    def test_correlation_entropy_detects_one_shared_risk_factor(self):
        diversified = correlation_report([{"A": a, "B": b} for a, b in [(1, 1), (1, -1), (-1, 1), (-1, -1)]],
                                         ["A", "B"])
        concentrated = correlation_report([{"A": a, "B": -a} for a in [1, 2, 3, 4]], ["A", "B"])
        self.assertAlmostEqual(diversified["normalized_spectral_entropy"], 1)
        self.assertAlmostEqual(concentrated["normalized_spectral_entropy"], 0)
        self.assertAlmostEqual(concentrated["largest_eigenvalue_share"], 1)
