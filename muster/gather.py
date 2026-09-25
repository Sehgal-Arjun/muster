"""Gather: the complete set of documents belonging to a process.

Search optimises precision. This optimises **recall of a complete set**, and the
asymmetry is the whole design. Missing one email means a confidently wrong answer
about what somebody is still owed; including one irrelevant email means a line
that can be ignored. Noise can be discarded later. Absence cannot be recovered,
because nothing downstream knows to look for what is not there.

Expansion runs along **deterministic edges** — shared threads, anchored
counterparties, attachment relationships, download provenance, reference numbers,
folders. Every one is an exact join, so the same inputs give the same set every
time. Nothing here uses similarity: a set that reshuffles between runs cannot be
the basis of a claim about what is outstanding.

Ordering is chronological, never by relevance. State is temporal. Relevance
ordering destroys the sequence that shows a deposit was requested in April and
paid in May, which is the only thing that makes the pair meaningful.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

# The artifact's shape. Other features consume this, so a change here is a
# contract change and must be visible.
GATHER_VERSION = 1

# Documents in a seed search. Deliberately small: the seed only has to find a
# foothold, and the edges do the rest.
SEED_LIMIT = 20

# Upper bound on a gathered set. Large enough for a two-year process, small
# enough that per-document extraction stays affordable. Reported when it binds,
# because a silently truncated set is a silently wrong answer.
MAX_DOCS = 300

# How far to follow edges. A fixed point is usually reached in three or four.
MAX_DEPTH = 6

# A file downloaded within this of an anchored email is usually the attachment of
# it, saved by hand. Narrow on purpose: this edge is combinatorial — every file
# is tested against every anchored email — so a wide window covers most of the
# calendar. At six hours against 200 anchored emails it matched 112 documents in
# a visa process, over half the set and nearly all of it unrelated.
DOWNLOAD_WINDOW = timedelta(hours=2)


@dataclass
class Inclusion:
    """Why one document is in the set. Never absent — an unexplained document
    in an answer about what is outstanding is worse than a missing one."""

    doc_id: int
    reason: str          # seed_only | anchor:<kind> | edge:<name>
    via: str | None = None   # the anchor value or the document that pulled it in

    def as_json(self) -> dict:
        return {"doc_id": self.doc_id, "reason": self.reason, "via": self.via}


@dataclass
class GatherResult:
    """A first-class artifact. Open items are one renderer over this, not the
    only one, so it carries everything a later consumer might need."""

    version: int
    process_slug: str
    process_name: str
    doc_ids: list[int]                       # chronological by doc_date
    inclusions: dict[int, Inclusion] = field(default_factory=dict)
    anchors: list[dict] = field(default_factory=list)
    seed_only: list[int] = field(default_factory=list)
    capped: bool = False
    # How many documents the cap discarded. Reported rather than implied: a set
    # that quietly dropped a hundred documents answers "what do I still owe"
    # wrongly and with confidence.
    dropped: int = 0
    depth_reached: int = 0
    gathered_at: str = ""

    @property
    def size(self) -> int:
        return len(self.doc_ids)

    def as_json(self) -> dict:
        return {
            "version": self.version,
            "process": {"slug": self.process_slug, "name": self.process_name},
            "gathered_at": self.gathered_at,
            "anchors": self.anchors,
            "doc_ids": self.doc_ids,
            "inclusions": [self.inclusions[d].as_json() for d in self.doc_ids],
            # The count that says an anchor is missing: documents the seed found
            # and no edge could explain.
            "seed_only": self.seed_only,
            "capped": self.capped,
            "dropped": self.dropped,
            "depth_reached": self.depth_reached,
            "size": len(self.doc_ids),
        }


def _rows_for(conn: sqlite3.Connection, doc_ids: set[int]) -> list[sqlite3.Row]:
    if not doc_ids:
        return []
    ids = sorted(doc_ids)
    out: list[sqlite3.Row] = []
    # Chunked: SQLite has a variable limit and a gathered set can approach it.
    for start in range(0, len(ids), 500):
        batch = ids[start : start + 500]
        out.extend(
            conn.execute(
                "SELECT id, title, source, source_uid, mime, doc_date, counterparty,"
                " sender, thread_uid, meta_json, content_hash, blob_path"
                f" FROM docs WHERE id IN ({','.join('?' * len(batch))})",
                batch,
            ).fetchall()
        )
    return out


def _anchored(anchors: list) -> dict[str, set[str]]:
    by_kind: dict[str, set[str]] = {}
    for anchor in anchors:
        by_kind.setdefault(anchor.kind, set()).add(anchor.value)
    return by_kind


def _seed(conn: sqlite3.Connection, name: str, limit: int) -> list[int]:
    """A foothold, not the answer. Parent documents, so an attachment brings its
    message and the thread edge can then do its work."""
    from muster.search import search as run_search

    hits = run_search(conn, name, mode="hybrid", limit=limit, min_score=0.0)
    seeds: list[int] = []
    for hit in hits:
        seeds.append(hit.doc_id)
        # An attachment's uid is <message>#attN; the message itself belongs too.
        if "#att" in (hit.source_uid or ""):
            parent_uid = hit.source_uid.split("#att")[0]
            row = conn.execute(
                "SELECT id FROM docs WHERE source = ? AND source_uid = ?",
                (hit.source, parent_uid),
            ).fetchone()
            if row:
                seeds.append(row[0])
    # Order-preserving dedupe.
    seen: set[int] = set()
    return [d for d in seeds if not (d in seen or seen.add(d))]


def _by_anchor(conn: sqlite3.Connection, kinds: dict[str, set[str]]) -> list[tuple[int, str, str]]:
    """Every document any anchor matches, unconditionally.

    Run over the whole archive rather than over the expanding frontier: an
    anchor is sufficient on its own, so a document matching one belongs even if
    nothing else in the set connects to it.
    """
    found: list[tuple[int, str, str]] = []

    def add(sql: str, params: tuple, kind: str, value: str) -> None:
        for row in conn.execute(sql, params):
            found.append((row[0], f"anchor:{kind}", value))

    live = "origin = 'captured' AND missing_since IS NULL"

    for value in kinds.get("domain", ()):
        like = f"%@{value}"
        add(f"SELECT id FROM docs WHERE {live} AND (lower(counterparty) LIKE ?"
            f" OR lower(sender) LIKE ?)", (like, like), "domain", value)
    for value in kinds.get("address", ()):
        add(f"SELECT id FROM docs WHERE {live} AND (lower(counterparty) = ?"
            f" OR lower(sender) = ?)", (value.lower(), value.lower()), "address", value)
    for value in kinds.get("thread_uid", ()):
        add(f"SELECT id FROM docs WHERE {live} AND thread_uid = ?", (value,),
            "thread_uid", value)
    for value in kinds.get("folder", ()):
        # The folder and everything beneath it.
        add(f"SELECT id FROM docs WHERE {live} AND source = 'file'"
            f" AND (json_extract(meta_json, '$.path') = ?"
            f" OR json_extract(meta_json, '$.path') LIKE ?)",
            (value, f"{value}/%"), "folder", value)
    for value in kinds.get("ref_pattern", ()):
        # Reference numbers live in the text, so this is the one anchor that
        # needs the index rather than a column.
        add("SELECT DISTINCT c.doc_id FROM chunks_fts"
            " JOIN chunks c ON c.id = chunks_fts.rowid"
            " JOIN docs d ON d.id = c.doc_id"
            f" WHERE chunks_fts MATCH ? AND d.origin = 'captured'"
            " AND d.missing_since IS NULL",
            (f'"{value}"',), "ref_pattern", value)
    for value in kinds.get("doc_id", ()):
        if str(value).isdigit():
            add(f"SELECT id FROM docs WHERE {live} AND id = ?", (int(value),),
                "doc_id", value)

    return found


def _expand_once(
    conn: sqlite3.Connection,
    included: set[int],
    kinds: dict[str, set[str]],
    is_anchored=lambda _doc_id: True,
) -> list[tuple[int, str, str]]:
    """One round of edges out of the current set. Exact joins only."""
    _is_anchored = is_anchored
    rows = _rows_for(conn, included)
    new: list[tuple[int, str, str]] = []

    threads = {r["thread_uid"] for r in rows if r["thread_uid"]}
    if threads:
        placeholders = ",".join("?" * len(threads))
        for row in conn.execute(
            f"SELECT id, thread_uid FROM docs WHERE thread_uid IN ({placeholders})"
            " AND origin = 'captured' AND missing_since IS NULL",
            sorted(threads),
        ):
            new.append((row[0], "edge:thread", row[1]))

    # Attachments of included messages, and the searchable document each became.
    ids = sorted(included)
    for start in range(0, len(ids), 500):
        batch = ids[start : start + 500]
        placeholders = ",".join("?" * len(batch))
        for row in conn.execute(
            f"SELECT doc_id, extracted_doc_id FROM attachments"
            f" WHERE doc_id IN ({placeholders}) AND extracted_doc_id IS NOT NULL",
            batch,
        ):
            new.append((row[1], "edge:attachment", str(row[0])))
        # And the reverse: an attachment document brings its message.
        for row in conn.execute(
            f"SELECT doc_id, extracted_doc_id FROM attachments"
            f" WHERE extracted_doc_id IN ({placeholders})",
            batch,
        ):
            new.append((row[0], "edge:attachment_parent", str(row[1])))

    # Files whose download provenance points at an anchored domain, and files
    # downloaded close in time to an anchored email.
    domains = kinds.get("domain", set())
    if domains:
        for row in conn.execute(
            "SELECT id, meta_json FROM docs WHERE source = 'file'"
            " AND meta_json LIKE '%where_from%' AND missing_since IS NULL"
        ):
            try:
                where_from = json.loads(row["meta_json"] or "{}").get("where_from") or []
            except (TypeError, ValueError):
                continue
            joined = " ".join(str(w).lower() for w in where_from)
            for domain in domains:
                if domain in joined:
                    new.append((row[0], "edge:where_from", domain))
                    break

    # Only anchored mail, and only mail: a file's own timestamp is not evidence
    # about another file, and a seed hit is not confirmed enough to date from.
    anchored_mail = [
        r["doc_date"] for r in rows
        if r["source"] != "file" and r["doc_date"] and _is_anchored(r["id"])
    ]
    if anchored_mail:
        new.extend(_downloads_near(conn, anchored_mail))

    return new


def _downloads_near(
    conn: sqlite3.Connection, stamps: list[str]
) -> list[tuple[int, str, str]]:
    """Files downloaded within hours of an anchored email.

    A saved attachment usually lands in Downloads minutes after the message it
    came from and carries no other link to it — no thread, no counterparty, no
    provenance if the browser did not record one. Time is the only edge left.

    Keyed on the recorded *download* time, not on `doc_date`. For a file
    `doc_date` is its EXIF capture date, its PDF creation date or its birthtime,
    none of which say when it arrived here: comparing a photograph's capture date
    against email timestamps is how this edge came to contribute half a set.
    Files with no recorded download time are skipped rather than guessed at.
    """
    parsed: list[datetime] = []
    for stamp in stamps:
        try:
            moment = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        parsed.append(moment)
    if not parsed:
        return []

    out: list[tuple[int, str, str]] = []
    for row in conn.execute(
        "SELECT id, meta_json FROM docs WHERE source = 'file'"
        " AND missing_since IS NULL"
        " AND json_extract(coalesce(meta_json, '{}'), '$.downloaded_date') IS NOT NULL"
    ):
        try:
            downloaded = json.loads(row["meta_json"])["downloaded_date"]
            when = datetime.fromisoformat(downloaded)
        except (ValueError, TypeError, KeyError):
            continue
        for moment in parsed:
            if moment.tzinfo != when.tzinfo:
                continue
            if abs(when - moment) <= DOWNLOAD_WINDOW:
                out.append((row[0], "edge:downloaded_near", moment.isoformat()))
                break
    return out


def gather(
    conn: sqlite3.Connection,
    process,
    *,
    seed_limit: int = SEED_LIMIT,
    max_docs: int = MAX_DOCS,
    max_depth: int = MAX_DEPTH,
    now: str | None = None,
) -> GatherResult:
    """Every document belonging to a process, chronologically, with reasons."""
    kinds = _anchored(process.anchors)

    inclusions: dict[int, Inclusion] = {}

    def record(doc_id: int, reason: str, via: str | None) -> bool:
        """First reason wins. Anchors are found before edges, so a document that
        matches an anchor is reported as anchored rather than as a side effect."""
        if doc_id in inclusions:
            return False
        inclusions[doc_id] = Inclusion(doc_id=doc_id, reason=reason, via=via)
        return True

    # Anchors first: they are sufficient, so their reason is the truest one.
    for doc_id, reason, via in _by_anchor(conn, kinds):
        record(doc_id, reason, via)

    for doc_id in _seed(conn, process.name, seed_limit):
        record(doc_id, "seed_only", None)

    included = set(inclusions)
    depth = 0
    while depth < max_depth and len(included) < max_docs:
        depth += 1
        added = False
        anchored_ids = {
            d for d, inc in inclusions.items() if inc.reason.startswith("anchor:")
        }
        for doc_id, reason, via in _expand_once(
            conn, included, kinds, is_anchored=anchored_ids.__contains__
        ):
            if doc_id not in inclusions and len(inclusions) < max_docs:
                record(doc_id, reason, via)
                added = True
        if not added:
            break                      # fixed point
        included = set(inclusions)

    # Chronological, never by relevance. A stable secondary key on id so the
    # order is total: two documents can share a timestamp to the second.
    rows = _rows_for(conn, set(inclusions))
    ordered = [r["id"] for r in sorted(rows, key=lambda r: (r["doc_date"] or "", r["id"]))]

    # The cap binds here, not during collection. Anchors are gathered without
    # limit — they are sufficient, and checking a bound mid-collection would make
    # the set depend on which anchor happened to be queried first, which is
    # exactly the non-determinism this whole module exists to avoid.
    dropped = 0
    if len(ordered) > max_docs:
        dropped = len(ordered) - max_docs
        # Keep the most recent: a question about the state of a process is
        # nearly always a question about where it stands now.
        ordered = ordered[-max_docs:]
        kept = set(ordered)
        inclusions = {d: i for d, i in inclusions.items() if d in kept}
        log.info(
            "gather for %s hit the %d document cap; dropped %d older documents",
            process.slug, max_docs, dropped,
        )

    return GatherResult(
        version=GATHER_VERSION,
        process_slug=process.slug,
        process_name=process.name,
        doc_ids=ordered,
        inclusions=inclusions,
        anchors=[{"kind": a.kind, "value": a.value} for a in process.anchors],
        seed_only=[d for d in ordered if inclusions[d].reason == "seed_only"],
        capped=dropped > 0,
        dropped=dropped,
        depth_reached=depth,
        gathered_at=now or "",
    )
