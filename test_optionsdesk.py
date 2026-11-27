import sqlite3
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from optionsdesk import Journal, contract, utc
from frontier import latest_review, record_review
from publish_snapshot import published_snapshot
from catalyst import freeze_study, replay_study
from providers import normalize_mcp


EXPIRY = "2026-11-27"


def quote(strike, bid, ask, observed="2026-11-25T20:59:59.999999990Z"):
    return {"kind": "option_quote", "instrument": contract("TEST", EXPIRY, strike, "C"),
            "observed_at": observed, "bid": bid, "ask": ask, "bid_size": 5,
            "ask_size": 5, "multiplier": 100}


def capture(*rows, received="2026-11-25T21:00:00Z", source="robinhood", feed="market_snapshot"):
    return {"received_at": received, "source": source, "feed": feed, "observations": rows}


class ProviderTests(unittest.TestCase):
    def test_robinhood_stock_selection_uses_exact_timestamp_order(self):
        stock = {"symbol": "TEST", "has_traded": True, "venue_last_trade_time": "2026-11-25T20:00:00Z",
                 "venue_last_non_reg_trade_time": "2026-11-25T15:00:00.000000001-05:00",
                 "last_trade_price": "100", "last_non_reg_trade_price": "101"}
        for regular in (stock["venue_last_trade_time"], None):
            with self.subTest(regular=regular):
                envelope = {"provider": "robinhood_stocks", "received_at": "2026-11-25T21:00:00Z",
                            "payload": {"data": {"results": [{"quote": {**stock, "venue_last_trade_time": regular}}]}}}
                row = normalize_mcp(envelope)["observations"][0]
                self.assertEqual(row["session"], "non_regular")
                self.assertEqual(row["observed_at"], "2026-11-25T20:00:00.000000001+00:00")

    def test_robinhood_option_uses_contract_root_and_rejects_truncated_strike(self):
        option = {"id": "option", "chain_symbol": "TEST", "root_symbol": "TEST1", "type": "call",
                  "expiration_date": EXPIRY, "strike_price": "100.1250", "trade_value_multiplier": "100"}
        market = {"instrument_id": "option", "updated_at": "2026-11-25T20:00:00Z",
                  "bid_price": "1.00", "ask_price": "1.05", "bid_size": 1, "ask_size": 1,
                  "volume": 1, "open_interest": 1}
        envelope = {"provider": "robinhood_options", "received_at": "2026-11-25T21:00:00Z",
                    "instruments": [option], "payload": {"data": {"results": [{"quote": market}]}}}
        row = normalize_mcp(envelope)["observations"][0]
        self.assertEqual(row["instrument"], "TEST1261127C00100125")
        option["strike_price"] = "100.1251"
        with self.assertRaisesRegex(ValueError, "fit the OCC strike code"):
            normalize_mcp(envelope)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.journal = Journal(self.connection)
        self.plan = {"name": "test spread", "symbol": "TEST", "expiry": EXPIRY,
                     "long_strike": 100, "short_strike": 105, "as_of": "2026-11-25T21:00:02Z",
                     "quote_source": "robinhood", "quote_feed": "market_snapshot", "thesis": "test thesis",
                     "invalidation": "test failure", "max_quote_age_seconds": 30, "max_leg_skew_seconds": 1,
                     "fees_dollars": 2, "required_expected_profit_dollars": 20,
                     "scenarios": [{"name": "down", "terminal_stock_price": 99, "probability": "0.4"},
                                   {"name": "up", "terminal_stock_price": 108, "probability": "0.6"}]}
        self.first = capture(quote(100, "3.00", "3.20"), quote(105, "1.00", "1.25"))

    def test_duplicate_capture_and_spread_payoff(self):
        first = self.journal.record(self.first)
        second = self.journal.record({**self.first, "observations": self.first["observations"][::-1]})
        self.assertEqual(first["capture_id"], second["capture_id"])
        self.assertFalse(second["inserted"])
        result = self.journal.evaluate(self.plan)["analysis"]
        self.assertEqual(result["quote_status"], "fresh")
        self.assertEqual(result["synthetic_ask_debit_dollars"], "220.00")
        self.assertEqual(result["synthetic_mid_debit_dollars"], "197.50")
        self.assertEqual(result["standalone_max_loss_dollars"], "222.00")
        self.assertEqual(result["standalone_max_profit_dollars"], "278.00")
        self.assertEqual(result["expiration_breakeven_stock_price"], "102.22")
        self.assertEqual(result["expected_pnl_dollars"], "78.00")
        self.assertEqual(result["max_debit_at_forecast_dollars"], "278.00")

    def test_as_of_excludes_late_receipt_and_replay_freezes_inputs(self):
        self.journal.record(self.first)
        initial = self.journal.evaluate(self.plan)
        better = quote(100, "2.80", "3.00", "2026-11-25T21:00:01Z")
        self.journal.record(capture(better, received="2026-11-25T21:00:03Z"))
        self.assertEqual(self.journal.evaluate(self.plan), initial)
        newer = self.journal.evaluate({**self.plan, "as_of": "2026-11-25T21:00:04Z"})
        self.assertEqual(newer["analysis"]["synthetic_ask_debit_dollars"], "200.00")
        self.assertEqual(newer["analysis"]["quote_status"], "unsynchronized")
        self.assertEqual(self.journal.replay(initial["decision_id"]), initial)

    def test_holiday_stale_and_mixed_feed_rejected(self):
        self.journal.record(capture(self.first["observations"][0]))
        self.journal.record(capture(self.first["observations"][1], source="alpaca", feed="indicative"))
        self.assertEqual(self.journal.evaluate(self.plan)["analysis"]["quote_status"], "missing")
        self.journal.record(self.first)
        result = self.journal.evaluate({**self.plan, "as_of": "2026-11-26T18:00:00-08:00"})["analysis"]
        self.assertEqual(result["quote_status"], "stale")
        self.assertEqual(result["decision_status"], "reference_only")

    def test_timestamp_order_preserves_nanoseconds(self):
        self.assertEqual(utc("2026-11-25T12:59:59.999999990-08:00"), "2026-11-25T20:59:59.999999990+00:00")
        with self.assertRaisesRegex(ValueError, "newer than its receipt"):
            self.journal.record(capture(*self.first["observations"], received="2026-11-25T20:59:59.999999989Z"))

    def test_freshness_and_leg_skew_preserve_nanoseconds(self):
        self.journal.record(capture(quote(100, 3, "3.20", "2026-11-25T20:59:59.999999999Z"),
                                    quote(105, 1, "1.25", "2026-11-25T21:00:00Z")))
        result = self.journal.evaluate({**self.plan, "max_quote_age_seconds": 2})["analysis"]
        self.assertEqual(result["quote_status"], "stale")
        self.assertEqual(result["oldest_quote_age_seconds"], 2.000000001)
        result = self.journal.evaluate({**self.plan, "max_leg_skew_seconds": "0.0000000009"})["analysis"]
        self.assertEqual(result["quote_status"], "unsynchronized")
        self.assertEqual(result["leg_timestamp_skew_seconds"], 1e-9)

    def test_future_analysis_and_indicative_pricing_are_gated(self):
        with self.assertRaisesRegex(ValueError, "cutoff cannot be in the future"):
            self.journal.evaluate({**self.plan, "as_of": "2099-01-01T00:00:00Z", "expiry": "2099-01-02"})
        self.journal.record(capture(*self.first["observations"], source="alpaca", feed="indicative"))
        result = self.journal.evaluate({**self.plan, "quote_source": "alpaca", "quote_feed": "indicative"})
        self.assertEqual(result["analysis"]["quote_status"], "indicative")
        self.assertEqual(result["analysis"]["decision_status"], "reference_only")

    def test_spread_rejects_missing_size_on_marking_side(self):
        long, short = self.first["observations"]
        self.journal.record(capture({**long, "bid_size": 0}, short))
        self.assertEqual(self.journal.evaluate(self.plan)["analysis"]["quote_status"], "no_two_sided_size")

    def test_payoff_respects_fees_and_debit_ceilings(self):
        self.journal.record(self.first)
        result = self.journal.evaluate({**self.plan, "fees_dollars": "281.00"})["analysis"]
        self.assertEqual(result["standalone_max_profit_dollars"], "-1.00")
        self.assertIsNone(result["expiration_breakeven_stock_price"])
        result = self.journal.evaluate({**self.plan, "required_expected_profit_dollars": "20.001"})["analysis"]
        self.assertEqual(result["max_debit_at_forecast_dollars"], "277.99")

    def test_interest_is_available_at_receipt_even_when_effective_date_is_older(self):
        raw = {"provider": "alpaca_contracts", "received_at": "2026-11-25T21:00:03Z",
               "payload": {"contracts": [{"symbol": contract("TEST", EXPIRY, 100, "C"),
                                           "open_interest_date": "2026-11-24", "open_interest": "10"}]}}
        self.journal.record(normalize_mcp(raw))
        old = self.journal.evaluate(self.plan)["evidence"]["open_interest"]
        self.assertTrue(all(not rows for rows in old.values()))
        new = self.journal.evaluate({**self.plan, "as_of": raw["received_at"]})["evidence"]["open_interest"]
        self.assertEqual(next(iter(new.values()))[0]["effective_date"], "2026-11-24")

    def test_open_interest_prefers_latest_session_then_latest_revision(self):
        code = contract("TEST", EXPIRY, 100, "C")
        for effective, amount, time in (("2026-11-24", 10, "2026-11-25T20:00:00Z"),
                                        ("2026-11-23", 30, "2026-11-25T20:01:00Z"),
                                        ("2026-11-24", 12, "2026-11-25T20:02:00Z")):
            self.journal.record(capture({"kind": "open_interest", "instrument": code, "observed_at": time,
                                         "effective_date": effective, "contracts": amount}, received=time))
        for time, expected in (("2026-11-25T20:01:00Z", 10), ("2026-11-25T20:02:00Z", 12)):
            self.assertEqual(self.journal.inspect(code, time)["open_interest"][0]["contracts"], expected)
        midnight_utc = "2026-11-26T00:10:00Z"
        row = {"kind": "open_interest", "instrument": code, "observed_at": midnight_utc,
               "effective_date": "2026-11-26", "contracts": 1}
        with self.assertRaisesRegex(ValueError, "invalid open interest or effective date"):
            self.journal.record(capture(row, received=midnight_utc))

    def test_portfolio_combines_only_synchronized_scenarios(self):
        self.journal.record(self.first)
        portfolio = {"as_of": self.plan["as_of"], "horizon": EXPIRY, "source": "test book",
                     "pnl_dollars": {"down": -1000, "up": 500}}
        result = self.journal.evaluate({**self.plan, "portfolio": portfolio})["analysis"]["portfolio_impact"]
        self.assertEqual([row["after_dollars"] for row in result["scenarios"]], ["-1222.00", "778.00"])
        with self.assertRaisesRegex(ValueError, "same horizon"):
            self.journal.evaluate({**self.plan, "portfolio": {**portfolio, "horizon": "2026-12-04"}})

    def test_review_excludes_future_evidence_and_late_reviews_stay_reference_only(self):
        self.journal.record(self.first)
        decision_id = self.journal.evaluate(self.plan)["decision_id"]
        item = {"title": "Frozen payoff", "claim": "Sample claim", "status": "observed",
                "source": "journal://decision/" + decision_id, "available_at": "2026-11-25T21:00:04Z"}
        review = {"decision_id": decision_id, "as_of": "2026-11-25T21:00:03Z", "analyst": "Test",
                  "verdict": "watch", "summary": "Sample review", "invalidation": "Sample invalidation",
                  "evidence": [item], "forecasts": {"baseline": {"down": "0.4", "up": "0.6"},
                  "enhanced": {"down": "0.5", "up": "0.5"}}}
        with self.assertRaisesRegex(ValueError, "after its cutoff"):
            record_review(self.journal, review)
        review["as_of"] = item["available_at"]
        result = record_review(self.journal, review)
        self.assertEqual(result["comparison"]["status"], "reference_only")
        self.assertEqual(result["comparison"]["delta_dollars"], "-50.00")
        self.assertEqual(latest_review(self.journal, decision_id), result)

        review["evidence"] = [{**item, "available_at": "2026-11-25T21:00:01Z"}]
        with self.assertRaisesRegex(ValueError, "cannot precede its decision cutoff"):
            record_review(self.journal, review)

    def test_latest_review_uses_insertion_order_when_cutoff_ties(self):
        self.journal.record(self.first)
        decision_id = self.journal.evaluate(self.plan)["decision_id"]
        review = {"decision_id": decision_id, "as_of": self.plan["as_of"], "analyst": "Test",
                  "verdict": "watch", "invalidation": "Sample invalidation",
                  "evidence": [{"title": "Frozen payoff", "claim": "Sample claim", "status": "observed",
                                "source": "journal://decision/" + decision_id, "available_at": self.plan["as_of"]}]}
        with patch("frontier.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 25, 21, 0, 30, tzinfo=timezone.utc)
            for summary in ("First review", "Revised review", "Second revision", "Final revision"):
                result = record_review(self.journal, {**review, "summary": summary})
                self.assertEqual(latest_review(self.journal, decision_id), result)

    def test_publication_includes_only_selected_fields_and_decisions(self):
        self.journal.record(self.first)
        first = self.journal.evaluate(self.plan)
        other = self.journal.evaluate({**self.plan, "name": "unselected"})
        snapshot = {**self.journal.status(), "decisions": [
            {**first, "review": None, "private_metadata": "exclude me"},
            {**other, "review": None},
        ]}
        hosted = published_snapshot(snapshot, [first["decision_id"]], self.plan["as_of"])
        published = hosted["decisions"][0]
        self.assertEqual(len(hosted["decisions"]), 1)
        self.assertEqual(published["decision_id"], first["decision_id"])
        self.assertEqual(published["analysis"]["synthetic_ask_debit_dollars"], "220.00")
        self.assertNotIn("evidence", published)
        self.assertNotIn("portfolio_impact", published["analysis"])
        self.assertNotIn("probability", published["analysis"]["scenarios"][0])
        self.assertNotIn("private_metadata", published)

    def catalyst_plan(self):
        return {
            "name": "Earnings study", "universe_name": "Fixture", "universe": ["TEST"], "symbol": "TEST",
            "as_of": self.plan["as_of"], "expiry": EXPIRY, "right": "C", "long_strike": 100,
            "short_strike": 105, "quote_source": "robinhood", "quote_feed": "market_snapshot",
            "max_quote_age_seconds": 30, "max_leg_skew_seconds": 1, "fees_per_leg_dollars": 1,
            "stress_slippage_per_leg_dollars": 5, "minimum_target_r": 1, "target_scenario": "target",
            "catalyst": {"title": "Fixture release", "source": "https://example.com/release",
                         "known_at": "2026-11-24T12:00:00Z", "event_at": "2026-11-26T12:00:00Z"},
            "scenarios": [{"name": "loss", "terminal_stock_price": 90},
                          {"name": "target", "terminal_stock_price": 105},
                          {"name": "tail", "terminal_stock_price": 115}],
            "account_stress": {"illustrative_equity_dollars": 10000, "risk_fraction": "0.02",
                               "drawdown_limit": "0.20", "simultaneous_positions": 3, "loss_streak": 10},
        }

    def test_catalyst_ranks_target_and_preserves_uncapped_tail(self):
        self.journal.record(self.first)
        result = freeze_study(self.journal, self.catalyst_plan())
        outright, spread = result["candidates"]
        self.assertEqual(result["study_status"], "reference_only")
        self.assertEqual(result["ranked_structures"], ["debit_spread", "outright"])
        self.assertEqual(spread["max_loss_dollars"], "222.00")
        self.assertEqual(spread["target_pnl_dollars"], "278.00")
        self.assertEqual(spread["outcomes"][-1]["pnl_dollars"], "278.00")
        self.assertEqual(outright["outcomes"][-1]["pnl_dollars"], "1179.00")
        self.assertEqual(spread["account_stress"]["loss_streak_drawdown_pct"], "18.29")
        self.assertEqual(spread["account_stress"]["contracts_within_illustrative_budget"], 0)
        self.assertIsNone(spread["account_stress"]["simulated_breach_pct"])
        self.assertEqual(replay_study(self.journal, result["study_id"]), result)

    def test_catalyst_puts_and_expiry_gate(self):
        plan = {**self.catalyst_plan(), "right": "P", "long_strike": 105, "short_strike": 100}
        plan["scenarios"][1]["terminal_stock_price"] = 100
        legs = [{**q, "instrument": contract("TEST", EXPIRY, strike, "P")}
                for q, strike in zip(self.first["observations"], (105, 100))]
        self.journal.record(capture(*legs))
        result = freeze_study(self.journal, plan)
        self.assertEqual(result["candidates"][1]["max_loss_dollars"], "222.00")
        self.assertEqual(result["candidates"][1]["target_pnl_dollars"], "278.00")
        with self.assertRaisesRegex(ValueError, "expiry after"):
            freeze_study(self.journal, {**plan, "expiry": "2026-11-26"})

    def test_catalyst_forecast_reports_modeled_drawdown_and_requires_all_probabilities(self):
        self.journal.record(self.first)
        plan = self.catalyst_plan()
        plan["forecast_basis"] = "Synthetic test distribution"
        for scenario, probability in zip(plan["scenarios"], ("0.5", "0.4", "0.1")):
            scenario["probability"] = probability
        result = freeze_study(self.journal, plan)
        spread = result["candidates"][1]
        self.assertEqual(spread["expected_pnl_dollars"], "28.00")
        self.assertEqual(spread["stressed_expected_pnl_dollars"], "18.00")
        self.assertEqual(spread["account_stress"]["simulated_paths"], 2000)
        self.assertIsNotNone(spread["account_stress"]["terminal_equity_multiples"]["p50"])
        self.assertEqual(replay_study(self.journal, result["study_id"])["candidates"], result["candidates"])
        plan["scenarios"][0].pop("probability")
        with self.assertRaisesRegex(ValueError, "all scenarios"):
            freeze_study(self.journal, plan)


if __name__ == "__main__":
    unittest.main()
