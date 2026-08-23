"""Re-derive everything downstream of the raw bytes.

Ingest is idempotent on ``(source, source_uid)``, which is exactly what makes a
crashed sync safe to re-run — and exactly what stops a parser fix from ever
reaching mail that is already indexed. Without this command, improving the
parser would mean deleting the index and re-fetching, and the whole point of
storing originals immutably is that re-fetching is never necessary.

Two properties:

* **Vectors survive unchanged text.** Chunks are keyed by ``text_hash``, so
  before the rebuild we snapshot every ``(text_hash, model_id) -> embedding``
  for the document and re-attach whatever still applies afterwards. A parser
  fix that changes one message in fifty does not cost forty-nine re-embeddings.
* **Resumable by being idempotent.** A document whose rebuilt chunks hash
  identically is left completely untouched, so re-running after an interrupt
  re-checks cheaply rather than rewriting.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field

from muster.blobs import BlobNotFound, BlobStore
from muster.categories import category_of
from muster.db import transaction
from muster.ingest import (
    _insert_attachment_doc,
    _insert_chunks,
    _text_hash,
    attachment_plan,
    build_chunks,
)
from muster.parser import parse_message

log = logging.getLogger(__name__)


@dataclass
class ReparseReport:
    scanned: int = 0
    changed: int = 0
    unchanged: int = 0
    failed: int = 0
    chunks_before: int = 0
    chunks_after: int = 0
    vectors_preserved: int = 0
    vectors_dropped: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)


def _parent_docs(
    conn: sqlite3.Connection, *, source: str | None, limit: int | None
) -> Iterator[sqlite3.Row]:
    """Documents that own raw bytes. Attachment docs are rebuilt with their parent."""
    sql = """
        SELECT d.id, d.source, d.source_uid, d.blob_path, d.meta_json
        FROM docs d
        WHERE d.origin = 'captured'
          AND d.blob_path IS NOT NULL
          AND d.mime = 'message/rfc822'
    """
    params: dict[str, object] = {}
    if source:
        sql += " AND d.source = :source"
        params["source"] = source
    sql += " ORDER BY d.id"
    if limit is not None:
        sql += " LIMIT :limit"
        params["limit"] = limit
    yield from conn.execute(sql, params)


def _existing_chunk_hashes(conn: sqlite3.Connection, doc_id: int) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT text_hash FROM chunks WHERE doc_id = ? ORDER BY ord", (doc_id,)
        )
    ]


def _snapshot_vectors(conn: sqlite3.Connection, doc_ids: list[int]) -> dict[tuple[str, str], bytes]:
    """(text_hash, model_id) -> embedding, for every chunk about to be deleted."""
    if not doc_ids:
        return {}
    placeholders = ",".join("?" * len(doc_ids))
    rows = conn.execute(
        f"""
        SELECT c.text_hash, v.model_id, v.embedding
        FROM vectors v JOIN chunks c ON c.id = v.chunk_id
        WHERE c.doc_id IN ({placeholders})
        """,
        doc_ids,
    ).fetchall()
    return {(row[0], row[1]): row[2] for row in rows}


def _restore_vectors(
    conn: sqlite3.Connection, doc_ids: list[int], snapshot: dict[tuple[str, str], bytes]
) -> int:
    if not snapshot or not doc_ids:
        return 0
    placeholders = ",".join("?" * len(doc_ids))
    chunks = conn.execute(
        f"SELECT id, text_hash FROM chunks WHERE doc_id IN ({placeholders})", doc_ids
    ).fetchall()

    rows = [
        (chunk_id, model_id, embedding)
        for chunk_id, text_hash in chunks
        for (snap_hash, model_id), embedding in snapshot.items()
        if snap_hash == text_hash
    ]
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO vectors (chunk_id, model_id, embedding) VALUES (?, ?, ?)",
            rows,
        )
    return len(rows)


def _child_doc_ids(conn: sqlite3.Connection, doc_id: int) -> list[int]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT extracted_doc_id FROM attachments"
            " WHERE doc_id = ? AND extracted_doc_id IS NOT NULL",
            (doc_id,),
        )
    ]


def _stored_signature(conn: sqlite3.Connection, doc_id: int):
    """The document's current derived content, in a form comparable to a fresh parse."""
    parent = _existing_chunk_hashes(conn, doc_id)
    attachments = []
    rows = conn.execute(
        "SELECT content_hash, filename, extracted_doc_id FROM attachments"
        " WHERE doc_id = ? ORDER BY id",
        (doc_id,),
    ).fetchall()
    for content_hash, filename, child_id in rows:
        child = _existing_chunk_hashes(conn, child_id) if child_id else []
        attachments.append((content_hash, filename, tuple(child)))
    return parent, attachments


def _new_signature(parsed, new_chunks: list[str], plan) -> tuple:
    return (
        [_text_hash(text) for text in new_chunks],
        [
            (attachment.content_hash, attachment.filename, tuple(_text_hash(c) for c in child))
            for attachment, child in zip(parsed.attachments, plan, strict=True)
        ],
    )


_DOC_FIELDS = (
    "title", "doc_date", "date_source", "counterparty", "thread_uid", "category", "sender",
)


def reparse_doc(
    conn: sqlite3.Connection,
    store: BlobStore,
    row: sqlite3.Row,
    *,
    own_addresses: set[str] | None = None,
    force: bool = False,
    redactor=None,
    sender_filter=None,
) -> tuple[bool, int, int, int]:
    """Rebuild one document. Returns (changed, chunks_before, chunks_after, vectors_kept)."""
    doc_id = int(row["id"])
    raw = store.get_blob(row["blob_path"])

    stored_meta = {}
    if row["meta_json"]:
        try:
            stored_meta = json.loads(row["meta_json"])
        except (TypeError, ValueError):
            log.debug("doc %s has unreadable meta_json; reparsing without it", doc_id)

    # The provider metadata the connector supplied originally is preserved in
    # meta_json, so reparse can resolve dates and labels exactly as sync did.
    meta = {
        "labels": stored_meta.get("labels") or [],
        "thread_uid": stored_meta.get("thread_uid"),
        "internal_date": stored_meta.get("internal_date"),
    }

    parsed = parse_message(
        raw, row["source_uid"], meta, own_addresses=own_addresses, redactor=redactor,
        sender_filter=sender_filter,
    )
    new_chunks = build_chunks(parsed)
    plan = attachment_plan(parsed)

    old_parent, old_attachments = _stored_signature(conn, doc_id)
    new_parent, new_attachments = _new_signature(parsed, new_chunks, plan)
    content_changed = force or (new_parent, new_attachments) != (old_parent, old_attachments)

    current = conn.execute(
        f"SELECT {', '.join(_DOC_FIELDS)} FROM docs WHERE id = ?", (doc_id,)
    ).fetchone()

    new_fields = {
        "title": parsed.title,
        "doc_date": parsed.doc_date,
        "date_source": parsed.date_source,
        "counterparty": parsed.counterparty,
        "thread_uid": parsed.thread_uid,
        "category": category_of(parsed.headers.get("labels")),
        "sender": (parsed.headers.get("from") or [None])[0],
    }

    # A message with no usable timestamp anywhere was stamped with the time it
    # was first seen. Re-stamping it with the time it was reparsed would move
    # the document in date order for no reason, and would mark it changed on
    # every single run. First sight is the better answer, so keep it.
    if new_fields["date_source"] == "ingest" and current["date_source"] == "ingest":
        new_fields["doc_date"] = current["doc_date"]

    if not content_changed:
        # Metadata can still move without the text changing — a category
        # backfill after migration 002, say. Update the row and leave the
        # chunks, and therefore the vectors, entirely alone.
        if all(current[field] == new_fields[field] for field in _DOC_FIELDS):
            return False, len(old_parent), len(old_parent), 0
        conn.execute(
            f"UPDATE docs SET {', '.join(f'{f} = :{f}' for f in _DOC_FIELDS)} WHERE id = :id",
            {**new_fields, "id": doc_id},
        )
        return True, len(old_parent), len(old_parent), 0

    child_ids = _child_doc_ids(conn, doc_id)
    affected = [doc_id, *child_ids]
    snapshot = _snapshot_vectors(conn, affected)

    with transaction(conn):
        # Attachment rows first: their extracted_doc_id references the child
        # docs, and that foreign key has no ON DELETE behaviour of its own.
        conn.execute("DELETE FROM attachments WHERE doc_id = ?", (doc_id,))
        if child_ids:
            placeholders = ",".join("?" * len(child_ids))
            conn.execute(f"DELETE FROM docs WHERE id IN ({placeholders})", child_ids)
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))

        conn.execute(
            f"""
            UPDATE docs SET {', '.join(f'{f} = :{f}' for f in _DOC_FIELDS)}, meta_json = :meta
            WHERE id = :id
            """,
            {
                **new_fields,
                "meta": json.dumps(
                    {**parsed.headers, "body_mime": parsed.mime,
                     "warnings": parsed.warnings, **meta},
                    default=str,
                ),
                "id": doc_id,
            },
        )
        _insert_chunks(conn, doc_id, new_chunks, parsed.title)
        _rebuild_attachments(conn, store, doc_id, parsed, plan, row["source"])

        rebuilt = [doc_id, *_child_doc_ids(conn, doc_id)]
        kept = _restore_vectors(conn, rebuilt, snapshot)

    return True, len(old_parent), len(new_chunks), kept


def _rebuild_attachments(conn, store, doc_id, parsed, plan, source) -> None:
    """Recreate attachment rows and child docs from the plan already computed."""
    for index, (attachment, child_chunks) in enumerate(
        zip(parsed.attachments, plan, strict=True)
    ):
        try:
            blob_path, content_hash = store.put_blob(attachment.data)
        except OSError as exc:
            log.error("could not store attachment %s: %s", attachment.filename, exc)
            continue

        child_id = None
        if child_chunks:
            child_id = _insert_attachment_doc(
                conn,
                store_path=blob_path,
                content_hash=content_hash,
                attachment=attachment,
                parent=parsed,
                parent_uid=parsed.source_uid,
                index=index,
                source=source,
                chunks=child_chunks,
            )

        conn.execute(
            """
            INSERT OR IGNORE INTO attachments
                (doc_id, blob_path, content_hash, filename, mime, extracted_doc_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (doc_id, blob_path, content_hash, attachment.filename, attachment.mime, child_id),
        )


def reparse(
    conn: sqlite3.Connection,
    store: BlobStore,
    *,
    source: str | None = None,
    limit: int | None = None,
    own_addresses: set[str] | None = None,
    force: bool = False,
    redactor=None,
    sender_filter=None,
    progress=None,
) -> ReparseReport:
    """Rebuild chunks, attachments and categories from stored raw bytes."""
    report = ReparseReport()

    for row in list(_parent_docs(conn, source=source, limit=limit)):
        report.scanned += 1
        try:
            changed, before, after, kept = reparse_doc(
                conn, store, row, own_addresses=own_addresses, force=force,
                redactor=redactor, sender_filter=sender_filter,
            )
        except BlobNotFound:
            report.failed += 1
            report.failures.append((row["source_uid"], "raw blob missing"))
            log.error("doc %s: raw blob %s missing", row["id"], row["blob_path"])
            continue
        except Exception as exc:
            report.failed += 1
            report.failures.append((row["source_uid"], str(exc)))
            log.exception("doc %s failed to reparse: %s", row["id"], exc)
            continue

        report.chunks_before += before
        report.chunks_after += after
        if changed:
            report.changed += 1
            report.vectors_preserved += kept
        else:
            report.unchanged += 1

        if progress and report.scanned % 50 == 0:
            progress(report)

    report.vectors_dropped = max(
        0, report.chunks_before - report.chunks_after - report.vectors_preserved
    )
    return report
