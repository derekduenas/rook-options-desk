from decimal import Decimal
import unittest

from catalyst import account_stress, price_candidate, render_study
from optionsdesk import utc


def quote(bid, ask, observed_at="2026-11-25T20:59:59Z"):
    return {"bid": bid, "ask": ask, "bid_size": 5, "ask_size": 5, "multiplier": 100,
            "observed_at": utc(observed_at)}


def study_fixture():
    return {
        "quote_source": "alpaca", "quote_feed": "opra",
        "as_of": utc("2026-11-25T21:00:00Z"),
        "catalyst": {"event_at": utc("2026-11-25T21:01:00Z")},
        "right": "C", "long_strike": 100, "short_strike": 105,
        "max_quote_age_seconds": 1, "max_leg_skew_seconds": 1,
        "fees_per_leg_dollars": 0, "stress_slippage_per_leg_dollars": 0,
        "minimum_target_r": 2, "target_scenario": "target",
        "scenarios": [{"name": "target", "terminal_stock_price": "101.50115"}],
    }


class CatalystReviewTests(unittest.TestCase):
    def test_breach_at_boundary_and_round_lot_sizing(self):
        risk = {"risk_fraction": "0.10", "drawdown_limit": "0.20", "simultaneous_positions": 2,
                "loss_streak": 2}
        outcomes = [{"probability": 1, "stressed_pnl": Decimal(-100)}]
        fractional = account_stress({"account_stress": risk}, outcomes, Decimal(100), 2)
        self.assertEqual(fractional["repeated_cluster_drawdown_pct"], "36.00")
        self.assertTrue(fractional["deterministic_stress_breaches_limit"])
        self.assertEqual(fractional["simulated_breach_pct"], "100.00")
        self.assertEqual(fractional["terminal_equity_multiples"]["p50"], "0.640")

        risk["illustrative_equity_dollars"] = 1000
        whole = account_stress({"account_stress": risk}, outcomes, Decimal(100), 2)
        self.assertEqual(whole["simulation_sizing"], "whole_contracts")
        self.assertEqual(whole["simulated_breach_pct"], "100.00")
        self.assertEqual(whole["terminal_equity_multiples"]["p50"], "0.800")

        risk["illustrative_equity_dollars"] = 999
        cash = account_stress({"account_stress": risk}, outcomes, Decimal(100), 2)
        self.assertEqual(cash["simulation_status"], "cash_only")
        self.assertEqual(cash["simulated_breach_pct"], "0.00")
        self.assertEqual(cash["terminal_equity_multiples"]["p50"], "1.000")

    def test_debit_ceiling_rounds_down_and_impossible_breakeven_is_absent(self):
        study = study_fixture()
        row, _ = price_candidate(study, [quote("0.40", "0.50")], False, 2)
        self.assertEqual(row["max_debit_for_target_r_dollars"], "50.03")
        study.update(fees_per_leg_dollars=1, short_strike=101)
        row, _ = price_candidate(study, [quote("1.45", "1.50"), quote("0.51", "0.60")], True, 2)
        self.assertEqual(row["max_profit_dollars"], "-1.00")
        self.assertIsNone(row["expiry_breakeven_stock_price"])

    def test_quote_age_uses_nanoseconds_and_indicative_is_reference_only(self):
        study = study_fixture()
        fresh = quote("0.40", "0.50")
        stale = quote("0.10", "0.20", "2026-11-25T20:59:58.999999999Z")
        frozen = {"version": 2, "study": study, "quotes": [fresh, stale], "recorded_at": study["as_of"]}
        report = render_study(frozen)
        self.assertEqual(report["candidates"][1]["quote_status"], "stale")
        self.assertEqual(report["study_status"], "reference_only")

        frozen["quotes"][1] = {**stale, "observed_at": fresh["observed_at"]}
        self.assertEqual(render_study(frozen)["study_status"], "prospective_research")
        study["quote_feed"] = "indicative"
        report = render_study(frozen)
        self.assertEqual([row["quote_status"] for row in report["candidates"]], ["indicative", "indicative"])
        self.assertEqual(report["study_status"], "reference_only")
        self.assertEqual(len(report["ranked_structures"]), 2)


if __name__ == "__main__":
    unittest.main()
