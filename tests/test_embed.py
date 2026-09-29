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


def test_bge_query_prefix_is_applied_for_bge_v15() -> None:
    model = FakeModel()
    encode_query(model, "invoice", "BAAI/bge-small-en-v1.5")
    assert model.calls[0][0] == BGE_QUERY_PREFIX + "invoice"


def test_bge_m3_is_instruction_free() -> None:
    """m3 is not trained with the v1.5 prefix; applying it would hurt recall."""
    model = FakeModel()
    encode_query(model, "invoice", "BAAI/bge-m3")
    assert model.calls[0][0] == "invoice"


def test_e5_uses_its_own_asymmetric_prefixes() -> None:
    from muster.embed import encode_documents

    model = FakeModel()
    encode_query(model, "invoice", "intfloat/multilingual-e5-base")
    assert model.calls[0][0] == "query: invoice"

    encode_documents(model, ["a body"], model_id="intfloat/multilingual-e5-base")
    assert model.calls[1][0] == "passage: a body"


def test_unknown_model_gets_the_bare_query() -> None:
    model = FakeModel()
    encode_query(model, "invoice", "someone/unknown-model")
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


def test_rrf_weights_make_a_rank_one_tie_impossible() -> None:
    """The MXUFEM case: each arm returns a different document at rank 1.

    Unweighted, both score exactly 1/(k+1) and the winner is arbitrary — an
    exact BM25 match lost to a vector hit on a token its model had never seen.
    """
    keyword = [_hit(1)]
    vector = [_hit(2)]

    tied = reciprocal_rank_fusion([keyword, vector], limit=2)
    assert tied[0].score == tied[1].score, "unweighted, this is a coin flip"

    fused = reciprocal_rank_fusion([keyword, vector], limit=2, weights=[1.0, 0.9])
    assert fused[0].doc_id == 1
    assert fused[0].score > fused[1].score


def test_weighting_does_not_override_genuine_agreement() -> None:
    """A document both arms rank must still beat one only the keyword arm has."""
    keyword = [_hit(1), _hit(2)]
    vector = [_hit(2), _hit(9)]

    fused = reciprocal_rank_fusion([keyword, vector], limit=3, weights=[1.0, 0.9])
    assert fused[0].doc_id == 2


# --- pausing --------------------------------------------------------------


def test_embedding_stops_between_batches_when_asked(chunked) -> None:
    """Checked between batches, never inside one.

    A batch is the unit of work we are willing to lose; stopping mid-batch
    would throw away GPU time already spent for nothing.
    """
    seen = {"batches": 0}

    def stop_after_first() -> bool:
        seen["batches"] += 1
        return seen["batches"] > 1

    report = embed_pending(
        chunked, model_id=MODEL, model=FakeModel(), batch_size=1,
        should_stop=stop_after_first,
    )
    assert report.stopped is True
    assert report.batches == 1, "it kept going after being asked to stop"


def test_what_was_not_embedded_is_still_pending(chunked) -> None:
    """Stopping must leave work to resume, not silently mark it done."""
    before = count_pending(chunked, MODEL)

    embed_pending(
        chunked, model_id=MODEL, model=FakeModel(), batch_size=1,
        should_stop=lambda: True,
    )
    assert count_pending(chunked, MODEL) == before, "work vanished on pause"


def test_pause_and_resume_round_trip(tmp_path) -> None:
    from muster import pausing

    assert pausing.is_paused(tmp_path) is False
    pausing.pause(tmp_path)
    assert pausing.is_paused(tmp_path) is True
    assert pausing.paused_since(tmp_path)
    assert pausing.resume(tmp_path) is True
    assert pausing.is_paused(tmp_path) is False
    assert pausing.resume(tmp_path) is False, "resuming twice is not an error"


def test_the_hand_started_embed_command_honours_the_pause_switch(
    chunked, cfg, monkeypatch, capsys
) -> None:
    """The run most likely to be in the way is the one started by hand.

    Wiring the switch into only the scheduled path left `muster embed` — the
    one actually hogging the GPU — unstoppable.
    """
    from muster import pausing
    from muster.cli import embed as embed_command

    monkeypatch.setattr("muster.cli.load_config", lambda *a, **k: cfg)
    monkeypatch.setattr("muster.db.connect", lambda *a, **k: chunked)
    monkeypatch.setattr("muster.embed.load_model", lambda *a, **k: FakeModel())
    pending_before = count_pending(chunked, MODEL)
    assert pending_before > 0, "fixture has nothing to embed"

    pausing.pause(cfg.home)
    try:
        embed_command(model=None, batch_size=1, device=None, limit=None)
    finally:
        pausing.resume(cfg.home)

    # The command closes its own connection, so the evidence is what it said.
    assert "paused before finishing" in capsys.readouterr().out


def test_the_model_loads_once_even_from_many_threads(monkeypatch) -> None:
    """Three threads once loaded three copies of a 2GB model onto one GPU. The
    machine spent minutes at 350% CPU and the panel looked hung."""
    import sys
    import threading
    import types

    from muster import embed

    built: list[str] = []
    ready = threading.Barrier(4)

    class Slow:
        def __init__(self, model_id, device=None):
            built.append(model_id)
            # Long enough that an unlocked check-then-set would certainly race.
            threading.Event().wait(0.2)

    fake = types.ModuleType("sentence_transformers")
    fake.SentenceTransformer = Slow
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)
    monkeypatch.setattr(embed, "_model_cache", {})
    monkeypatch.setattr(embed, "resolve_device", lambda _p=None: "cpu")

    def load() -> None:
        ready.wait()
        embed.load_model("some/model")

    threads = [threading.Thread(target=load) for _ in range(3)]
    for thread in threads:
        thread.start()
    ready.wait()
    for thread in threads:
        thread.join()

    assert built == ["some/model"]


def test_encoding_never_overlaps_itself(monkeypatch) -> None:
    """Two threads in the MPS backend at once crashed the process inside
    mps_copy_. There is one GPU; overlapping was never faster, only unsafe."""
    import threading

    from muster import embed

    inside = 0
    overlapped = False
    guard = threading.Lock()
    ready = threading.Barrier(4)

    class Model:
        def encode(self, _texts, **_kwargs):
            nonlocal inside, overlapped
            with guard:
                inside += 1
                overlapped = overlapped or inside > 1
            threading.Event().wait(0.05)
            with guard:
                inside -= 1
            return [[0.0]]

    model = Model()

    def work(index: int) -> None:
        ready.wait()
        if index % 2:
            embed.encode_query(model, "a query", "test/model")
        else:
            embed.encode_documents(model, ["a document"], model_id="test/model")

    threads = [threading.Thread(target=work, args=(i,)) for i in range(3)]
    for thread in threads:
        thread.start()
    ready.wait()
    for thread in threads:
        thread.join()

    assert not overlapped
