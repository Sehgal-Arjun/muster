"""HTTP API over the index.

A wrapper, not a second implementation. ``/search`` calls
:func:`muster.search.search` and reshapes what comes back; anything that would
change a ranking belongs in the library, behind the eval harness, where it can
be measured. If a query behaves differently here than through the CLI, that is
a bug in this file.

Read-only but for one endpoint. ``POST /sync/refresh`` pulls new mail and
nothing else; embed, reparse and purge stay on the command line, because they
run for hours and destroy work, and a button that silently starts a two-hour
backfill is a mistake waiting to happen. Pulling new mail is neither: it is
bounded by what has arrived, and the panel being able to ask for it is the
difference between an index that is current and one the user has to remember to
update.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from muster import db, migrate
from muster.blobs import BlobNotFound, BlobStore, relpath_for
from muster.categories import DEFAULT_EXCLUDED
from muster.config import Config, load_config
from muster.search import DEFAULT_LIMIT, Hit, _floor, _scaled_keyword_floor, build_match_query
from muster.search import search as run_search
from muster.temporal import extract as extract_temporal

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

SCORE_SCALES = {"keyword": "bm25", "vector": "cosine", "hybrid": "rrf"}

_local = threading.local()

# Sync jobs started through the API. In-process and deliberately not persisted:
# a job is only interesting while the panel that asked for it is still open, and
# the lock — not this dict — is what actually stops two syncs colliding.
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

# Whether the embedding model is resident. The first vector query otherwise
# pays ~25s to load bge-m3, during which the panel has nothing to show and looks
# exactly like a search that found nothing.
_model_ready = threading.Event()


def _warm_model(cfg: Config) -> None:
    """Load the embedding model in the background at startup.

    A search box that takes 25 seconds to answer its first question is not a
    search box. This moves that cost to a moment when nobody is waiting — and
    /status reports it, so the panel can say "starting up" rather than draw an
    empty result list.
    """
    try:
        from muster.embed import encode_query, load_model

        model = load_model(cfg.embed.model_id, cfg.embed.device)
        # Loaded is not ready. Metal compiles its kernels on the first inference,
        # so a model that has been read off disk but never run still costs most
        # of a minute on the query that happens to be first. Run one here.
        encode_query(model, "warm", cfg.embed.model_id)
    except Exception as exc:  # noqa: BLE001 — keyword search works regardless
        log.warning("could not preload %s: %s", cfg.embed.model_id, exc)
    finally:
        # Set either way: waiting on a load that failed helps nobody, and
        # keyword search does not need it.
        _model_ready.set()


def _run_sync_job(cfg: Config, job_id: str) -> None:
    """Run one incremental sync on a worker thread."""
    from muster import incremental
    from muster.connectors.gmail import GmailConnector
    from muster.locking import LockHeld, sync_lock

    state, result, error = "done", None, None
    # Its own connection: sqlite3 objects are not shareable across threads, and
    # this one writes.
    conn = None
    try:
        with sync_lock(cfg.lock_path):
            conn = db.connect(cfg.db_path)
            connector = GmailConnector(cfg.gmail.credentials_path, cfg.gmail.token_path)
            outcome = incremental.sync(conn, cfg, connector)
            result = outcome.summary()
    except LockHeld:
        # The scheduled job got there first. Nothing went wrong.
        state, result = "skipped", "another sync is already running"
    except Exception as exc:  # noqa: BLE001 — surfaced to the caller, not raised
        log.exception("sync job %s failed", job_id)
        state, error = "failed", str(exc)
    finally:
        if conn is not None:
            conn.close()

    with _jobs_lock:
        _jobs[job_id].update(state=state, result=result, error=error)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# --- connections ----------------------------------------------------------


def _connection(cfg: Config) -> sqlite3.Connection:
    """One connection per worker thread.

    sqlite3 connections are not shareable across threads, and uvicorn runs sync
    endpoints in a pool. Caching per thread avoids reopening and re-applying
    pragmas on every request without pretending a connection is thread-safe.
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = db.connect(cfg.db_path)
        _local.conn = conn
    return conn


# --- shaping --------------------------------------------------------------


def _kind(mime: str | None, source_uid: str) -> str:
    if "#att" in (source_uid or ""):
        return "attachment"
    if mime == "message/rfc822":
        return "message"
    return "other"


def _sender(name: str | None, address: str | None) -> dict[str, str | None] | None:
    if not name and not address:
        return None
    return {"name": name, "address": address}


def _hit_json(hit: Hit, rank: int, mode: str, best: float, extra: sqlite3.Row | None) -> dict:
    mime = extra["mime"] if extra else None
    return {
        "rank": rank,
        "doc_id": hit.doc_id,
        "title": hit.title,
        "snippet": hit.snippet,
        "date": hit.doc_date,
        "source": hit.source,
        "kind": _kind(mime, hit.source_uid),
        "mime": mime,
        "counterparty": hit.counterparty,
        "sender": _sender(
            extra["sender_name"] if extra else None, extra["sender"] if extra else None
        ),
        "category": extra["category"] if extra else None,
        "attachment_count": extra["attachment_count"] if extra else 0,
        "content_hash": hit.content_hash,
        # Whether /thumb will produce something. Computed here so the list does
        # not fire a request per row to discover that most have no preview.
        "preview": _has_preview(extra["mime"] if extra else None),
        # Copies present in this result list, not in the index: if the floor cut
        # two of four, two is what the list is entitled to claim.
        "sources": [
            {
                "doc_id": ref.doc_id,
                "source": ref.source,
                "source_uid": ref.source_uid,
                "title": ref.title,
                "date": ref.doc_date,
            }
            for ref in (hit.copies or [hit.as_ref()])
        ],
        "score": {
            "value": round(hit.score, 6),
            "scale": SCORE_SCALES.get(mode, "unknown"),
            "of_best": round(hit.score / best, 4) if best else None,
            # Ranks each arm gave this document before fusion. Two arms
            # agreeing is much stronger evidence than one; RRF collapses that
            # into a number that cannot express it.
            "arms": hit.arms or None,
        },
    }


def _doc_extras(conn: sqlite3.Connection, doc_ids: list[int]) -> dict[int, sqlite3.Row]:
    """Per-document fields the ranker does not carry, in one query."""
    if not doc_ids:
        return {}
    placeholders = ",".join("?" * len(doc_ids))
    rows = conn.execute(
        f"""
        SELECT d.id, d.mime, d.sender, d.sender_name, d.category,
               (SELECT count(*) FROM attachments a WHERE a.doc_id = d.id) AS attachment_count
        FROM docs d WHERE d.id IN ({placeholders})
        """,
        doc_ids,
    ).fetchall()
    return {row["id"]: row for row in rows}


def _why_empty(
    conn: sqlite3.Connection, query: str, mode: str, params: dict
) -> dict[str, Any]:
    """Why the result list is empty. Never let the UI guess."""
    if not build_match_query(query):
        return {
            "code": "empty_query",
            "message": "The query has no searchable terms.",
            "matched": 0,
            "best_score": None,
            "floor": None,
            "retry": None,
        }

    # Re-run without the floor to tell "nothing matched" apart from "nothing was
    # good enough". Only on the empty path, so the common case pays nothing.
    unfiltered = run_search(conn, query, mode=mode, **{**params, "min_score": 0.0})
    floor = _scaled_keyword_floor(conn, _floor("keyword"), build_match_query(query))

    if not unfiltered:
        return {
            "code": "no_matches",
            "message": "Nothing in the index matches this query.",
            "matched": 0,
            "best_score": None,
            "floor": round(floor, 4),
            "retry": None,
        }
    return {
        "code": "below_confidence_floor",
        "message": (
            f"{len(unfiltered)} document(s) matched but none scored above the "
            "confidence floor."
        ),
        "matched": len(unfiltered),
        "best_score": round(unfiltered[0].score, 4),
        "floor": round(floor, 4),
        "retry": {"min_score": 0},
    }


# --- app ------------------------------------------------------------------


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    store = BlobStore(cfg.blob_dir)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Load the embedding model off the request path. Started here rather
        # than on first use so the cost lands when nobody is waiting.
        threading.Thread(target=_warm_model, args=(cfg,), daemon=True, name="warm").start()
        yield

    app = FastAPI(
        lifespan=lifespan,
        title="Muster",
        version="0.1.0",
        summary="Read-only HTTP access to a local mail index.",
    )
    app.add_middleware(
        CORSMiddleware,
        # The Tauri shell and a browser during development. Nothing else: this
        # serves an entire personal mailbox with no authentication.
        allow_origin_regex=r"^(tauri://localhost|http://localhost(:\d+)?|http://127\.0\.0\.1(:\d+)?)$",
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    def require_db() -> None:
        """Guard only. The connection is opened inside the endpoint, on purpose.

        A sync dependency and a sync endpoint are two separate dispatches into
        the threadpool, so a connection built here can be handed to a different
        thread than the one that uses it — which sqlite3 refuses, intermittently
        and only under load. Whatever runs the query has to open it.
        """
        if not Path(cfg.db_path).exists():
            raise ApiError(503, "unavailable", f"No database at {cfg.db_path}; run `muster init`.")

    @app.get("/search")
    def search(
        q: str = Query(..., description="Free text. A date inside it becomes a filter."),
        mode: Literal["keyword", "vector", "hybrid"] = "hybrid",
        limit: int = Query(DEFAULT_LIMIT, ge=1, le=100),
        since: str | None = None,
        until: str | None = None,
        min_score: float | None = None,
        all_categories: bool = False,
        _db: None = Depends(require_db),
    ) -> dict:
        conn = _connection(cfg)
        started = time.perf_counter()
        excluded = frozenset() if all_categories else DEFAULT_EXCLUDED
        params: dict[str, Any] = {
            "limit": limit,
            "since": since,
            "until": until,
            "exclude_categories": excluded,
        }
        if min_score is not None:
            params["min_score"] = min_score

        try:
            hits = run_search(conn, q, mode=mode, **params)
        except ValueError as exc:
            message = str(exc)
            if "muster embed" in message:
                raise ApiError(503, "unavailable", message) from exc
            raise ApiError(400, "bad_request", message) from exc

        extras = _doc_extras(conn, [h.doc_id for h in hits])
        best = hits[0].score if hits else 0.0
        searched, span = extract_temporal(q)

        return {
            "query": {
                "raw": q,
                # What actually reached the ranker. Only the keyword arm reads
                # dates out of the query, so this is null for vector mode.
                "searched": searched if (span and mode != "vector") else q,
                "mode": mode,
                "limit": limit,
                "temporal": (
                    {"matched": span.matched, "since": span.since, "until": span.until}
                    if span and mode != "vector" and not (since or until)
                    else None
                ),
                "filters": {
                    "since": since,
                    "until": until,
                    "excluded_categories": sorted(excluded),
                },
            },
            "results": [
                _hit_json(hit, rank, mode, best, extras.get(hit.doc_id))
                for rank, hit in enumerate(hits, start=1)
            ],
            "empty_reason": None if hits else _why_empty(conn, q, mode, params),
            "took_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    @app.get("/folders")
    def folders(
        q: str = Query(..., description="A name, not a sentence."),
        limit: int = Query(3, ge=1, le=10),
        _db: None = Depends(require_db),
    ) -> dict:
        """Directories a query might have meant.

        Kept out of /search deliberately. A folder has no content, so ranking it
        against documents would be inventing a score — and a suggestion that
        pushes a real result off the list is worse than no suggestion.
        """
        from muster.folders import suggest

        conn = _connection(cfg)
        return {
            "folders": [
                {"path": f.path, "display": f.display, "name": f.name, "depth": f.depth}
                for f in suggest(conn, q, limit=limit)
            ]
        }

    @app.get("/doc/{doc_id}")
    def doc(doc_id: int, _db: None = Depends(require_db)) -> dict:
        conn = _connection(cfg)
        row = conn.execute("SELECT * FROM docs WHERE id = ?", (doc_id,)).fetchone()
        if row is None:
            raise ApiError(404, "not_found", f"No document {doc_id}.")

        meta = _meta(row)
        text, chunk_count = _document_text(conn, store, row)

        attachments = [
            {
                "filename": a["filename"],
                "mime": a["mime"],
                "content_hash": a["content_hash"],
                "bytes": _blob_size(store, a["blob_path"]),
                "url": f"/blob/{a['content_hash']}",
                "doc_id": a["extracted_doc_id"],
            }
            for a in conn.execute(
                "SELECT * FROM attachments WHERE doc_id = ? ORDER BY id", (doc_id,)
            )
        ]
        copies = [
            {
                "doc_id": c["id"],
                "source": c["source"],
                "source_uid": c["source_uid"],
                "title": c["title"],
                "date": c["doc_date"],
            }
            for c in conn.execute(
                "SELECT id, source, source_uid, title, doc_date FROM docs"
                " WHERE content_hash = ? ORDER BY doc_date DESC",
                (row["content_hash"],),
            )
        ]

        return {
            "id": row["id"],
            "source": row["source"],
            "source_uid": row["source_uid"],
            "kind": _kind(row["mime"], row["source_uid"]),
            "title": row["title"],
            "date": row["doc_date"],
            "date_source": row["date_source"],
            "mime": row["mime"],
            "category": row["category"],
            "sender": _sender(row["sender_name"], row["sender"]),
            "recipients": _recipients(meta),
            "counterparty": row["counterparty"],
            "thread_uid": row["thread_uid"],
            "thread": _thread(conn, row),
            "provenance": _provenance(row, meta),
            "text": text,
            "chunk_count": chunk_count,
            "redacted_by": meta.get("redacted_by"),
            "excluded_sender": meta.get("excluded_sender"),
            # A file document keeps no blob — the file on disk is the original —
            # so its bytes are served from the recorded path instead. Same shape
            # either way, so a reader does not have to care which it is.
            "blob": _blob_ref(store, row, meta),
            "attachments": attachments,
            "copies": copies,
        }

    @app.get("/doc/{doc_id}/body")
    def doc_body(doc_id: int, _db: None = Depends(require_db)) -> dict:
        """The message as it was sent, rather than as it was indexed.

        Indexed text is mangled on purpose for retrieval — links reduced to bare
        domains, navigation flattened into prose — which reads as a wall of
        repeated hostnames where a message used to be. The original is still in
        the blob store, so it can be shown properly.
        """
        conn = _connection(cfg)
        row = conn.execute("SELECT * FROM docs WHERE id = ?", (doc_id,)).fetchone()
        if row is None:
            raise ApiError(404, "not_found", f"No document {doc_id}.")
        if row["mime"] != "message/rfc822" or not row["blob_path"]:
            return {"is_html": False, "html": "", "images_removed": 0}

        from muster import mailhtml

        try:
            rendered = mailhtml.render(store.get_blob(row["blob_path"]))
        except (BlobNotFound, OSError) as exc:
            raise ApiError(410, "gone", f"The stored message is unreadable ({exc}).") from exc

        return {
            "is_html": rendered.is_html,
            "html": rendered.html,
            # Reported so the reader knows something was withheld, and why.
            "images_removed": rendered.images_removed,
        }

    @app.get("/file/{doc_id}")
    def file_bytes(
        doc_id: int,
        download: bool = False,
        request: Request = None,  # type: ignore[assignment]
        _db: None = Depends(require_db),
    ) -> Response:
        """The bytes of an indexed file, read from where it lives on disk.

        The path comes from the database, never from the caller: this endpoint
        takes a document id, so there is no string a client could supply that
        would make it read something that was not indexed.
        """
        conn = _connection(cfg)
        row = conn.execute("SELECT * FROM docs WHERE id = ?", (doc_id,)).fetchone()
        if row is None or not _is_filesystem_doc(row, _meta(row)):
            raise ApiError(404, "not_found", f"No file document {doc_id}.")

        path = Path(_meta(row)["path"])
        try:
            data = path.read_bytes()
        except OSError as exc:
            # The index outlives the file: this is the honest failure for a
            # document whose original was moved or deleted since the last walk.
            raise ApiError(
                410, "gone", f"{path} is no longer readable ({exc.strerror})."
            ) from exc

        mime = row["mime"] or "application/octet-stream"
        headers = {
            "Content-Disposition": (
                f'{"attachment" if download else "inline"}; filename="{path.name}"'
            ),
            # Unlike a blob, a path is not a hash — the bytes behind it can
            # change, so this must be revalidated rather than cached forever.
            "Cache-Control": "private, no-cache",
            "Accept-Ranges": "bytes",
        }
        range_header = request.headers.get("range") if request is not None else None
        if range_header:
            return _partial(data, range_header, mime, headers)
        return Response(content=data, media_type=mime, headers=headers)

    @app.get("/thumb/{doc_id}")
    def thumb(
        doc_id: int,
        size: int = Query(96, ge=16, le=512),
        _db: None = Depends(require_db),
    ) -> Response:
        """A small preview image for a document, or 404 when there is none.

        Rendered on demand and cached by content hash. A search result showing a
        page of the actual document is worth far more than a generic file icon,
        and the whole cost is one PNG the size of a favicon.
        """
        conn = _connection(cfg)
        row = conn.execute("SELECT * FROM docs WHERE id = ?", (doc_id,)).fetchone()
        if row is None:
            raise ApiError(404, "not_found", f"No document {doc_id}.")

        png = _thumbnail(cfg, store, row, size)
        if png is None:
            raise ApiError(404, "not_found", "No preview for this document.")
        return Response(
            content=png,
            media_type="image/png",
            headers={
                # Keyed by content hash, so it can never go stale.
                "Cache-Control": "private, max-age=31536000, immutable",
                "ETag": f'"{row["content_hash"]}-{size}"',
            },
        )

    @app.get("/blob/{content_hash}")
    def blob(
        content_hash: str,
        download: bool = False,
        request: Request = None,  # type: ignore[assignment]
        _db: None = Depends(require_db),
    ) -> Response:
        conn = _connection(cfg)
        # Validate before touching the filesystem: this is the only endpoint
        # that turns a caller-supplied string into a path.
        try:
            relpath_for(content_hash)
        except ValueError as exc:
            raise ApiError(400, "bad_request", f"Not a sha256 digest: {content_hash!r}") from exc

        try:
            data = store.get_blob(content_hash)
        except BlobNotFound as exc:
            raise ApiError(404, "not_found", f"No blob {content_hash}.") from exc

        mime, filename = _blob_identity(conn, content_hash)
        disposition = "attachment" if download else "inline"
        headers = {
            "Content-Disposition": f'{disposition}; filename="{filename}"',
            # The URL is the hash of the body, so this can never go stale.
            "Cache-Control": "private, max-age=31536000, immutable",
            "ETag": f'"{content_hash}"',
            "Accept-Ranges": "bytes",
        }
        if request is not None and request.headers.get("if-none-match") == f'"{content_hash}"':
            return Response(status_code=304, headers=headers)

        range_header = request.headers.get("range") if request is not None else None
        if range_header:
            return _partial(data, range_header, mime, headers)
        return Response(content=data, media_type=mime, headers=headers)

    @app.post("/sync/refresh")
    def sync_refresh(background: BackgroundTasks) -> dict:
        """Start an incremental sync and return immediately.

        The one write this API has. It is narrow on purpose: it can only pull
        new mail, and it cannot purge, reparse or re-embed — those run for hours
        and destroy work, and a button that starts them silently is a mistake
        waiting to happen.

        Never blocks. The panel calls this when it opens, and a search box that
        waits on the network to become usable is worse than a stale index.
        """
        with _jobs_lock:
            running = next(
                (j for j in _jobs.values() if j["state"] == "running"), None
            )
            # Already syncing: hand back the job in flight rather than queueing
            # a second one behind it.
            if running:
                return {"job": running["id"], "state": "running", "started": False}
            job_id = f"sync-{len(_jobs) + 1}-{int(time.time())}"
            _jobs[job_id] = {
                "id": job_id, "state": "running", "started_at": time.time(),
                "result": None, "error": None,
            }
        background.add_task(_run_sync_job, cfg, job_id)
        return {"job": job_id, "state": "running", "started": True}

    @app.get("/sync/jobs/{job_id}")
    def sync_job(job_id: str) -> dict:
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job is None:
                raise ApiError(404, "not_found", f"No sync job {job_id}.")
            return {
                "job": job["id"],
                "state": job["state"],
                "result": job["result"],
                "error": job["error"],
            }

    @app.get("/status")
    def status(_db: None = Depends(require_db)) -> dict:
        from muster.embed import count_pending
        from muster.evaluate import corpus_fingerprint

        conn = _connection(cfg)

        def scalar(sql: str, *args) -> Any:
            return conn.execute(sql, args).fetchone()[0]

        applied = sorted(migrate.applied_versions(conn))
        counts = {
            table: scalar(f"SELECT count(*) FROM {table}")
            for table in ("docs", "chunks", "vectors", "attachments")
        }
        by_kind = dict(
            conn.execute(
                "SELECT CASE WHEN mime = 'message/rfc822' THEN 'message'"
                " WHEN source_uid LIKE '%#att%' THEN 'attachment' ELSE 'other' END, count(*)"
                " FROM docs WHERE origin = 'captured' GROUP BY 1"
            ).fetchall()
        )
        by_category = dict(
            conn.execute(
                "SELECT COALESCE(category, 'uncategorised'), count(*) FROM docs GROUP BY 1"
            ).fetchall()
        )
        earliest, latest = conn.execute(
            "SELECT min(doc_date), max(doc_date) FROM docs WHERE origin = 'captured'"
        ).fetchone()

        return {
            "database": str(cfg.db_path),
            # The UI needs a real path to reveal a file in Finder; a URL cannot
            # be handed to the file manager. Blobs are content-addressed, so
            # the path is derivable from a hash and this is the only missing
            # half of it.
            "blob_dir": str(cfg.blob_dir),
            "schema": {
                "applied": applied,
                "pending": [m.version for m in migrate.discover() if m.version not in set(applied)],
            },
            "counts": counts,
            "health": {
                "unfindable": scalar(
                    "SELECT count(*) FROM docs d WHERE d.origin = 'captured'"
                    " AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.doc_id = d.id)"
                    " AND coalesce(json_extract(d.meta_json, '$.redacted_by'), '') = ''"
                ),
                "redacted": scalar(
                    "SELECT count(*) FROM docs"
                    " WHERE coalesce(json_extract(meta_json, '$.redacted_by'), '') != ''"
                ),
                "chunks_awaiting_vectors": count_pending(conn, cfg.embed.model_id),
            },
            "by_kind": by_kind,
            "by_category": by_category,
            "date_range": {"earliest": earliest, "latest": latest},
            "embedding": {
                "model_id": cfg.embed.model_id,
                # False for the first few seconds after a restart. The panel
                # uses it to say why a search is slow instead of showing an
                # empty list.
                "ready": _model_ready.is_set(),
                "dimensions": _vector_dimensions(conn, cfg.embed.model_id),
            },
            "search_defaults": {
                "modes": ["keyword", "vector", "hybrid"],
                "default_mode": "hybrid",
                "excluded_categories": sorted(DEFAULT_EXCLUDED),
            },
            "corpus_fingerprint": corpus_fingerprint(conn),
        }

    return app


# --- helpers --------------------------------------------------------------


def _meta(row: sqlite3.Row) -> dict:
    import json

    try:
        return json.loads(row["meta_json"]) if row["meta_json"] else {}
    except (TypeError, ValueError):
        return {}


def _recipients(meta: dict) -> dict[str, list[str]]:
    """Who it was addressed to, out of the stored headers.

    A reader needs to see whether a message was addressed to them or to three
    hundred people, and that is not derivable from ``counterparty`` — which is
    one address chosen for ranking, not the header.
    """

    def addresses(key: str) -> list[str]:
        value = meta.get(key)
        return [str(a) for a in value if a] if isinstance(value, list) else []

    return {"to": addresses("to"), "cc": addresses("cc")}


# A thread is context, not a mailbox: enough to see where a message sits in a
# conversation, capped so one runaway list cannot make the response enormous.
THREAD_LIMIT = 50


def _thread(conn: sqlite3.Connection, row: sqlite3.Row) -> dict | None:
    """The other messages in this conversation, oldest first."""
    if not row["thread_uid"]:
        return None

    siblings = conn.execute(
        "SELECT id, title, doc_date, sender, sender_name FROM docs"
        " WHERE thread_uid = ? AND origin = 'captured'"
        " ORDER BY doc_date LIMIT ?",
        (row["thread_uid"], THREAD_LIMIT),
    ).fetchall()

    # A conversation of one is the message itself, which the caller already has.
    if len(siblings) < 2:
        return None

    return {
        "uid": row["thread_uid"],
        "count": len(siblings),
        "messages": [
            {
                "doc_id": s["id"],
                "title": s["title"],
                "date": s["doc_date"],
                "sender": _sender(s["sender_name"], s["sender"]),
                "current": s["id"] == row["id"],
            }
            for s in siblings
        ],
    }


def _provenance(row: sqlite3.Row, meta: dict) -> dict | None:
    """Where a file came from, for documents that are files on disk.

    ``where_from`` and ``downloaded_date`` are Spotlight's record of the
    download, and they are the join back to the message that pointed at the
    file. Absent for mail, which has no path and no download.
    """
    if not _is_filesystem_doc(row, meta):
        return None
    path = meta["path"]

    where_from = meta.get("where_from")
    if isinstance(where_from, str):
        where_from = [where_from]

    return {
        "path": str(path),
        "where_from": [str(w) for w in where_from if w] if isinstance(where_from, list) else [],
        "downloaded_date": meta.get("downloaded_date"),
        "size_bytes": meta.get("size"),
        "modified": meta.get("mtime"),
        "created": meta.get("birthtime"),
    }


def _document_text(conn, store, row) -> tuple[str, int]:
    """The indexed body.

    Rebuilt rather than joined from chunks: chunks overlap by 15%, so
    concatenating them repeats every boundary. Re-parsing the stored blob costs
    a few milliseconds and is exactly what the indexer saw.
    """
    chunk_count = conn.execute(
        "SELECT count(*) FROM chunks WHERE doc_id = ?", (row["id"],)
    ).fetchone()[0]

    if row["mime"] == "message/rfc822" and row["blob_path"]:
        from muster.parser import parse_message

        try:
            return parse_message(store.get_blob(row["blob_path"]), row["source_uid"]).body, chunk_count
        except (BlobNotFound, OSError) as exc:
            log.warning("doc %s: cannot re-read blob (%s)", row["id"], exc)

    # Attachment documents have no message to re-parse; their chunks do not
    # overlap enough to matter and are the only copy of the extracted text.
    rows = conn.execute(
        "SELECT text FROM chunks WHERE doc_id = ? ORDER BY ord", (row["id"],)
    ).fetchall()
    return "\n\n".join(r[0] for r in rows), chunk_count


def _is_filesystem_doc(row: sqlite3.Row, meta: dict) -> bool:
    """Whether this document's original is a file on disk we can still read.

    Checked by shape rather than by the source string alone. Mail imported from
    an mbox was historically stored under the same source and carries a path in
    its metadata too, so the source is not on its own enough to tell a document
    on disk from a message that came out of one.
    """
    if row["source"] != "file" or row["mime"] == "message/rfc822":
        return False
    path = meta.get("path")
    return bool(path) and Path(path).is_absolute()


def _has_preview(mime: str | None) -> bool:
    mime = (mime or "").lower()
    return mime.startswith("image/") or mime == "application/pdf"


def _blob_ref(store: BlobStore, row: sqlite3.Row, meta: dict) -> dict | None:
    """Where a document's bytes can be fetched from.

    Two kinds of original, one shape. Mail and attachments live in the blob
    store and are addressed by hash; a file lives on disk and is addressed by
    document id, because a path is not content and cannot be cached forever.
    """
    if row["blob_path"]:
        return {
            "hash": row["content_hash"],
            "bytes": _blob_size(store, row["blob_path"]),
            "mime": row["mime"],
            "url": f"/blob/{row['content_hash']}",
            "filename": _download_name(row),
        }

    if _is_filesystem_doc(row, meta):
        path = meta["path"]
        return {
            "hash": row["content_hash"],
            "bytes": meta.get("size"),
            "mime": row["mime"],
            "url": f"/file/{row['id']}",
            "filename": Path(path).name,
        }
    return None


def _thumbnail(cfg: Config, store: BlobStore, row: sqlite3.Row, size: int) -> bytes | None:
    """A small PNG preview, rendered once and cached by content hash.

    The first page of a PDF or a downscaled image. Anything else has nothing
    worth showing, and a generic glyph is what the icon is already for.
    """
    if not _has_preview(row["mime"]):
        return None

    cache = cfg.home / "thumbs" / f"{row['content_hash']}-{size}.png"
    try:
        if cache.exists():
            return cache.read_bytes()
    except OSError:
        pass

    data = _original_bytes(store, row)
    if data is None:
        return None

    try:
        png = _render_thumb(data, row["mime"], size)
    except Exception as exc:  # noqa: BLE001 — an unreadable original is not an error
        log.debug("no thumbnail for doc %s: %s", row["id"], exc)
        return None

    if png:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(png)
        except OSError:
            pass  # a cache that cannot be written is still a working thumbnail
    return png


def _original_bytes(store: BlobStore, row: sqlite3.Row) -> bytes | None:
    if row["blob_path"]:
        try:
            return store.get_blob(row["blob_path"])
        except (BlobNotFound, OSError):
            return None
    meta = _meta(row)
    path = meta.get("path") or (row["source_uid"] if row["source"] == "file" else None)
    if not path:
        return None
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def _render_thumb(data: bytes, mime: str | None, size: int) -> bytes | None:
    import io

    mime = (mime or "").lower()

    if mime == "application/pdf":
        import pymupdf

        with pymupdf.open(stream=data, filetype="pdf") as doc:
            if not doc.page_count:
                return None
            page = doc.load_page(0)
            # Scale so the long edge lands on the requested size.
            box = page.rect
            zoom = size / max(box.width, box.height, 1)
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            return pix.tobytes("png")

    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        image = image.convert("RGB")
        image.thumbnail((size, size))
        out = io.BytesIO()
        image.save(out, format="PNG", optimize=True)
        return out.getvalue()


def _blob_size(store: BlobStore, blob_path: str | None) -> int | None:
    if not blob_path:
        return None
    try:
        return store.abspath(blob_path).stat().st_size
    except OSError:
        return None


def _download_name(row: sqlite3.Row) -> str:
    title = (row["title"] or f"doc-{row['id']}").strip() or f"doc-{row['id']}"
    safe = "".join(c for c in title if c.isalnum() or c in " ._-")[:120].strip()
    return f"{safe or 'document'}.eml" if row["mime"] == "message/rfc822" else safe


def _blob_identity(conn: sqlite3.Connection, content_hash: str) -> tuple[str, str]:
    row = conn.execute(
        "SELECT mime, filename FROM attachments WHERE content_hash = ? LIMIT 1", (content_hash,)
    ).fetchone()
    if row and row["mime"]:
        return row["mime"], row["filename"] or content_hash
    doc = conn.execute(
        "SELECT mime, title, id FROM docs WHERE content_hash = ? LIMIT 1", (content_hash,)
    ).fetchone()
    if doc:
        return doc["mime"] or "application/octet-stream", _download_name(doc)
    return "application/octet-stream", content_hash


def _vector_dimensions(conn: sqlite3.Connection, model_id: str) -> int | None:
    row = conn.execute(
        "SELECT length(embedding) FROM vectors WHERE model_id = ? LIMIT 1", (model_id,)
    ).fetchone()
    return row[0] // 4 if row else None


def _partial(data: bytes, range_header: str, mime: str, headers: dict) -> Response:
    """Byte ranges, without which a PDF viewer will not stream."""
    total = len(data)
    try:
        units, _, spec = range_header.partition("=")
        if units.strip() != "bytes":
            raise ValueError(units)
        start_text, _, end_text = spec.partition("-")
        start = int(start_text) if start_text else max(total - int(end_text), 0)
        end = int(end_text) if end_text and start_text else total - 1
    except ValueError:
        return Response(content=data, media_type=mime, headers=headers)

    if start >= total:
        return Response(
            status_code=416, headers={**headers, "Content-Range": f"bytes */{total}"}
        )
    end = min(end, total - 1)
    return Response(
        content=data[start : end + 1],
        status_code=206,
        media_type=mime,
        headers={**headers, "Content-Range": f"bytes {start}-{end}/{total}"},
    )


def serve(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    cfg: Config | None = None,
    reload: bool = False,
) -> None:
    import uvicorn

    uvicorn.run(create_app(cfg), host=host, port=port, log_level="info", reload=reload)
