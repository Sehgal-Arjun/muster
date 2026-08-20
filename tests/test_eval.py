from __future__ import annotations

import json
import mailbox
import sqlite3
from pathlib import Path

import pytest

from muster.blobs import BlobStore
from muster.config import Config
from muster.evaluate import (
    EvalError,
    QueryRecord,
    evaluate,
    load_queries,
    resolve_relevant,
    write_run,
)
from muster.ingest import ingest_message

FIXTURE = Path(__file__).parent / "fixtures" / "sample.mbox"
SEED_QUERIES = Path(__file__).parent.parent / "eval" / "queries.jsonl"


@pytest.fixture
def indexed(conn: sqlite3.Connection, cfg: Config) -> sqlite3.Connection:
    from muster.connectors.mbox import MboxConnector

    store = BlobStore(cfg.blob_dir)
    for message in MboxConnector(FIXTURE).fetch():
        ingest_message(
            conn,
            store,
            source="file",
            source_uid=message.source_uid,
            raw=message.raw,
            own_addresses={"you@example.com"},
        )
    return conn


# --- loading --------------------------------------------------------------


def test_loads_the_seed_query_file() -> None:
    records = load_queries(SEED_QUERIES)
    assert len(records) == 5
    assert all(r.q and r.note for r in records)
    assert all(r.relevant_doc_ids for r in records)


def test_blank_lines_and_comments_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "q.jsonl"
    path.write_text('\n# a comment\n{"q": "x", "relevant_doc_ids": [1]}\n\n')
    assert len(load_queries(path)) == 1


def test_malformed_json_names_the_line(tmp_path: Path) -> None:
    path = tmp_path / "q.jsonl"
    path.write_text('{"q": "ok"}\nnot json at all\n')
    with pytest.raises(EvalError, match=r":2:"):
        load_queries(path)


def test_record_without_q_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "q.jsonl"
    path.write_text('{"relevant_doc_ids": [1]}\n')
    with pytest.raises(EvalError, match="needs a 'q' field"):
        load_queries(path)


def test_missing_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(EvalError, match="no query file"):
        load_queries(tmp_path / "nope.jsonl")


# --- id resolution --------------------------------------------------------


def test_source_uids_resolve_to_row_ids(indexed) -> None:
    resolved, unresolved = resolve_relevant(indexed, ["ordinary-1@example.com"])
    assert len(resolved) == 1 and not unresolved
    title = indexed.execute("SELECT title FROM docs WHERE id = ?", (resolved[0],)).fetchone()[0]
    assert title == "Re: Q2 warehouse lease renewal"


def test_integer_ids_resolve_too(indexed) -> None:
    doc_id = indexed.execute("SELECT id FROM docs LIMIT 1").fetchone()[0]
    resolved, unresolved = resolve_relevant(indexed, [doc_id])
    assert resolved == [doc_id] and not unresolved


def test_unknown_ids_are_reported_not_dropped(indexed) -> None:
    resolved, unresolved = resolve_relevant(indexed, ["nope@nowhere", 99999])
    assert resolved == []
    assert set(unresolved) == {"nope@nowhere", "99999"}


# --- scoring --------------------------------------------------------------


def test_perfect_retrieval_scores_one(indexed) -> None:
    record = QueryRecord(q="warehouse lease renewal", relevant_doc_ids=["ordinary-1@example.com"])
    run = evaluate(indexed, [record], k=10)
    assert run.recall_at_k == 1.0
    assert run.mrr == 1.0
    assert run.results[0].first_relevant_rank == 1


def test_a_miss_scores_zero(indexed) -> None:
    record = QueryRecord(q="zzzzunfindable", relevant_doc_ids=["ordinary-1@example.com"])
    run = evaluate(indexed, [record], k=10)
    assert run.recall_at_k == 0.0
    assert run.mrr == 0.0
    assert run.results[0].first_relevant_rank is None


def test_partial_recall_counts_both_documents(indexed) -> None:
    record = QueryRecord(
        q="Rechnung 2026-0042",
        relevant_doc_ids=["attachment-5@lieferant.de", "attachment-5@lieferant.de#att0"],
    )
    run = evaluate(indexed, [record], k=10)
    assert 0.0 < run.recall_at_k <= 1.0
    assert len(run.results[0].relevant) == 2


def test_unresolvable_records_are_skipped_not_scored_zero(indexed) -> None:
    good = QueryRecord(q="warehouse lease", relevant_doc_ids=["ordinary-1@example.com"])
    unfilled = QueryRecord(q="something I have not labelled yet", relevant_doc_ids=[])
    run = evaluate(indexed, [good, unfilled], k=10)

    assert run.queries_total == 2
    assert run.queries_scored == 1
    assert run.recall_at_k == 1.0, "an unfilled record must not drag the average down"


def test_k_bounds_the_retrieved_list(indexed) -> None:
    record = QueryRecord(q="the invoice plan lease", relevant_doc_ids=["ordinary-1@example.com"])
    run = evaluate(indexed, [record], k=2)
    assert len(run.results[0].retrieved) <= 2


def test_reciprocal_rank_reflects_position(indexed) -> None:
    record = QueryRecord(q="warehouse lease renewal", relevant_doc_ids=["ordinary-1@example.com"])
    result = evaluate(indexed, [record], k=10).results[0]
    assert result.reciprocal_rank == pytest.approx(1 / result.first_relevant_rank)


# --- output ---------------------------------------------------------------


def test_run_is_written_as_readable_json(indexed, tmp_path: Path) -> None:
    run = evaluate(
        indexed,
        [QueryRecord(q="warehouse lease", relevant_doc_ids=["ordinary-1@example.com"])],
        k=10,
    )
    path = write_run(run, tmp_path / "results")

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["mode"] == "keyword"
    assert payload["recall_at_k"] == 1.0
    assert payload["corpus"]["docs"] > 0
    # The retrieved list per query is what makes two runs diffable.
    assert payload["results"][0]["retrieved"]


def test_seed_queries_all_resolve_against_the_fixture(indexed) -> None:
    run = evaluate(indexed, load_queries(SEED_QUERIES), k=10)
    assert run.queries_scored == 5, [r.unresolved for r in run.results]
