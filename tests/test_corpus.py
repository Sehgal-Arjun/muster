"""Pinned eval corpora.

Numbers that move because the mailbox grew are worse than no numbers, because
they look like progress. A run is only comparable to another run over the same
corpus, so the corpus has an identity and every results file records it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from muster import db, migrate
from muster.blobs import BlobStore
from muster.config import Config
from muster.connectors.mbox import MboxConnector
from muster.evaluate import (
    QueryRecord,
    corpus_fingerprint,
    describe_corpus,
    evaluate,
    snapshot,
    write_run,
)
from muster.ingest import ingest_message

FIXTURE = Path(__file__).parent / "fixtures" / "sample.mbox"


def _ingest(conn, store, limit=None):
    for message in MboxConnector(FIXTURE).fetch(limit=limit):
        ingest_message(
            conn, store, source="file", source_uid=message.source_uid, raw=message.raw
        )


@pytest.fixture
def indexed(conn: sqlite3.Connection, cfg: Config) -> sqlite3.Connection:
    _ingest(conn, BlobStore(cfg.blob_dir))
    return conn


# --- fingerprint ----------------------------------------------------------


def test_fingerprint_is_stable_across_calls(indexed) -> None:
    assert corpus_fingerprint(indexed) == corpus_fingerprint(indexed)


def test_fingerprint_changes_when_a_document_is_added(indexed, cfg) -> None:
    before = corpus_fingerprint(indexed)
    ingest_message(
        indexed,
        BlobStore(cfg.blob_dir),
        source="file",
        source_uid="new@example.com",
        raw=b"Message-ID: <new@example.com>\r\nSubject: New\r\n\r\nSomething new arrived.\r\n",
    )
    assert corpus_fingerprint(indexed) != before


def test_fingerprint_ignores_row_ids_and_insertion_order(tmp_path, cfg) -> None:
    """Two indexes holding the same mail must fingerprint identically."""
    import os

    fingerprints = []
    for name in ("a", "b"):
        path = tmp_path / f"{name}.db"
        conn = db.connect(path)
        migrate.migrate(conn)
        store = BlobStore(tmp_path / f"blobs-{name}")
        messages = list(MboxConnector(FIXTURE).fetch())
        if name == "b":
            messages.reverse()
        for message in messages:
            ingest_message(
                conn, store, source="file", source_uid=message.source_uid, raw=message.raw
            )
        fingerprints.append(corpus_fingerprint(conn))
        conn.close()
        os.sync

    assert fingerprints[0] == fingerprints[1]


def test_fingerprint_does_not_change_when_only_chunks_are_rebuilt(indexed, cfg) -> None:
    """Reparse changes derived content; the corpus is still the same mail."""
    from muster.reparse import reparse

    before = corpus_fingerprint(indexed)
    reparse(indexed, BlobStore(cfg.blob_dir), force=True)
    assert corpus_fingerprint(indexed) == before


# --- snapshot -------------------------------------------------------------


def test_snapshot_produces_an_independent_copy(indexed, cfg, tmp_path) -> None:
    frozen_path = snapshot(indexed, tmp_path / "corpus.db")
    assert frozen_path.exists()

    frozen = db.connect(frozen_path)
    try:
        assert corpus_fingerprint(frozen) == corpus_fingerprint(indexed)
        frozen_docs = frozen.execute("SELECT count(*) FROM docs").fetchone()[0]
    finally:
        frozen.close()

    # Growing the live index must not touch the snapshot.
    ingest_message(
        indexed,
        BlobStore(cfg.blob_dir),
        source="file",
        source_uid="later@example.com",
        raw=b"Message-ID: <later@example.com>\r\nSubject: Later\r\n\r\nArrived afterwards.\r\n",
    )

    frozen = db.connect(frozen_path)
    try:
        assert frozen.execute("SELECT count(*) FROM docs").fetchone()[0] == frozen_docs
        assert corpus_fingerprint(frozen) != corpus_fingerprint(indexed)
    finally:
        frozen.close()


def test_snapshot_is_searchable(indexed, tmp_path) -> None:
    """The FTS index and its triggers have to survive the copy."""
    from muster.search import keyword_search

    frozen = db.connect(snapshot(indexed, tmp_path / "corpus.db"))
    try:
        assert keyword_search(frozen, "warehouse lease renewal")
    finally:
        frozen.close()


# --- results files --------------------------------------------------------


def test_results_record_the_corpus_they_were_measured_on(indexed, tmp_path) -> None:
    run = evaluate(
        indexed,
        [QueryRecord(q="warehouse lease", relevant_doc_ids=["ordinary-1@example.com"])],
        corpus_path="/some/corpus.db",
    )
    path = write_run(run, tmp_path / "results")
    payload = json.loads(path.read_text())

    assert payload["corpus"]["path"] == "/some/corpus.db"
    assert payload["corpus"]["fingerprint"] == corpus_fingerprint(indexed)
    assert payload["corpus"]["docs"] == 16
    assert payload["corpus"]["excluded_categories"] == []


def test_results_filename_carries_the_fingerprint(indexed, tmp_path) -> None:
    """Two runs over different corpora must not look like the same experiment."""
    run = evaluate(indexed, [QueryRecord(q="x")], corpus_path="c.db")
    path = write_run(run, tmp_path / "results")
    assert run.corpus.fingerprint in path.name


def test_two_runs_over_one_corpus_agree(indexed) -> None:
    records = [QueryRecord(q="warehouse lease", relevant_doc_ids=["ordinary-1@example.com"])]
    first = evaluate(indexed, records)
    second = evaluate(indexed, records)

    assert first.corpus.fingerprint == second.corpus.fingerprint
    assert (first.recall_at_k, first.mrr) == (second.recall_at_k, second.mrr)


def test_a_grown_index_is_visibly_a_different_corpus(indexed, cfg) -> None:
    records = [QueryRecord(q="warehouse lease", relevant_doc_ids=["ordinary-1@example.com"])]
    before = evaluate(indexed, records)

    ingest_message(
        indexed,
        BlobStore(cfg.blob_dir),
        source="file",
        source_uid="noise@example.com",
        raw=b"Message-ID: <noise@example.com>\r\nSubject: Noise\r\n\r\nUnrelated content here.\r\n",
    )
    after = evaluate(indexed, records)

    assert before.corpus.fingerprint != after.corpus.fingerprint, (
        "the numbers are not comparable and the fingerprint has to say so"
    )


def test_excluded_categories_are_recorded(indexed) -> None:
    run = evaluate(indexed, [QueryRecord(q="x")], exclude_categories=frozenset({"promotions"}))
    assert run.corpus.excluded_categories == ["promotions"]


def test_describe_corpus_counts_only_captured_docs(indexed) -> None:
    indexed.execute(
        "INSERT INTO docs (source, source_uid, origin, content_hash, doc_date,"
        " date_source, ingested_at) VALUES ('muster', 'd1', 'derived', 'h',"
        " '2026-01-01T00:00:00Z', 'header', '2026-01-01T00:00:00Z')"
    )
    assert describe_corpus(indexed, "x").docs == 16
