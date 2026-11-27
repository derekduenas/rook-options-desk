"""Run the earnings CLI with synthetic market and paper evidence in a scratch journal."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from dashboard import export_terminal, terminal_snapshot
from optionsdesk import main, utc


PRE = "2024-05-07T19:45:00Z"
RELEASE = "2024-05-07T20:05:00Z"
POST = "2024-05-08T13:45:00Z"
EXIT = "2024-05-08T19:15:00Z"
EXPIRY = "2024-05-10"


class EarningsWorkflowTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.db = self.path / "scratch.sqlite3"

    def cli(self, *args, payload=None, at=None, expected_exit=0):
        output, errors = StringIO(), StringIO()
        if payload is not None:
            args += ("-",)
        fixture_time = utc(at or EXIT)
        with patch("sys.stdin", StringIO(json.dumps(payload))), patch("earnings.now_utc", return_value=fixture_time), \
                patch("capture.now_utc", return_value=fixture_time), redirect_stdout(output), redirect_stderr(errors):
            status = main(["--db", str(self.db), *args])
        self.assertEqual(status, expected_exit, errors.getvalue())
        return json.loads(output.getvalue()) if status == 0 else errors.getvalue()

    def tape(self, at, long, short, spot):
        instruments, results = [], []
        for index, (strike, prices) in enumerate(((200, long), (210, short))):
            option_id = f"synthetic-{index}"
            instruments.append({"id": option_id, "type": "call", "chain_symbol": "CRM", "expiration_date": EXPIRY,
                                "strike_price": str(strike), "trade_value_multiplier": "100"})
            results.append({"quote": {"instrument_id": option_id, "updated_at": at, "bid_price": prices[0],
                                      "ask_price": prices[1], "bid_size": 2, "ask_size": 2, "volume": 50,
                                      "open_interest": 25, "implied_volatility": ".40"}})
        self.cli("record-mcp", payload={"provider": "robinhood_options", "received_at": at, "instruments": instruments,
                                        "payload": {"data": {"results": results}}})
        stock = {"symbol": "CRM", "has_traded": True, "state": "active", "venue_last_trade_time": at,
                 "last_trade_price": spot}
        self.cli("record-mcp", payload={"provider": "robinhood_stocks", "received_at": at,
                                        "payload": {"data": {"results": [{"quote": stock}]}}})

    def test_registration_capture_replay_paper_score_forecast_and_terminal(self):
        source = "https://example.com/fictitious-results"
        event = {"symbol": "CRM", "title": "Synthetic CRM release", "source": source,
                 "known_at": "2024-05-07T18:00:00Z", "event_at": RELEASE, "pre_at": PRE, "post_at": POST,
                 "exit_at": EXIT, "expiry": EXPIRY, "right": "C", "long_strike": 200, "short_strike": 210}
        protocol = {"name": "Synthetic CRM smoke test", "universe": ["CRM"], "risk_budget_dollars": 500,
                    "quote_source": "robinhood", "quote_feed": "market_snapshot", "underlying_source": "robinhood",
                    "underlying_feed": "equity_snapshot", "max_quote_age_seconds": 45, "max_leg_skew_seconds": 10,
                    "max_record_delay_seconds": 30, "option_fee_per_leg_dollars": 1, "equity_fee_dollars": 1}
        case = self.cli("earnings-register", payload={**protocol, "events": [event]}, at="2024-05-07T19:00:00Z")
        keys = {"experiment_id": case["experiment_id"], "event_id": case["events"][0]["event_id"]}
        pre_request = {**keys, "phase": "pre", "action": "paper", "reason": "Synthetic eligible candidate"}
        self.tape("2024-05-07T19:44:59Z", ("2.90", "3.20"), ("1.00", "1.25"), 200)
        self.assertIn("scheduled cutoff", self.cli("earnings-entry", payload=pre_request,
                                                  at="2024-05-07T19:44:59Z", expected_exit=1))
        before = self.cli("earnings-entry", payload=pre_request, at=PRE)
        self.assertEqual(before["classification"], "prospective")
        self.assertEqual({row["structure"]: row["quantity"] for row in before["candidates"]},
                         {"outright": 1, "debit_spread": 2, "underlying": 2, "cash": 0})

        self.tape("2024-05-08T13:44:59Z", ("4.90", "5.10"), ("2.20", "2.40"), 209)
        after = self.cli("earnings-entry", payload={**keys, "phase": "post", "action": "pass",
                         "reason": "Synthetic discretionary pass", "release": {"source": source, "published_at": RELEASE,
                                                                                "first_seen_at": RELEASE}}, at=POST)
        self.assertEqual(after["classification"], "prospective")
        self.assertEqual(next(row for row in after["candidates"] if row["structure"] == "outright")["status"], "risk_budget")

        self.assertEqual(self.cli("reconcile-due", at=POST)["replays"], [])
        self.tape("2024-05-08T19:14:58Z", ("6.50", "6.70"), ("2.90", "3.10"), 211)
        self.assertEqual(len(self.cli("reconcile-due")["replays"]), 2)
        self.assertEqual(self.cli("reconcile-due")["replays"], [])
        pre_replay = self.cli("earnings-resolve", before["entry_id"])
        self.assertEqual({row["structure"]: row["pnl_dollars"] for row in pre_replay["outcomes"]},
                         {"outright": "328.00", "debit_spread": "232.00", "underlying": "20.00", "cash": "0.00"})

        attempt = {"entry_id": before["entry_id"], "source": "synthetic paper log", "attempted_at": "2024-05-07T19:45:01Z"}
        partial = self.cli("earnings-paper", payload={**attempt, "structure": "debit_spread", "status": "partial",
                           "quantity": 1, "reference": "SIM-1", "entry_at": "2024-05-07T19:45:02Z",
                           "exit_at": "2024-05-08T19:14:59Z", "entry_net_price": "2.25", "exit_net_price": "3.30", "fees_dollars": 4})
        self.assertEqual(partial["pnl_dollars"], "101.00")
        self.cli("earnings-paper", payload={**attempt, "structure": "outright", "status": "unfilled", "reference": "SIM-2"})
        self.assertEqual(self.cli("earnings-entry", payload=pre_request, at=EXIT), before)

        scorecard = self.cli("scorecard", "--as-of", EXIT)
        pre = next(row for row in scorecard["cohorts"] if row["phase"] == "pre")
        post = next(row for row in scorecard["cohorts"] if row["phase"] == "post")
        self.assertEqual(post["coverage"]["eligible_passes"], 1)
        self.assertEqual(pre["paper"]["structures"]["outright"]["unfilled"], 1)
        paper = pre["paper"]["structures"]["debit_spread"]
        self.assertEqual((paper["partial"], paper["mean_net_pnl_dollars"], paper["mean_round_trip_slippage_dollars"]),
                         (1, "101.00", "15.00"))
        forecast = self.cli("forecast-validate", "--as-of", EXIT)["reports"][0]
        self.assertEqual((forecast["eligible_labeled_events"], forecast["scores"]), (1, None))

        html = self.path / "terminal.html"
        export_terminal(self.db, html)
        model = json.loads(html.read_text().split('<script id="desk-snapshot" type="application/json">', 1)[1].split("</script>", 1)[0])
        self.assertEqual(len(model["earnings_experiments"][0]["events"][0]["entries"]), 2)
        self.assertNotIn("research_monitor", terminal_snapshot(self.db, include_studies=False))


if __name__ == "__main__":
    unittest.main()
