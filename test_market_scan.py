from datetime import date, timedelta
from decimal import Decimal
import unittest

from market_scan import BREADTH, news_leads, scan_report
from optionsdesk import utc


CUTOFF = "2026-11-27T16:20:00Z"


class ScanTests(unittest.TestCase):
    def test_countertrend_requires_complete_fresh_breadth_and_prior_session(self):
        days = [date(2026, 11, 25) - timedelta(days=i) for i in reversed(range(165))]
        quotes = {symbol: {"observed_at": CUTOFF, "received_at": CUTOFF, "previous_session": "2026-11-25",
                           "previous_close": 100, "last": 101} for symbol in BREADTH}
        raw = {"as_of": CUTOFF, "session": {"open": "2026-11-27T14:30:00Z", "close": "2026-11-27T18:00:00Z",
                                            "previous_session": "2026-11-25"},
               "max_quote_age_seconds": 60, "spy_closes": {d.isoformat(): 500 - n for n, d in enumerate(days)},
               "quotes": quotes, "news": [], "news_coverage": {},
               "liquidity_limits": {"max_spread_pct_of_mid": 10, "min_size_per_side": 5},
               "option_quotes": [{"contract": "TEST call", "bid": 2, "ask": 3,
                                  "bid_size": 10, "ask_size": 1, "observed_at": CUTOFF, "received_at": CUTOFF,
                                  "broker_cutoff_at": CUTOFF}]}
        report = scan_report(raw, evaluated_at="2026-11-27T16:21:01Z")
        self.assertEqual(report["regime"]["label"], "countertrend_rally")
        self.assertTrue(report["regime"]["snapshot_stale"])
        self.assertEqual(report["liquidity"][0]["reasons"], ["wide_spread", "thin_displayed_size", "broker_cutoff_reached"])
        self.assertEqual(Decimal(str(report["liquidity"][0]["spread_pct_of_mid"])), Decimal("40"))
        quotes["RSP"]["received_at"] = "2026-11-27T16:20:00.000000001Z"
        self.assertEqual(scan_report(raw, evaluated_at=CUTOFF)["regime"]["label"], "unclassified")

    def test_news_prefers_primary_and_preserves_availability(self):
        base = {"title": "Trade deficit update", "provider": "Wire", "source": "https://example.com/news",
                "published_at": "2026-11-27T13:30:00Z", "received_at": "2026-11-27T13:35:00Z", "symbols": ["SPY"]}
        official = {**base, "provider": "Agency", "received_at": "2026-11-27T13:40:00Z",
                    "source": "https://example.gov/release", "evidence": "primary_release", "symbols": ["QQQ"]}
        future = {**base, "title": "Future release", "received_at": "2026-11-27T18:00:00Z"}
        row, = news_leads([base, future, official], utc(CUTOFF))
        self.assertEqual(row["provider"], "Agency")
        self.assertEqual(row["symbols"], ["QQQ", "SPY"])
        self.assertEqual(row["first_seen_at"], utc(official["received_at"]))
        self.assertEqual(row["tags"], ["macro"])
