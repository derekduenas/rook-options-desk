from copy import deepcopy
from datetime import date, timedelta
import json
from pathlib import Path
import unittest

from candidate_review import candidate_review
from market_scan import BREADTH


class CandidateReviewTests(unittest.TestCase):
    def setUp(self):
        self.case = json.loads(Path(__file__).with_name("examples").joinpath("candidate-review.json").read_text())

    def test_positive_model_candidate_can_fail_combined_portfolio_stress(self):
        report = candidate_review(self.case)
        self.assertEqual(report["entry_price_result"]["worst_model_expected_net_dollars"], "27.75")
        self.assertEqual(report["status"], "pass_on_candidate")
        self.assertIn("entry_adjusted_portfolio_stress_limit", report["reasons"])
        self.case["portfolio"]["positions"] = []
        self.assertEqual(candidate_review(self.case)["status"], "passes_research_gates")
        self.case["available_loss_budget_dollars"] = 0
        self.assertIn("available_loss_budget", candidate_review(self.case)["reasons"])

    def test_prices_and_positions_must_refer_to_the_same_trade(self):
        leg = self.case["candidate_positions"][0]
        leg["strike"] = 105
        with self.assertRaisesRegex(ValueError, "candidate strikes"):
            candidate_review(self.case)
        leg["strike"] = 100
        leg["entry_price"] = "1.05"
        with self.assertRaisesRegex(ValueError, "entry debit"):
            candidate_review(self.case)

    def test_entry_cost_above_the_mark_counts_against_stress(self):
        self.case["portfolio"]["positions"] = []
        self.case["portfolio"]["risk_limits"]["max_loss_from_mark_dollars"] = 100
        self.case["candidate_positions"][0]["iv"] = ".08"
        self.case["portfolio"]["scenarios"][1]["iv_shifts"]["DEMO"] = "-.04"
        review = candidate_review(self.case)
        self.assertGreater(float(review["entry_cost_vs_model_mark_dollars"]), 0)
        self.assertEqual(review["reasons"], ["entry_adjusted_portfolio_stress_limit"])

    def set_operational_context(self):
        self.case["portfolio"]["positions"] = []
        cutoff = self.case["price_case"]["as_of"]
        snapshot = {"account": "fixture", "observed_at": cutoff, "received_at": cutoff,
                    "cash": {"USD_settled": 5000}, "positions": {}, "working_orders": {}}
        days = [date(2026, 11, 25) - timedelta(days=i) for i in range(50)]
        intake = {"as_of": cutoff, "session": {"open": "2026-11-27T14:30:00Z", "close": "2026-11-27T18:00:00Z",
                                               "previous_session": "2026-11-25"},
                  "max_quote_age_seconds": 10, "spy_closes": {d.isoformat(): 100 for d in days},
                  "quotes": {symbol: {"observed_at": cutoff, "received_at": cutoff, "previous_session": "2026-11-25",
                                      "previous_close": 100, "last": 101} for symbol in BREADTH}}
        context = {"account_state": {"max_age_seconds": 120, "max_observation_skew_seconds": 60,
                                     "broker": snapshot, "ledger": deepcopy(snapshot)},
                   "market_intake": intake, "entry_cutoffs": {"DEMO261204C00100000": {
                       "last_entry_at": "2026-12-04T20:00:00Z", "known_at": cutoff}}}
        self.case["operational_context"] = context
        return context

    def test_strict_gate_requires_context_and_accepts_matching_fresh_evidence(self):
        self.case["portfolio"]["positions"] = []
        report = candidate_review(self.case, require_operational_context=True)
        self.assertEqual(report["reasons"], ["missing_operational_context"])
        self.set_operational_context()
        report = candidate_review(self.case, require_operational_context=True)
        self.assertEqual(report["status"], "passes_research_gates")
        self.assertEqual(report["operational_review"]["status"], "supplied_checks_passed")

    def test_stale_account_incomplete_breadth_and_reached_cutoff_block(self):
        context = self.set_operational_context()
        context["account_state"]["broker"]["observed_at"] = "2026-11-27T15:39:29Z"
        context["market_intake"]["quotes"].pop("RSP")
        context["entry_cutoffs"]["DEMO261204C00100000"]["last_entry_at"] = self.case["price_case"]["as_of"]
        report = candidate_review(self.case)
        self.assertEqual(report["status"], "pass_on_candidate")
        self.assertEqual(report["operational_review"]["reasons"],
                         ["broker_stale", "market_regime_unclassified", "entry_cutoff_invalid_or_reached:DEMO261204C00100000"])

    def test_equal_snapshots_are_insufficient_when_portfolio_omits_exposure(self):
        context = self.set_operational_context()
        working = {"id": {"instrument": "DEMO261204C00100000", "side": "buy", "remaining_quantity": 1,
                          "type": "limit", "time_in_force": "day", "limit": "1.10"}}
        for snapshot in ("broker", "ledger"):
            context["account_state"][snapshot].update(positions={"DEMO261204C00100000": "1"}, working_orders=working)
        self.assertEqual(candidate_review(self.case)["operational_review"]["reasons"],
                         ["portfolio_does_not_match_broker", "working_orders_outside_portfolio_stress"])
