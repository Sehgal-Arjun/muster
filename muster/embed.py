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

# Retrieval models are trained with asymmetric prefixes, and they differ per
# family. Using the wrong one — or none — measurably degrades recall, so this is
# a lookup rather than a guess based on the model name.
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

MODEL_PREFIXES: dict[str, tuple[str, str]] = {
    # model_id: (query prefix, document prefix)
    "BAAI/bge-small-en-v1.5": (BGE_QUERY_PREFIX, ""),
    "BAAI/bge-base-en-v1.5": (BGE_QUERY_PREFIX, ""),
    "BAAI/bge-large-en-v1.5": (BGE_QUERY_PREFIX, ""),
    # bge-m3 is instruction-free by design; adding the v1.5 prefix hurts it.
    "BAAI/bge-m3": ("", ""),
    "intfloat/multilingual-e5-base": ("query: ", "passage: "),
    "intfloat/multilingual-e5-small": ("query: ", "passage: "),
    "intfloat/multilingual-e5-large": ("query: ", "passage: "),
}


def prefixes_for(model_id: str) -> tuple[str, str]:
    if model_id in MODEL_PREFIXES:
        return MODEL_PREFIXES[model_id]
    lowered = model_id.lower()
    if "e5" in lowered:
        return "query: ", "passage: "
    if "bge" in lowered and "m3" not in lowered:
        return BGE_QUERY_PREFIX, ""
    log.debug("no prefix convention known for %s; using none", model_id)
    return "", ""

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


def encode_documents(
    model, texts: list[str], batch_size: int = DEFAULT_BATCH, model_id: str = ""
):
    _query_prefix, doc_prefix = prefixes_for(model_id)
    if doc_prefix:
        texts = [doc_prefix + text for text in texts]
    return model.encode(
        texts,
        batch_size=batch_size,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )


def encode_query(model, query: str, model_id: str = ""):
    query_prefix, _doc_prefix = prefixes_for(model_id)
    return model.encode(
        query_prefix + query,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )


# --- work discovery -------------------------------------------------------


@dataclass
class PendingGroup:
    text_hash: str
    text: str
    chunk_ids: list[int]
    title: str = ""
    first_ord: int = 0

    def encode_input(self, prepend_title: bool) -> str:
        """What actually gets embedded.

        The title goes only on the document's first chunk: repeating it on every
        chunk of a long document would pull all of them toward the subject and
        blur what distinguishes them.
        """
        if prepend_title and self.title and self.first_ord == 0:
            return f"{self.title}\n\n{self.text}"
        return self.text


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
    conn: sqlite3.Connection,
    model_id: str,
    *,
    limit: int | None = None,
    prepend_title: bool = False,
) -> Iterator[PendingGroup]:
    """Chunks with no vector for this model, grouped by identical text."""
    # When the title is prepended the encoded string differs per title, so the
    # dedup key has to include it or two chunks with identical bodies under
    # different subjects would wrongly share one vector.
    group_key = "c.text_hash, coalesce(c.title, '')" if prepend_title else "c.text_hash"
    title_select = "min(coalesce(c.title, ''))" if prepend_title else "''"
    sql = f"""
        SELECT c.text_hash                     AS text_hash,
               min(c.text)                     AS text,
               {title_select}                  AS title,
               min(c.ord)                      AS first_ord,
               group_concat(c.id)              AS chunk_ids
        FROM chunks c
        WHERE NOT EXISTS (
            SELECT 1 FROM vectors v WHERE v.chunk_id = c.id AND v.model_id = :model
        )
        GROUP BY {group_key}
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
            title=row["title"] or "",
            first_ord=int(row["first_ord"]),
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
    # True when a caller asked it to stop early. The remainder is still
    # pending, so the next run continues from here.
    stopped: bool = False



def embed_pending(
    conn: sqlite3.Connection,
    *,
    model_id: str,
    device: str | None = None,
    batch_size: int = DEFAULT_BATCH,
    limit: int | None = None,
    progress=None,
    should_stop=None,
    model=None,
    prepend_title: bool = False,
) -> EmbedReport:
    """Embed every chunk lacking a vector for ``model_id``. Safe to interrupt."""
    device = resolve_device(device)
    report = EmbedReport(model_id=model_id, device=device)

    groups = list(pending_groups(conn, model_id, limit=limit, prepend_title=prepend_title))
    if not groups:
        return report

    model = model or load_model(model_id, device)

    for start in range(0, len(groups), batch_size):
        # Checked between batches, never inside one: a batch is the unit of work
        # we are willing to lose, and stopping mid-batch would throw away GPU
        # time already spent for nothing.
        if should_stop is not None and should_stop():
            report.stopped = True
            break
        batch = groups[start : start + batch_size]
        vectors = encode_documents(
            model,
            [g.encode_input(prepend_title) for g in batch],
            batch_size=batch_size,
            model_id=model_id,
        )

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
