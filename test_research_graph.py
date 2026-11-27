import sqlite3
import unittest
from unittest.mock import patch

from optionsdesk import Journal, utc
from research_graph import graph_as_of, import_evidence


class ResearchGraphTests(unittest.TestCase):
    def setUp(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        self.journal = Journal(db)
        self.raw = {"document": {"source_url": "https://example.com/issuer", "title": "Issuer release",
                                  "text": "Café Corp schedules earnings on December 17.",
                                  "published_at": "2026-11-20T12:00:00Z", "first_seen_at": "2026-11-20T12:01:00Z"},
                    "claims": [{"subject": {"type": "issuer", "key": "cafe-corp"}, "predicate": "announces",
                                "object": {"type": "event", "key": "cafe-2026-q4"},
                                "span": {"start": 0, "end": 28, "text": "Café Corp schedules earnings"},
                                "valid_to": "2026-12-18T00:00:00Z", "origin": "human", "extractor": "researcher"}]}

    def import_at(self, when, raw=None):
        with patch("research_graph.now_utc", return_value=utc(when)):
            return import_evidence(self.journal, self.raw if raw is None else raw)

    def test_separate_late_knowledge_from_market_validity_and_deduplicate(self):
        first = self.import_at("2026-11-26T21:00:00Z")
        retry = self.import_at("2026-11-26T21:01:00Z")
        self.assertEqual(first["edge_ids"], retry["edge_ids"])
        self.assertEqual((first["inserted_edges"], retry["inserted_edges"]), (1, 0))
        earlier = graph_as_of(self.journal, as_of="2026-11-26T20:59:59.999999999Z")
        self.assertEqual(earlier["edges"], [])
        subject = {"type": "issuer", "key": "cafe-corp"}
        ended = graph_as_of(self.journal, as_of="2026-12-18T00:00:00Z", subject=subject)
        self.assertEqual(ended["edges"], [])
        historical = graph_as_of(self.journal, as_of="2026-12-18T00:00:00Z", effective_at="2026-11-20T20:00:00Z", subject=subject)
        self.assertEqual(historical["edges"][0]["id"], first["edge_ids"][0])
        self.assertEqual(historical["documents"][0]["recorded_at"], utc("2026-11-26T21:00:00Z"))

    def test_model_claim_requires_opt_in_and_exact_span(self):
        claim = {**self.raw["claims"][0], "origin": "model",
                 "extractor": {"provider": "fixture", "model": "v1", "prompt_id": "extract-v1"}}
        invalid = {**claim, "span": {**claim["span"], "text": "Paraphrase"}}
        when = "2026-11-26T21:00:00Z"
        with self.assertRaisesRegex(ValueError, "prompt_id"):
            self.import_at(when, {**self.raw, "claims": [{**claim, "extractor": "unknown"}]})
        with self.assertRaisesRegex(ValueError, "exactly match"):
            self.import_at(when, {**self.raw, "claims": [claim, invalid]})
        self.assertEqual(graph_as_of(self.journal, as_of=when, include_model=True)["edges"], [])
        self.import_at(when, {**self.raw, "claims": [claim]})
        self.assertEqual(graph_as_of(self.journal, as_of=when)["edges"], [])
        with self.assertRaisesRegex(ValueError, "include_model must be a boolean"):
            graph_as_of(self.journal, as_of=when, include_model="false")
        result = graph_as_of(self.journal, as_of=when, include_model=True)
        self.assertEqual(result["edges"][0]["origin"], "model")
        self.assertEqual(result["edges"][0]["span"]["text"], "Café Corp schedules earnings")

    def test_correction_hides_earlier_edge_at_both_cutoffs_and_protects_human_evidence(self):
        initial = "2026-11-26T21:00:00Z"
        later = "2026-11-26T21:05:00Z"
        effective = "2026-11-27T00:00:00Z"
        first_id, = self.import_at(initial)["edge_ids"]
        correction = {**self.raw["claims"][0], "supersedes": first_id, "valid_from": effective,
                      "object": {"type": "event", "key": "cafe-2026-q4-amended"}}
        invalid_model = {**correction, "origin": "model",
                         "extractor": {"provider": "fixture", "model": "v1", "prompt_id": "extract-v1"}}
        with self.assertRaisesRegex(ValueError, "cannot supersede human"):
            self.import_at(later, {**self.raw, "claims": [invalid_model]})
        different_subject = {**correction, "subject": {"type": "issuer", "key": "other-corp"}}
        with self.assertRaisesRegex(ValueError, "same subject and predicate"):
            self.import_at(later, {**self.raw, "claims": [different_subject]})
        correction_id, = self.import_at(later, {**self.raw, "claims": [correction]})["edge_ids"]
        for as_of, effective_at in ((initial, effective), (later, initial)):
            edges = graph_as_of(self.journal, as_of=as_of, effective_at=effective_at)["edges"]
            self.assertEqual([edge["id"] for edge in edges], [first_id])
        edges = graph_as_of(self.journal, as_of=later, effective_at=effective)["edges"]
        self.assertEqual([edge["id"] for edge in edges], [correction_id])

    def test_graph_resolves_overlapping_corrections_at_the_requested_times(self):
        claim = self.raw["claims"][0]
        model = {"origin": "model", "extractor": {"provider": "fixture", "model": "v1", "prompt_id": "extract-v1"}}
        original, = self.import_at("2026-11-26T21:00:00Z", {**self.raw, "claims": [{**claim, **model}]})["edge_ids"]
        replacement = {**claim, "supersedes": original, "object": {"type": "event", "key": "amended"}}
        amended, = self.import_at("2026-11-26T21:01:00Z", {**self.raw, "claims": [replacement]})["edge_ids"]
        temporary = {**replacement, "valid_to": "2026-11-28T00:00:00Z",
                     "object": {"type": "event", "key": "temporary"}}
        latest, = self.import_at("2026-11-26T21:02:00Z", {**self.raw, "claims": [temporary]})["edge_ids"]
        later_model = {**replacement, **model, "object": {"type": "event", "key": "model-guess"}}
        self.import_at("2026-11-26T21:03:00Z", {**self.raw, "claims": [later_model]})

        checks = [("2026-11-26T21:01:59.999999999Z", "2026-11-27T00:00:00Z", amended),
                  ("2026-11-26T21:03:00Z", "2026-11-27T00:00:00Z", latest),
                  ("2026-11-26T21:03:00Z", "2026-11-28T00:00:00Z", amended)]
        for as_of, effective_at, expected in checks:
            for include_model in (False, True):
                with self.subTest(as_of=as_of, effective_at=effective_at, include_model=include_model):
                    edges = graph_as_of(self.journal, as_of=as_of, effective_at=effective_at,
                                        include_model=include_model)["edges"]
                    self.assertEqual([edge["id"] for edge in edges], [expected])


if __name__ == "__main__":
    unittest.main()
