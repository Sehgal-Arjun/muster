"""Retrieval evaluation.

A change to chunking, quote stripping or ranking either moves these numbers or
it does not. Without that, every future decision about retrieval is taste.

Queries live in ``eval/queries.jsonl``, one JSON object per line::

    {"q": "...", "relevant_doc_ids": [...], "note": "..."}

``relevant_doc_ids`` holds strings only. Row ids are rejected outright: they
change whenever the index is rebuilt, and a query file that silently rots is
worse than no query file. Three forms are accepted:

    "gmail:19fe48042d9ff694"   source-qualified, the unambiguous form
    "19fe48042d9ff694"         bare source_uid, if unique across sources
    "sha256:<content_hash>"    the raw bytes, stable even if uids change

Runs are scored against a pinned corpus so numbers are comparable over time.
``muster corpus snapshot`` freezes the index; ``muster eval --corpus`` scores
against the snapshot, and every results file records the corpus fingerprint it
was measured on.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from muster.search import search

log = logging.getLogger(__name__)

DEFAULT_K = 10


@dataclass
class QueryRecord:
    q: str
    relevant_doc_ids: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class QueryResult:
    q: str
    note: str
    k: int
    relevant: list[int]
    unresolved: list[str]
    retrieved: list[int]
    hits: list[int]
    recall_at_k: float
    reciprocal_rank: float
    first_relevant_rank: int | None
    titles: list[str]


@dataclass
class Corpus:
    """What the numbers were measured on. Without this they mean nothing."""

    path: str
    fingerprint: str
    docs: int
    chunks: int
    vectors: int
    excluded_categories: list[str]


@dataclass
class EvalRun:
    mode: str
    k: int
    started_at: str
    queries_total: int
    queries_scored: int
    recall_at_k: float
    mrr: float
    results: list[QueryResult]
    corpus: Corpus

    def to_json(self) -> str:
        payload = asdict(self)
        return json.dumps(payload, indent=2, ensure_ascii=False)


class EvalError(RuntimeError):
    pass


def load_queries(path: Path) -> list[QueryRecord]:
    path = Path(path)
    if not path.exists():
        raise EvalError(f"no query file at {path}")

    records: list[QueryRecord] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("//") or line.startswith("#"):
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvalError(f"{path}:{lineno}: not valid JSON ({exc})") from exc
        if not isinstance(raw, dict) or "q" not in raw:
            raise EvalError(f"{path}:{lineno}: every record needs a 'q' field")

        ids = list(raw.get("relevant_doc_ids") or [])
        numeric = [i for i in ids if isinstance(i, int) and not isinstance(i, bool)]
        if numeric:
            raise EvalError(
                f"{path}:{lineno}: relevant_doc_ids must be strings, got row ids "
                f"{numeric}. Row ids change when the index is rebuilt — use a "
                f"source_uid ('gmail:19fe48...') or 'sha256:<content_hash>'."
            )
        records.append(
            QueryRecord(
                q=str(raw["q"]),
                relevant_doc_ids=[str(i) for i in ids],
                note=str(raw.get("note", "")),
            )
        )
    return records


KNOWN_SOURCES = ("gmail", "file", "outlook", "imap")


def _lookup(conn: sqlite3.Connection, value: str) -> list[int]:
    """Every doc a query-file identifier could mean."""
    if value.startswith("sha256:"):
        rows = conn.execute(
            "SELECT id FROM docs WHERE content_hash = ?", (value[len("sha256:") :],)
        ).fetchall()
        return [int(r[0]) for r in rows]

    source, _, uid = value.partition(":")
    if uid and source in KNOWN_SOURCES:
        rows = conn.execute(
            "SELECT id FROM docs WHERE source = ? AND source_uid = ?", (source, uid)
        ).fetchall()
        return [int(r[0]) for r in rows]

    rows = conn.execute("SELECT id FROM docs WHERE source_uid = ?", (value,)).fetchall()
    return [int(r[0]) for r in rows]


def resolve_relevant(
    conn: sqlite3.Connection, ids: list[str]
) -> tuple[list[int], list[str]]:
    """Map identifiers to live doc ids, reporting the ones that do not resolve."""
    resolved: list[int] = []
    unresolved: list[str] = []
    for value in ids:
        matches = _lookup(conn, str(value))
        if not matches:
            unresolved.append(str(value))
        elif len(matches) > 1:
            # Ambiguity is a bug in the query file, not a retrieval result.
            unresolved.append(f"{value} (ambiguous: {len(matches)} docs)")
        else:
            resolved.append(matches[0])
    return resolved, unresolved


def corpus_fingerprint(conn: sqlite3.Connection) -> str:
    """Identity of the indexed set, independent of row ids and file layout.

    Hashes the captured documents' (source, source_uid, content_hash) triples in
    sorted order. Two indexes holding the same mail fingerprint identically even
    if they were built in a different order — and a fingerprint that changes
    between runs is the signal that the numbers are not comparable.
    """
    digest = hashlib.sha256()
    rows = conn.execute(
        "SELECT source, source_uid, content_hash FROM docs"
        " WHERE origin = 'captured' ORDER BY source, source_uid"
    )
    for source, source_uid, content_hash in rows:
        digest.update(f"{source}\x1f{source_uid}\x1f{content_hash}\x1e".encode())
    return digest.hexdigest()[:16]


def describe_corpus(
    conn: sqlite3.Connection, path: str, excluded: frozenset[str] | None = None
) -> Corpus:
    def count(sql: str) -> int:
        return conn.execute(sql).fetchone()[0]

    return Corpus(
        path=str(path),
        fingerprint=corpus_fingerprint(conn),
        docs=count("SELECT count(*) FROM docs WHERE origin='captured'"),
        chunks=count("SELECT count(*) FROM chunks"),
        vectors=count("SELECT count(*) FROM vectors"),
        excluded_categories=sorted(excluded or ()),
    )


def snapshot(conn: sqlite3.Connection, destination: Path) -> Path:
    """Freeze the index to a standalone file using SQLite's backup API."""
    destination = Path(destination).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(destination)
    try:
        conn.backup(target)
    finally:
        target.close()
    return destination


def evaluate(
    conn: sqlite3.Connection,
    records: list[QueryRecord],
    *,
    mode: str = "keyword",
    k: int = DEFAULT_K,
    corpus_path: str = "",
    exclude_categories: frozenset[str] | None = None,
) -> EvalRun:
    results: list[QueryResult] = []
    recalls: list[float] = []
    rrs: list[float] = []

    for record in records:
        relevant, unresolved = resolve_relevant(conn, record.relevant_doc_ids)
        hits = search(
            conn, record.q, mode=mode, limit=k,
            **({} if exclude_categories is None else {"exclude_categories": exclude_categories}),
        )
        retrieved = [h.doc_id for h in hits]

        relevant_set = set(relevant)
        found = [d for d in retrieved if d in relevant_set]

        first_rank = next(
            (i for i, doc_id in enumerate(retrieved, start=1) if doc_id in relevant_set), None
        )
        recall = len(set(found)) / len(relevant_set) if relevant_set else 0.0
        rr = 1.0 / first_rank if first_rank else 0.0

        # A query with no resolvable ground truth is unscored, not scored zero:
        # averaging in a zero for an unfilled record would flatter or damn the
        # index for a reason that has nothing to do with retrieval.
        if relevant_set:
            recalls.append(recall)
            rrs.append(rr)
        elif record.relevant_doc_ids:
            log.warning("query %r: none of its relevant ids exist in the index", record.q)

        results.append(
            QueryResult(
                q=record.q,
                note=record.note,
                k=k,
                relevant=relevant,
                unresolved=unresolved,
                retrieved=retrieved,
                hits=found,
                recall_at_k=round(recall, 4),
                reciprocal_rank=round(rr, 4),
                first_relevant_rank=first_rank,
                titles=[(h.title or "")[:80] for h in hits],
            )
        )

    return EvalRun(
        mode=mode,
        k=k,
        started_at=datetime.now(UTC).isoformat(),
        queries_total=len(records),
        queries_scored=len(recalls),
        recall_at_k=round(sum(recalls) / len(recalls), 4) if recalls else 0.0,
        mrr=round(sum(rrs) / len(rrs), 4) if rrs else 0.0,
        results=results,
        corpus=describe_corpus(conn, corpus_path, exclude_categories),
    )


def write_run(run: EvalRun, results_dir: Path) -> Path:
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    stamp = run.started_at.replace(":", "").replace("-", "").split(".")[0]
    path = results_dir / f"{stamp}-{run.mode}-k{run.k}-{run.corpus.fingerprint}.json"
    path.write_text(run.to_json(), encoding="utf-8")
    return path
