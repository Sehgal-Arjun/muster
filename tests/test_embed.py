from __future__ import annotations

import hashlib
import math
import sqlite3

import pytest

from muster.embed import (
    BGE_QUERY_PREFIX,
    count_pending,
    embed_pending,
    encode_query,
    pack,
    pending_groups,
    resolve_device,
    unpack,
)
from muster.search import Hit, reciprocal_rank_fusion

DIM = 8


class FakeModel:
    """Deterministic unit vectors, so tests assert on behaviour not weights."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def encode(self, texts, **kwargs):
        single = isinstance(texts, str)
        batch = [texts] if single else list(texts)
        self.calls.append(batch)
        out = [self._vector(t) for t in batch]
        return out[0] if single else out

    @staticmethod
    def _vector(text: str) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        raw = [b / 255.0 for b in digest[:DIM]]
        norm = math.sqrt(sum(v * v for v in raw)) or 1.0
        return [v / norm for v in raw]


@pytest.fixture
def chunked(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.execute(
        "INSERT INTO docs (id, source, source_uid, content_hash, doc_date, date_source,"
        " ingested_at) VALUES (1, 'file', 'd1', 'h', '2026-01-01T00:00:00Z', 'header',"
        " '2026-01-01T00:00:00Z')"
    )
    texts = ["alpha text", "beta text", "alpha text", "gamma text"]
    for ord_, text in enumerate(texts):
        conn.execute(
            "INSERT INTO chunks (doc_id, ord, text, text_hash) VALUES (1, ?, ?, ?)",
            (ord_, text, hashlib.sha256(text.encode()).hexdigest()),
        )
    return conn


MODEL = "test-model-v1"


# --- packing --------------------------------------------------------------


def test_pack_round_trips_as_float32() -> None:
    vector = [0.5, -0.25, 1.0, 0.0]
    assert len(pack(vector)) == 4 * len(vector)
    assert unpack(pack(vector)) == pytest.approx(vector)


# --- work discovery -------------------------------------------------------


def test_identical_text_is_encoded_once(chunked) -> None:
    groups = list(pending_groups(chunked, MODEL))
    assert len(groups) == 3, "four chunks, three distinct texts"

    duplicate = next(g for g in groups if g.text == "alpha text")
    assert len(duplicate.chunk_ids) == 2


def test_count_pending_counts_chunks_not_groups(chunked) -> None:
    assert count_pending(chunked, MODEL) == 4


def test_embedding_writes_a_vector_for_every_chunk(chunked) -> None:
    model = FakeModel()
    report = embed_pending(chunked, model_id=MODEL, model=model, batch_size=2)

    assert report.groups == 3
    assert report.chunks == 4, "the duplicate chunk gets its own row"
    assert report.dimensions == DIM
    assert chunked.execute("SELECT count(*) FROM vectors").fetchone()[0] == 4
    assert sum(len(c) for c in model.calls) == 3, "the duplicate text is not re-encoded"


def test_duplicate_chunks_share_the_same_vector(chunked) -> None:
    embed_pending(chunked, model_id=MODEL, model=FakeModel())
    rows = chunked.execute(
        "SELECT v.embedding FROM vectors v JOIN chunks c ON c.id = v.chunk_id"
        " WHERE c.text = 'alpha text'"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0][0] == rows[1][0]


# --- resumability ---------------------------------------------------------


def test_second_run_is_a_no_op(chunked) -> None:
    embed_pending(chunked, model_id=MODEL, model=FakeModel())
    assert count_pending(chunked, MODEL) == 0

    model = FakeModel()
    report = embed_pending(chunked, model_id=MODEL, model=model)
    assert report.chunks == 0
    assert model.calls == []


def test_a_partial_run_resumes_where_it_stopped(chunked) -> None:
    first = embed_pending(chunked, model_id=MODEL, model=FakeModel(), limit=1)
    assert first.groups == 1
    assert count_pending(chunked, MODEL) < 4

    second = embed_pending(chunked, model_id=MODEL, model=FakeModel())
    assert count_pending(chunked, MODEL) == 0
    assert first.chunks + second.chunks == 4


def test_new_chunks_are_picked_up_without_touching_old_vectors(chunked) -> None:
    embed_pending(chunked, model_id=MODEL, model=FakeModel())
    before = chunked.execute(
        "SELECT chunk_id, embedding FROM vectors ORDER BY chunk_id"
    ).fetchall()

    chunked.execute(
        "INSERT INTO chunks (doc_id, ord, text, text_hash) VALUES (1, 99, 'delta', 'dh')"
    )
    embed_pending(chunked, model_id=MODEL, model=FakeModel())

    after = dict(chunked.execute("SELECT chunk_id, embedding FROM vectors").fetchall())
    assert all(after[chunk_id] == blob for chunk_id, blob in before)
    assert len(after) == 5


# --- the model-swap invariant --------------------------------------------


def test_a_second_model_inserts_alongside_the_first(chunked) -> None:
    embed_pending(chunked, model_id=MODEL, model=FakeModel())
    embed_pending(chunked, model_id="other-model-v2", model=FakeModel())

    counts = dict(
        chunked.execute("SELECT model_id, count(*) FROM vectors GROUP BY model_id").fetchall()
    )
    assert counts == {MODEL: 4, "other-model-v2": 4}, "swapping models must not wipe anything"


# --- query encoding -------------------------------------------------------


def test_bge_query_prefix_is_applied_for_bge_models() -> None:
    class Bgeish(FakeModel):
        def __init__(self):
            super().__init__()
            self._name = "BAAI/bge-small-en-v1.5"

        def __str__(self):
            return self._name

    model = Bgeish()
    model.model_card_data = type("MC", (), {"base_model": "BAAI/bge-small-en-v1.5"})()
    encode_query(model, "invoice")
    assert model.calls[0][0].startswith(BGE_QUERY_PREFIX)


def test_non_bge_model_gets_the_bare_query() -> None:
    model = FakeModel()
    encode_query(model, "invoice")
    assert model.calls[0][0] == "invoice"


def test_device_preference_is_honoured() -> None:
    assert resolve_device("cpu") == "cpu"
    assert resolve_device() in {"mps", "cuda", "cpu"}


# --- fusion ---------------------------------------------------------------


def _hit(doc_id: int, score: float = 1.0) -> Hit:
    return Hit(
        doc_id=doc_id,
        chunk_id=doc_id,
        title=f"doc {doc_id}",
        source="file",
        source_uid=str(doc_id),
        doc_date="2026-01-01T00:00:00Z",
        counterparty=None,
        score=score,
        snippet="",
    )


def test_rrf_promotes_documents_both_arms_agree_on() -> None:
    keyword = [_hit(1), _hit(2), _hit(3)]
    vector = [_hit(9), _hit(2), _hit(8)]

    fused = reciprocal_rank_fusion([keyword, vector], limit=10)
    # doc 2 is second in both lists; docs 1 and 9 top one list but are absent
    # from the other. Agreement across arms beats a single first place.
    assert fused[0].doc_id == 2
    assert fused[1].doc_id in {1, 9}


def test_rrf_is_convex_so_a_first_place_can_outweigh_two_seconds() -> None:
    # Worth pinning: 1/61 + 1/63 > 2/62, so ranking (1, 3) edges out (2, 2).
    # A future change to RRF_K alters this, and it should not do so silently.
    fused = reciprocal_rank_fusion(
        [[_hit(1), _hit(2), _hit(3)], [_hit(3), _hit(2), _hit(1)]], limit=3
    )
    assert fused[0].doc_id in {1, 3}
    assert fused[-1].doc_id == 2


def test_rrf_ignores_raw_score_magnitude() -> None:
    keyword = [_hit(1, score=9999.0), _hit(2, score=0.001)]
    vector = [_hit(2, score=0.5), _hit(1, score=0.4)]

    fused = reciprocal_rank_fusion([keyword, vector], limit=10)
    assert {h.doc_id for h in fused} == {1, 2}
    assert fused[0].score == pytest.approx(fused[1].score), "same positions, same fused score"


def test_rrf_uses_the_documented_constant() -> None:
    fused = reciprocal_rank_fusion([[_hit(1)]], limit=1)
    assert fused[0].score == pytest.approx(1 / 61)


def test_rrf_deduplicates_and_respects_limit() -> None:
    fused = reciprocal_rank_fusion([[_hit(i) for i in range(20)]] * 2, limit=5)
    assert len(fused) == 5
    assert len({h.doc_id for h in fused}) == 5
