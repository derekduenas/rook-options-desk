"""Integration coverage for the forecast's journal inputs and as-of cutoff."""

import sqlite3
import unittest
from unittest.mock import patch

from earnings import list_experiments, record_entry, register_experiment, resolve_entry
from forecast import FEATURES, forecast_report, sample_rows
from optionsdesk import Journal, contract, utc


class IntelligenceIntegrationTests(unittest.TestCase):
    def test_closed_earnings_replay_supplies_a_label_only_after_journal_receipt(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        journal = Journal(db)
        pre = "2025-09-09T19:55:00Z"
        exit_at = "2025-09-10T19:55:00Z"
        resolved_at = "2025-09-10T19:55:01Z"
        expiry = "2025-09-12"
        event = {"title": "Fixture fiscal earnings", "symbol": "TEST", "source": "https://example.com/ir",
                 "known_at": "2025-09-08T13:00:00Z", "pre_at": pre, "event_at": "2025-09-09T20:30:00Z",
                 "post_at": "2025-09-10T13:35:00Z", "exit_at": exit_at, "expiry": expiry,
                 "right": "C", "long_strike": 100, "short_strike": 105}
        protocol = {"name": "Forecast fixture", "quote_source": "alpaca", "quote_feed": "opra",
                    "underlying_source": "alpaca", "underlying_feed": "sip", "universe": ["TEST"],
                    "risk_budget_dollars": 500, "max_quote_age_seconds": 3, "max_leg_skew_seconds": 1,
                    "max_record_delay_seconds": 5, "option_fee_per_leg_dollars": 1, "equity_fee_dollars": 0,
                    "events": [event]}
        with patch("earnings.now_utc", return_value=utc("2025-09-09T18:00:00Z")):
            experiment = register_experiment(journal, protocol)

        for time, stock, legs in ((pre, 100, [(100, 3, 3.2), (105, 1, 1.2)]),
                                  (exit_at, 107, [(100, 4, 4.2), (105, 1.1, 1.3)])):
            journal.record({"received_at": time, "source": "alpaca", "feed": "opra", "observations": [
                {"kind": "option_quote", "instrument": contract("TEST", expiry, strike, "C"),
                 "observed_at": time, "bid": bid, "ask": ask, "bid_size": 10, "ask_size": 10,
                 "multiplier": 100, "implied_volatility": 0.42}
                for strike, bid, ask in legs]})
            journal.record({"received_at": time, "source": "alpaca", "feed": "sip", "observations": [
                {"kind": "underlying_trade", "instrument": "TEST", "observed_at": time,
                 "session": "regular", "price": stock}]})

        experiment_id = experiment["experiment_id"]
        event_id = experiment["events"][0]["event_id"]
        with patch("earnings.now_utc", return_value=utc(pre)):
            entry = record_entry(journal, {"experiment_id": experiment_id, "event_id": event_id,
                                           "phase": "pre", "action": "pass", "reason": "Fixture"})
        with patch("earnings.now_utc", return_value=utc(resolved_at)):
            resolve_entry(journal, entry["entry_id"])

        experiment = list_experiments(journal)[0]
        self.assertEqual(sample_rows(experiment, "pre", "debit_spread_profitable", utc(exit_at)),
                         ([], {"replay_unavailable": 1}))
        for target in ("stock_up", "outright_profitable", "debit_spread_profitable"):
            with self.subTest(target=target):
                rows, gaps = sample_rows(experiment, "pre", target, utc(resolved_at))
                self.assertEqual(gaps, {})
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["label"], 1)
                self.assertEqual(dict(zip(FEATURES, rows[0]["features"]))["long_iv"], 0.42)

        report = forecast_report(journal, {"as_of": resolved_at, "experiment_id": experiment_id})["reports"][0]
        self.assertEqual(report["eligible_labeled_events"], 1)
        self.assertEqual(report["status"], "insufficient_outcomes")
        self.assertIsNone(report["scores"])

        corrected_at = "2025-09-11T15:30:00Z"
        with patch("earnings.now_utc", return_value=utc(corrected_at)):
            record_entry(journal, {"experiment_id": experiment_id, "event_id": event_id, "phase": "post",
                                   "action": "pass", "reason": "Fixture late release confirmation",
                                   "release": {"source": "https://example.com/report",
                                               "published_at": "2025-09-09T19:54:00Z", "first_seen_at": pre}})
        experiment = list_experiments(journal)[0]
        historical, gaps = sample_rows(experiment, "pre", "stock_up", utc(resolved_at))
        self.assertEqual(gaps, {})
        self.assertIsNone(historical[0]["invalidated_at"])
        corrected, gaps = sample_rows(experiment, "pre", "stock_up", utc(corrected_at))
        self.assertEqual(gaps, {"pre_after_release": 1})
        self.assertEqual(corrected[0]["invalidated_at"], utc(corrected_at))
        current = forecast_report(journal, {"as_of": corrected_at, "experiment_id": experiment_id})["reports"][0]
        self.assertEqual(current["eligible_labeled_events"], 0)
