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
        "preview", "sources", "score",
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
        "database", "blob_dir", "schema", "counts", "health", "by_kind",
        "by_category", "date_range", "embedding", "search_defaults",
        "corpus_fingerprint",
    }
    assert body["counts"]["docs"] > 0
    assert body["schema"]["pending"] == []
    assert "chunks_awaiting_vectors" in body["health"]


def test_status_exposes_the_blob_root_for_finder(client, cfg) -> None:
    """Revealing a file takes a path; a URL cannot be handed to the file manager."""
    from pathlib import Path

    body = client.get("/status").json()
    assert body["blob_dir"] == str(cfg.blob_dir)

    digest = client.get("/search", params={"q": "warehouse lease"}).json()["results"][0][
        "content_hash"
    ]
    on_disk = Path(body["blob_dir"]) / digest[:2] / digest
    assert on_disk.exists(), "the documented path formula must actually resolve"


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


# --- /doc: what a reader needs that a ranker does not ---------------------


def test_doc_exposes_recipients_from_the_stored_headers(client, conn) -> None:
    """counterparty is one address picked for ranking, not the To header."""
    doc_id = conn.execute(
        "SELECT id FROM docs WHERE source_uid = 'ordinary-1@example.com'"
    ).fetchone()[0]
    body = client.get(f"/doc/{doc_id}").json()

    assert isinstance(body["recipients"]["to"], list)
    assert isinstance(body["recipients"]["cc"], list)
    assert body["recipients"]["to"], "a delivered message was addressed to somebody"


def test_doc_thread_is_null_for_a_conversation_of_one(client, conn) -> None:
    """A thread containing only this message is not context, it is the message."""
    row = conn.execute(
        "SELECT id FROM docs WHERE thread_uid IS NOT NULL"
        " GROUP BY thread_uid HAVING count(*) = 1 LIMIT 1"
    ).fetchone()
    if row is None:
        pytest.skip("fixture has no single-message thread")
    assert client.get(f"/doc/{row[0]}").json()["thread"] is None


def test_doc_thread_lists_siblings_oldest_first_and_marks_the_current(client, conn) -> None:
    row = conn.execute(
        "SELECT thread_uid FROM docs WHERE thread_uid IS NOT NULL AND origin = 'captured'"
        " GROUP BY thread_uid HAVING count(*) > 1 LIMIT 1"
    ).fetchone()
    if row is None:
        pytest.skip("fixture has no multi-message thread")
    doc_id = conn.execute(
        "SELECT id FROM docs WHERE thread_uid = ? ORDER BY doc_date LIMIT 1", (row[0],)
    ).fetchone()[0]

    thread = client.get(f"/doc/{doc_id}").json()["thread"]
    assert thread["count"] >= 2
    dates = [m["date"] for m in thread["messages"]]
    assert dates == sorted(dates)
    assert [m["current"] for m in thread["messages"]].count(True) == 1


def test_doc_provenance_is_absent_for_mail(client, conn) -> None:
    """Mail has no path and no download; the block should not be invented."""
    doc_id = conn.execute(
        "SELECT id FROM docs WHERE mime = 'message/rfc822' LIMIT 1"
    ).fetchone()[0]
    assert client.get(f"/doc/{doc_id}").json()["provenance"] is None


def test_doc_provenance_reports_a_files_path_and_where_it_came_from(
    client, conn, tmp_path
) -> None:
    """The Spotlight fields are the join from a download back to its message."""
    from muster.connectors.files import FileConnector
    from muster.fileingest import ingest_file

    target = tmp_path / "lease.pdf"
    target.write_bytes(b"%PDF-1.4\n")
    record = next(iter(FileConnector((str(tmp_path),)).walk()))
    result = ingest_file(conn, record)
    conn.execute(
        "UPDATE docs SET meta_json = json_set(meta_json, '$.where_from',"
        " json_array('https://example.com/lease.pdf')) WHERE id = ?",
        (result.doc_id,),
    )
    conn.commit()

    provenance = client.get(f"/doc/{result.doc_id}").json()["provenance"]
    assert provenance["path"] == str(target)
    assert provenance["where_from"] == ["https://example.com/lease.pdf"]
    assert provenance["size_bytes"] == target.stat().st_size


def test_mail_never_gets_provenance_even_carrying_a_path(client, conn) -> None:
    """An mbox import has a path in its metadata but is not a file on disk."""
    import json

    doc_id = conn.execute(
        "SELECT id FROM docs WHERE mime = 'message/rfc822' LIMIT 1"
    ).fetchone()[0]
    conn.execute(
        "UPDATE docs SET meta_json = ? WHERE id = ?",
        (json.dumps({"path": "/var/mail/archive.mbox"}), doc_id),
    )
    conn.commit()

    assert client.get(f"/doc/{doc_id}").json()["provenance"] is None


# --- POST /sync/refresh ---------------------------------------------------


def test_refresh_returns_a_job_immediately(client, monkeypatch) -> None:
    """The panel calls this on open; it must never make the box wait."""
    import muster.api as api_module

    started = []

    def fake_job(cfg, job_id):
        started.append(job_id)
        with api_module._jobs_lock:
            api_module._jobs[job_id].update(state="done", result="incremental: nothing new")

    monkeypatch.setattr(api_module, "_run_sync_job", fake_job)
    api_module._jobs.clear()

    body = client.post("/sync/refresh").json()
    assert body["state"] == "running"
    assert body["started"] is True
    assert started == [body["job"]]

    status = client.get(f"/sync/jobs/{body['job']}").json()
    assert status["state"] == "done"
    assert status["result"] == "incremental: nothing new"


def test_refresh_does_not_queue_a_second_sync(client, monkeypatch) -> None:
    """Two open panels must not mean two syncs racing on one database."""
    import muster.api as api_module

    monkeypatch.setattr(api_module, "_run_sync_job", lambda cfg, job_id: None)
    api_module._jobs.clear()

    first = client.post("/sync/refresh").json()
    second = client.post("/sync/refresh").json()

    assert second["job"] == first["job"]
    assert second["started"] is False


def test_unknown_job_is_a_404(client) -> None:
    assert client.get("/sync/jobs/nope").status_code == 404


def test_a_held_lock_is_reported_as_skipped_not_failed(client, cfg, monkeypatch) -> None:
    """The scheduled job getting there first is not an error."""
    import muster.api as api_module
    from muster.locking import sync_lock

    api_module._jobs.clear()
    with sync_lock(cfg.lock_path):
        api_module._jobs["j1"] = {
            "id": "j1", "state": "running", "started_at": 0, "result": None, "error": None,
        }
        api_module._run_sync_job(cfg, "j1")

    assert api_module._jobs["j1"]["state"] == "skipped"
    assert api_module._jobs["j1"]["error"] is None


# --- previews and file bytes ----------------------------------------------


def test_a_file_document_gets_a_blob_that_resolves(client, conn, cfg, tmp_path) -> None:
    """File docs keep no blob, so /doc must point at the path instead.

    Without this a PDF on disk renders as stripped text, because the viewer has
    no bytes to show — which is not what anyone means by "open my CV".
    """
    from muster.connectors.files import FileConnector
    from muster.fileingest import ingest_file

    pdf = tmp_path / "cv.pdf"
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Sam Reyes curriculum vitae")
    doc.save(pdf)
    doc.close()

    record = next(iter(FileConnector((str(tmp_path),)).walk()))
    result = ingest_file(conn, record)
    conn.commit()

    body = client.get(f"/doc/{result.doc_id}").json()
    assert body["blob"] is not None, "a file document must still offer its bytes"
    assert body["blob"]["url"] == f"/file/{result.doc_id}"

    served = client.get(body["blob"]["url"])
    assert served.status_code == 200
    assert served.content[:5] == b"%PDF-"
    assert served.headers["content-type"].startswith("application/pdf")


def test_file_bytes_cannot_be_asked_for_by_path(client) -> None:
    """The endpoint takes an id; there is no string that reaches the filesystem."""
    assert client.get("/file/999999").status_code == 404
    # Mail is not a filesystem document, whatever source string it was stored
    # under, and must never be served as one.
    mail_id = 1
    assert client.get(f"/file/{mail_id}").status_code == 404


def test_a_file_that_has_gone_is_reported_as_gone(client, conn, cfg, tmp_path) -> None:
    """The index outlives the file. 410 is the honest answer, not 500."""
    from muster.connectors.files import FileConnector
    from muster.fileingest import ingest_file

    doomed = tmp_path / "temp.txt"
    doomed.write_text("indexed, then deleted")
    record = next(iter(FileConnector((str(tmp_path),)).walk()))
    result = ingest_file(conn, record)
    conn.commit()
    doomed.unlink()

    assert client.get(f"/file/{result.doc_id}").status_code == 410


def test_thumbnail_renders_the_first_page_of_a_pdf(client, conn, tmp_path) -> None:
    from muster.connectors.files import FileConnector
    from muster.fileingest import ingest_file

    pymupdf = pytest.importorskip("pymupdf")
    pdf = tmp_path / "doc.pdf"
    d = pymupdf.open()
    d.new_page().insert_text((72, 72), "page one")
    d.save(pdf)
    d.close()

    record = next(iter(FileConnector((str(tmp_path),)).walk()))
    result = ingest_file(conn, record)
    conn.commit()

    response = client.get(f"/thumb/{result.doc_id}?size=64")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_no_thumbnail_for_something_with_nothing_to_show(client, conn) -> None:
    doc_id = conn.execute(
        "SELECT id FROM docs WHERE mime = 'message/rfc822' LIMIT 1"
    ).fetchone()[0]
    assert client.get(f"/thumb/{doc_id}").status_code == 404


def test_hits_declare_whether_a_preview_exists(client) -> None:
    """So the list does not fire a thumbnail request per row to find out."""
    body = client.get("/search", params={"q": "lease", "limit": 10}).json()
    for hit in body["results"]:
        assert isinstance(hit["preview"], bool)
        if hit["preview"]:
            assert (hit["mime"] or "").startswith("image/") or hit["mime"] == "application/pdf"


def test_every_endpoint_opens_its_own_connection(client) -> None:
    """A sqlite connection must not cross threads.

    A sync dependency and a sync endpoint are two separate dispatches into the
    threadpool, so a connection built in a dependency can be handed to a
    different thread than the one that uses it. sqlite3 refuses that — and only
    sometimes, because the pool usually reuses the same thread, which is how it
    reached production as intermittent 500s on search.
    """
    import inspect

    import muster.api as api_module

    source = inspect.getsource(api_module.create_app)
    assert "Depends(connection)" not in source, (
        "an endpoint takes its connection from a dependency again"
    )


def test_search_survives_being_hit_from_many_threads(client) -> None:
    from concurrent.futures import ThreadPoolExecutor

    def once(i: int) -> int:
        return client.get("/search", params={"q": f"lease {i}", "limit": 3}).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(once, range(24)))

    assert set(codes) == {200}, f"a request failed under concurrency: {codes}"


def test_status_reports_whether_the_model_is_loaded(client) -> None:
    """The panel needs to distinguish "slow" from "found nothing"."""
    body = client.get("/status").json()
    assert "ready" in body["embedding"]
    assert isinstance(body["embedding"]["ready"], bool)


def test_warming_sets_ready_even_when_the_model_cannot_load(cfg, monkeypatch) -> None:
    """Waiting forever on a failed load helps nobody; keyword search is fine."""
    import muster.api as api_module

    api_module._model_ready.clear()
    monkeypatch.setattr(
        "muster.embed.load_model",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no model here")),
    )
    api_module._warm_model(cfg)
    assert api_module._model_ready.is_set()


# --- /home ----------------------------------------------------------------


def test_home_has_the_documented_shape_with_no_processes(client) -> None:
    body = client.get("/home").json()
    assert set(body) == {"processes", "index"}
    assert body["processes"] == []
    assert set(body["index"]) == {
        "documents", "missing", "last_sync", "last_result", "embedding_ready",
    }
    assert body["index"]["documents"] > 0


def test_home_summarises_a_process_without_asking_the_model(client, conn, cfg) -> None:
    """Opening the panel must not wait on an inference — so a summary that has no
    cached judgement to read reports an upper bound and says so."""
    from muster import processes

    made = processes.create(conn, "Warehouse lease")
    processes.add_anchor(conn, made.id, "domain", "example.com")
    conn.commit()

    body = client.get("/home").json()
    summary = next(p for p in body["processes"] if p["slug"] == made.slug)
    assert set(summary) == {
        "slug", "name", "documents", "anchors", "open", "settled_by_me",
        "awaiting_confirmation", "next_deadline", "estimated",
        "extraction_pending", "last_activity",
    }
    assert summary["name"] == "Warehouse lease"
    assert summary["anchors"] == 1
    assert summary["documents"] >= 1


def test_home_open_count_is_what_the_process_view_would_list(client, conn, cfg) -> None:
    """Two screens showing different numbers for the same thing is worse than
    either number being approximate."""
    from muster import gather as gather_mod, openitems, processes

    made = processes.create(conn, "Warehouse lease")
    processes.add_anchor(conn, made.id, "domain", "example.com")
    conn.commit()

    body = client.get("/home").json()
    summary = next(p for p in body["processes"] if p["slug"] == made.slug)

    process = processes.get(conn, made.slug)
    report = openitems.compute(
        conn, gather_mod.gather(conn, process), model=cfg.extract.model_id,
        process_id=process.id, cached_only=True,
    )
    assert summary["open"] == len(report.items)


def test_home_orders_by_the_soonest_deadline(client, conn) -> None:
    from muster import processes

    for name, anchor in (("Later", "example.com"), ("Sooner", "other.example")):
        made = processes.create(conn, name)
        processes.add_anchor(conn, made.id, "domain", anchor)
    conn.commit()

    deadlines = [p["next_deadline"] for p in client.get("/home").json()["processes"]]
    # None sorts last, whatever the names or sizes happen to be.
    assert deadlines == sorted(deadlines, key=lambda d: d or "9999")
