"""Processes and their anchors.

A process is the thing a pile of documents is *about*. An anchor is a confirmed
identifier belonging to it — a domain, an address, a thread, a reference-number
pattern, a folder, one document.

Anchors are **sufficient, not necessary**. Anything matching one is in the
process unconditionally; documents also arrive from the seed search alone. A visa
email sent from a personal address and a scan called `scan_047.pdf` belong to the
process without matching any anchor, and requiring one would lose precisely the
documents least likely to look official.

Proposals are never applied on their own. Clustering by embedding similarity is
deliberately not used anywhere here: clusters reshuffle on every run, so a
grouping built from them could never be trusted to mean the same thing twice.
Anchors are exact joins, and that is the whole point of them.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

log = logging.getLogger(__name__)

# How many search results to mine for patterns. Enough that a recurring domain
# shows up several times; small enough that one noisy query cannot bury it.
PROPOSAL_DEPTH = 50

# A pattern has to recur to be worth proposing. Once is a coincidence.
MIN_OCCURRENCES = 2

# Reference-number shapes: long enough not to be a word, mixed enough not to be
# a date or a price. Deliberately conservative — a bad anchor pulls in unrelated
# documents forever, and a missing one only costs a proposal.
REF_SHAPES = (
    re.compile(r"\b[A-Z]{2,5}[-_ ]?\d{4,12}\b"),        # AB-12345678, TUM 4471
    re.compile(r"\b\d{2,4}[-/]\d{3,8}[-/]\d{2,8}\b"),   # 21-4471-99
    re.compile(r"\b[A-Z0-9]{8,14}\b"),                  # AA00FCH0WH
)

# Domains that belong to everybody and identify nothing.
GENERIC_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "icloud.com",
    "me.com", "yahoo.com", "proton.me", "protonmail.com", "live.com",
    "google.com", "docs.google.com", "drive.google.com", "mail.google.com",
    "googleusercontent.com", "accounts.google.com", "apple.com",
})


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def slugify(name: str) -> str:
    slug = re.sub(r"[^0-9a-z]+", "-", name.lower()).strip("-")
    return slug or "process"


@dataclass(frozen=True)
class Anchor:
    kind: str
    value: str


@dataclass
class Process:
    id: int
    slug: str
    name: str
    created_at: str
    updated_at: str
    anchors: list[Anchor] = field(default_factory=list)


@dataclass
class Proposal:
    """A candidate anchor, with the evidence for it."""

    kind: str
    value: str
    count: int          # documents supporting it
    example: str        # one document title, so a human can judge

    def line(self) -> str:
        return f"{self.kind:12} {self.value:38} {self.count:3} docs   e.g. {self.example[:44]}"


# --- storage ---------------------------------------------------------------


def create(conn: sqlite3.Connection, name: str, slug: str | None = None) -> Process:
    slug = slug or slugify(name)
    now = _now()
    cursor = conn.execute(
        "INSERT INTO processes (slug, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (slug, name, now, now),
    )
    conn.commit()
    return Process(id=int(cursor.lastrowid), slug=slug, name=name,
                   created_at=now, updated_at=now)


def get(conn: sqlite3.Connection, slug: str) -> Process | None:
    row = conn.execute("SELECT * FROM processes WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        return None
    process = Process(
        id=row["id"], slug=row["slug"], name=row["name"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )
    process.anchors = [
        Anchor(kind=a["kind"], value=a["value"])
        for a in conn.execute(
            "SELECT kind, value FROM process_anchors WHERE process_id = ?"
            " ORDER BY kind, value",
            (process.id,),
        )
    ]
    return process


def all_processes(conn: sqlite3.Connection) -> list[Process]:
    return [
        p for p in (get(conn, row[0]) for row in
                   conn.execute("SELECT slug FROM processes ORDER BY name"))
        if p is not None
    ]


def add_anchor(conn: sqlite3.Connection, process_id: int, kind: str, value: str) -> bool:
    """True if it was new. Re-adding an anchor is not an error."""
    try:
        conn.execute(
            "INSERT INTO process_anchors (process_id, kind, value, created_at)"
            " VALUES (?, ?, ?, ?)",
            (process_id, kind, value, _now()),
        )
    except sqlite3.IntegrityError:
        return False
    conn.execute("UPDATE processes SET updated_at = ? WHERE id = ?", (_now(), process_id))
    conn.commit()
    return True


def remove_anchor(conn: sqlite3.Connection, process_id: int, kind: str, value: str) -> bool:
    cursor = conn.execute(
        "DELETE FROM process_anchors WHERE process_id = ? AND kind = ? AND value = ?",
        (process_id, kind, value),
    )
    conn.commit()
    return cursor.rowcount > 0


def delete(conn: sqlite3.Connection, slug: str) -> bool:
    cursor = conn.execute("DELETE FROM processes WHERE slug = ?", (slug,))
    conn.commit()
    return cursor.rowcount > 0


# --- proposal --------------------------------------------------------------


# A reference number is not a phone number. Ten and eleven digit runs, and
# seven-digit local numbers, are contact details: they identify an office, not a
# process, and anchoring on one would pull in everything that office ever
# touched. Mining this archive proposed 555-301-0142 before this existed.
PHONE_DIGIT_COUNTS = frozenset({7, 10, 11})


def _looks_like_phone(token: str) -> bool:
    digits = [c for c in token if c.isdigit()]
    return not any(c.isalpha() for c in token) and len(digits) in PHONE_DIGIT_COUNTS


# An address on a large share of the whole archive is the user's own, or a
# service they cannot distinguish anything by. Either way it is useless as an
# anchor: it matches nearly everything, which is the same as matching nothing.
UBIQUITOUS_SHARE = 0.10


def own_addresses_from_archive(conn: sqlite3.Connection) -> set[str]:
    """The user's own addresses, derived rather than configured.

    A message Gmail labelled SENT was sent by the account, so its From header is
    the user. Reading it from the archive rather than trusting configuration
    matters because config is easy to leave incomplete — on this archive the
    configured identity omitted the Gmail address entirely, and the user's own
    address was being proposed as an anchor for every process.
    """
    rows = conn.execute(
        """
        SELECT lower(sender) AS address, count(*) AS n
        FROM docs
        WHERE origin = 'captured' AND sender IS NOT NULL
          AND json_extract(coalesce(meta_json, '{}'), '$.labels') LIKE '%SENT%'
        GROUP BY lower(sender)
        ORDER BY n DESC
        LIMIT 5
        """
    ).fetchall()
    if not rows:
        return set()
    # Only addresses that account for a real share of sent mail: a single
    # forwarded message would otherwise nominate a stranger.
    best = rows[0]["n"]
    return {r["address"] for r in rows if r["n"] >= max(best * 0.2, 3)}


def _ubiquitous_addresses(conn: sqlite3.Connection) -> set[str]:
    total = conn.execute(
        "SELECT count(*) FROM docs WHERE origin = 'captured'"
    ).fetchone()[0]
    if not total:
        return set()
    floor = max(int(total * UBIQUITOUS_SHARE), 25)
    return {
        row[0].lower()
        for row in conn.execute(
            "SELECT counterparty FROM docs WHERE origin = 'captured'"
            " AND counterparty IS NOT NULL"
            " GROUP BY lower(counterparty) HAVING count(*) >= ?",
            (floor,),
        )
    }


def _domain_of(address: str | None) -> str | None:
    if not address or "@" not in address:
        return None
    domain = address.rsplit("@", 1)[1].strip().lower()
    return domain or None


def _seed_docs(conn: sqlite3.Connection, name: str, depth: int) -> list[sqlite3.Row]:
    """Top search results for the process name, as full document rows."""
    from muster.search import search as run_search

    hits = run_search(conn, name, mode="hybrid", limit=depth, min_score=0.0)
    if not hits:
        return []
    ids = [h.doc_id for h in hits]
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, title, source, source_uid, mime, counterparty, thread_uid,"
        f" sender, meta_json FROM docs WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    order = {doc_id: i for i, doc_id in enumerate(ids)}
    return sorted(rows, key=lambda r: order.get(r["id"], 1_000_000))


# How much body text to mine per document for reference numbers. The first chunk
# is where an application id or case number lives; scanning a whole thread finds
# every number anybody ever quoted.
REF_TEXT_CHARS = 1200


def propose(
    conn: sqlite3.Connection,
    name: str,
    depth: int = PROPOSAL_DEPTH,
    own_addresses: frozenset[str] = frozenset(),
) -> list[Proposal]:
    """Recurring identifiers in the documents a process name finds.

    Counts are over *documents*, not occurrences: a reference number repeated
    forty times in one thread is one document's worth of evidence, and treating
    it as forty would promote whatever happens to be verbose.
    """
    rows = _seed_docs(conn, name, depth)
    if not rows:
        return []

    own = (
        {a.lower() for a in own_addresses}
        | _ubiquitous_addresses(conn)
        | own_addresses_from_archive(conn)
    )
    own_domains = {d for d in (_domain_of(a) for a in own_addresses) if d}

    # First chunk per document, for reference numbers. Titles almost never carry
    # them — on this archive, mining titles alone proposed none at all.
    first_chunk = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT doc_id, substr(text, 1, ?) FROM chunks WHERE ord = 0 AND doc_id IN ("
            + ",".join("?" * len(rows)) + ")",
            (REF_TEXT_CHARS, *[r["id"] for r in rows]),
        )
    }

    domains: dict[str, list[sqlite3.Row]] = {}
    addresses: dict[str, list[sqlite3.Row]] = {}
    threads: dict[str, list[sqlite3.Row]] = {}
    folders: dict[str, list[sqlite3.Row]] = {}
    refs: dict[str, set[int]] = {}
    ref_example: dict[str, str] = {}

    for row in rows:
        title = row["title"] or f"doc {row['id']}"

        for address in {a for a in (row["counterparty"], row["sender"]) if a}:
            low = address.lower()
            # Own addresses match everything the user has ever been party to, so
            # as an anchor they are the same as no anchor at all.
            if low in own:
                continue
            addresses.setdefault(low, []).append(row)
            domain = _domain_of(address)
            if domain and domain not in GENERIC_DOMAINS and domain not in own_domains:
                domains.setdefault(domain, []).append(row)

        if row["thread_uid"]:
            threads.setdefault(row["thread_uid"], []).append(row)

        if row["source"] == "file":
            try:
                path = json.loads(row["meta_json"] or "{}").get("path")
            except (TypeError, ValueError):
                path = None
            if path:
                parent = path.rsplit("/", 1)[0]
                folders.setdefault(parent, []).append(row)

        # Title and the opening of the body. Bounded on purpose: an application
        # id appears near the top, and scanning a whole thread would collect
        # every number anyone ever quoted.
        haystack = f"{title}\n{first_chunk.get(row['id'], '')}"
        for shape in REF_SHAPES:
            for token in shape.findall(haystack):
                token = token.strip()
                if len(token) < 5 or token.isalpha() or _looks_like_phone(token):
                    continue
                refs.setdefault(token, set()).add(row["id"])
                ref_example.setdefault(token, title)

    out: list[Proposal] = []

    def collect(kind: str, groups: dict[str, list[sqlite3.Row]]) -> None:
        for value, docs in groups.items():
            if len(docs) < MIN_OCCURRENCES:
                continue
            out.append(Proposal(
                kind=kind, value=value, count=len(docs),
                example=docs[0]["title"] or f"doc {docs[0]['id']}",
            ))

    collect("domain", domains)
    collect("address", addresses)
    collect("thread_uid", threads)
    collect("folder", folders)
    for token, doc_ids in refs.items():
        if len(doc_ids) >= MIN_OCCURRENCES:
            out.append(Proposal(
                kind="ref_pattern", value=token,
                count=len(doc_ids), example=ref_example[token],
            ))

    # Most-evidenced first, and a stable order within a count so the same
    # archive proposes the same list twice.
    out.sort(key=lambda p: (-p.count, p.kind, p.value))
    return out


def unclaimed_clusters(
    conn: sqlite3.Connection, limit: int = 10, min_docs: int = 4
) -> list[Proposal]:
    """Counterparty domains carrying real correspondence that no process claims.

    Grouped by domain rather than by similarity, for the same reason anchors are
    exact: a suggestion that names a different set every time it runs is not a
    suggestion, it is noise.
    """
    claimed = {
        row[0].lower()
        for row in conn.execute("SELECT value FROM process_anchors WHERE kind = 'domain'")
    }
    rows = conn.execute(
        """
        SELECT lower(substr(counterparty, instr(counterparty, '@') + 1)) AS domain,
               count(*) AS n,
               max(title) AS example
        FROM docs
        WHERE origin = 'captured' AND missing_since IS NULL
          AND counterparty LIKE '%@%'
        GROUP BY domain
        HAVING n >= ?
        ORDER BY n DESC
        """,
        (min_docs,),
    ).fetchall()

    out = []
    for row in rows:
        domain = row["domain"]
        if not domain or domain in GENERIC_DOMAINS or domain in claimed:
            continue
        out.append(Proposal(kind="domain", value=domain, count=row["n"],
                            example=row["example"] or ""))
        if len(out) >= limit:
            break
    return out
