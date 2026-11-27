from contextlib import closing
from io import BytesIO
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from dashboard import export_terminal, make_handler, terminal_snapshot
from desk_run import RUN_FILE, run_desk, write_run
from optionsdesk import Journal
from publish_snapshot import RELEASE_FILES, published_snapshot, write_publication


class TerminalBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.db = self.directory / "journal.sqlite3"
        with closing(sqlite3.connect(self.db)) as connection:
            Journal(connection)

    def test_offline_export_is_standalone_and_preserves_journal(self):
        before = self.db.read_bytes()
        with self.assertRaisesRegex(ValueError, "separate from the source journal"):
            export_terminal(self.db, self.db)
        output = self.directory / "offline.html"
        export_terminal(self.db, output)
        html = output.read_text(encoding="utf-8")
        self.assertIn('id="desk-snapshot" type="application/json"', html)
        self.assertNotIn('href="terminal.css"', html)
        self.assertNotIn('src="terminal.js"', html)
        self.assertEqual(self.db.read_bytes(), before)

    def test_offline_export_protects_active_sqlite_sidecars(self):
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("PRAGMA journal_mode=wal")
            connection.execute("CREATE TABLE sandbox_fixture (value INTEGER)")
            wal = self.db.with_name(self.db.name + "-wal")
            before = wal.read_bytes()
            with self.assertRaisesRegex(ValueError, "SQLite sidecar"):
                export_terminal(self.db, wal)
            self.assertEqual(wal.read_bytes(), before)

    def test_desk_run_is_private_read_only_and_tampering_does_not_break_terminal(self):
        before = self.db.read_bytes()
        run = run_desk(self.db)
        self.assertEqual(set(run["engines"]), {"directional", "volatility", "catalyst"})
        self.assertTrue(all(row["status"] == "awaiting_inputs" for row in run["engines"].values()))
        path = write_run(self.db, run)
        self.assertEqual(path.name, RUN_FILE)
        self.assertEqual(terminal_snapshot(self.db)["desk_run"], run)
        self.assertNotIn("desk_run", terminal_snapshot(self.db, include_studies=False))
        self.assertEqual(self.db.read_bytes(), before)
        path.write_text(json.dumps({**run, "generated_at": "modified"}), encoding="utf-8")
        result = terminal_snapshot(self.db)
        self.assertNotIn("desk_run", result)
        self.assertIn("could not be verified", result["desk_run_error"])

    def test_desk_run_protects_journal_even_when_report_is_a_hardlink(self):
        path = self.directory / RUN_FILE
        path.hardlink_to(self.db)
        before = self.db.read_bytes()
        with self.assertRaisesRegex(ValueError, "overlaps the journal"):
            write_run(self.db, {})
        self.assertEqual(self.db.read_bytes(), before)

    def test_publication_writer_rejects_private_envelope_and_nested_fields(self):
        output = self.directory / "dist"
        base = {"schema_version": 1, "mode": "published_snapshot", "sources": [], "decisions": []}
        with self.assertRaisesRegex(ValueError, "nonpublication fields"):
            write_publication(output, {**base, "research_monitor": {"internal": "private"}})
        with self.assertRaisesRegex(ValueError, "scalar"):
            write_publication(output, {**base, "observations": {"internal": "private"}})
        decision = {"decision_id": "fixture", "plan": {"symbol": "TEST"}, "analysis": {"scenarios": []},
                    "review": {"comparison": {}, "evidence": [{"source": "https://user:secret@example.com"}]}}
        with self.assertRaisesRegex(ValueError, "embedded credentials"):
            write_publication(output, {**base, "decisions": [decision]})
        nested = {**decision, "review": None, "plan": {"symbol": "TEST", "private": "hidden"}}
        with self.assertRaisesRegex(ValueError, "nonpublication decision fields"):
            write_publication(output, {**base, "decisions": [nested]})
        self.assertFalse(output.exists())

    def test_workspace_projection_omits_account_fields_and_rejects_nested_extras(self):
        market = {"as_of": "2026-11-27T18:00:00Z", "market_rows": [{"symbol": "SPY", "last": "100"}],
                  "contracts": [{"code": "SPY261204C00100000", "symbol": "SPY", "expiry": "2026-12-04",
                                 "right": "C", "strike": "100", "quote": {"ask": "1.15", "account": "omit"}}],
                  "history": {"SPY": [["2026-11-25", 100.0]]}, "account": "omit"}
        exported = published_snapshot({"decisions": [], "sources": [], "observations": 0,
                                       "workspace_market": market}, [], "2026-11-27T18:00:01Z")
        projected = exported["workspace_market"]
        self.assertNotIn("account", projected)
        self.assertEqual(projected["contracts"][0]["quote"], {"ask": "1.15"})
        write_publication(self.directory / "dist", exported)
        projected["contracts"][0]["quote"]["account"] = "invalid"
        with self.assertRaisesRegex(ValueError, "nonpublication market fields"):
            write_publication(self.directory / "dist", exported)

    def test_publication_read_path_loads_only_selected_decisions(self):
        plan = {"name": "Fixture spread", "symbol": "TEST", "expiry": "2024-05-10", "long_strike": 100,
                "short_strike": 105, "as_of": "2024-05-07T19:00:00Z", "quote_source": "alpaca",
                "quote_feed": "opra", "thesis": "Fixture thesis", "invalidation": "Fixture invalidation",
                "max_quote_age_seconds": 10, "max_leg_skew_seconds": 1,
                "scenarios": [{"name": "base", "terminal_stock_price": 105}]}
        with closing(sqlite3.connect(self.db)) as connection:
            selected = Journal(connection).evaluate(plan)["decision_id"]
            with connection:
                connection.execute("INSERT INTO decisions VALUES (?, ?)", ("unrelated", "{}"))
        with self.assertRaisesRegex(ValueError, "integrity check"):
            terminal_snapshot(self.db)
        filtered = terminal_snapshot(self.db, include_studies=False, decision_ids=[selected])
        export = published_snapshot(filtered, [selected], "2024-05-07T20:00:00Z")
        self.assertEqual([row["decision_id"] for row in export["decisions"]], [selected])

    def test_publication_refuses_unknown_files_and_asset_symlinks(self):
        output = self.directory / "dist"
        snapshot = {"schema_version": 1, "mode": "published_snapshot", "decisions": []}
        write_publication(output, snapshot)
        self.assertEqual({path.name for path in output.iterdir()}, set(RELEASE_FILES))
        html = (output / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-desk-endpoint="desk.json"', html)
        unknown = output / "unexpected.txt"
        unknown.write_text("private", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, unknown.name):
            write_publication(output, {**snapshot, "observations": 1})
        self.assertEqual(json.loads((output / "desk.json").read_text(encoding="utf-8")), snapshot)
        unknown.unlink()
        symlink = output / "terminal.css"
        symlink.unlink()
        symlink.symlink_to(self.db)
        with self.assertRaisesRegex(ValueError, symlink.name):
            write_publication(output, snapshot)
        self.assertTrue(symlink.is_symlink())

    def test_private_api_restricts_request_host(self):
        class Handler(make_handler(self.db)):
            def log_message(self, *args):
                pass

        server = SimpleNamespace(server_name="localhost", server_port=8765)

        class Request:
            def makefile(self, *args):
                return self.input

            def sendall(self, data):
                self.output.write(data)

        def get(host, path="/api/desk"):
            request = Request()
            request.input = BytesIO(f"GET {path} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
            request.output = BytesIO()
            Handler(request, ("127.0.0.1", 12345), server)
            return request.output.getvalue()

        self.assertIn(b"200 OK", get("localhost:8765"))
        self.assertIn(b"200 OK", get("localhost:8765", "/?audit=1"))
        self.assertIn(b"200 OK", get("localhost:8765", "/api/desk?audit=1"))
        forbidden = get("example.test")
        self.assertIn(b"403 Forbidden", forbidden)
        self.assertNotIn(b'"decisions"', forbidden)


if __name__ == "__main__":
    unittest.main()
