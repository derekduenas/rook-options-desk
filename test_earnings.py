import copy
import sqlite3
import unittest
from unittest.mock import patch

from earnings import list_experiments, record_entry, record_paper, register_experiment, resolve_entry
from optionsdesk import Journal, contract, utc


REGISTER_AT = "2024-05-07T19:00:00Z"
PRE_AT = "2024-05-07T19:45:00Z"
EVENT_AT = "2024-05-07T20:05:00Z"
POST_AT = "2024-05-08T13:35:00Z"
EXIT_AT = "2024-05-08T19:55:00Z"
EXPIRY = "2024-05-10"


class EarningsExperimentTests(unittest.TestCase):
    def setUp(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        self.journal = Journal(connection)
        self.protocol = {
            "name": "Fixture earnings comparison", "universe": ["TEST"], "risk_budget_dollars": 250,
            "quote_source": "alpaca", "quote_feed": "opra", "underlying_source": "alpaca", "underlying_feed": "sip",
            "max_quote_age_seconds": 3, "max_leg_skew_seconds": 1, "max_record_delay_seconds": 10,
            "option_fee_per_leg_dollars": 1, "equity_fee_dollars": 1,
            "events": [{"symbol": "TEST", "title": "Fixture earnings", "source": "https://example.com/release",
                        "known_at": "2024-05-07T18:00:00Z", "event_at": EVENT_AT, "pre_at": PRE_AT,
                        "post_at": POST_AT, "exit_at": EXIT_AT, "expiry": EXPIRY, "right": "C",
                        "long_strike": 100, "short_strike": 105}],
        }

    def register(self, at=REGISTER_AT, protocol=None):
        with patch("earnings.now_utc", return_value=utc(at)):
            return register_experiment(self.journal, self.protocol if protocol is None else protocol)

    def phase_entry(self, registration, at=PRE_AT, phase="pre", action="paper", **fields):
        with patch("earnings.now_utc", return_value=utc(at)):
            return record_entry(self.journal, {
                "experiment_id": registration["experiment_id"], "event_id": registration["events"][0]["event_id"],
                "phase": phase, "action": action, "reason": "Fixture decision", **fields,
            })

    def option_quote(self, strike, bid, ask, observed, size=8):
        return {"kind": "option_quote", "instrument": contract("TEST", EXPIRY, strike, "C"),
                "observed_at": observed, "bid": bid, "ask": ask, "bid_size": size, "ask_size": size,
                "multiplier": 100}

    def save_quotes(self, at, long, short, *, received=None, feed="opra", size=8):
        self.journal.record({"received_at": received or at, "source": "alpaca", "feed": feed,
                             "observations": [self.option_quote(100, *long, at, size),
                                              self.option_quote(105, *short, at, size)]})

    def save_stock(self, at, price):
        self.journal.record({"received_at": at, "source": "alpaca", "feed": "sip",
                             "observations": [{"kind": "underlying_trade", "instrument": "TEST",
                                               "observed_at": at, "session": "regular", "price": price}]})

    def test_registration_requires_known_evidence_and_precedes_the_decision(self):
        with self.assertRaises(ValueError):
            self.register("2024-05-07T17:59:59.999999999Z")
        with self.assertRaises(ValueError):
            self.register(PRE_AT)
        protocol = copy.deepcopy(self.protocol)
        protocol["events"][0]["post_at"] = EVENT_AT
        with self.assertRaises(ValueError):
            self.register(protocol=protocol)
        protocol = copy.deepcopy(self.protocol)
        protocol["events"][0]["expiry"] = "2024-05-07"
        with self.assertRaises(ValueError):
            self.register(protocol=protocol)
        result = self.register()
        self.assertEqual(result["registered_at"], utc(REGISTER_AT))
        self.assertEqual(result["events"][0]["symbol"], "TEST")
        self.assertTrue(result["events"][0]["event_id"])

    def test_pre_entry_freezes_the_cutoff_and_respects_cash_risk_limits(self):
        registration = self.register()
        self.save_quotes(PRE_AT, ("3.00", "3.20"), ("1.00", "1.25"))
        self.save_stock(PRE_AT, 100)
        # The favorable update shares the quote's event time but reached the desk after its cutoff.
        self.save_quotes(PRE_AT, ("0.70", "0.80"), ("0.10", "0.20"),
                         received="2024-05-07T19:45:00.000000001Z")
        with self.assertRaises(ValueError):
            self.phase_entry(registration, "2024-05-07T19:44:59.999999999Z")
        result = self.phase_entry(registration, "2024-05-07T19:45:10Z")
        rows = {candidate["structure"]: candidate for candidate in result["candidates"]}
        self.assertEqual(result["classification"], "prospective")
        self.assertEqual(rows["outright"]["status"], "risk_budget")
        self.assertEqual(rows["debit_spread"]["status"], "eligible")
        self.assertEqual(rows["debit_spread"]["quantity"], 1)
        self.assertEqual(rows["underlying"]["status"], "eligible")
        self.assertEqual(rows["underlying"]["quantity"], 2)
        self.assertEqual(rows["cash"]["status"], "eligible")
        self.assertEqual(self.phase_entry(registration, "2024-05-07T19:45:11Z"), result)
        with self.assertRaises(ValueError):
            self.phase_entry(registration, "2024-05-07T19:45:10Z", action="pass")

    def test_resolution_crosses_quote_sides_and_uses_the_fixed_exit_cutoff(self):
        registration = self.register()
        self.save_quotes(PRE_AT, ("3.00", "3.20"), ("1.00", "1.25"))
        self.save_stock(PRE_AT, 100)
        entry = self.phase_entry(registration)
        self.save_quotes(EXIT_AT, ("4.00", "4.20"), ("1.20", "1.40"))
        self.save_stock(EXIT_AT, 108)
        self.save_quotes(EXIT_AT, ("9.50", "9.60"), ("0.50", "0.60"),
                         received="2024-05-08T19:55:00.000000001Z")
        with patch("earnings.now_utc", return_value=utc("2024-05-08T19:54:59.999999999Z")):
            with self.assertRaises(ValueError):
                resolve_entry(self.journal, entry["entry_id"])
        with patch("earnings.now_utc", return_value=utc("2024-05-08T19:55:01Z")):
            resolution = resolve_entry(self.journal, entry["entry_id"])
        outcomes = {outcome["structure"]: outcome for outcome in resolution["outcomes"]}
        self.assertEqual(outcomes["cash"]["pnl_dollars"], "0.00")
        self.assertIsNone(outcomes["outright"]["pnl_dollars"])
        self.assertEqual(outcomes["debit_spread"]["pnl_dollars"], "36.00")
        self.assertEqual(outcomes["underlying"]["pnl_dollars"], "14.00")

    def test_resolution_leaves_pnl_unknown_when_exit_depth_cannot_cover_entry(self):
        registration = self.register(protocol={**self.protocol, "risk_budget_dollars": 1000})
        self.save_quotes(PRE_AT, ("3.00", "3.20"), ("1.00", "1.25"))
        entry = self.phase_entry(registration)
        self.save_quotes(EXIT_AT, ("4.00", "4.20"), ("1.20", "1.40"), size=1)
        with patch("earnings.now_utc", return_value=utc(EXIT_AT)):
            resolution = resolve_entry(self.journal, entry["entry_id"])
        outcomes = {outcome["structure"]: outcome for outcome in resolution["outcomes"]}
        self.assertEqual(outcomes["debit_spread"]["quantity"], 4)
        self.assertEqual(outcomes["debit_spread"]["status"], "insufficient_exit_size")
        self.assertIsNone(outcomes["debit_spread"]["pnl_dollars"])
        self.assertEqual(outcomes["cash"]["pnl_dollars"], "0.00")

    def test_resolution_excludes_spread_exit_mark_above_strike_width(self):
        registration = self.register()
        self.save_quotes(PRE_AT, ("3.00", "3.20"), ("1.00", "1.25"))
        entry = self.phase_entry(registration)
        self.save_quotes(EXIT_AT, ("6.75", "7.00"), ("1.00", "1.25"))
        with patch("earnings.now_utc", return_value=utc(EXIT_AT)):
            resolution = resolve_entry(self.journal, entry["entry_id"])
        outcomes = {outcome["structure"]: outcome for outcome in resolution["outcomes"]}
        self.assertEqual(outcomes["debit_spread"]["status"], "inconsistent_quote_exit")
        self.assertIsNone(outcomes["debit_spread"]["pnl_dollars"])
        self.assertEqual(outcomes["cash"]["pnl_dollars"], "0.00")

    def test_post_classification_depends_on_release_evidence_at_the_cutoff(self):
        registration = self.register()
        another = self.register(protocol={**self.protocol, "name": "Independent fixture"})
        self.save_quotes(POST_AT, ("4.00", "4.20"), ("1.20", "1.40"))
        self.save_stock(POST_AT, 108)
        release = {"source": "https://example.com/release", "published_at": EVENT_AT,
                   "first_seen_at": "2024-05-08T13:35:00.000000001Z"}
        with self.assertRaises(ValueError):
            self.phase_entry(registration, POST_AT, "post", release=release)
        stale = {**release, "published_at": "2024-05-07T03:59:59.999999999Z", "first_seen_at": POST_AT}
        with self.assertRaises(ValueError):
            self.phase_entry(registration, POST_AT, "post", release=stale)
        entry = self.phase_entry(registration, POST_AT, "post")
        self.assertEqual(entry["classification"], "release_unconfirmed")
        release["first_seen_at"] = POST_AT
        entry = self.phase_entry(another, POST_AT, "post", release=release)
        self.assertEqual(entry["classification"], "prospective")

    def test_post_earnings_prices_must_be_observed_after_release(self):
        registration = self.register()
        before_release = "2024-05-08T13:34:59.500Z"
        self.save_quotes(before_release, ("1.70", "2.00"), ("0.90", "1.10"))
        self.save_stock(before_release, 100)
        self.journal.record({"received_at": POST_AT, "source": "alpaca", "feed": "opra",
                             "observations": [self.option_quote(100, "1.70", "2.00", POST_AT)]})
        entry = self.phase_entry(registration, POST_AT, "post", release={
            "source": "https://example.com/release", "published_at": "2024-05-08T13:34:59.999999999Z",
            "first_seen_at": POST_AT,
        })
        candidates = {row["structure"]: row for row in entry["candidates"]}
        self.assertEqual(entry["classification"], "prospective")
        self.assertEqual(candidates["outright"]["quantity"], 1)
        self.assertEqual(candidates["debit_spread"]["status"], "pre_release_quote")
        self.assertEqual(candidates["underlying"]["status"], "pre_release_underlying")
        self.assertEqual(candidates["cash"]["status"], "eligible")
        self.assertEqual(entry["evidence"]["options"][1]["observed_at"], utc(before_release))

    def test_early_release_updates_effective_classification_without_editing_the_frozen_entry(self):
        registration = self.register()
        pre_recorded_at = "2024-05-07T19:45:01Z"
        pre = self.phase_entry(registration, pre_recorded_at)
        self.assertEqual(pre["classification"], "prospective")
        self.phase_entry(registration, POST_AT, "post", release={
            "source": "https://example.com/release", "published_at": "2024-05-07T19:44:59.999999999Z",
            "first_seen_at": "2024-05-07T19:45:02Z",
        })
        event = list_experiments(self.journal)[0]["events"][0]
        reported = next(entry for entry in event["entries"] if entry["phase"] == "pre")
        self.assertEqual(reported["entry_id"], pre["entry_id"])
        self.assertEqual(reported["classification"], "pre_after_release")
        self.assertEqual(reported["recorded_classification"], "prospective")
        self.assertEqual(self.phase_entry(registration, POST_AT), pre)

    def test_release_between_pre_decision_and_pre_fill_excludes_only_the_fill(self):
        registration = self.register()
        self.save_quotes(PRE_AT, ("3.00", "3.20"), ("1.00", "1.25"))
        pre = self.phase_entry(registration, "2024-05-07T19:45:01Z")
        release_recorded = "2024-05-08T13:35:01Z"
        self.phase_entry(registration, release_recorded, "post", release={
            "source": "https://example.com/release", "published_at": "2024-05-07T19:45:02.500Z",
            "first_seen_at": "2024-05-07T19:45:04Z",
        })
        ticket = {"entry_id": pre["entry_id"], "structure": "debit_spread", "status": "filled",
                  "source": "paper_broker", "reference": "released-before-fill",
                  "attempted_at": "2024-05-07T19:45:02Z", "entry_at": "2024-05-07T19:45:03Z",
                  "exit_at": EXIT_AT, "quantity": 1, "entry_net_price": "2.20", "exit_net_price": "2.60",
                  "fees_dollars": 4}
        with patch("earnings.now_utc", return_value=utc("2024-05-08T20:00:00Z")):
            fill = record_paper(self.journal, ticket)
        event = list_experiments(self.journal)[0]["events"][0]
        entry = next(item for item in event["entries"] if item["phase"] == "pre")
        self.assertEqual(entry["classification"], "prospective")
        self.assertEqual(entry["paper"][0], {**fill, "classification": "pre_after_release",
                                            "classification_effective_at": utc(release_recorded)})
        with patch("earnings.now_utc", return_value=utc("2024-05-08T20:01:00Z")):
            self.assertEqual(record_paper(self.journal, ticket), fill)

    def test_paper_pnl_uses_actual_prices_and_enforces_depth_and_budget(self):
        registration = self.register(protocol={**self.protocol, "risk_budget_dollars": 1000})
        self.save_quotes(PRE_AT, ("3.00", "3.20"), ("1.00", "1.25"), size=1)
        self.save_stock(PRE_AT, 100)
        recorded_at = "2024-05-07T19:45:01Z"
        entry = self.phase_entry(registration, recorded_at)
        spread = next(candidate for candidate in entry["candidates"] if candidate["structure"] == "debit_spread")
        self.assertEqual(spread["quantity"], 1)
        self.assertEqual(spread["entry_net_price"], "2.20")
        paper = {"entry_id": entry["entry_id"], "structure": "debit_spread", "status": "filled",
                 "source": "paper_broker", "reference": "fixture-order", "attempted_at": recorded_at,
                 "quantity": 1, "entry_at": "2024-05-07T19:45:02Z", "exit_at": EXIT_AT,
                 "entry_net_price": "2.25", "exit_net_price": "2.55", "fees_dollars": 4}
        with patch("earnings.now_utc", return_value=utc("2024-05-08T20:00:00Z")):
            for change in ({"quantity": 2}, {"attempted_at": PRE_AT}, {"fees_dollars": 800},
                           {"exit_at": "2024-05-08T19:55:00.000000001Z"}):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    record_paper(self.journal, {**paper, **change})
            with self.assertRaisesRegex(ValueError, "below the registered strike width"):
                record_paper(self.journal, {**paper, "entry_net_price": 5})
            with self.assertRaisesRegex(ValueError, "unfilled attempt"):
                record_paper(self.journal, {**paper, "status": "unfilled"})
            result = record_paper(self.journal, paper)
            self.assertEqual(record_paper(self.journal, {**paper, "entry_net_price": "2.2500", "fees_dollars": "4.00"}),
                             result)
        self.assertEqual(result["pnl_dollars"], "26.00")

    def test_sizing_reserves_both_entry_and_exit_fees(self):
        registration = self.register(protocol={**self.protocol, "risk_budget_dollars": 446})
        self.save_quotes(PRE_AT, ("3.00", "3.20"), ("1.00", "1.25"))
        self.save_stock(PRE_AT, 100)
        entry = self.phase_entry(registration)
        spread = next(candidate for candidate in entry["candidates"] if candidate["structure"] == "debit_spread")
        self.assertEqual(spread["quantity"], 1)

    def test_late_entries_and_passes_cannot_report_paper_fills(self):
        registration = self.register()
        pass_registration = self.register(protocol={**self.protocol, "name": "Pass fixture"})
        self.save_quotes(PRE_AT, ("3.00", "3.20"), ("1.00", "1.25"))
        self.save_stock(PRE_AT, 100)
        entry = self.phase_entry(registration, "2024-05-07T19:45:10.000000001Z")
        self.assertEqual(entry["classification"], "late")
        passed = self.phase_entry(pass_registration, action="pass")
        with patch("earnings.now_utc", return_value=utc("2024-05-08T20:00:00Z")):
            for decision in (entry, passed):
                with self.subTest(decision=decision["entry_id"]), self.assertRaises(ValueError):
                    record_paper(self.journal, {
                        "entry_id": decision["entry_id"], "structure": "debit_spread", "status": "filled",
                        "source": "paper_broker", "reference": "fixture-order",
                        "attempted_at": "2024-05-07T19:45:11Z", "quantity": 1,
                        "entry_at": "2024-05-07T19:45:12Z", "exit_at": EXIT_AT,
                        "entry_net_price": "2.20", "exit_net_price": "2.60", "fees_dollars": 4,
                    })


if __name__ == "__main__":
    unittest.main()
