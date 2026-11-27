import unittest

from earnings_direction import event_rows, option_overlay, score


class DirectionResearchTest(unittest.TestCase):
    def setUp(self):
        self.days = [f"2026-06-{day}" for day in (10, 11, 12, 15, 16)]
        self.plan = {
            "universe": ["ABC"],
            "event_window": {"start": "2026-06-01", "end": "2026-06-30"},
            "signal": {"benchmark": "QQQ", "ticker_lookback_completed_sessions": 2},
        }
        self.evidence = {
            "sessions": [{"day": day, "close_et": "16:00:00"} for day in self.days],
            "reports": {"ABC": [{"day": self.days[3], "reported": True, "verified": True, "timing": "pm"}]},
            "closes": {"ABC": dict(zip(self.days, (100, 111, 120, 80, 78))),
                       "QQQ": dict(zip(self.days, (100, 101, 110, 150, 160)))},
        }

    def test_features_use_completed_sessions_before_release_and_ignore_release_day(self):
        rows, exclusions = event_rows(self.plan, self.evidence)
        self.assertFalse(exclusions)
        row, = rows
        self.assertEqual((row["lookback_start"], row["lookback_end"], row["exit_day"]),
                         (self.days[0], self.days[2], self.days[4]))
        self.assertEqual(row["relative_return_pct"], "10.00")
        self.assertTrue(row["predicted_up"])
        self.assertFalse(row["actual_up"])
        self.assertIsNone(score(rows, lambda item: item["predicted_up"])["balanced_accuracy"])
        del self.evidence["closes"]["QQQ"][self.days[2]]
        rows, exclusions = event_rows(self.plan, self.evidence)
        self.assertFalse(rows)
        self.assertEqual(exclusions[0]["missing"], ["benchmark_end"])

    def test_morning_and_unverified_reports_do_not_score(self):
        self.evidence["reports"]["ABC"] = [{"day": self.days[3], "reported": True, "verified": True, "timing": "am"},
                                           {"day": self.days[4], "reported": True, "verified": False, "timing": "pm"}]
        rows, exclusions = event_rows(self.plan, self.evidence)
        self.assertFalse(rows)
        self.assertEqual([row["status"] for row in exclusions], ["unconfirmed_after_close"] * 2)

    def test_overlay_keeps_matched_events_and_reports_unpriced_exits(self):
        def event(symbol, outright, spread):
            arms = {
                "outright": {"pnl": outright, "outcome": {"status": "modeled"}},
                "debit_spread": {"pnl": spread, "outcome":
                                 {"status": "modeled" if spread else "insufficient_exit_size"}},
            }
            return {"symbol": symbol, "release": self.days[3], "exit_session": self.days[4],
                    "variants": {"selected_1555": {"arms": arms}}}

        rows = [{"symbol": ticker, "day": self.days[3], "exit_day": self.days[4], "predicted_up": predicted}
                for ticker, predicted in (("ABC", True), ("DEF", False), ("GHI", True))]
        replay = {"results": [event("ABC", "-5.00", "-3.00"), event("DEF", "-2.00", "-1.00"),
                              event("GHI", "10.00", None)]}
        result = option_overlay(rows, replay, "selected_1555")
        self.assertEqual(result["up_signals"], 1)
        self.assertEqual(result["totals"]["outright"], {"always_on_pnl_dollars": "-7.00",
                                                        "signal_or_cash_pnl_dollars": "-5.00"})
        self.assertEqual(result["excluded"][0]["unresolved_arms"], {"debit_spread": "insufficient_exit_size"})
