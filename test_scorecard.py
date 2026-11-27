from datetime import date, timedelta
from decimal import Decimal
import unittest
from unittest.mock import patch

from earnings import record_paper, resolve_entry
from optionsdesk import utc
from scorecard import MIN_INTERVAL_DAYS, cohort_report, estimate
import test_earnings as fixtures


class ScorecardTests(unittest.TestCase):
    def setUp(self):
        # Share the real journal and quote fixture with the earnings engine tests.
        self.fixture = fixtures.EarningsExperimentTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.journal = self.fixture.journal

    def test_pending_to_missed_transition_and_future_registration(self):
        self.fixture.register()
        self.assertEqual(cohort_report(self.journal, as_of="2024-05-07T18:59:59.999999999Z")["cohorts"], [])
        at_deadline = cohort_report(self.journal, as_of="2024-05-07T19:45:10Z")
        pre, post = at_deadline["cohorts"]
        self.assertEqual(pre["coverage"]["pending"], 1)
        self.assertEqual(post["coverage"]["pending"], 1)
        self.assertIsNone(pre["replay"]["structures"]["cash"]["mean_net_pnl_dollars"])
        late = cohort_report(self.journal, as_of="2024-05-07T19:45:10.000000001Z")
        self.assertEqual(late["cohorts"][0]["coverage"]["missed"], 1)
        self.assertEqual(late["cohorts"][1]["coverage"]["missed"], 0)

    def test_as_of_cannot_see_later_release_classification(self):
        registration = self.fixture.register()
        self.fixture.phase_entry(registration, "2024-05-07T19:45:01Z")
        self.fixture.phase_entry(registration, fixtures.POST_AT, "post", release={
            "source": "https://example.com/release", "published_at": "2024-05-07T19:44:59.999999999Z",
            "first_seen_at": "2024-05-07T19:45:02Z",
        })
        early = cohort_report(self.journal, as_of="2024-05-07T19:46:00Z")["cohorts"][0]["coverage"]
        later = cohort_report(self.journal, as_of=fixtures.POST_AT)["cohorts"][0]["coverage"]
        self.assertEqual((early["eligible"], early["replay_pending"]), (1, 1))
        self.assertEqual((later["eligible"], later["excluded"], later["pre_after_release"]), (0, 1, 1))

    def test_replay_pass_is_hypothetical_and_pairing_excludes_unmatched(self):
        registration = self.fixture.register()
        self.fixture.save_quotes(fixtures.PRE_AT, ("3.00", "3.20"), ("1.00", "1.25"))
        self.fixture.save_stock(fixtures.PRE_AT, 100)
        entry = self.fixture.phase_entry(registration, action="pass")
        self.fixture.save_quotes(fixtures.EXIT_AT, ("4.00", "4.20"), ("1.20", "1.40"))
        self.fixture.save_stock(fixtures.EXIT_AT, 108)
        resolved_at = "2024-05-08T19:55:01Z"
        with patch("earnings.now_utc", return_value=utc(resolved_at)):
            resolve_entry(self.journal, entry["entry_id"])
        pre = cohort_report(self.journal, as_of=fixtures.EXIT_AT)["cohorts"][0]
        self.assertEqual(pre["coverage"]["replay_missing"], 1)
        self.assertIsNone(pre["replay"]["structures"]["cash"]["mean_net_pnl_dollars"])

        report = cohort_report(self.journal, as_of=resolved_at)
        pre = report["cohorts"][0]
        self.assertEqual(pre["coverage"]["eligible_passes"], 1)
        self.assertEqual(pre["coverage"]["replay_complete"], 1)
        rows = pre["replay"]["structures"]
        self.assertEqual(rows["debit_spread"]["mean_net_pnl_dollars"], "36.00")
        self.assertEqual(rows["underlying"]["mean_net_pnl_dollars"], "14.00")
        self.assertEqual(rows["cash"]["mean_net_pnl_dollars"], "0.00")
        self.assertIsNone(rows["outright"]["mean_net_pnl_dollars"])
        unmatched = pre["replay"]["comparisons"][0]
        self.assertEqual((unmatched["paired"], unmatched["right_only"], unmatched["mean_delta_dollars"]), (0, 1, None))
        paired = pre["replay"]["comparisons"][2]
        self.assertEqual((paired["paired"], paired["mean_delta_dollars"], paired["interval_95_dollars"]), (1, "22.00", None))
        self.assertEqual(pre["paper"]["structures"]["debit_spread"]["eligible_candidates"], 0)
        phase = next(row for row in report["phase_comparisons"] if row["structure"] == "debit_spread")
        self.assertEqual((phase["paired"], phase["left_only"], phase["right_only"]), (0, 1, 0))

    def test_paper_attempt_denominators_and_contemporaneous_fill_slippage(self):
        registration = self.fixture.register(protocol={**self.fixture.protocol, "risk_budget_dollars": 1000})
        self.fixture.save_quotes(fixtures.PRE_AT, ("3.00", "3.20"), ("1.00", "1.25"))
        self.fixture.save_stock(fixtures.PRE_AT, 100)
        entry = self.fixture.phase_entry(registration, "2024-05-07T19:45:01Z")
        entry_fill_at = "2024-05-07T19:45:02Z"
        self.fixture.save_quotes(entry_fill_at, ("3.00", "3.25"), ("1.00", "1.25"))
        self.fixture.save_quotes("2024-05-07T19:45:03Z", ("3.30", "3.50"), ("1.00", "1.20"),
                                 received="2024-05-07T19:45:03.000000001Z")
        self.fixture.save_quotes("2024-05-07T19:45:07Z", ("2.90", "3.10"), ("1.00", "1.20"))
        self.fixture.save_quotes("2024-05-07T19:45:12Z", ("3.60", "3.80"), ("1.10", "1.30"))
        self.fixture.save_quotes("2024-05-07T19:50:02Z", ("3.20", "3.40"), ("1.05", "1.25"))
        self.fixture.save_quotes(fixtures.EXIT_AT, ("4.00", "4.20"), ("1.20", "1.40"))
        ledger_at = "2024-05-08T20:00:00Z"
        ledger = {"entry_id": entry["entry_id"], "source": "paper_broker", "attempted_at": entry_fill_at}
        with patch("earnings.now_utc", return_value=utc(ledger_at)):
            record_paper(self.journal, {**ledger, "structure": "debit_spread", "status": "partial",
                                       "reference": "partial-spread", "quantity": 1, "entry_at": entry_fill_at,
                                       "exit_at": fixtures.EXIT_AT, "entry_net_price": "2.26",
                                       "exit_net_price": "2.55", "fees_dollars": "4.00"})
            record_paper(self.journal, {**ledger, "structure": "outright", "status": "unfilled", "reference": "miss"})
        before_ingestion = cohort_report(self.journal, as_of=fixtures.EXIT_AT)["cohorts"][0]["paper"]["structures"]
        self.assertEqual(before_ingestion["debit_spread"]["attempted"], 0)
        rows = cohort_report(self.journal, as_of=ledger_at)["cohorts"][0]["paper"]["structures"]
        spread, outright, stock = rows["debit_spread"], rows["outright"], rows["underlying"]
        self.assertEqual((spread["eligible_after_exit"], spread["attempted"], spread["partial"]), (1, 1, 1))
        self.assertEqual((spread["any_fill_rate_pct"], spread["full_fill_rate_pct"], spread["unit_fill_rate_pct"]),
                         ("100.00", "0.00", "25.00"))
        self.assertEqual((spread["mean_entry_slippage_dollars"], spread["mean_exit_slippage_dollars"],
                          spread["mean_round_trip_slippage_dollars"]), ("1.00", "5.00", "6.00"))
        self.assertEqual(spread["entry_markouts"]["1"]["midpoint_move_samples"], 0)
        self.assertEqual(spread["entry_markouts"]["1"]["incomplete_reasons"], {"followup_no_post_fill_quote": 1})
        self.assertEqual(spread["entry_markouts"]["5"]["mean_midpoint_move_dollars"], "-10.00")
        self.assertEqual(spread["entry_markouts"]["5"]["mean_fill_to_midpoint_dollars"], "-36.00")
        self.assertEqual(spread["entry_markouts"]["10"], {
            "eligible_fills": 1, "midpoint_move_samples": 1, "mean_midpoint_move_dollars": "50.00",
            "fill_to_midpoint_samples": 1, "mean_fill_to_midpoint_dollars": "24.00",
            "incomplete_reasons": {},
        })
        self.assertEqual(spread["entry_markouts"]["60"]["fill_to_midpoint_samples"], 0)
        self.assertEqual(spread["entry_markouts"]["60"]["incomplete_reasons"], {"followup_stale": 1})
        self.assertEqual(spread["entry_markouts"]["300"]["mean_midpoint_move_dollars"], "15.00")
        self.assertEqual(spread["entry_markouts"]["300"]["mean_fill_to_midpoint_dollars"], "-11.00")
        self.assertEqual((spread["mean_entry_debit_dollars"], spread["mean_fees_dollars"],
                          spread["mean_net_pnl_dollars"], spread["mean_hold_seconds"]),
                         ("226.00", "4.00", "25.00", 86998.0))
        self.assertEqual((outright["unfilled"], outright["any_fill_rate_pct"], outright["mean_net_pnl_dollars"]),
                         (1, "0.00", None))
        self.assertEqual((stock["missing_after_exit"], stock["attempt_coverage_pct"], stock["any_fill_rate_pct"]),
                         (1, "0.00", None))

    def test_uncertainty_requires_distinct_event_dates(self):
        day = date(2024, 5, 7)
        clustered = [(day, Decimal(index)) for index in range(MIN_INTERVAL_DAYS * 3)]
        self.assertIsNone(estimate(clustered)[1])
        diverse = [(day + timedelta(days=index), Decimal(index)) for index in range(MIN_INTERVAL_DAYS)]
        mean, interval, days = estimate(diverse)
        self.assertEqual(days, MIN_INTERVAL_DAYS)
        self.assertLess(Decimal(interval[0]), Decimal(mean))
        self.assertGreater(Decimal(interval[1]), Decimal(mean))

    def test_missing_exit_and_late_fill_quote_leave_comparisons_unknown(self):
        registration = self.fixture.register()
        self.fixture.save_quotes(fixtures.PRE_AT, ("3.00", "3.20"), ("1.00", "1.25"))
        entry = self.fixture.phase_entry(registration, "2024-05-07T19:45:01Z")
        fill_at = "2024-05-07T19:45:05Z"
        self.fixture.save_quotes(fill_at, ("3.00", "3.20"), ("1.00", "1.25"),
                                 received="2024-05-07T19:45:05.000000001Z")
        recorded_at = "2024-05-08T19:55:01Z"
        with patch("earnings.now_utc", return_value=utc(recorded_at)):
            resolve_entry(self.journal, entry["entry_id"])
            record_paper(self.journal, {"entry_id": entry["entry_id"], "structure": "debit_spread", "status": "filled",
                                       "source": "paper_broker", "reference": "fill-no-quotes", "attempted_at": fill_at,
                                       "quantity": 1, "entry_at": fill_at, "exit_at": fixtures.EXIT_AT,
                                       "entry_net_price": "2.20", "exit_net_price": "2.40", "fees_dollars": 4})
        pre = cohort_report(self.journal, as_of=recorded_at)["cohorts"][0]
        self.assertEqual(pre["coverage"]["replay_partial"], 1)
        modeled = pre["replay"]["structures"]["debit_spread"]
        self.assertEqual((modeled["missing"], modeled["mean_net_pnl_dollars"]), (1, None))
        actual = pre["paper"]["structures"]["debit_spread"]
        self.assertEqual((actual["mean_net_pnl_dollars"], actual["round_trip_slippage_samples"]), ("16.00", 0))
        self.assertIsNone(actual["mean_entry_slippage_dollars"])
        self.assertIsNone(actual["mean_exit_slippage_dollars"])

    def test_cash_only_does_not_count_as_complete_market_replay(self):
        registration = self.fixture.register()
        entry = self.fixture.phase_entry(registration)
        with patch("earnings.now_utc", return_value=utc(fixtures.EXIT_AT)):
            resolve_entry(self.journal, entry["entry_id"])
        pre = cohort_report(self.journal, as_of=fixtures.EXIT_AT)["cohorts"][0]
        counts = pre["coverage"]
        self.assertEqual((counts["replay_complete"], counts["replay_cash_only"],
                          counts["replay_no_market_outcomes"]), (0, 1, 1))
        rows = pre["replay"]["structures"]
        self.assertEqual(rows["cash"]["mean_net_pnl_dollars"], "0.00")
        self.assertIsNone(rows["debit_spread"]["mean_net_pnl_dollars"])
        self.assertEqual(rows["debit_spread"]["entry_blockers"], {"missing": 1})

    def test_paper_attempt_excluded_only_after_earlier_release_becomes_known(self):
        registration = self.fixture.register()
        self.fixture.save_quotes(fixtures.PRE_AT, ("3.00", "3.20"), ("1.00", "1.25"))
        entry = self.fixture.phase_entry(registration, "2024-05-07T19:45:01Z")
        imported = "2024-05-07T20:01:00Z"
        with patch("earnings.now_utc", return_value=utc(imported)):
            record_paper(self.journal, {"entry_id": entry["entry_id"], "structure": "debit_spread", "status": "filled",
                                       "source": "paper_broker", "reference": "early-release-fill", "quantity": 1,
                                       "attempted_at": "2024-05-07T19:47:00Z", "entry_at": "2024-05-07T19:47:02Z",
                                       "exit_at": "2024-05-07T19:55:00Z", "entry_net_price": "2.20",
                                       "exit_net_price": "2.40", "fees_dollars": 4})
        self.fixture.phase_entry(registration, fixtures.POST_AT, "post", release={
            "source": "https://example.com/release", "published_at": "2024-05-07T19:46:00Z",
            "first_seen_at": fixtures.POST_AT,
        })
        earlier = cohort_report(self.journal, as_of=imported)["cohorts"][0]
        measured = earlier["paper"]["structures"]["debit_spread"]
        self.assertEqual((measured["attempted"], measured["mean_net_pnl_dollars"]), (1, "16.00"))
        later = cohort_report(self.journal, as_of=fixtures.EXIT_AT)["cohorts"][0]
        self.assertEqual((later["coverage"]["eligible"], later["coverage"]["excluded_paper_attempts"]), (1, 1))
        excluded = later["paper"]["structures"]["debit_spread"]
        self.assertEqual((excluded["attempted"], excluded["excluded_after_exit"], excluded["missing_after_exit"]),
                         (0, 1, 0))
        self.assertIsNone(excluded["mean_net_pnl_dollars"])


if __name__ == "__main__":
    unittest.main()
