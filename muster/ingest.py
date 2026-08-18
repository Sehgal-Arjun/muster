"""Parsed messages to database rows.

Idempotency lives here. A message is identified by ``(source, source_uid)``; if
that row exists the whole message is skipped, so re-running a sync over the same
mailbox produces zero new rows and a crashed run is safe to re-run. Each
message commits in its own transaction, so a crash costs at most the message in
flight.

Text-bearing attachments become their own ``docs`` rows. They are ``captured``,
not ``derived``: a PDF that arrived in the post is something we received, and
extracting its text layer is reading it, not inferring from it. ``derived`` is
reserved for content Muster itself concludes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from muster.blobs import BlobStore
from muster.chunking import chunk_text
from muster.db import transaction
from muster.extract import extract_text, is_text_bearing
from muster.parser import ParsedAttachment, ParsedMessage, parse_message

log = logging.getLogger(__name__)


@dataclass
class IngestResult:
    doc_id: int | None
    created: bool
    attachment_docs: int = 0
    chunks: int = 0
    warnings: list[str] | None = None

    @property
    def skipped(self) -> bool:
        return not self.created


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def existing_doc_id(conn: sqlite3.Connection, source: str, source_uid: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM docs WHERE source = ? AND source_uid = ?", (source, source_uid)
    ).fetchone()
    return row[0] if row else None


def _insert_chunks(conn: sqlite3.Connection, doc_id: int, texts: list[str]) -> int:
    rows = [(doc_id, ord_, text, _text_hash(text)) for ord_, text in enumerate(texts)]
    if rows:
        conn.executemany(
            "INSERT INTO chunks (doc_id, ord, text, text_hash) VALUES (?, ?, ?, ?)", rows
        )
    return len(rows)


def _annotate_with_filenames(chunks: list[str], attachments: list[ParsedAttachment]) -> list[str]:
    """Put attachment filenames in the first chunk so filename queries hit.

    Searching for "Rechnung_2026.pdf" should find the message it came with, not
    only the extracted document, because that is how people remember email.
    """
    names = [a.filename for a in attachments if a.filename]
    if not names:
        return chunks
    line = "Attachments: " + ", ".join(names)
    if not chunks:
        return [line]
    return [f"{line}\n\n{chunks[0]}", *chunks[1:]]


def ingest_message(
    conn: sqlite3.Connection,
    store: BlobStore,
    *,
    source: str,
    source_uid: str,
    raw: bytes,
    meta: dict | None = None,
    own_addresses: set[str] | None = None,
) -> IngestResult:
    """Ingest one raw message. Returns without writing if it is already indexed."""
    meta = meta or {}

    if (existing := existing_doc_id(conn, source, source_uid)) is not None:
        return IngestResult(doc_id=existing, created=False)

    parsed = parse_message(raw, source_uid, meta, own_addresses=own_addresses)
    for warning in parsed.warnings:
        log.warning("%s/%s: %s", source, source_uid, warning)

    # The original goes to the blob store before anything references it.
    blob_path, content_hash = store.put_blob(raw)

    body_chunks = _annotate_with_filenames(chunk_text(parsed.body), parsed.attachments)

    with transaction(conn):
        # Re-check inside the transaction: a concurrent worker may have won.
        if (existing := existing_doc_id(conn, source, source_uid)) is not None:
            return IngestResult(doc_id=existing, created=False)

        doc_id = _insert_doc(
            conn,
            source=source,
            source_uid=source_uid,
            content_hash=content_hash,
            blob_path=blob_path,
            mime="message/rfc822",
            title=parsed.title,
            parsed=parsed,
            meta_extra={"body_mime": parsed.mime, "warnings": parsed.warnings, **meta},
        )
        n_chunks = _insert_chunks(conn, doc_id, body_chunks)
        attachment_docs = _ingest_attachments(conn, store, doc_id, parsed, source)

    return IngestResult(
        doc_id=doc_id,
        created=True,
        attachment_docs=attachment_docs,
        chunks=n_chunks,
        warnings=parsed.warnings,
    )


def _insert_doc(
    conn: sqlite3.Connection,
    *,
    source: str,
    source_uid: str,
    content_hash: str,
    blob_path: str | None,
    mime: str | None,
    title: str | None,
    parsed: ParsedMessage,
    meta_extra: dict,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO docs (
            source, source_uid, origin, content_hash, blob_path, mime, title,
            doc_date, date_source, counterparty, thread_uid, meta_json, ingested_at
        ) VALUES (?, ?, 'captured', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source,
            source_uid,
            content_hash,
            blob_path,
            mime,
            title,
            parsed.doc_date,
            parsed.date_source,
            parsed.counterparty,
            parsed.thread_uid,
            json.dumps({**parsed.headers, **meta_extra}, default=str),
            _now(),
        ),
    )
    return int(cursor.lastrowid)


def _ingest_attachments(
    conn: sqlite3.Connection,
    store: BlobStore,
    doc_id: int,
    parsed: ParsedMessage,
    source: str,
) -> int:
    created = 0
    for index, attachment in enumerate(parsed.attachments):
        try:
            blob_path, content_hash = store.put_blob(attachment.data)
        except OSError as exc:
            log.error("could not store attachment %s: %s", attachment.filename, exc)
            continue

        child_id = None
        if is_text_bearing(attachment.mime, attachment.filename):
            text = extract_text(attachment.data, attachment.mime, attachment.filename)
            if text.strip():
                child_id = _insert_attachment_doc(
                    conn,
                    store_path=blob_path,
                    content_hash=content_hash,
                    attachment=attachment,
                    parent=parsed,
                    parent_uid=parsed.source_uid,
                    index=index,
                    source=source,
                    text=text,
                )
                created += 1

        conn.execute(
            """
            INSERT OR IGNORE INTO attachments
                (doc_id, blob_path, content_hash, filename, mime, extracted_doc_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (doc_id, blob_path, content_hash, attachment.filename, attachment.mime, child_id),
        )
    return created


def _insert_attachment_doc(
    conn: sqlite3.Connection,
    *,
    store_path: str,
    content_hash: str,
    attachment: ParsedAttachment,
    parent: ParsedMessage,
    parent_uid: str,
    index: int,
    source: str,
    text: str,
) -> int:
    child_uid = f"{parent_uid}#att{index}"
    if (existing := existing_doc_id(conn, source, child_uid)) is not None:
        return existing

    cursor = conn.execute(
        """
        INSERT INTO docs (
            source, source_uid, origin, content_hash, blob_path, mime, title,
            doc_date, date_source, counterparty, thread_uid, meta_json, ingested_at
        ) VALUES (?, ?, 'captured', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source,
            child_uid,
            content_hash,
            store_path,
            attachment.mime,
            attachment.filename or f"attachment {index}",
            parent.doc_date,
            parent.date_source,
            parent.counterparty,
            parent.thread_uid,
            json.dumps(
                {
                    "attachment_of": parent_uid,
                    "filename": attachment.filename,
                    "parent_subject": parent.title,
                },
                default=str,
            ),
            _now(),
        ),
    )
    child_id = int(cursor.lastrowid)
    _insert_chunks(conn, child_id, chunk_text(text))
    return child_id
