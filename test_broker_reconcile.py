from copy import deepcopy
import unittest

from broker_reconcile import reconcile


class BrokerReconcileTests(unittest.TestCase):
    def setUp(self):
        state = {"account": "paper-1", "observed_at": "2026-11-27T17:00:00Z", "received_at": "2026-11-27T17:00:01Z",
                 "positions": {"XYZ": "100", "OPT": "-1"}, "cash": {"USD_settled": "2000"},
                 "working_orders": {"1": {"instrument": "OPT", "side": "buy", "remaining_quantity": 2,
                                          "type": "limit", "time_in_force": "day", "limit": "1.20"}}}
        self.raw = {"as_of": "2026-11-27T17:00:02Z", "max_age_seconds": 5, "max_observation_skew_seconds": 2,
                    "broker": state, "ledger": deepcopy(state)}

    def test_matches_equivalent_decimal_representations(self):
        self.raw["ledger"]["positions"]["XYZ"] = "100.00"
        self.assertEqual(reconcile(self.raw)["status"], "matched")

    def test_surfaces_position_cash_and_partial_order_mismatches(self):
        broker = self.raw["broker"]
        broker["positions"]["OPT"] = 0
        broker["cash"]["USD_settled"] = "1879"
        broker["working_orders"]["1"]["remaining_quantity"] = 1
        result = reconcile(self.raw)
        self.assertEqual(result["status"], "review_required")
        self.assertEqual(result["differences"], {"positions": ["OPT"], "cash": ["USD_settled"], "working_orders": ["1"]})

    def test_rejects_snapshot_unavailable_at_cutoff(self):
        self.raw["broker"]["received_at"] = "2026-11-27T17:00:02.000000001Z"
        result = reconcile(self.raw)
        self.assertEqual(result["reasons"], ["broker_unavailable_at_cutoff"])
        self.assertEqual(result["differences"], {})
