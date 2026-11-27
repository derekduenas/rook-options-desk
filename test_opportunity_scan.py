import sqlite3
import unittest

from opportunity_scan import scan
from optionsdesk import Journal


AT = "2026-11-27T16:00:01Z"
CODE = "TEST261204C00100000"


class OpportunityScanTests(unittest.TestCase):
    def setUp(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        self.journal = Journal(db)
        self.raw = {
            "market_sessions": [
                {"open_at": "2026-11-27T14:30:00Z", "close_at": "2026-11-27T18:00:00Z", "previous_session": "2026-11-25"},
                {"open_at": "2026-11-30T14:30:00Z", "close_at": "2026-11-30T21:00:00Z", "previous_session": "2026-11-27"},
            ],
            "option_feed": ["robinhood", "market_snapshot"], "stock_feed": ["robinhood", "equity_snapshot"],
            "max_quote_age_seconds": 30, "max_quote_skew_seconds": 2, "one_lot_budget_dollars": 250,
            "round_trip_fee_dollars": 2, "min_underlying_move_pct": ".3",
            "liquidity_limits": {"max_spread_pct_of_mid": 8, "min_size_per_side": 5},
            "contracts": [{"symbol": "TEST", "expiry": "2026-12-04", "strike": 100, "right": "C",
                           "last_entry_at": "2026-12-04T20:45:00Z", "known_at": "2026-11-27T15:00:00Z"}],
        }
        self.option = {"kind": "option_quote", "instrument": CODE, "observed_at": "2026-11-27T16:00:00Z",
                       "bid": "1.00", "ask": "1.03", "bid_size": 6, "ask_size": 8, "multiplier": 100}
        self.stock = {"kind": "underlying_trade", "instrument": "TEST", "observed_at": "2026-11-27T16:00:00Z",
                      "price": 101, "session": "regular", "prior_close": {
                          "price": 100, "date": "2026-11-25", "interpolated": False}}

    def observe(self, row, kind, at="2026-11-27T16:00:00.500Z"):
        source, feed = self.raw[kind + "_feed"]
        self.journal.record({"received_at": at, "source": source, "feed": feed, "observations": [row]})

    def test_fresh_matching_quotes_get_watchlist_status(self):
        self.observe(self.option, "option")
        self.observe(self.stock, "stock")
        report = scan(self.journal, self.raw, as_of=AT)
        self.assertEqual(report["status"], "watchlist_available")
        self.assertEqual(report["leads"][0]["one_lot_reserve_dollars"], "105.00")
        self.raw["one_lot_budget_dollars"] = 104
        self.assertEqual(scan(self.journal, self.raw, as_of=AT)["excluded"][0]["reasons"], ["one_lot_exceeds_budget"])
        self.assertIn("stale_or_unavailable", scan(self.journal, self.raw, as_of="2026-11-27T16:00:31Z")["excluded"][0]["reasons"])

    def test_preserves_early_close_and_excludes_future_receipts(self):
        self.observe(self.option, "option", at="2026-11-27T16:00:01.000000001Z")
        self.observe(self.stock, "stock")
        self.assertIn("option_quote_missing", scan(self.journal, self.raw, as_of=AT)["excluded"][0]["reasons"])
        after_close = scan(self.journal, self.raw, as_of="2026-11-27T18:00:00Z")
        self.assertEqual(after_close["status"], "market_closed")
        self.assertEqual(after_close["next_open_at"], "2026-11-30T14:30:00.000000000+00:00")
