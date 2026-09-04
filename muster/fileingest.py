"""Indexing a file without copying it.

Every other document in this index owns its bytes: the blob store holds an
immutable original and the row points at it. A file on disk already has one, so
here the row points at the path instead and `blob_path` stays null.

Two consequences follow. The document is only as durable as the file — delete it
and the text remains searchable but the original is gone, which is the honest
trade for not keeping a second copy of everything. And identity has to be
carried by the hash, because a path is not stable: moving a file from Downloads
to Documents changes the only name it has.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from muster.chunking import chunk_text
from muster.connectors.files import FileRecord, content_hash, spotlight
from muster.ingest import _insert_chunks, _now

log = logging.getLogger(__name__)

SOURCE = "file"

# In priority order, best first. mtime is last because it lies constantly: a
# backup, a sync client or an editor rewrites it for reasons that have nothing
# to do with when the document was written.
DATE_SOURCES = ("exif", "pdfmeta", "birthtime", "mtime")


@dataclass
class FileResult:
    doc_id: int | None = None
    created: bool = False
    updated: bool = False
    moved: bool = False
    unchanged: bool = False
    chunks: int = 0
    date_source: str = "mtime"
    empty: bool = False  # nothing extractable; recorded but not indexed


def exif_date(path: Path) -> str | None:
    """DateTimeOriginal, for photographs and scans.

    The only date that says when the thing in the image happened, as opposed to
    when the file holding it was written.
    """
    try:
        from PIL import Image

        with Image.open(path) as image:
            exif = image.getexif()
            if not exif:
                return None
            # 36867 DateTimeOriginal, 36868 DateTimeDigitized, 306 DateTime.
            for tag in (36867, 36868, 306):
                value = exif.get(tag)
                if not value:
                    # DateTimeOriginal usually lives in the Exif sub-IFD.
                    value = (exif.get_ifd(0x8769) or {}).get(tag)
                if value:
                    parsed = datetime.strptime(str(value).strip(), "%Y:%m:%d %H:%M:%S")
                    return parsed.replace(tzinfo=UTC).isoformat()
    except Exception as exc:  # noqa: BLE001 — a missing or odd tag is not an error
        log.debug("no exif date for %s: %s", path, exc)
    return None


def pdf_date(path: Path) -> str | None:
    """The PDF's own /CreationDate, which survives copying as mtime does not."""
    try:
        import pymupdf

        with pymupdf.open(path) as doc:
            raw = (doc.metadata or {}).get("creationDate") or ""
    except Exception as exc:  # noqa: BLE001
        log.debug("no pdf date for %s: %s", path, exc)
        return None

    # D:YYYYMMDDHHmmSSOHH'mm'
    match = re.match(r"D:(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?", raw)
    if not match:
        return None
    year, month, day, hour, minute, second = (
        int(part) if part else 0 for part in match.groups()
    )
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=UTC).isoformat()
    except ValueError:
        return None


def resolve_date(record: FileRecord) -> tuple[str, str]:
    """The best date available, and which kind it is."""
    name = record.path.name.lower()
    if name.endswith((".jpg", ".jpeg", ".tiff", ".tif", ".heic", ".png")):
        found = exif_date(record.path)
        if found:
            return found, "exif"
    if name.endswith(".pdf"):
        found = pdf_date(record.path)
        if found:
            return found, "pdfmeta"
    if record.birthtime:
        return record.birthtime, "birthtime"
    return record.mtime, "mtime"


def ingest_file(conn, record: FileRecord, *, force: bool = False) -> FileResult:
    """Index one file, or decline to because nothing about it changed."""
    from muster.extract import extract_text

    existing = conn.execute(
        "SELECT id, content_hash, meta_json FROM docs WHERE source = ? AND source_uid = ?",
        (SOURCE, record.source_uid),
    ).fetchone()

    if existing and not force:
        meta = _meta(existing["meta_json"])
        # The cheap check, and the reason a re-walk of Downloads costs seconds:
        # size and mtime together are enough to say nothing happened, without
        # opening the file or hashing a single byte of it.
        if meta.get("size") == record.size and meta.get("mtime") == record.mtime:
            return FileResult(doc_id=existing["id"], unchanged=True)

    digest = content_hash(record.path)

    # A file that moved is the same document under a new name. Recognising that
    # is the whole reason identity is the hash here and not the path.
    if not existing:
        moved = conn.execute(
            "SELECT id, source_uid FROM docs WHERE source = ? AND content_hash = ?",
            (SOURCE, digest),
        ).fetchone()
        if moved and not Path(moved["source_uid"]).exists():
            conn.execute(
                "UPDATE docs SET source_uid = ?, meta_json = ? WHERE id = ?",
                (record.source_uid, json.dumps(_metadata(record, digest)), moved["id"]),
            )
            conn.commit()
            return FileResult(doc_id=moved["id"], moved=True)

    if existing and existing["content_hash"] == digest:
        # Touched but not changed. Record the new mtime so the cheap check above
        # succeeds next time instead of hashing it again on every walk.
        conn.execute(
            "UPDATE docs SET meta_json = ? WHERE id = ?",
            (json.dumps(_metadata(record, digest)), existing["id"]),
        )
        conn.commit()
        return FileResult(doc_id=existing["id"], unchanged=True)

    try:
        data = record.path.read_bytes()
    except OSError as exc:
        log.warning("cannot read %s: %s", record.path, exc)
        return FileResult(empty=True)

    text = extract_text(data, record.mime, record.path.name)
    chunks = chunk_text(text)
    doc_date, date_source = resolve_date(record)
    meta = _metadata(record, digest)

    if existing:
        conn.execute(
            "UPDATE docs SET content_hash = ?, mime = ?, title = ?, doc_date = ?,"
            " date_source = ?, meta_json = ?, ingested_at = ? WHERE id = ?",
            (
                digest, record.mime, record.path.name, doc_date, date_source,
                json.dumps(meta), _now(), existing["id"],
            ),
        )
        # The triggers keep the FTS index in step with chunks, so replacing them
        # wholesale is enough; touching chunks_fts directly would desynchronise it.
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (existing["id"],))
        doc_id = existing["id"]
        result = FileResult(doc_id=doc_id, updated=True, date_source=date_source)
    else:
        cursor = conn.execute(
            """
            INSERT INTO docs (
                source, source_uid, origin, content_hash, blob_path, mime, title,
                doc_date, date_source, counterparty, thread_uid, meta_json,
                category, sender, sender_name, ingested_at
            ) VALUES (?, ?, 'captured', ?, NULL, ?, ?, ?, ?, NULL, NULL, ?,
                      NULL, NULL, NULL, ?)
            """,
            (
                SOURCE, record.source_uid, digest, record.mime, record.path.name,
                doc_date, date_source, json.dumps(meta), _now(),
            ),
        )
        doc_id = int(cursor.lastrowid)
        result = FileResult(doc_id=doc_id, created=True, date_source=date_source)

    result.chunks = _insert_chunks(conn, doc_id, chunks, title=record.path.name)
    result.empty = not chunks
    conn.commit()
    return result


def _metadata(record: FileRecord, digest: str) -> dict:
    """What is stored instead of the bytes."""
    meta = {
        "path": record.source_uid,
        "size": record.size,
        "mtime": record.mtime,
        "birthtime": record.birthtime,
        "content_hash": digest,
    }
    meta.update(spotlight(record.path))
    return meta


def _meta(raw: str | None) -> dict:
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
