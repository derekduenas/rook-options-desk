"""Serve the Options Desk terminal from the local journal."""
import argparse
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile
from opportunity_scan import read_scan

from capture import capture_status
from catalyst import latest_studies
from desk_run import RUN_FILE, load_run
from earnings import list_experiments
from frontier import latest_review
from optionsdesk import Journal
from research_graph import graph_as_of
from scorecard import cohort_report
from workspace_market import read_workspace


ROOT = Path(__file__).resolve().parent
STATIC = {
    "/": ("index.html", "text/html"),
    "/terminal.css": ("terminal.css", "text/css"),
    "/terminal.js": ("terminal.js", "text/javascript"),
    "/workspace.js": ("workspace.js", "text/javascript"),
}


def research_monitor(journal, experiments):
    from event_quantiles import report as quantile_report

    scorecard = cohort_report(journal)
    auth_ready = bool(os.environ.get("APCA_API_KEY_ID") and os.environ.get("APCA_API_SECRET_KEY"))
    capture = capture_status(journal, as_of=scorecard["as_of"], auth=auth_ready)
    names = {experiment["experiment_id"]: experiment["protocol"]["name"] for experiment in experiments}
    tasks = []
    for task in capture["tasks"]:
        summary = {key: task[key] for key in ("symbol", "stage", "cutoff", "status", "fresh_option_legs",
                                              "synchronized", "fresh_underlying", "transport", "capture_window_seconds",
                                              "feeds", "quote_legs", "underlying_status", "option_pair_status")}
        summary["experiment"] = names.get(task["experiment_id"])
        attempt = task["last_attempt"]
        summary["last_attempt"] = None if attempt is None else {
            "at": attempt["at"],
            "feeds": {kind: result.get("status", "recorded") for kind, result in attempt.get("feeds", {}).items()},
        }
        tasks.append(summary)
    graph = graph_as_of(journal, as_of=scorecard["as_of"], include_model=True)
    sources = {row["id"]: row for row in graph["documents"]}
    evidence = [{"subject": edge["subject"]["key"], "predicate": edge["predicate"], "object": edge["object"]["key"],
                 "origin": edge["origin"], "recorded_at": edge["recorded_at"],
                 "excerpt": edge["span"]["text"][:400], "source": sources[edge["document_id"]]["source_url"]}
                for edge in reversed(graph["edges"][-6:])]
    return {"scorecard": scorecard, "capture": {"as_of": capture["as_of"], "tasks": tasks}, "evidence": evidence,
            "event_quantiles": quantile_report(journal, scorecard["as_of"])}


def terminal_snapshot(db, *, include_studies=True, decision_ids=None, include_workspace=False):
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("BEGIN")
        journal = Journal(connection, initialize=False)
        status = journal.status()
        requested = set(decision_ids) if decision_ids is not None else None
        decisions = []
        for decision_id in status["decisions"]:
            if requested is None or decision_id in requested:
                decisions.append({**journal.replay(decision_id), "review": latest_review(journal, decision_id)})
        snapshot = {**status, "decisions": decisions,
                    "catalyst_studies": latest_studies(journal) if include_studies else []}
        if include_studies or include_workspace:
            from earnings import now_utc

            snapshot["workspace_market"] = read_workspace(journal, db.parent, now_utc())
        if include_studies:
            from earnings import now_utc

            experiments = list_experiments(journal)
            snapshot["earnings_experiments"] = experiments
            snapshot["research_monitor"] = research_monitor(journal, experiments)
            try:
                snapshot["opportunity_scan"] = read_scan(db, as_of=now_utc())
            except (OSError, ValueError, TypeError, KeyError):
                snapshot["opportunity_scan_error"] = "Scanner inputs are unavailable or invalid."
            try:
                snapshot["desk_run"] = load_run(db.parent / RUN_FILE)
            except (OSError, ValueError):
                snapshot["desk_run_error"] = "The latest research run could not be verified."
        return snapshot


def export_terminal(db, output):
    database = db.resolve()
    protected = [database, *(Path(str(database) + suffix) for suffix in ("-wal", "-shm", "-journal"))]
    if output.resolve() in protected or (output.exists() and any(
            path.is_file() and output.samefile(path) for path in protected)):
        raise ValueError("the HTML export must be separate from the source journal and SQLite sidecar files")
    assets = ROOT / "terminal"
    html = (assets / "index.html").read_text(encoding="utf-8")
    css = (assets / "terminal.css").read_text(encoding="utf-8")
    javascript = (assets / "terminal.js").read_text(encoding="utf-8")
    workspace = (assets / "workspace.js").read_text(encoding="utf-8")
    snapshot = json.dumps(terminal_snapshot(db), allow_nan=False).replace("<", "\\u003c")
    html = html.replace('<link rel="stylesheet" href="terminal.css">', f"<style>{css}</style>")
    html = html.replace('<script src="terminal.js" defer></script>', "")
    html = html.replace('<script src="workspace.js" defer></script>', "")
    html = html.replace("</body>", f'<script id="desk-snapshot" type="application/json">{snapshot}</script>'
                        f"<script>{workspace}</script><script>{javascript}</script></body>")
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, delete=False) as stream:
        staged = Path(stream.name)
        try:
            stream.write(html)
            stream.close()
            staged.replace(output)
        finally:
            staged.unlink(missing_ok=True)


def make_handler(db):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            media = "application/json"
            code = 200
            hosts = self.headers.get_all("Host", [])
            port = self.server.server_port
            allowed_hosts = {"localhost", "127.0.0.1", f"localhost:{port}", f"127.0.0.1:{port}"}
            path = self.path.partition("?")[0]
            if len(hosts) != 1 or hosts[0].lower() not in allowed_hosts:
                code = 403
                payload = b'{"error":"The terminal requires a loopback host."}'
            elif path in STATIC:
                filename, media = STATIC[path]
                payload = (ROOT / "terminal" / filename).read_bytes()
            elif path == "/api/desk":
                try:
                    payload = json.dumps(terminal_snapshot(db), allow_nan=False).encode()
                except (sqlite3.Error, ValueError):
                    code = 503
                    payload = b'{"error":"The local journal could not be read."}'
            elif path == "/api/scanner":
                from earnings import now_utc

                try:
                    payload = json.dumps(read_scan(db, as_of=now_utc()), allow_nan=False).encode()
                except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
                    code = 503
                    payload = b'{"error":"Scanner inputs are unavailable or invalid."}'
            else:
                code = 404
                payload = b'{"error":"Page not found."}'
            self.send_response(code)
            self.send_header("Content-Type", media + "; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "journal.sqlite3")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--export", type=Path, help="write a standalone HTML snapshot")
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error("capture market data before starting the terminal")
    # Apply the additive journal schema migration before opening the read-only server.
    with closing(sqlite3.connect(args.db)) as connection:
        Journal(connection)
    if args.export:
        try:
            export_terminal(args.db, args.export)
        except (OSError, ValueError) as error:
            parser.error(str(error))
        return
    with ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.db)) as server:
        print(f"Options Desk at http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
