"""Local, append-only text evidence and point-in-time research relationships."""

import json

from earnings import now_utc, source_url
from optionsdesk import canonical, digest, identifier, utc


NODE_TYPES = {"issuer", "ticker", "sector", "event", "theme", "metric", "document", "contract"}
RELATIONS = {
    "lists_as": ({"issuer"}, {"ticker"}),
    "belongs_to": ({"issuer", "ticker"}, {"sector", "theme"}),
    "announces": ({"issuer", "document"}, {"event"}),
    "disclosed_in": ({"event", "metric"}, {"document"}),
    "reports": ({"issuer", "document", "event"}, {"metric"}),
    "affects": ({"event", "theme"}, {"issuer", "ticker", "sector", "metric"}),
    "underlies": ({"ticker"}, {"contract"}),
}


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS research_documents (
        id TEXT PRIMARY KEY, recorded_at TEXT NOT NULL, frozen TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS research_edges (
        id TEXT PRIMARY KEY, subject_type TEXT NOT NULL, subject_key TEXT NOT NULL,
        valid_from TEXT NOT NULL, valid_to TEXT, recorded_at TEXT NOT NULL,
        document_id TEXT NOT NULL REFERENCES research_documents(id), frozen TEXT NOT NULL)""")
    db.execute("""CREATE INDEX IF NOT EXISTS research_edges_subject
        ON research_edges(subject_type, subject_key, recorded_at)""")


def node(raw):
    kind = identifier(raw["type"])
    key = identifier(raw["key"])
    if kind not in NODE_TYPES:
        raise ValueError(f"unknown graph node type: {kind}")
    return {"type": kind, "key": key.upper() if kind == "ticker" else key}


def normalize_document(raw):
    text = raw["text"]
    if not isinstance(text, str) or not text.strip() or len(text) > 1_000_000:
        raise ValueError("document text must contain one to 1,000,000 characters")
    published_at = utc(raw["published_at"])
    first_seen_at = utc(raw["first_seen_at"])
    if published_at > first_seen_at:
        raise ValueError("document publication must precede or equal its first receipt")
    return {"source_url": source_url(raw["source_url"]), "title": identifier(raw["title"]), "text": text,
            "published_at": published_at, "first_seen_at": first_seen_at}


def normalize_claim(raw, document, document_id):
    subject, object_ = node(raw["subject"]), node(raw["object"])
    relation = identifier(raw["predicate"])
    kinds = RELATIONS.get(relation)
    if not kinds or subject["type"] not in kinds[0] or object_["type"] not in kinds[1]:
        raise ValueError("unsupported relationship for these node types")
    valid_from = utc(raw.get("valid_from", document["published_at"]))
    valid_to = utc(raw["valid_to"]) if raw.get("valid_to") is not None else None
    if valid_to is not None and valid_to <= valid_from:
        raise ValueError("valid_to must be after valid_from")
    span = raw["span"]
    start, end = span["start"], span["end"]
    if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(document["text"])
            or document["text"][start:end] != span["text"] or not span["text"].strip()):
        raise ValueError("claim span must exactly match the document's character offsets")
    origin = identifier(raw["origin"])
    if origin not in {"human", "model"}:
        raise ValueError("claim origin must be human or model")
    extractor = raw["extractor"]
    if origin == "model":
        keys = ("provider", "model", "prompt_id")
        if not isinstance(extractor, dict) or not all(key in extractor for key in keys):
            raise ValueError("model claims require provider, model, and prompt_id provenance")
        extractor = {key: identifier(extractor[key]) for key in keys}
    else:
        extractor = identifier(extractor)
    claim = {"document_id": document_id, "subject": subject, "predicate": relation, "object": object_,
             "valid_from": valid_from, "valid_to": valid_to, "span": {"start": start, "end": end, "text": span["text"]},
             "origin": origin, "extractor": extractor}
    if raw.get("supersedes") is not None:
        claim["supersedes"] = identifier(raw["supersedes"])
    return claim


def import_evidence(journal, raw):
    """Import {'document': {...}, 'claims': [...]} as immutable, cited graph evidence."""
    claims = raw["claims"]
    if not isinstance(claims, list) or len(claims) > 1000:
        raise ValueError("supply a list of at most 1,000 claims")
    document = normalize_document(raw["document"])
    document_id = digest(document)
    edges = {}
    for claim in claims:
        normalized = normalize_claim(claim, document, document_id)
        edges[digest(normalized)] = normalized
    db = journal.db
    with db:
        db.execute("BEGIN IMMEDIATE")
        now = now_utc()
        if document["first_seen_at"] > now:
            raise ValueError("document receipt cannot be later than ingestion")
        initialize(db)
        for edge in edges.values():
            if "supersedes" not in edge:
                continue
            previous = db.execute("SELECT id, recorded_at, frozen FROM research_edges WHERE id = ?",
                                  (edge["supersedes"],)).fetchone()
            if previous is None:
                raise ValueError("correction must reference an existing research edge")
            prior = checked_row(*previous)
            if prior["recorded_at"] > now or any(prior[key] != edge[key] for key in ("subject", "predicate")):
                raise ValueError("correction requires a previously recorded edge with the same subject and predicate")
            if edge["origin"] == "model" and prior["origin"] == "human":
                raise ValueError("model claims cannot supersede human evidence")
        db.execute("INSERT OR IGNORE INTO research_documents VALUES (?, ?, ?)",
                   (document_id, now, canonical(document)))
        inserted = 0
        for edge_id, edge in edges.items():
            subject = edge["subject"]
            inserted += db.execute("INSERT OR IGNORE INTO research_edges VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                   (edge_id, subject["type"], subject["key"], edge["valid_from"], edge["valid_to"],
                                    now, document_id, canonical(edge))).rowcount
    return {"document_id": document_id, "edge_ids": list(edges), "inserted_edges": inserted}


def checked_row(key, recorded_at, frozen):
    content = json.loads(frozen)
    if digest(content) != key:
        raise ValueError("research graph record failed its integrity check")
    return {"id": key, "recorded_at": recorded_at, **content}


def graph_as_of(journal, *, as_of, subject=None, effective_at=None, include_model=False):
    """Query one knowledge cutoff and one validity date, with model proposals opt-in."""
    if type(include_model) is not bool:
        raise ValueError("include_model must be a boolean")
    as_of = utc(as_of)
    effective_at = as_of if effective_at is None else utc(effective_at)
    requested = node(subject) if subject is not None else None
    clause = "" if requested is None else " AND e.subject_type = ? AND e.subject_key = ?"
    parameters = [as_of, as_of]
    if requested is not None:
        parameters.extend((requested["type"], requested["key"]))
    db = journal.db
    result = {"as_of": as_of, "effective_at": effective_at, "documents": [], "edges": []}
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_edges'").fetchone():
        return result
    known, roots, eligible, documents = {}, {}, [], {}
    rows = db.execute(f"""SELECT e.id, e.recorded_at, e.frozen, d.id, d.recorded_at, d.frozen
        FROM research_edges e JOIN research_documents d ON d.id = e.document_id
        WHERE e.recorded_at <= ? AND d.recorded_at <= ?{clause}
        ORDER BY e.recorded_at, e.rowid""", parameters)
    for edge_id, edge_time, edge_json, doc_id, doc_time, doc_json in rows:
        edge = checked_row(edge_id, edge_time, edge_json)
        known[edge_id] = edge
        roots[edge_id] = roots.get(edge.get("supersedes"), edge_id)
        if edge["origin"] == "model" and not include_model:
            continue
        if edge["valid_from"] > effective_at or (edge["valid_to"] and edge["valid_to"] <= effective_at):
            continue
        eligible.append(edge)
        if doc_id not in documents:
            source = checked_row(doc_id, doc_time, doc_json)
            documents[doc_id] = {key: source[key] for key in (
                "id", "title", "source_url", "published_at", "first_seen_at", "recorded_at")}
    superseded = set()
    for edge in eligible:
        target = edge.get("supersedes")
        while target in known and target not in superseded:
            superseded.add(target)
            target = known[target].get("supersedes")
    # Corrections may refer to an older version. Resolve overlapping branches at
    # the requested effective time, favoring human review and then later receipt.
    selected = {}
    for edge in eligible:
        if edge["id"] in superseded:
            continue
        root = roots[edge["id"]]
        previous = selected.get(root)
        if previous is None or edge["origin"] == "human" or previous["origin"] == "model":
            selected[root] = edge
    selected_ids = {edge["id"] for edge in selected.values()}
    result["edges"] = [edge for edge in eligible if edge["id"] in selected_ids]
    result["documents"] = [documents[key] for key in dict.fromkeys(edge["document_id"] for edge in result["edges"])]
    return result
