from datetime import date, timedelta
import sqlite3
import unittest

from forecast import FEATURES, TRAIN_DAYS, forecast_report, scores, walk_forward
from optionsdesk import Journal, utc


def fixture(days):
    rows = []
    for index in range(days):
        day = (date(2024, 4, 1) + timedelta(days=index)).isoformat()
        label = index % 2
        features = ((label - .5) * .2, .03, .02, .15, .4, 1)
        rows.append({"event_id": str(index), "entry_id": f"entry-{index}", "replay_id": f"replay-{index}",
                     "event_day": day, "cutoff": utc(day + "T19:45:00Z"),
                     "label_available_at": utc(day + "T23:00:00Z"), "features": features, "label": label})
    return rows


class ForecastTests(unittest.TestCase):
    def test_walk_forward_scores_theoretical_signal_and_excludes_unavailable_labels(self):
        rows = fixture(32)
        regular = walk_forward(rows)
        self.assertEqual(regular[0]["event_id"], str(TRAIN_DAYS))
        self.assertLess(scores(regular, "model_probability")["brier"], scores(regular, "baseline_probability")["brier"])
        rows[0]["label_available_at"] = rows[-1]["cutoff"]
        delayed = walk_forward(rows)
        self.assertEqual(delayed[0]["event_id"], str(TRAIN_DAYS + 1))
        self.assertEqual(delayed[0]["training_event_days"], TRAIN_DAYS)
        self.assertEqual(sum(row["observations"] for row in scores(delayed, "model_probability")["reliability"]), len(delayed))

    def test_same_event_day_never_enters_its_own_fit(self):
        rows = fixture(21)
        second = {**rows[-1], "event_id": "same-day", "entry_id": "entry-duplicate-day",
                  "label_available_at": rows[-1]["cutoff"]}
        predicted = walk_forward([*rows, second])
        self.assertEqual(len(predicted), 2)
        self.assertEqual(predicted[0]["training_manifest"], predicted[1]["training_manifest"])
        self.assertEqual(len(rows[-1]["features"]), len(FEATURES))

    def test_fit_uses_corrections_only_after_they_arrive(self):
        rows = fixture(23)
        by_day = {row["event_id"]: row for row in walk_forward(rows)}
        already_corrected = {row["event_id"]: row for row in walk_forward(rows[1:])}
        rows[0]["invalidated_at"] = rows[21]["cutoff"]
        rows[22]["invalidated_at"] = "2024-05-01T00:00:00.000000000+00:00"

        replayed = {row["event_id"]: row for row in walk_forward(rows)}
        self.assertEqual(replayed["20"], by_day["20"])
        self.assertEqual(replayed["21"], already_corrected["21"])
        self.assertNotIn("22", replayed)

    def test_empty_journal_has_no_scores(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        report = forecast_report(Journal(db), {})
        self.assertEqual(report["reports"], [])
