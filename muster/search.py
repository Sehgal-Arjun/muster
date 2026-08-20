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


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    mode: str = "keyword",
    limit: int = DEFAULT_LIMIT,
    **filters,
) -> list[Hit]:
    """Front door for retrieval. Vector and hybrid modes arrive in M7."""
    if mode == "keyword":
        return keyword_search(conn, query, limit=limit, **filters)
    raise ValueError(f"unknown search mode: {mode}")
