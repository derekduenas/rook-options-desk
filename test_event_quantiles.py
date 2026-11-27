import unittest
from unittest.mock import patch

from earnings import resolve_entry
from event_quantiles import LEVELS, register, report, seal
from optionsdesk import utc
from research_graph import import_evidence
import test_earnings as fixtures


SEAL_AT = "2024-05-08T13:30:00Z"
SOURCE = "https://example.com/actual-results"


class EventQuantileTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.EarningsExperimentTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.journal = self.fixture.journal
        self.registration = self.fixture.register()
        self.event_id = self.registration["events"][0]["event_id"]
        self.recipe = {"experiment_id": self.registration["experiment_id"],
                       "recipes": {"numerical": "numeric-v1", "with_claims": "numeric-plus-claims-v1"}}
        with patch("event_quantiles.now_utc", return_value=utc(fixtures.PRE_AT)):
            register(self.journal, self.recipe)

    def claim(self, *, at="2024-05-07T20:06:00Z"):
        text = "Revenue guidance increased."
        with patch("research_graph.now_utc", return_value=utc(at)):
            return import_evidence(self.journal, {
                "document": {"source_url": SOURCE, "title": "Results", "text": text,
                             "published_at": fixtures.EVENT_AT, "first_seen_at": at},
                "claims": [{"subject": {"type": "event", "key": self.event_id}, "predicate": "reports",
                            "object": {"type": "metric", "key": "revenue_guidance_increased"},
                            "span": {"start": 0, "end": len(text), "text": text}, "origin": "model",
                            "extractor": {"provider": "fixture", "model": "v1", "prompt_id": "claims-v1"}}],
            })["edge_ids"][0]

    def case(self, claim):
        return {"event_id": self.event_id, "claim_ids": [claim],
                "forecasts": {"numerical": dict(zip(LEVELS, [-200, -100, 0, 100, 200])),
                              "with_claims": dict(zip(LEVELS, [0, 100, 200, 300, 400]))}}

    def test_sealed_pair_scores_against_same_later_underlying_move(self):
        case = self.case(self.claim())
        with patch("event_quantiles.now_utc", return_value=utc(SEAL_AT)):
            first = seal(self.journal, case)
        self.fixture.save_stock(fixtures.POST_AT, 100)
        entry = self.fixture.phase_entry(self.registration, fixtures.POST_AT, phase="post", action="pass",
                                         release={"source": SOURCE, "published_at": fixtures.EVENT_AT,
                                                  "first_seen_at": "2024-05-07T20:06:00Z"})
        self.fixture.save_stock(fixtures.EXIT_AT, 102)
        with patch("earnings.now_utc", return_value=utc(fixtures.EXIT_AT)):
            resolve_entry(self.journal, entry["entry_id"])
        with patch("event_quantiles.now_utc", return_value=utc(fixtures.EXIT_AT)):
            self.assertEqual(seal(self.journal, case), first)
            case["forecasts"]["numerical"]["0.50"] = 1
            with self.assertRaisesRegex(ValueError, "already has a sealed"):
                seal(self.journal, case)
            before = report(self.journal, fixtures.POST_AT)["reports"][0]
            self.assertEqual(before["outcomes"], {"exit_replay_unavailable": 1})
            scored = report(self.journal)["reports"][0]
        self.assertEqual(scored["outcomes"], {"scored": 1})
        self.assertEqual(scored["status"], "insufficient_event_days")
        self.assertEqual(scored["scores"]["with_claims"]["mean_pinball_loss_bps"], "18.000000")
        self.assertEqual(scored["scores"]["numerical"]["mean_pinball_loss_bps"], "58.000000")

    def test_rejects_late_claims_and_post_cutoff_forecasts(self):
        case = self.case(self.claim(at=fixtures.POST_AT))
        with patch("event_quantiles.now_utc", return_value=utc(SEAL_AT)):
            with self.assertRaisesRegex(ValueError, "available when"):
                seal(self.journal, case)
        with patch("event_quantiles.now_utc", return_value=utc("2024-05-08T13:35:00.000000001Z")):
            with self.assertRaisesRegex(ValueError, "post-release cutoff"):
                seal(self.journal, case)

    def test_recipe_cannot_be_replaced_and_crossed_quantiles_rejected(self):
        with self.assertRaisesRegex(ValueError, "frozen quantile recipes"):
            register(self.journal, {**self.recipe, "recipes": {"numerical": "v2", "with_claims": "v3"}})
        case = self.case(self.claim())
        case["forecasts"]["with_claims"]["0.50"] = 0
        with self.assertRaisesRegex(ValueError, "must be ordered"):
            seal(self.journal, case)

    def test_forecast_citing_different_release_is_excluded(self):
        case = self.case(self.claim())
        with patch("event_quantiles.now_utc", return_value=utc(SEAL_AT)):
            seal(self.journal, case)
        self.fixture.phase_entry(self.registration, fixtures.POST_AT, phase="post", action="pass",
                                 release={"source": "https://example.com/different-release", "published_at": fixtures.EVENT_AT,
                                          "first_seen_at": "2024-05-07T20:06:00Z"})
        with patch("event_quantiles.now_utc", return_value=utc(fixtures.POST_AT)):
            compared = report(self.journal)["reports"][0]
        self.assertEqual(compared["outcomes"], {"release_mismatch": 1})
        self.assertIsNone(compared["scores"])
