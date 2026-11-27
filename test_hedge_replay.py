import json
from pathlib import Path
import unittest

from hedge_replay import hedge_replay


class HedgeReplayTests(unittest.TestCase):
    def setUp(self):
        self.path = json.loads((Path(__file__).parent / "examples/hedge-replay.json").read_text())

    def policies(self):
        return {row["policy"]: row for row in hedge_replay(self.path)["policies"]}

    def test_band_cuts_turnover_and_cost_in_fixed_fixture(self):
        policies = self.policies()
        self.assertEqual(policies["unhedged"]["terminal_pnl_dollars"], "-6.65")
        self.assertEqual(policies["delta_rebalance"]["modeled_hedge_friction_dollars"], "20.08")
        self.assertEqual(policies["no_trade_band"]["modeled_hedge_friction_dollars"], "13.36")
        self.assertEqual(policies["no_trade_band"]["shares_traded"], 334)

    def test_zero_latency_rejects_book_observed_at_decision_time(self):
        quotes = self.path["quotes"]
        self.path["latency_seconds"] = "0"
        quotes[1]["observed_at"] = quotes[0]["received_at"]
        quotes[1]["received_at"] = "2026-12-04T20:30:00.200Z"
        quotes[1]["iv_known_at"] = quotes[1]["received_at"]
        first = self.policies()["delta_rebalance"]["attempts"][0]
        self.assertTrue(first["execution_quote_at"].startswith("2026-12-04T20:36:00"))

    def test_requires_point_in_time_iv(self):
        self.path["quotes"][0]["iv_known_at"] = self.path["quotes"][1]["received_at"]
        with self.assertRaisesRegex(ValueError, "IV must be known"):
            hedge_replay(self.path)

    def test_reports_partial_hedge_and_unpriced_terminal_exit(self):
        self.path["quotes"][1]["bid_size"] = 3
        self.path["settlement"]["ask_size"] = 0
        policy = self.policies()["delta_rebalance"]
        self.assertEqual(policy["attempts"][0]["filled_shares"], -3)
        self.assertGreater(policy["partial_hedges"], 0)
        self.assertLess(policy["unclosed_hedge_shares"], 0)
        self.assertEqual(policy["status"], "unpriced_terminal_exit")
        self.assertIsNone(policy["terminal_pnl_dollars"])

    def test_rejects_terminal_book_received_after_allowance(self):
        self.path["settlement"]["received_at"] = "2026-12-04T21:00:03Z"
        self.path["settlement"]["observed_at"] = "2026-12-04T21:00:02Z"
        with self.assertRaisesRegex(ValueError, "terminal hedge book"):
            hedge_replay(self.path)
