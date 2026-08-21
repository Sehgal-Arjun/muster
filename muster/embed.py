"""Embedding chunks.

Two properties matter more than speed here:

* **Resumable.** Each batch commits. A run killed halfway leaves the vectors it
  already wrote, and re-running picks up exactly where it stopped.
* **Additive across models.** Vectors are keyed ``(chunk_id, model_id)``, so
  switching models is an insert alongside the existing rows. Nothing is wiped,
  and the old model stays queryable for comparison.

Work is deduplicated by ``text_hash``: identical chunk text — a boilerplate
footer, a re-sent message — is encoded once and the vector written for every
chunk that shares it.
"""

from __future__ import annotations

import logging
import sqlite3
import struct
from collections.abc import Iterator
from dataclasses import dataclass

log = logging.getLogger(__name__)

# BGE models are trained with an asymmetric prefix: queries get this, documents
# get nothing. Omitting it measurably degrades retrieval, so it is not optional.
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

DEFAULT_BATCH = 64
_model_cache: dict[tuple[str, str | None], object] = {}


def pack(vector) -> bytes:
    """float32 little-endian — the layout sqlite-vec reads from a BLOB."""
    return struct.pack(f"<{len(vector)}f", *(float(v) for v in vector))


def unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def resolve_device(preference: str | None = None) -> str:
    if preference:
        return preference
    try:
        import torch
    except ImportError:
        return "cpu"
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def load_model(model_id: str, device: str | None = None):
    """Load and cache. Loading bge-small twice in one process is pure waste."""
    device = resolve_device(device)
    key = (model_id, device)
    if key not in _model_cache:
        from sentence_transformers import SentenceTransformer

        log.info("loading %s on %s", model_id, device)
        _model_cache[key] = SentenceTransformer(model_id, device=device)
    return _model_cache[key]


def encode_documents(model, texts: list[str], batch_size: int = DEFAULT_BATCH):
    return model.encode(
        texts,
        batch_size=batch_size,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )


def encode_query(model, query: str):
    return model.encode(
        BGE_QUERY_PREFIX + query if _is_bge(model) else query,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )


def _is_bge(model) -> bool:
    name = ""
    for attribute in ("model_card_data", "_model_config"):
        holder = getattr(model, attribute, None)
        name = str(getattr(holder, "base_model", "") or holder or "")
        if name:
            break
    return "bge" in (name or str(getattr(model, "_first_module", lambda: "")())).lower()


# --- work discovery -------------------------------------------------------


@dataclass
class PendingGroup:
    text_hash: str
    text: str
    chunk_ids: list[int]


def count_pending(conn: sqlite3.Connection, model_id: str) -> int:
    return conn.execute(
        """
        SELECT count(*) FROM chunks c
        WHERE NOT EXISTS (
            SELECT 1 FROM vectors v WHERE v.chunk_id = c.id AND v.model_id = ?
        )
        """,
        (model_id,),
    ).fetchone()[0]


def pending_groups(
    conn: sqlite3.Connection, model_id: str, *, limit: int | None = None
) -> Iterator[PendingGroup]:
    """Chunks with no vector for this model, grouped by identical text."""
    sql = """
        SELECT c.text_hash                     AS text_hash,
               min(c.text)                     AS text,
               group_concat(c.id)              AS chunk_ids
        FROM chunks c
        WHERE NOT EXISTS (
            SELECT 1 FROM vectors v WHERE v.chunk_id = c.id AND v.model_id = :model
        )
        GROUP BY c.text_hash
        ORDER BY min(c.id)
    """
    params: dict[str, object] = {"model": model_id}
    if limit is not None:
        sql += " LIMIT :limit"
        params["limit"] = limit

    for row in conn.execute(sql, params):
        yield PendingGroup(
            text_hash=row["text_hash"],
            text=row["text"],
            chunk_ids=[int(i) for i in str(row["chunk_ids"]).split(",")],
        )


@dataclass
class EmbedReport:
    model_id: str
    device: str
    groups: int = 0
    chunks: int = 0
    batches: int = 0
    dimensions: int | None = None


def embed_pending(
    conn: sqlite3.Connection,
    *,
    model_id: str,
    device: str | None = None,
    batch_size: int = DEFAULT_BATCH,
    limit: int | None = None,
    progress=None,
    model=None,
) -> EmbedReport:
    """Embed every chunk lacking a vector for ``model_id``. Safe to interrupt."""
    device = resolve_device(device)
    report = EmbedReport(model_id=model_id, device=device)

    groups = list(pending_groups(conn, model_id, limit=limit))
    if not groups:
        return report

    model = model or load_model(model_id, device)

    for start in range(0, len(groups), batch_size):
        batch = groups[start : start + batch_size]
        vectors = encode_documents(model, [g.text for g in batch], batch_size=batch_size)

        rows = [
            (chunk_id, model_id, pack(vector))
            for group, vector in zip(batch, vectors, strict=True)
            for chunk_id in group.chunk_ids
        ]
        # One transaction per batch: the unit of work we are willing to lose.
        conn.execute("BEGIN")
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO vectors (chunk_id, model_id, embedding) VALUES (?, ?, ?)",
                rows,
            )
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

        report.groups += len(batch)
        report.chunks += len(rows)
        report.batches += 1
        report.dimensions = len(vectors[0])
        if progress:
            progress(report, len(groups))

    return report
