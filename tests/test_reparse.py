from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from muster.blobs import BlobStore
from muster.config import Config
from muster.connectors.mbox import MboxConnector
from muster.ingest import ingest_message
from muster.reparse import reparse

FIXTURE = Path(__file__).parent / "fixtures" / "sample.mbox"
OWN = {"you@example.com"}


@pytest.fixture
def store(cfg: Config) -> BlobStore:
    return BlobStore(cfg.blob_dir)


@pytest.fixture
def indexed(conn: sqlite3.Connection, store: BlobStore) -> sqlite3.Connection:
    for message in MboxConnector(FIXTURE).fetch():
        ingest_message(
            conn, store, source="file", source_uid=message.source_uid,
            raw=message.raw, meta=message.meta, own_addresses=OWN,
        )
    return conn


def _counts(conn) -> dict[str, int]:
    return {
        t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in ("docs", "chunks", "attachments", "vectors")
    }


# --- the point of the command --------------------------------------------


def test_reparse_of_an_unchanged_index_changes_nothing(indexed, store) -> None:
    before = _counts(indexed)
    report = reparse(indexed, store, own_addresses=OWN)

    assert report.scanned == 15
    assert report.changed == 0
    assert report.failed == 0
    assert _counts(indexed) == before


def test_a_parser_fix_reaches_already_ingested_docs(indexed, store, monkeypatch) -> None:
    """The whole reason reparse exists: ingest would skip these on source_uid."""
    original = indexed.execute(
        "SELECT text FROM chunks c JOIN docs d ON d.id = c.doc_id"
        " WHERE d.source_uid = 'ordinary-1@example.com' AND c.ord = 0"
    ).fetchone()[0]

    import muster.ingest as ingest_module

    monkeypatch.setattr(
        ingest_module, "build_chunks", lambda parsed: ["REPARSED: " + parsed.body[:80]]
    )
    import muster.reparse as reparse_module

    monkeypatch.setattr(reparse_module, "build_chunks", ingest_module.build_chunks)

    report = reparse(indexed, store, own_addresses=OWN)
    assert report.changed == 15

    updated = indexed.execute(
        "SELECT text FROM chunks c JOIN docs d ON d.id = c.doc_id"
        " WHERE d.source_uid = 'ordinary-1@example.com' AND c.ord = 0"
    ).fetchone()[0]
    assert updated.startswith("REPARSED:")
    assert updated != original


def test_reparse_is_resumable_by_being_idempotent(indexed, store) -> None:
    first = reparse(indexed, store, own_addresses=OWN, limit=5)
    assert first.scanned == 5

    # Re-running covers the same documents again and rewrites nothing.
    second = reparse(indexed, store, own_addresses=OWN, limit=5)
    assert second.changed == 0
    assert second.unchanged == 5


def test_limit_and_source_narrow_the_scan(indexed, store) -> None:
    assert reparse(indexed, store, limit=3).scanned == 3
    assert reparse(indexed, store, source="gmail").scanned == 0


# --- vectors --------------------------------------------------------------


def test_unchanged_text_keeps_its_vectors(indexed, store) -> None:
    from muster.embed import embed_pending
    from tests.test_embed import FakeModel

    embed_pending(indexed, model_id="m1", model=FakeModel())
    before = dict(indexed.execute("SELECT chunk_id, embedding FROM vectors").fetchall())
    assert before

    reparse(indexed, store, own_addresses=OWN)

    after = dict(indexed.execute("SELECT chunk_id, embedding FROM vectors").fetchall())
    assert after == before, "an unchanged document must not lose its vectors"


def test_rebuilt_chunks_recover_vectors_by_text_hash(indexed, store) -> None:
    """A parser fix touching one message must not cost the others their vectors."""
    from muster.embed import embed_pending
    from tests.test_embed import FakeModel

    embed_pending(indexed, model_id="m1", model=FakeModel())
    total_before = indexed.execute("SELECT count(*) FROM vectors").fetchone()[0]

    report = reparse(indexed, store, own_addresses=OWN, force=True)
    assert report.changed == 15

    total_after = indexed.execute("SELECT count(*) FROM vectors").fetchone()[0]
    assert total_after == total_before
    assert report.vectors_preserved == total_before


def test_vectors_are_dropped_when_the_text_actually_changes(
    indexed, store, monkeypatch
) -> None:
    from muster.embed import embed_pending
    from tests.test_embed import FakeModel

    embed_pending(indexed, model_id="m1", model=FakeModel())
    assert indexed.execute("SELECT count(*) FROM vectors").fetchone()[0] > 0

    import muster.reparse as reparse_module

    monkeypatch.setattr(reparse_module, "build_chunks", lambda parsed: ["entirely new text here"])
    reparse(indexed, store, own_addresses=OWN)

    # Attachment child docs are excluded: build_chunks does not produce their
    # text, so theirs legitimately survives.
    stale = indexed.execute(
        "SELECT count(*) FROM vectors v"
        " JOIN chunks c ON c.id = v.chunk_id"
        " JOIN docs d ON d.id = c.doc_id"
        " WHERE c.text != 'entirely new text here'"
        "   AND d.source_uid NOT LIKE '%#att%'"
    ).fetchone()[0]
    assert stale == 0, "vectors for text that no longer exists must not survive"


# --- attachments ----------------------------------------------------------


def test_attachment_docs_are_rebuilt_not_duplicated(indexed, store) -> None:
    def attachment_docs() -> int:
        return indexed.execute(
            "SELECT count(*) FROM docs WHERE source_uid LIKE '%#att%'"
        ).fetchone()[0]

    before = attachment_docs()
    assert before == 1

    reparse(indexed, store, own_addresses=OWN, force=True)
    assert attachment_docs() == before

    reparse(indexed, store, own_addresses=OWN, force=True)
    assert attachment_docs() == before


def test_attachment_rows_keep_pointing_at_live_docs(indexed, store) -> None:
    reparse(indexed, store, own_addresses=OWN, force=True)
    orphans = indexed.execute(
        "SELECT count(*) FROM attachments a WHERE a.extracted_doc_id IS NOT NULL"
        " AND NOT EXISTS (SELECT 1 FROM docs d WHERE d.id = a.extracted_doc_id)"
    ).fetchone()[0]
    assert orphans == 0


# --- failure handling -----------------------------------------------------


def test_a_missing_blob_is_reported_not_fatal(indexed, store, cfg) -> None:
    row = indexed.execute(
        "SELECT blob_path FROM docs WHERE source_uid = 'ordinary-1@example.com'"
    ).fetchone()
    target = cfg.blob_dir / row["blob_path"]
    target.chmod(0o644)
    target.unlink()

    report = reparse(indexed, store, own_addresses=OWN)
    assert report.failed == 1
    assert report.scanned == 15
    assert any("blob missing" in reason for _uid, reason in report.failures)


def test_reparse_backfills_the_category_column(indexed, store) -> None:
    indexed.execute("UPDATE docs SET category = 'promotions'")
    reparse(indexed, store, own_addresses=OWN, force=True)

    # The fixture is mbox, so nothing carries Gmail labels: all must clear.
    remaining = indexed.execute(
        "SELECT count(*) FROM docs WHERE category IS NOT NULL"
    ).fetchone()[0]
    assert remaining == 0


# --- no document may become unfindable ------------------------------------


def test_a_body_that_cleans_to_nothing_still_yields_a_title_chunk() -> None:
    """Cleaning must not be able to delete a document from search."""
    from muster.ingest import build_chunks
    from muster.parser import parse_message

    raw = (
        b"Message-ID: <imageonly@bank.example>\r\n"
        b"Date: Mon, 16 Mar 2026 09:00:00 +0000\r\n"
        b"From: Bank <alerts@bank.example>\r\n"
        b"To: Sam <you@example.com>\r\n"
        b"Subject: Available balance is below the set threshold\r\n"
        b'Content-Type: text/html; charset="utf-8"\r\n\r\n'
        b'<html><body><img src="x.png"><a href="https://click.bank.example/a"></a>'
        b"\xe2\x80\x8b\xe2\x80\x8b\xe2\x80\x8b</body></html>\r\n"
    )
    parsed = parse_message(raw, "imageonly@bank.example")
    chunks = build_chunks(parsed)

    assert chunks, "a document with no chunks cannot be found by any query"
    assert "Available balance" in chunks[0]


def test_every_reparsed_doc_that_had_text_still_has_chunks(indexed, store) -> None:
    reparse(indexed, store, own_addresses=OWN, force=True)
    empty = indexed.execute(
        "SELECT count(*) FROM docs d WHERE d.mime = 'message/rfc822'"
        " AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.doc_id = d.id)"
    ).fetchone()[0]
    # Only the deliberately empty fixture message may have no chunks.
    assert empty <= 1
