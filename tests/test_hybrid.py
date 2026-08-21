"""Vector and hybrid retrieval, end to end against a real embedding model.

Skipped rather than failed when the model is unavailable — a laptop with no
network should still be able to run the rest of the suite.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from muster.blobs import BlobStore
from muster.config import Config
from muster.connectors.mbox import MboxConnector
from muster.embed import embed_pending
from muster.ingest import ingest_message
from muster.search import hybrid_search, keyword_search, search, vector_search

FIXTURE = Path(__file__).parent / "fixtures" / "sample.mbox"
MODEL = "BAAI/bge-small-en-v1.5"


@pytest.fixture(scope="module")
def model():
    st = pytest.importorskip("sentence_transformers")
    try:
        from muster.embed import load_model

        return load_model(MODEL)
    except Exception as exc:  # offline, or no cached weights
        pytest.skip(f"{MODEL} unavailable: {exc}")


@pytest.fixture
def embedded(conn: sqlite3.Connection, cfg: Config, model) -> sqlite3.Connection:
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
    embed_pending(conn, model_id=MODEL, model=model, batch_size=32)
    return conn


def titles(hits) -> list[str]:
    return [h.title for h in hits]


# --- vector ---------------------------------------------------------------


def test_vector_search_returns_ranked_hits(embedded) -> None:
    hits = vector_search(embedded, "warehouse lease renewal", model_id=MODEL, limit=5)
    assert hits
    assert all(hits[i].score >= hits[i + 1].score for i in range(len(hits) - 1))
    assert all(-1.0 <= h.score <= 1.0 for h in hits)


def test_vector_search_beats_bm25_on_a_paraphrase(embedded) -> None:
    """The reason vectors are here at all.

    The query shares no content word with the message, which says "warehouse
    lease renewal at 4200 EUR per month". Asserting the rank gap rather than a
    bare hit keeps the test honest: if a future change lets BM25 answer this
    just as well, the comparison fails and says so.
    """
    query = "when must I tell the property owner if we are staying in the depot"
    target = "Re: Q2 warehouse lease renewal"

    vector = titles(vector_search(embedded, query, model_id=MODEL, limit=10))
    keyword = titles(keyword_search(embedded, query, limit=10))

    assert target in vector
    vector_rank = vector.index(target)
    keyword_rank = keyword.index(target) if target in keyword else len(keyword) + 1
    assert vector_rank < keyword_rank


def test_vector_search_respects_origin_and_filters(embedded) -> None:
    assert vector_search(embedded, "lease", model_id=MODEL, since="2026-01-01")
    assert not vector_search(embedded, "lease", model_id=MODEL, since="2026-12-01")
    assert not vector_search(embedded, "lease", model_id=MODEL, source="gmail")


def test_vector_search_is_one_hit_per_doc(embedded) -> None:
    hits = vector_search(embedded, "transition plan milestones", model_id=MODEL, limit=10)
    assert len({h.doc_id for h in hits}) == len(hits)


def test_vector_search_without_vectors_is_an_actionable_error(conn, cfg, model) -> None:
    with pytest.raises(ValueError, match="muster embed"):
        vector_search(conn, "anything", model_id="never-embedded-model")


def test_empty_query_returns_nothing(embedded) -> None:
    assert vector_search(embedded, "  ", model_id=MODEL) == []


# --- hybrid ---------------------------------------------------------------


def test_hybrid_returns_results_from_both_arms(embedded) -> None:
    query = "warehouse lease renewal"
    fused = {h.doc_id for h in hybrid_search(embedded, query, limit=10)}
    keyword = {h.doc_id for h in keyword_search(embedded, query, limit=10)}
    vector = {h.doc_id for h in vector_search(embedded, query, model_id=MODEL, limit=10)}

    assert fused & keyword
    assert fused & vector
    assert fused <= (keyword | vector), "fusion must not invent documents"


def test_hybrid_keeps_an_exact_keyword_match_on_top(embedded) -> None:
    hits = hybrid_search(embedded, "warehouse lease renewal 4200 EUR", limit=5)
    assert hits[0].title == "Re: Q2 warehouse lease renewal"


def test_hybrid_degrades_to_keyword_when_nothing_is_embedded(
    conn: sqlite3.Connection, cfg: Config
) -> None:
    store = BlobStore(cfg.blob_dir)
    for message in MboxConnector(FIXTURE).fetch(limit=3):
        ingest_message(
            conn, store, source="file", source_uid=message.source_uid, raw=message.raw
        )

    hits = hybrid_search(conn, "warehouse lease", limit=5)
    assert titles(hits) == titles(keyword_search(conn, "warehouse lease", limit=5))


def test_hybrid_respects_the_captured_filter(embedded) -> None:
    embedded.execute(
        "INSERT INTO docs (source, source_uid, origin, content_hash, doc_date,"
        " date_source, title, ingested_at) VALUES ('muster', 'derived-x', 'derived', 'h',"
        " '2026-04-01T00:00:00Z', 'header', 'Derived note', '2026-04-01T00:00:00Z')"
    )
    doc_id = embedded.execute("SELECT id FROM docs WHERE source_uid='derived-x'").fetchone()[0]
    embedded.execute(
        "INSERT INTO chunks (doc_id, ord, text, text_hash) VALUES (?, 0, ?, 'th-x')",
        (doc_id, "An inferred conclusion about the warehouse lease renewal."),
    )
    # Deliberately left unembedded: the keyword arm alone must not leak it.
    assert doc_id not in {h.doc_id for h in hybrid_search(embedded, "warehouse lease")}


# --- all three modes stay comparable --------------------------------------


@pytest.mark.parametrize("mode", ["keyword", "vector", "hybrid"])
def test_every_mode_answers_the_same_query_shape(embedded, mode: str) -> None:
    hits = search(embedded, "warehouse lease renewal", mode=mode, limit=5)
    assert hits
    assert len({h.doc_id for h in hits}) == len(hits)
    assert all(h.doc_date and h.source for h in hits)
