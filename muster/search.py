"""Retrieval.

Keyword search over the FTS5 index, ranked by BM25. Two rules apply to every
query and will apply to every retrieval mode added later:

* ``docs.origin = 'captured'`` unless explicitly widened. What Muster was told
  and what Muster concluded must never be silently mixed in a result list.
* Results are deduplicated to one row per document, keeping its best-scoring
  chunk. Chunks are an implementation detail of embedding; documents are what
  a person is looking for.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

DEFAULT_LIMIT = 10
# Chunks to consider before collapsing to documents. A long document can hold
# several matching chunks, so the chunk-level cut has to be wider than the
# document-level one or the tail of the ranking goes missing.
CANDIDATE_FACTOR = 6
# Reciprocal rank fusion constant. 60 is the value from the original paper and
# is where every comparable system sits; changing it needs an eval run, not an
# opinion.
RRF_K = 60
# How much deeper each arm of a hybrid query retrieves than the final cut.
FUSION_DEPTH = 3

_TERM_RE = re.compile(r'"[^"]+"|\S+')
_STRIP_RE = re.compile(r"^\W+|\W+$", re.UNICODE)
# Mirrors what the unicode61 tokenizer does to the indexed text. Note the
# excluded underscore: \w would keep "Rechnung_2026" whole, but FTS5 splits it.
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass
class Hit:
    doc_id: int
    chunk_id: int
    title: str | None
    source: str
    source_uid: str
    doc_date: str
    counterparty: str | None
    score: float
    snippet: str

    @property
    def rank_key(self) -> float:
        return -self.score


def build_match_query(query: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression.

    Terms are OR-ed rather than AND-ed. A natural-language question run as a
    conjunction usually matches nothing at all, and BM25 already floats the
    documents containing every term to the top — so OR costs precision at the
    tail and buys recall, which is the trade this index wants.

    Every term is quoted, so punctuation in a query is searched for rather than
    interpreted as FTS5 syntax. But quoting alone turns any punctuated term into
    a strict phrase: "Rechnung_2026-0042.pdf" would only match documents with
    those four tokens adjacent and in order, which finds the covering email and
    misses the invoice itself. So a punctuated term contributes both the phrase
    — which ranks an exact filename match highest — and its parts.

    A term the user quoted explicitly stays a strict phrase. That is what the
    quotes are for.
    """
    terms: list[str] = []
    seen: set[str] = set()

    def push(expression: str) -> None:
        if expression not in seen:
            seen.add(expression)
            terms.append(expression)

    for raw in _TERM_RE.findall(query or ""):
        if raw.startswith('"') and raw.endswith('"') and len(raw) > 1:
            phrase = " ".join(_WORD_RE.findall(raw[1:-1]))
            if phrase:
                push(f'"{phrase}"')
            continue

        parts = _WORD_RE.findall(_STRIP_RE.sub("", raw))
        if not parts:
            continue
        if len(parts) > 1:
            push('"' + " ".join(parts) + '"')
        for part in parts:
            push(f'"{part}"')

    return " OR ".join(terms)


def keyword_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    since: str | None = None,
    until: str | None = None,
    origin: str | None = "captured",
    source: str | None = None,
) -> list[Hit]:
    match = build_match_query(query)
    if not match:
        return []

    sql = """
        SELECT c.id                                        AS chunk_id,
               c.doc_id                                    AS doc_id,
               d.title, d.source, d.source_uid, d.doc_date, d.counterparty,
               bm25(chunks_fts)                            AS score,
               snippet(chunks_fts, 0, '[', ']', ' … ', 18) AS snippet
        FROM chunks_fts
        JOIN chunks c ON c.id = chunks_fts.rowid
        JOIN docs   d ON d.id = c.doc_id
        WHERE chunks_fts MATCH :match
    """
    params: dict[str, object] = {"match": match, "n": max(limit, 1) * CANDIDATE_FACTOR}
    if origin is not None:
        sql += " AND d.origin = :origin"
        params["origin"] = origin
    if since:
        sql += " AND d.doc_date >= :since"
        params["since"] = since
    if until:
        sql += " AND d.doc_date <= :until"
        params["until"] = until
    if source:
        sql += " AND d.source = :source"
        params["source"] = source
    # bm25() is negative, most relevant first.
    sql += " ORDER BY score ASC LIMIT :n"

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        raise ValueError(f"could not run query {query!r}: {exc}") from exc

    return _collapse_to_docs(rows, limit)


def _collapse_to_docs(rows, limit: int) -> list[Hit]:
    """One hit per document, keeping the best-scoring chunk."""
    best: dict[int, Hit] = {}
    for row in rows:
        hit = Hit(
            doc_id=row["doc_id"],
            chunk_id=row["chunk_id"],
            title=row["title"],
            source=row["source"],
            source_uid=row["source_uid"],
            doc_date=row["doc_date"],
            counterparty=row["counterparty"],
            score=-float(row["score"]),
            snippet=" ".join((row["snippet"] or "").split()),
        )
        current = best.get(hit.doc_id)
        if current is None or hit.score > current.score:
            best[hit.doc_id] = hit

    ranked = sorted(best.values(), key=lambda h: (-h.score, h.doc_date))
    return ranked[:limit]


def _filter_clause(
    origin: str | None, since: str | None, until: str | None, source: str | None
) -> tuple[str, dict[str, object]]:
    sql = ""
    params: dict[str, object] = {}
    if origin is not None:
        sql += " AND d.origin = :origin"
        params["origin"] = origin
    if since:
        sql += " AND d.doc_date >= :since"
        params["since"] = since
    if until:
        sql += " AND d.doc_date <= :until"
        params["until"] = until
    if source:
        sql += " AND d.source = :source"
        params["source"] = source
    return sql, params


def vector_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    model_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    origin: str | None = "captured",
    source: str | None = None,
) -> list[Hit]:
    """Nearest neighbours by cosine distance, using sqlite-vec on the vectors table."""
    from muster.config import load_config
    from muster.db import load_sqlite_vec
    from muster.embed import encode_query, load_model, pack

    if not (query or "").strip():
        return []

    cfg = load_config()
    model_id = model_id or cfg.embed.model_id

    if not conn.execute(
        "SELECT 1 FROM vectors WHERE model_id = ? LIMIT 1", (model_id,)
    ).fetchone():
        raise ValueError(f"no vectors for model {model_id!r} — run `muster embed` first")

    load_sqlite_vec(conn)
    embedding = pack(encode_query(load_model(model_id, cfg.embed.device), query))

    where, params = _filter_clause(origin, since, until, source)
    params |= {
        "embedding": embedding,
        "model": model_id,
        "n": max(limit, 1) * CANDIDATE_FACTOR,
    }

    rows = conn.execute(
        f"""
        SELECT c.id     AS chunk_id,
               c.doc_id AS doc_id,
               d.title, d.source, d.source_uid, d.doc_date, d.counterparty,
               substr(c.text, 1, 220) AS snippet,
               vec_distance_cosine(v.embedding, :embedding) AS distance
        FROM vectors v
        JOIN chunks c ON c.id = v.chunk_id
        JOIN docs   d ON d.id = c.doc_id
        WHERE v.model_id = :model{where}
        ORDER BY distance ASC
        LIMIT :n
        """,
        params,
    ).fetchall()

    # Cosine distance in [0, 2]; report similarity so higher is better in every
    # mode and the CLI does not need to know which one produced a row.
    hits = [
        Hit(
            doc_id=row["doc_id"],
            chunk_id=row["chunk_id"],
            title=row["title"],
            source=row["source"],
            source_uid=row["source_uid"],
            doc_date=row["doc_date"],
            counterparty=row["counterparty"],
            score=1.0 - float(row["distance"]),
            snippet=" ".join((row["snippet"] or "").split()),
        )
        for row in rows
    ]

    best: dict[int, Hit] = {}
    for hit in hits:
        if hit.doc_id not in best or hit.score > best[hit.doc_id].score:
            best[hit.doc_id] = hit
    return sorted(best.values(), key=lambda h: (-h.score, h.doc_date))[:limit]


def reciprocal_rank_fusion(
    rankings: list[list[Hit]], *, k: int = RRF_K, limit: int = DEFAULT_LIMIT
) -> list[Hit]:
    """Fuse ranked lists by 1/(k + rank), summed per document.

    RRF uses only positions, never the underlying scores, which is the whole
    point: BM25 scores and cosine similarities are not on comparable scales and
    any attempt to weight them directly needs a calibration constant that would
    have to be re-tuned per corpus.
    """
    scores: dict[int, float] = {}
    representative: dict[int, Hit] = {}

    for ranking in rankings:
        for rank, hit in enumerate(ranking, start=1):
            scores[hit.doc_id] = scores.get(hit.doc_id, 0.0) + 1.0 / (k + rank)
            # Keep the first list's snippet: keyword snippets show the match,
            # vector ones are just the head of the chunk.
            representative.setdefault(hit.doc_id, hit)

    fused = []
    for doc_id, score in scores.items():
        hit = representative[doc_id]
        fused.append(
            Hit(
                doc_id=hit.doc_id,
                chunk_id=hit.chunk_id,
                title=hit.title,
                source=hit.source,
                source_uid=hit.source_uid,
                doc_date=hit.doc_date,
                counterparty=hit.counterparty,
                score=score,
                snippet=hit.snippet,
            )
        )
    return sorted(fused, key=lambda h: (-h.score, h.doc_date))[:limit]


def hybrid_search(
    conn: sqlite3.Connection, query: str, *, limit: int = DEFAULT_LIMIT, **filters
) -> list[Hit]:
    """BM25 and vector candidates, fused with RRF.

    Each arm retrieves deeper than the final cut so that a document ranked
    modestly by both — which is exactly what RRF is meant to promote — is still
    in both lists when they are fused.
    """
    depth = max(limit, 1) * FUSION_DEPTH
    keyword = keyword_search(conn, query, limit=depth, **filters)
    try:
        vector = vector_search(conn, query, limit=depth, **filters)
    except ValueError as exc:
        # An unembedded index should degrade to keyword search, not fail.
        import logging

        logging.getLogger(__name__).warning("hybrid falling back to keyword: %s", exc)
        return keyword[:limit]

    return reciprocal_rank_fusion([keyword, vector], limit=limit)


MODES = {"keyword": keyword_search, "vector": vector_search, "hybrid": hybrid_search}


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    mode: str = "keyword",
    limit: int = DEFAULT_LIMIT,
    **filters,
) -> list[Hit]:
    """Front door for retrieval."""
    try:
        runner = MODES[mode]
    except KeyError:
        raise ValueError(
            f"unknown search mode: {mode} (expected one of {', '.join(sorted(MODES))})"
        ) from None
    return runner(conn, query, limit=limit, **filters)
