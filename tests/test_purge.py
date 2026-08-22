"""Removing documents from the working index.

Fixture mail imported for testing competes with real correspondence in real
searches. It belongs in the pinned eval corpus, which is a frozen copy.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from muster.blobs import BlobStore
from muster.connectors.mbox import MboxConnector
from muster.ingest import ingest_message
from muster.purge import plan, purge

FIXTURE = Path(__file__).parent / "fixtures" / "sample.mbox"

REAL = (
    b"Message-ID: <real@example.com>\r\nDate: Mon, 16 Mar 2026 09:00:00 +0000\r\n"
    b"From: Bob <bob@example.com>\r\nTo: A <a@b>\r\nSubject: Warehouse lease\r\n"
    b"Content-Type: text/plain\r\n\r\nConfirming the warehouse lease renewal.\r\n"
)


@pytest.fixture
def mixed(conn: sqlite3.Connection, cfg) -> sqlite3.Connection:
    store = BlobStore(cfg.blob_dir)
    for message in MboxConnector(FIXTURE).fetch():
        ingest_message(
            conn, store, source="file", source_uid=message.source_uid, raw=message.raw
        )
    ingest_message(conn, store, source="gmail", source_uid="g1", raw=REAL)
    return conn


def _counts(conn):
    return {
        t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in ("docs", "chunks", "vectors", "attachments")
    }


# --- the point ------------------------------------------------------------


def test_purging_a_source_removes_only_that_source(mixed) -> None:
    purge(mixed, source="file")

    remaining = [r[0] for r in mixed.execute("SELECT DISTINCT source FROM docs")]
    assert remaining == ["gmail"]
    assert mixed.execute("SELECT count(*) FROM docs").fetchone()[0] == 1


def test_fixture_docs_stop_ranking_after_a_purge(mixed) -> None:
    from muster.search import keyword_search

    before = {h.title for h in keyword_search(mixed, "boundary charset invented", min_score=0.0)}
    assert before, "the fixture noise is there to begin with"

    purge(mixed, source="file")
    after = {h.title for h in keyword_search(mixed, "boundary charset invented", min_score=0.0)}
    assert after == set()


def test_chunks_vectors_and_fts_rows_go_too(mixed) -> None:
    mixed.execute(
        "INSERT INTO vectors (chunk_id, model_id, embedding)"
        " SELECT id, 'm', X'00' FROM chunks"
    )
    assert _counts(mixed)["vectors"] > 0

    purge(mixed, source="file")
    after = _counts(mixed)

    orphan_chunks = mixed.execute(
        "SELECT count(*) FROM chunks c WHERE NOT EXISTS"
        " (SELECT 1 FROM docs d WHERE d.id = c.doc_id)"
    ).fetchone()[0]
    assert orphan_chunks == 0
    assert after["vectors"] == mixed.execute("SELECT count(*) FROM vectors").fetchone()[0]
    # The FTS index is external-content and must have followed the triggers.
    assert mixed.execute(
        "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'capybara'"
    ).fetchone()[0] == 0


def test_attachment_docs_and_rows_are_cleaned_up(mixed) -> None:
    assert mixed.execute("SELECT count(*) FROM attachments").fetchone()[0] > 0
    purge(mixed, source="file")

    assert mixed.execute("SELECT count(*) FROM attachments").fetchone()[0] == 0
    dangling = mixed.execute(
        "SELECT count(*) FROM attachments a WHERE a.extracted_doc_id IS NOT NULL"
        " AND NOT EXISTS (SELECT 1 FROM docs d WHERE d.id = a.extracted_doc_id)"
    ).fetchone()[0]
    assert dangling == 0


# --- safety ---------------------------------------------------------------


def test_a_purge_without_a_selector_is_refused(mixed) -> None:
    """Deleting everything must never be the accidental default."""
    with pytest.raises(ValueError, match="without a selector"):
        purge(mixed)


def test_plan_changes_nothing(mixed) -> None:
    before = _counts(mixed)
    preview = plan(mixed, source="file")

    assert preview.docs == 16
    assert preview.chunks > 0
    assert preview.titles
    assert _counts(mixed) == before


def test_a_selector_matching_nothing_is_a_no_op(mixed) -> None:
    before = _counts(mixed)
    result = purge(mixed, source="outlook")
    assert result.empty
    assert _counts(mixed) == before


def test_blobs_survive_so_the_mail_can_be_reindexed(mixed, cfg) -> None:
    store = BlobStore(cfg.blob_dir)
    digests_before = set(store.iter_digests())
    purge(mixed, source="file")
    assert set(store.iter_digests()) == digests_before


def test_reingest_after_purge_restores_the_documents(mixed, cfg) -> None:
    store = BlobStore(cfg.blob_dir)
    purge(mixed, source="file")
    for message in MboxConnector(FIXTURE).fetch():
        ingest_message(
            mixed, store, source="file", source_uid=message.source_uid, raw=message.raw
        )
    assert mixed.execute("SELECT count(*) FROM docs WHERE source='file'").fetchone()[0] == 16


# --- other selectors ------------------------------------------------------


def test_purge_by_uid_prefix(mixed) -> None:
    result = purge(mixed, uid_prefix="attachment-5@")
    assert result.docs >= 1
    assert mixed.execute(
        "SELECT count(*) FROM docs WHERE source_uid LIKE 'attachment-5@%'"
    ).fetchone()[0] == 0


def test_purge_by_category(mixed, cfg) -> None:
    store = BlobStore(cfg.blob_dir)
    ingest_message(
        mixed, store, source="gmail", source_uid="promo1", raw=REAL,
        meta={"labels": ["CATEGORY_PROMOTIONS"]},
    )
    assert purge(mixed, category="promotions").docs == 1
    assert mixed.execute(
        "SELECT count(*) FROM docs WHERE category='promotions'"
    ).fetchone()[0] == 0


# --- the test suite itself ------------------------------------------------


def test_the_suite_cannot_reach_the_real_index() -> None:
    """The reason fixture docs got into the live index in the first place."""
    from muster.config import muster_home

    assert muster_home() != Path("~/.muster").expanduser().resolve()
