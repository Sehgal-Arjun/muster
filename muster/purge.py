"""Removing documents from the index.

Fixture mail imported for testing ends up competing with real correspondence in
real searches — "Boundary never appears" has no business ranking fifth for
anything. Fixtures belong in the pinned eval corpus, which is a frozen copy, not
in the working index.

Blobs are deliberately left alone. They are content-addressed, immutable, and
cheap; deleting them would break the one guarantee the store makes, and would
also delete bytes shared with documents that are staying. Re-indexing purged
mail is a `muster sync` away precisely because the originals survive.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from muster.db import transaction

log = logging.getLogger(__name__)


@dataclass
class PurgePlan:
    docs: int
    chunks: int
    vectors: int
    attachments: int
    titles: list[str]

    @property
    def empty(self) -> bool:
        return self.docs == 0


def _selector(
    source: str | None, uid_prefix: str | None, category: str | None
) -> tuple[str, dict]:
    clauses: list[str] = []
    params: dict[str, object] = {}
    if source:
        clauses.append("d.source = :source")
        params["source"] = source
    if uid_prefix:
        clauses.append("d.source_uid LIKE :uid_prefix")
        params["uid_prefix"] = f"{uid_prefix}%"
    if category:
        clauses.append("d.category = :category")
        params["category"] = category
    if not clauses:
        raise ValueError("refusing to purge without a selector")
    return " AND ".join(clauses), params


def plan(
    conn: sqlite3.Connection,
    *,
    source: str | None = None,
    uid_prefix: str | None = None,
    category: str | None = None,
) -> PurgePlan:
    """What a purge would remove. Always run before doing it."""
    where, params = _selector(source, uid_prefix, category)

    ids = [
        row[0] for row in conn.execute(f"SELECT d.id FROM docs d WHERE {where}", params)
    ]
    if not ids:
        return PurgePlan(0, 0, 0, 0, [])

    placeholders = ",".join("?" * len(ids))
    counts = {
        "chunks": conn.execute(
            f"SELECT count(*) FROM chunks WHERE doc_id IN ({placeholders})", ids
        ).fetchone()[0],
        "vectors": conn.execute(
            f"SELECT count(*) FROM vectors v JOIN chunks c ON c.id = v.chunk_id"
            f" WHERE c.doc_id IN ({placeholders})",
            ids,
        ).fetchone()[0],
        "attachments": conn.execute(
            f"SELECT count(*) FROM attachments WHERE doc_id IN ({placeholders})", ids
        ).fetchone()[0],
    }
    titles = [
        row[0] or "(no title)"
        for row in conn.execute(
            f"SELECT d.title FROM docs d WHERE {where} ORDER BY d.doc_date DESC LIMIT 12",
            params,
        )
    ]
    return PurgePlan(len(ids), counts["chunks"], counts["vectors"], counts["attachments"], titles)


def purge(
    conn: sqlite3.Connection,
    *,
    source: str | None = None,
    uid_prefix: str | None = None,
    category: str | None = None,
) -> PurgePlan:
    """Delete matching documents. Chunks, vectors and FTS rows follow by cascade."""
    before = plan(conn, source=source, uid_prefix=uid_prefix, category=category)
    if before.empty:
        return before

    where, params = _selector(source, uid_prefix, category)
    with transaction(conn):
        # Attachment rows first: extracted_doc_id has no ON DELETE behaviour, so
        # deleting a child document out from under it would leave a dangling
        # reference. Clearing the pointer is enough — the row itself goes with
        # its parent's cascade.
        conn.execute(
            f"UPDATE attachments SET extracted_doc_id = NULL WHERE extracted_doc_id IN"
            f" (SELECT d.id FROM docs d WHERE {where})",
            params,
        )
        conn.execute(f"DELETE FROM docs WHERE id IN (SELECT d.id FROM docs d WHERE {where})", params)

    return before
