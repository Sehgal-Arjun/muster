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

import math
import re
import sqlite3
from dataclasses import dataclass

from muster.categories import DEFAULT_EXCLUDED

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
# BM25 magnitude scales with log(collection size), because that is how IDF
# scales. A floor calibrated on one index would be meaningless on another, so
# the configured value is expressed at this collection size and scaled to the
# index actually being searched.
REFERENCE_CHUNKS = 1000
# A clause matching more than this share of the index tells us nothing about
# relevance — it is a stopword for this collection, whatever a dictionary says.
DISCRIMINATING_SHARE = 0.10

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
    exclude_categories=DEFAULT_EXCLUDED,
    min_score: float | None = None,
) -> list[Hit]:
    match = build_match_query(query)
    if not match:
        return []

    sql = """
        SELECT c.id                                        AS chunk_id,
               c.doc_id                                    AS doc_id,
               d.title, d.source, d.source_uid, d.doc_date, d.counterparty,
               bm25(chunks_fts, :wtext, :wtitle)           AS score,
               snippet(chunks_fts, 0, '[', ']', ' … ', 18) AS snippet
        FROM chunks_fts
        JOIN chunks c ON c.id = chunks_fts.rowid
        JOIN docs   d ON d.id = c.doc_id
        WHERE chunks_fts MATCH :match
    """
    where, params = _filter_clause(origin, since, until, source, exclude_categories)
    sql += where
    weights = _weights()
    params |= {
        "match": match,
        "n": max(limit, 1) * CANDIDATE_FACTOR,
        "wtext": weights[0],
        "wtitle": weights[1],
    }
    # bm25() is negative, most relevant first.
    sql += " ORDER BY score ASC LIMIT :n"

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        raise ValueError(f"could not run query {query!r}: {exc}") from exc

    floor = (
        _scaled_keyword_floor(conn, _floor("keyword"), match)
        if min_score is None
        else min_score
    )
    return _collapse_to_docs(rows, limit, floor, _ratio_floor() if min_score is None else 0.0)


def _weights() -> tuple[float, float]:
    from muster.config import load_config

    cfg = load_config().search
    return cfg.text_weight, cfg.title_weight


def _floor(mode: str) -> float:
    from muster.config import load_config

    return getattr(load_config().search, f"min_score_{mode}", 0.0)


def _ratio_floor() -> float:
    from muster.config import load_config

    return load_config().search.min_score_ratio


def _discriminating_clauses(conn: sqlite3.Connection, match: str, n_chunks: int) -> int:
    """How many clauses are rare enough to carry signal.

    The count is capped: we only need to know whether a clause is below the
    threshold, so the scan stops there rather than walking the whole doclist of
    a term like "the".
    """
    if not match:
        return 1
    cap = max(int(n_chunks * DISCRIMINATING_SHARE), 1)
    found = 0
    for clause in match.split(" OR "):
        try:
            hits = conn.execute(
                "SELECT count(*) FROM ("
                " SELECT 1 FROM chunks_fts WHERE chunks_fts MATCH ? LIMIT ?)",
                (clause, cap + 1),
            ).fetchone()[0]
        except sqlite3.OperationalError:
            continue
        if 0 < hits <= cap:
            found += 1
    return max(found, 1)


def _scaled_keyword_floor(
    conn: sqlite3.Connection, configured: float, match: str = ""
) -> float:
    """Turn the configured floor into a raw-BM25 threshold for this query.

    Two normalisations, because a raw BM25 sum is comparable neither across
    indexes nor across queries:

    * **Collection size.** BM25 magnitude tracks IDF, which grows with
      log(collection size). The same phrase scores ~2 on a 20-chunk index and
      ~15 on a 1500-chunk one.
    * **Discriminating query terms.** The sum runs over clauses that actually
      matched something rare. Counting *all* clauses over-penalises a politely
      phrased question, whose extra words are stopwords contributing 0.0;
      counting none of them cannot separate a distinctive one-word query
      ("Solaranlage", 13.8) from a vague two-word one ("lost jacket", 7.2),
      because both are simply small.

    So the configured number means "BM25 per discriminating clause, on an index
    of REFERENCE_CHUNKS chunks". Measured on a 1447-chunk index, queries with a
    real answer score 4.9-13.8 by that measure and queries without one 1.8-3.7.
    """
    if configured <= 0:
        return 0.0
    try:
        n = conn.execute("SELECT count(*) FROM chunks").fetchone()[0] or 1
    except sqlite3.Error:
        n = REFERENCE_CHUNKS
    clauses = _discriminating_clauses(conn, match, n)
    return configured * (math.log1p(n) / math.log1p(REFERENCE_CHUNKS)) * clauses


def _collapse_to_docs(
    rows, limit: int, floor: float = 0.0, ratio: float = 0.0
) -> list[Hit]:
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
    if floor:
        ranked = [hit for hit in ranked if hit.score >= floor]
    if ratio and ranked:
        # Scale-free companion to the absolute floor: a long tail scoring a
        # fraction of the best hit is the shape of a query the index cannot
        # answer, whatever the absolute numbers happen to be.
        cutoff = ranked[0].score * ratio
        ranked = [hit for hit in ranked if hit.score >= cutoff]
    return ranked[:limit]


def _filter_clause(
    origin: str | None,
    since: str | None,
    until: str | None,
    source: str | None,
    exclude_categories=None,
) -> tuple[str, dict[str, object]]:
    sql = ""
    params: dict[str, object] = {}
    if origin is not None:
        sql += " AND d.origin = :origin"
        params["origin"] = origin
    if exclude_categories:
        # NULL is "not categorised" — every non-Gmail document and all ordinary
        # correspondence — so it must always pass.
        names = sorted(exclude_categories)
        keys = [f"cat{i}" for i in range(len(names))]
        placeholders = ", ".join(f":{key}" for key in keys)
        sql += f" AND (d.category IS NULL OR d.category NOT IN ({placeholders}))"
        params.update(dict(zip(keys, names, strict=True)))
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
    exclude_categories=DEFAULT_EXCLUDED,
    min_score: float | None = None,
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
    embedding = pack(encode_query(load_model(model_id, cfg.embed.device), query, model_id))

    where, params = _filter_clause(origin, since, until, source, exclude_categories)
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

    floor = _floor("vector") if min_score is None else min_score
    best: dict[int, Hit] = {}
    for hit in hits:
        if hit.score < floor:
            continue
        if hit.doc_id not in best or hit.score > best[hit.doc_id].score:
            best[hit.doc_id] = hit
    return sorted(best.values(), key=lambda h: (-h.score, h.doc_date))[:limit]


def reciprocal_rank_fusion(
    rankings: list[list[Hit]],
    *,
    k: int = RRF_K,
    limit: int = DEFAULT_LIMIT,
    weights: list[float] | None = None,
) -> list[Hit]:
    """Fuse ranked lists by w/(k + rank), summed per document.

    RRF uses only positions, never the underlying scores, which is the whole
    point: BM25 scores and cosine similarities are not on comparable scales and
    any attempt to weight them directly needs a calibration constant that would
    have to be re-tuned per corpus.

    Discarding magnitude has one degenerate case. When each arm returns a
    different document at rank 1, both score exactly 1/(k+1) and the tie is
    broken arbitrarily — so a search for the confirmation code "MXUFEM", which
    BM25 matches exactly at 12.35, lost to whatever the vector arm happened to
    return for a token its model has never seen. Unequal weights make ties
    impossible. The size of the difference does not matter and is not a tuning
    knob: measured on the real query set, every vector weight from 0.5 to 0.9
    gives identical recall and MRR, which is what it looks like when the only
    thing being changed is a tie.
    """
    scores: dict[int, float] = {}
    representative: dict[int, Hit] = {}
    weights = weights or [1.0] * len(rankings)

    for ranking, weight in zip(rankings, weights, strict=True):
        for rank, hit in enumerate(ranking, start=1):
            scores[hit.doc_id] = scores.get(hit.doc_id, 0.0) + weight / (k + rank)
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
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    min_score: float | None = None,
    **filters,
) -> list[Hit]:
    """BM25 and vector candidates, fused with RRF.

    Each arm retrieves deeper than the final cut so that a document ranked
    modestly by both — which is exactly what RRF is meant to promote — is still
    in both lists when they are fused.

    The confidence floor is applied to each arm *before* fusion, never to the
    fused score. RRF deliberately discards magnitude, compressing every result
    into a narrow band just above 1/(k+1), so a threshold on the fused number
    cannot distinguish a strong match from a weak one. BM25 and cosine still
    carry that information, so that is where the question gets asked.
    """
    depth = max(limit, 1) * FUSION_DEPTH
    keyword = keyword_search(conn, query, limit=depth, min_score=min_score, **filters)
    try:
        vector = vector_search(conn, query, limit=depth, min_score=min_score, **filters)
    except ValueError as exc:
        # An unembedded index should degrade to keyword search, not fail.
        import logging

        logging.getLogger(__name__).warning("hybrid falling back to keyword: %s", exc)
        return keyword[:limit]

    from muster.config import load_config

    cfg = load_config().search
    fused = reciprocal_rank_fusion(
        [keyword, vector],
        limit=limit,
        weights=[cfg.rrf_keyword_weight, cfg.rrf_vector_weight],
    )
    fused_floor = _floor("hybrid")
    if fused_floor:
        fused = [hit for hit in fused if hit.score >= fused_floor]
    return fused


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
