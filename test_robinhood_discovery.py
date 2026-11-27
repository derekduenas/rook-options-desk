import unittest

from robinhood_discovery import report


class RobinhoodDiscoveryTests(unittest.TestCase):
    def preview(self, at, matches, *symbols):
        return {"received_at": at, "result": {"total_items": matches, "results": [
            {"ticker": symbol, "instrument_type": "EQUITY",
             "columns": {"Last": "10", "% Change": ".02", "Options volume": "10000"}}
            for symbol in symbols
        ]}}

    def test_tracks_row_caps_duplicates_and_knowledge_cutoff(self):
        raw = {"previews": [self.preview("2026-11-27T18:00:00Z", 5, "SPY", "AAPL"),
                            self.preview("2026-11-27T18:01:00Z", 2, "SPY", "TSLA")],
               "unpartitioned_preview": {"matches": 4, "received_at": "2026-11-27T18:01:00Z"}}
        earlier = report(raw, as_of="2026-11-27T18:00:00Z")
        self.assertEqual(earlier["distinct_usable_symbols"], 2)
        self.assertIsNone(earlier["unpartitioned_matches"])
        actual = report(raw, as_of="2026-11-27T18:01:00Z")
        self.assertEqual(actual["distinct_usable_symbols"], 3)
        self.assertEqual(actual["duplicates"], 1)
        self.assertTrue(actual["any_partition_truncated"])
        self.assertTrue(actual["count_discrepancy"])
