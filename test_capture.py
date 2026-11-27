import sqlite3
import unittest
from unittest.mock import patch

from capture import (archive_fetch, archive_replay, capture_once, capture_status, claim_attempt, massive_point, pull_options,
                     unix_nanos)
from earnings import register_experiment
from optionsdesk import Journal, canonical, contract, utc
from providers import normalize_mcp


EVENT = {"symbol": "TEST", "title": "Fixture earnings", "source": "https://example.com/release",
         "known_at": "2024-05-07T18:00:00Z", "pre_at": "2024-05-07T19:45:00Z", "event_at": "2024-05-07T20:05:00Z",
         "post_at": "2024-05-08T13:35:00Z", "exit_at": "2024-05-08T19:55:00Z", "expiry": "2024-05-10",
         "right": "C", "long_strike": 100, "short_strike": 105}
PROTOCOL = {"name": "Fixture", "universe": ["TEST"], "risk_budget_dollars": 450,
            "quote_source": "alpaca", "quote_feed": "opra", "underlying_source": "alpaca", "underlying_feed": "sip",
            "max_quote_age_seconds": 15, "max_leg_skew_seconds": 1, "max_record_delay_seconds": 10,
            "option_fee_per_leg_dollars": 1, "equity_fee_dollars": 1}


def option_quote(strike, price, observed):
    return {"kind": "option_quote", "instrument": contract("TEST", EVENT["expiry"], strike, "C"),
            "observed_at": observed, "bid": price, "ask": price + 0.25, "bid_size": 4, "ask_size": 4,
            "multiplier": 100}


def stock_trade(observed):
    return {"kind": "underlying_trade", "instrument": "TEST", "observed_at": observed, "price": 100,
            "session": "regular"}


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.journal = Journal(self.db)
        with patch("earnings.now_utc", return_value="2024-05-07T19:00:00.000000000+00:00"):
            self.event = register_experiment(self.journal, {**PROTOCOL, "events": [EVENT]})["events"][0]

    def test_capture_status_has_no_events_before_they_were_registered(self):
        status = capture_status(self.journal, as_of="2024-05-07T18:59:59.999999999Z")
        self.assertEqual(status["tasks"], [])

    def test_attempt_lease_and_historical_result_visibility(self):
        moment = utc("2024-05-07T19:44:58Z")
        completion = utc("2024-05-07T19:44:59.500Z")
        with patch("capture.now_utc", return_value=moment):
            self.assertEqual(claim_attempt(self.journal, self.event, "pre", 15), moment)
            self.assertIsNone(claim_attempt(self.journal, self.event, "pre", 15))
        with self.db:
            self.db.execute("UPDATE desk_capture_attempts SET result=?", (canonical({"status": "finished",
                "finished_at": completion, "feeds": {"quote": {"status": "error"}}}),))
        before = capture_status(self.journal, as_of="2024-05-07T19:44:59Z")["tasks"][0]
        self.assertEqual(before["last_attempt"], {"at": moment, "status": "in_progress"})
        self.assertEqual(capture_status(self.journal, as_of=completion)["tasks"][0]["last_attempt"]["status"], "finished")
        with patch("capture.now_utc", return_value=utc(EVENT["pre_at"])):
            self.assertIsNone(claim_attempt(self.journal, self.event, "pre", 15))

    def test_unverified_option_does_not_drop_the_verified_outright_leg(self):
        long = contract("TEST", EVENT["expiry"], 100, "C")
        short = contract("TEST", EVENT["expiry"], 105, "C")

        def check(_host, code, _symbol, _headers):
            if code == short:
                raise ValueError("nonstandard deliverable")

        market = {"quotes": {long: {"t": EVENT["pre_at"], "bp": 1, "ap": 2, "bs": 3, "as": 4}}}
        with patch("capture.option_reference", side_effect=check), patch("capture.market_get", return_value=market) as request:
            result = pull_options(PROTOCOL, EVENT, ({}, "paper-api.alpaca.markets"))
        self.assertEqual(request.call_args.args[2]["symbols"], long)
        self.assertEqual(result["observations"][0]["instrument"], long)
        self.assertEqual(result["problems"], {short: "nonstandard deliverable"})

        crossed = {**market["quotes"][long], "bp": 3, "ap": 2}
        with patch("capture.option_reference"), patch("capture.market_get", return_value={
                "quotes": {**market["quotes"], short: crossed}}):
            result = pull_options(PROTOCOL, EVENT, ({}, "paper-api.alpaca.markets"))
        self.assertEqual([quote["instrument"] for quote in result["observations"]], [long])
        self.assertEqual(result["problems"], {short: f"crossed quote for {short}"})

    def test_alpaca_snapshot_honors_explicit_adjusted_deliverables_and_multiplier(self):
        code = contract("TEST", EVENT["expiry"], 100, "C")
        raw = {"provider": "alpaca_snapshots", "received_at": EVENT["pre_at"],
               "instruments": [{"symbol": code, "size": "100", "multiplier": "50"}],
               "payload": {"request": {"feed": "opra"}, "snapshots": {code: {"latest_quote": {
                   "timestamp": EVENT["pre_at"], "bid_price": 2, "ask_price": 3,
                   "bid_size": 4, "ask_size": 5}}}}}
        result = normalize_mcp(raw)["observations"][0]
        self.assertEqual(result["multiplier"], "50")
        self.assertTrue(result["adjusted"])
        raw["instruments"][0] = {"symbol": code, "size": "100", "multiplier": "100", "deliverables": [
            {"type": "cash", "amount": "100", "symbol": "USD"}]}
        self.assertTrue(normalize_mcp(raw)["observations"][0]["adjusted"])

    def test_capture_received_after_cutoff_is_excluded(self):
        moment = "2024-05-07T19:44:58Z"
        late = "2024-05-07T19:45:00.000000001Z"
        legitimate = {"source": "alpaca", "feed": "opra", "received_at": moment,
                      "observations": [option_quote(100, 3, moment)]}
        self.journal.record(legitimate)
        self.journal.record({**legitimate, "received_at": late,
                             "observations": [option_quote(105, 1, moment)]})
        report = capture_status(self.journal, as_of=late)
        self.assertEqual(report["tasks"][0]["status"], "partial")
        self.assertEqual(report["tasks"][0]["fresh_option_legs"], 1)
        self.assertEqual(report["tasks"][0]["transport"]["quote"], "credentials_required")
        task = report["tasks"][0]
        self.assertEqual(task["feeds"], {"quote": ["alpaca", "opra"], "underlying": ["alpaca", "sip"]})
        self.assertEqual(task["capture_window_seconds"], "15")
        self.assertEqual(task["evidence_as_of"], utc(EVENT["pre_at"]))
        self.assertEqual([leg["status"] for leg in task["quote_legs"]], ["fresh", "missing"])
        self.assertIsNone(task["quote_legs"][1]["received_at"])
        self.assertEqual(capture_status(self.journal, as_of=late, symbol="OTHER")["tasks"], [])

    def test_indicative_cannot_fill_an_opra_capture_requirement(self):
        moment = "2024-05-07T19:44:58Z"
        self.journal.record({"source": "alpaca", "feed": "indicative", "received_at": moment,
                             "observations": [option_quote(100, 3, moment), option_quote(105, 1, moment)]})
        capture = capture_status(self.journal, as_of=EVENT["pre_at"], symbol="TEST")["tasks"][0]
        self.assertEqual(capture["fresh_option_legs"], 0)
        self.assertEqual(capture["option_pair_status"], "missing")

    def test_capture_in_window_records_success_without_masking_other_feed(self):
        moment = "2024-05-07T19:44:58Z"
        bundle = {"source": "alpaca", "feed": "sip", "received_at": moment, "observations": [stock_trade(moment)]}
        with patch("capture.now_utc", return_value=utc(moment)), patch("capture.pull_options", side_effect=ValueError("no entitlement")), \
                patch("capture.pull_trade", return_value=bundle):
            attempted = capture_once(self.journal, auth=({}, "paper-api.alpaca.markets"))["attempts"]
            self.assertEqual(capture_once(self.journal, auth=({}, "paper-api.alpaca.markets"))["attempts"], [])
        self.assertEqual(attempted[0]["feeds"]["quote"]["status"], "error")
        result = capture_status(self.journal, as_of=EVENT["pre_at"])["tasks"][0]
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["fresh_underlying"])
        self.assertEqual(result["last_attempt"]["feeds"]["quote"]["reason"], "no entitlement")

    def test_archive_uses_market_time_but_cannot_write_prospective_state(self):
        pre = EVENT["pre_at"]
        exit_time = EVENT["exit_at"]
        observations = [option_quote(100, 3, pre), option_quote(105, 1, pre), stock_trade(pre),
                        option_quote(100, 4, exit_time), option_quote(105, 1, exit_time), stock_trade(exit_time),
                        option_quote(100, 1, "2024-05-07T19:45:00.000000001Z")]
        result = archive_replay({"protocol": PROTOCOL, "event": EVENT, "archive_source": "Fixture NBBO",
                                 "archive_reference": "fixture", "observations": observations})
        self.assertEqual(result["basis"], "historical_market_time_only")
        self.assertEqual(result["phases"][0]["entry_evidence"]["options"][0]["bid"], 3)
        self.assertEqual(result["phases"][0]["outcomes"][0]["pnl_dollars"], "73.00")
        self.assertEqual(result["phases"][1]["outcomes"][0]["status"], "stale")
        self.assertEqual(self.db.execute("SELECT count(*) FROM earnings_entries").fetchone()[0], 0)
        self.assertEqual(self.journal.status()["observations"], 0)

    def test_archive_proves_strikes_with_cutoff_underlying_and_after_selection_quotes(self):
        selection_at = "2024-05-07T19:44:50.000000005Z"
        old = "2024-05-07T19:44:50.000000004Z"
        after = "2024-05-07T19:44:50.000000006Z"
        exit_time = EVENT["exit_at"]
        event = {**EVENT, "long_strike": 105, "short_strike": 110}
        raw = {"protocol": PROTOCOL, "event": event, "archive_source": "Fixture NBBO", "archive_reference": "fixture",
               "strike_selection": {"as_of": selection_at, "strike_increment": 5, "spread_width": 5},
               "observations": [stock_trade(selection_at), {**stock_trade(after), "price": 106},
                                option_quote(105, 3, selection_at), option_quote(110, 1, old),
                                option_quote(105, 4, exit_time), option_quote(110, 1, exit_time),
                                stock_trade(exit_time)]}
        report = archive_replay(raw)
        self.assertEqual(report["basis"], "historical_market_time_only")
        self.assertEqual(report["strike_selection"]["underlying"]["observed_at"], utc(selection_at))
        self.assertEqual(report["strike_selection"]["underlying"]["price"], 100)
        phase = report["phases"][0]
        self.assertIsNone(phase["entry_evidence"]["options"][1])
        self.assertEqual(phase["outcomes"][0]["pnl_dollars"], "73.00")
        self.assertEqual(phase["outcomes"][1]["status"], "missing")
        raw["observations"].append(option_quote(110, 1, after))
        self.assertEqual(archive_replay(raw)["phases"][0]["outcomes"][1]["status"], "modeled")

    def test_archive_rejects_stale_missing_mismatched_and_late_strike_selection(self):
        selection_at = "2024-05-07T19:44:50.000000005Z"
        selection = {"as_of": selection_at, "strike_increment": 5, "spread_width": 5}
        raw = {"protocol": PROTOCOL, "event": EVENT, "archive_source": "Fixture NBBO", "archive_reference": "fixture"}
        fresh_at_boundary = {**stock_trade("2024-05-07T19:44:35.000000005Z"), "price": 99}
        self.assertEqual(archive_replay({**raw, "strike_selection": selection,
                                       "observations": [fresh_at_boundary]})["strike_selection"]["underlying"]["price"],
                         99)
        for rule, trades, reason in (
            (selection, [stock_trade("2024-05-07T19:44:50.000000006Z")], "requires a fresh underlying"),
            (selection, [stock_trade("2024-05-07T19:44:35.000000004Z")], "requires a fresh underlying"),
            (selection, [stock_trade(selection_at)], "strikes do not match"),
            ({**selection, "as_of": "2024-05-07T19:45:00.000000001Z"}, [], "between the known event and the pre cutoff"),
            ({**selection, "strike_increment": 0}, [], "positive strike increment"),
            ({**selection, "spread_width": 0}, [], "positive strike increment"),
        ):
            with self.subTest(reason=reason, rule=rule, trades=trades):
                with self.assertRaisesRegex(ValueError, reason):
                    archive_replay({**raw, "strike_selection": rule, "observations": trades})

    def test_archive_fetch_passes_through_selection_and_fetches_its_stock_reference(self):
        selection_at = "2024-05-07T19:44:50.000000005Z"
        request = {"protocol": PROTOCOL, "event": EVENT,
                   "strike_selection": {"as_of": selection_at, "strike_increment": 5, "spread_width": 5}}

        def point(path, cutoff, _headers):
            if path == "/v3/trades/TEST" and cutoff == utc(selection_at):
                return cutoff, {"price": 99}
            return None, None

        with patch("capture.massive_contract_supported"), patch("capture.massive_point", side_effect=point) as fetch:
            archive = archive_fetch(request, "fixture")
        self.assertEqual(len(fetch.call_args_list), 10)
        self.assertEqual(archive["strike_selection"]["as_of"], utc(selection_at))
        self.assertEqual(archive_replay(archive)["strike_selection"]["underlying"]["price"], 99)

    def test_provider_time_precision_and_after_cutoff_rejection(self):
        self.assertEqual(unix_nanos(1_234_567_899), "1970-01-01T00:00:01.234567899+00:00")
        with patch("capture.market_get", return_value={"results": [{"sip_timestamp": 1_234_567_899}]}):
            with self.assertRaisesRegex(ValueError, "after its requested cutoff"):
                massive_point("/v3/quotes/O:TEST", "1970-01-01T00:00:01.234567898+00:00", {})
