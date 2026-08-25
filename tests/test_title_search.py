"""The subject as its own weighted FTS5 column.

Subjects are the highest-signal field in email and were previously reachable
only when a body chunk happened to repeat them. Measured on a 1447-chunk index
with 120 subject-derived probes, giving the title column weight 3.0 moved
subject recall@10 from 0.8167 to 1.0000 and subject MRR from 0.6726 to 0.9056,
at a cost of 3.7% on body-derived MRR.
"""

from __future__ import annotations

import sqlite3

import pytest

from muster.blobs import BlobStore
from muster.ingest import ingest_message
from muster.search import keyword_search


def _raw(uid: str, subject: str, body: str) -> bytes:
    return (
        f"Message-ID: <{uid}>\r\nDate: Mon, 16 Mar 2026 09:00:00 +0000\r\n"
        f"From: S <s@example.com>\r\nTo: A <a@example.com>\r\n"
        f"Subject: {subject}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n"
    ).encode()


@pytest.fixture
def indexed(conn: sqlite3.Connection, cfg) -> sqlite3.Connection:
    store = BlobStore(cfg.blob_dir)
    corpus = [
        # The word "kestrel" appears ONLY in this subject, never in any body.
        ("kestrel@x", "Kestrel deployment schedule", "The rollout begins on Tuesday as agreed."),
        ("body@x", "Weekly note", "A passing mention of kestrel somewhere in the body text here."),
        ("other@x", "Unrelated", "Nothing to do with birds of prey or deployments at all."),
    ]
    for uid, subject, body in corpus:
        ingest_message(conn, store, source="file", source_uid=uid, raw=_raw(uid, subject, body))
    return conn


def test_the_fts_columns_are_in_the_order_bm25_expects(conn) -> None:
    """Order is load-bearing: bm25() takes weights positionally and
    snippet(chunks_fts, 0, ...) must keep pointing at the body."""
    columns = [row[1] for row in conn.execute("PRAGMA table_info(chunks_fts)")]
    assert columns == ["text", "title", "party"]


def test_every_chunk_carries_its_document_title(indexed) -> None:
    """Denormalised onto every chunk so a subject query reaches chunk seven."""
    missing = indexed.execute(
        "SELECT count(*) FROM chunks c JOIN docs d ON d.id = c.doc_id"
        " WHERE d.title IS NOT NULL AND c.title IS NOT DISTINCT FROM NULL"
    ).fetchone()[0]
    assert missing == 0


def test_a_subject_only_term_is_findable(indexed) -> None:
    """Before the title column this document was unreachable by its own subject."""
    hits = keyword_search(indexed, "Kestrel deployment schedule", min_score=0.0)
    assert "kestrel@x" in [h.source_uid for h in hits]


def test_a_subject_match_outranks_a_passing_body_mention(indexed) -> None:
    hits = keyword_search(indexed, "kestrel", min_score=0.0)
    assert hits[0].source_uid == "kestrel@x"


def test_body_matches_still_work(indexed) -> None:
    hits = keyword_search(indexed, "rollout begins Tuesday", min_score=0.0)
    assert hits[0].source_uid == "kestrel@x"


def test_the_weights_are_configurable(indexed, home) -> None:
    (home / "config.toml").write_text("[search]\ntitle_weight = 0.0\ntext_weight = 1.0\n")
    zeroed = keyword_search(indexed, "kestrel", min_score=0.0)

    (home / "config.toml").write_text("[search]\ntitle_weight = 10.0\ntext_weight = 1.0\n")
    boosted = keyword_search(indexed, "kestrel", min_score=0.0)

    by_uid = {h.source_uid: h.score for h in zeroed}
    boosted_by_uid = {h.source_uid: h.score for h in boosted}
    assert boosted_by_uid["kestrel@x"] > by_uid["kestrel@x"]


def test_triggers_keep_the_title_column_in_sync(indexed) -> None:
    indexed.execute("UPDATE chunks SET title = 'Peregrine rollout' WHERE doc_id = 1")
    assert indexed.execute(
        "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'title:Peregrine'"
    ).fetchone()[0] == 1
    assert indexed.execute(
        "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'title:Kestrel'"
    ).fetchone()[0] == 0


def test_deleting_a_chunk_removes_its_title_from_the_index(indexed) -> None:
    indexed.execute("DELETE FROM chunks WHERE doc_id = 1")
    assert indexed.execute(
        "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'title:Kestrel'"
    ).fetchone()[0] == 0


def test_snippet_still_points_at_the_body(indexed) -> None:
    """snippet(chunks_fts, 0, ...) — column 0 must remain the text."""
    hits = keyword_search(indexed, "rollout", min_score=0.0)
    assert "rollout" in hits[0].snippet.lower()
