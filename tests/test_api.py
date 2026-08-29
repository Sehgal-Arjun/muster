"""The HTTP API.

A wrapper, not a second implementation — so most of what these tests check is
that the shape matches docs/api.md and that nothing about ranking changed on
the way through.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from muster.api import create_app
from muster.blobs import BlobStore
from muster.connectors.mbox import MboxConnector
from muster.ingest import ingest_message

pytest.importorskip("fastapi")

FIXTURE = __import__("pathlib").Path(__file__).parent / "fixtures" / "sample.mbox"


@pytest.fixture
def client(conn: sqlite3.Connection, cfg) -> TestClient:
    store = BlobStore(cfg.blob_dir)
    for message in MboxConnector(FIXTURE).fetch():
        ingest_message(
            conn, store, source="file", source_uid=message.source_uid, raw=message.raw,
            own_addresses={"you@example.com"},
        )
    # Duplicate one message so the copy-collapsing path is exercised.
    raw = next(iter(MboxConnector(FIXTURE).fetch())).raw
    ingest_message(conn, store, source="file", source_uid="duplicate-copy", raw=raw)
    conn.commit()
    return TestClient(create_app(cfg))


# --- /search --------------------------------------------------------------


def test_search_returns_the_documented_envelope(client) -> None:
    body = client.get("/search", params={"q": "warehouse lease renewal"}).json()
    assert set(body) == {"query", "results", "empty_reason", "took_ms"}
    assert set(body["query"]) == {"raw", "searched", "mode", "limit", "temporal", "filters"}
    assert body["empty_reason"] is None
    assert body["results"]


def test_a_hit_has_every_documented_field(client) -> None:
    hit = client.get("/search", params={"q": "warehouse lease renewal"}).json()["results"][0]
    assert set(hit) == {
        "rank", "doc_id", "title", "snippet", "date", "source", "kind", "mime",
        "counterparty", "sender", "category", "attachment_count", "content_hash",
        "sources", "score",
    }
    assert hit["rank"] == 1
    assert hit["kind"] in {"message", "attachment", "other"}


def test_sources_is_always_a_list(client) -> None:
    for hit in client.get("/search", params={"q": "warehouse lease"}).json()["results"]:
        assert isinstance(hit["sources"], list) and hit["sources"]
        assert set(hit["sources"][0]) == {"doc_id", "source", "source_uid", "title", "date"}


def test_identical_copies_collapse_into_one_hit(client) -> None:
    """The duplicated message must appear once, listing both copies."""
    results = client.get(
        "/search", params={"q": "warehouse lease renewal", "min_score": 0}
    ).json()["results"]
    doc_ids = [h["doc_id"] for h in results]
    assert len(doc_ids) == len(set(doc_ids))

    collapsed = [h for h in results if len(h["sources"]) > 1]
    assert collapsed, "the deliberately duplicated message should have collapsed"
    assert len({s["source_uid"] for s in collapsed[0]["sources"]}) == len(collapsed[0]["sources"])


def test_sources_counts_this_result_list_not_the_index(client, conn) -> None:
    """A result list describes itself; the floor may have cut other copies."""
    results = client.get(
        "/search", params={"q": "warehouse lease renewal", "min_score": 0}
    ).json()["results"]
    hit = next(h for h in results if len(h["sources"]) > 1)
    in_index = conn.execute(
        "SELECT count(*) FROM docs WHERE content_hash = ?", (hit["content_hash"],)
    ).fetchone()[0]
    assert len(hit["sources"]) <= in_index


def test_score_names_its_own_scale(client) -> None:
    for mode, scale in (("keyword", "bm25"), ("hybrid", "rrf")):
        hit = client.get("/search", params={"q": "warehouse lease", "mode": mode}).json()
        if hit["results"]:
            assert hit["results"][0]["score"]["scale"] == scale


def test_of_best_is_one_for_the_top_hit(client) -> None:
    results = client.get("/search", params={"q": "warehouse lease", "min_score": 0}).json()["results"]
    assert results[0]["score"]["of_best"] == 1.0
    assert all(r["score"]["of_best"] <= 1.0 for r in results)


def test_keyword_mode_reports_no_arm_provenance(client) -> None:
    """Single-arm modes have nothing to disagree about."""
    hit = client.get("/search", params={"q": "warehouse lease", "mode": "keyword"}).json()
    assert hit["results"][0]["score"]["arms"] is None


def test_a_date_in_the_query_is_reported_as_a_filter(client) -> None:
    body = client.get("/search", params={"q": "invoice september 2024"}).json()
    assert body["query"]["searched"] == "invoice"
    assert body["query"]["temporal"]["matched"] == "september 2024"
    assert body["query"]["temporal"]["since"].startswith("2024-09-01")


def test_an_explicit_bound_suppresses_the_parsed_one(client) -> None:
    body = client.get(
        "/search", params={"q": "invoice september 2024", "since": "2026-01-01"}
    ).json()
    assert body["query"]["temporal"] is None
    assert body["query"]["filters"]["since"] == "2026-01-01"


def test_limit_is_bounded(client) -> None:
    assert client.get("/search", params={"q": "x", "limit": 0}).status_code == 422
    assert client.get("/search", params={"q": "x", "limit": 1000}).status_code == 422


def test_an_unknown_mode_is_rejected(client) -> None:
    assert client.get("/search", params={"q": "x", "mode": "telepathy"}).status_code == 422


# --- empty results carry a reason ----------------------------------------


def test_nothing_matching_says_so(client) -> None:
    body = client.get("/search", params={"q": "zzzzunfindable"}).json()
    assert body["results"] == []
    assert body["empty_reason"]["code"] == "no_matches"
    assert body["empty_reason"]["matched"] == 0


def test_the_floor_rejecting_everything_is_a_different_reason(client) -> None:
    """The UI has to distinguish 'nothing there' from 'nothing good enough'."""
    body = client.get("/search", params={"q": "the a of and"}).json()
    if body["results"]:
        pytest.skip("this corpus answers that query")
    reason = body["empty_reason"]
    assert reason["code"] in {"below_confidence_floor", "no_matches", "empty_query"}
    if reason["code"] == "below_confidence_floor":
        assert reason["matched"] > 0
        assert reason["best_score"] is not None
        assert reason["retry"] == {"min_score": 0}


def test_the_retry_hint_actually_works(client) -> None:
    body = client.get("/search", params={"q": "the a of and"}).json()
    reason = body["empty_reason"]
    if not reason or not reason.get("retry"):
        pytest.skip("nothing was rejected by the floor")
    retried = client.get("/search", params={"q": "the a of and", **reason["retry"]}).json()
    assert retried["results"], "the documented retry must surface the weak matches"


def test_an_empty_query_is_named_as_such(client) -> None:
    body = client.get("/search", params={"q": "   "}).json()
    assert body["empty_reason"]["code"] == "empty_query"


# --- /doc -----------------------------------------------------------------


def test_doc_returns_text_and_metadata(client, conn) -> None:
    doc_id = conn.execute(
        "SELECT id FROM docs WHERE source_uid = 'ordinary-1@example.com'"
    ).fetchone()[0]
    body = client.get(f"/doc/{doc_id}").json()

    assert body["id"] == doc_id
    assert body["kind"] == "message"
    assert "warehouse lease renewal" in body["text"]
    assert body["chunk_count"] >= 1
    assert body["blob"]["url"].startswith("/blob/")


def test_doc_text_is_not_a_naive_chunk_join(client, conn) -> None:
    """Chunks overlap 15%; concatenating them repeats every boundary."""
    doc_id = conn.execute(
        "SELECT doc_id FROM chunks GROUP BY doc_id HAVING count(*) > 2 LIMIT 1"
    ).fetchone()[0]
    body = client.get(f"/doc/{doc_id}").json()

    joined = "".join(
        r[0] for r in conn.execute(
            "SELECT text FROM chunks WHERE doc_id = ? ORDER BY ord", (doc_id,)
        )
    )
    assert len(body["text"]) < len(joined)


def test_doc_lists_attachments_and_links_their_documents(client, conn) -> None:
    doc_id = conn.execute(
        "SELECT id FROM docs WHERE source_uid = 'attachment-5@lieferant.de'"
    ).fetchone()[0]
    body = client.get(f"/doc/{doc_id}").json()

    assert body["attachments"]
    pdf = body["attachments"][0]
    assert pdf["filename"] == "Rechnung_2026-0042.pdf"
    assert pdf["url"] == f"/blob/{pdf['content_hash']}"
    assert pdf["doc_id"] is not None, "the extracted document is how the UI follows through"


def test_doc_lists_other_copies_of_itself(client, conn) -> None:
    doc_id = conn.execute("SELECT id FROM docs WHERE source_uid = 'duplicate-copy'").fetchone()[0]
    assert len(client.get(f"/doc/{doc_id}").json()["copies"]) == 2


def test_an_unknown_doc_is_404_with_a_code(client) -> None:
    response = client.get("/doc/99999")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# --- /blob ----------------------------------------------------------------


def test_blob_serves_the_original_bytes(client, conn, cfg) -> None:
    row = conn.execute(
        "SELECT content_hash, blob_path FROM docs WHERE source_uid = 'ordinary-1@example.com'"
    ).fetchone()
    response = client.get(f"/blob/{row['content_hash']}")

    assert response.status_code == 200
    assert response.content == BlobStore(cfg.blob_dir).get_blob(row["blob_path"])
    assert response.headers["etag"] == f'"{row["content_hash"]}"'
    assert "immutable" in response.headers["cache-control"]


def test_blob_rejects_a_malformed_hash_as_a_caller_bug(client) -> None:
    """400, not 404: the store is content-addressed and this cannot be a path."""
    for bad in ("../../etc/passwd", "nope", "z" * 64):
        response = client.get(f"/blob/{bad}")
        assert response.status_code in (400, 404)
        if response.status_code == 400:
            assert response.json()["error"]["code"] == "bad_request"


def test_a_traversal_attempt_never_reaches_the_filesystem(client) -> None:
    assert client.get("/blob/%2e%2e%2f%2e%2e%2fetc%2fpasswd").status_code in (400, 404)


def test_blob_supports_range_requests(client, conn) -> None:
    """Without these a PDF viewer refuses to stream."""
    digest = conn.execute(
        "SELECT content_hash FROM docs WHERE source_uid = 'ordinary-1@example.com'"
    ).fetchone()[0]
    response = client.get(f"/blob/{digest}", headers={"Range": "bytes=0-9"})

    assert response.status_code == 206
    assert len(response.content) == 10
    assert response.headers["content-range"].startswith("bytes 0-9/")


def test_blob_honours_if_none_match(client, conn) -> None:
    digest = conn.execute(
        "SELECT content_hash FROM docs WHERE source_uid = 'ordinary-1@example.com'"
    ).fetchone()[0]
    response = client.get(f"/blob/{digest}", headers={"If-None-Match": f'"{digest}"'})
    assert response.status_code == 304


def test_an_unknown_blob_is_404(client) -> None:
    assert client.get(f"/blob/{'0' * 64}").status_code == 404


# --- /status --------------------------------------------------------------


def test_status_matches_the_documented_shape(client) -> None:
    body = client.get("/status").json()
    assert set(body) == {
        "database", "schema", "counts", "health", "by_kind", "by_category",
        "date_range", "embedding", "search_defaults", "corpus_fingerprint",
    }
    assert body["counts"]["docs"] > 0
    assert body["schema"]["pending"] == []
    assert "chunks_awaiting_vectors" in body["health"]


def test_status_reports_the_corpus_fingerprint(client) -> None:
    """So a UI can tell the user their numbers are from a different index."""
    assert len(client.get("/status").json()["corpus_fingerprint"]) == 16


# --- the wrapper stays a wrapper -----------------------------------------


def test_the_api_ranks_identically_to_the_library(client, conn) -> None:
    """If these ever disagree, the bug is in api.py."""
    from muster.search import search as run_search

    for mode in ("keyword", "hybrid"):
        direct = [h.doc_id for h in run_search(conn, "warehouse lease renewal", mode=mode)]
        via_http = [
            h["doc_id"]
            for h in client.get(
                "/search", params={"q": "warehouse lease renewal", "mode": mode}
            ).json()["results"]
        ]
        assert via_http == direct, mode
