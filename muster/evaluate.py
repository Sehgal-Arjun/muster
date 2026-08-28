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

**The real query set is private.** ``eval/queries.jsonl`` names real senders,
subjects and message ids, and is gitignored; only ``eval/queries.example.jsonl``
is committed, and it is the fallback when no local set exists. Results written
to disk are redacted for the same reason: a query and the titles it retrieved
are as revealing as the mailbox itself. Each query is identified by a stable
hash so runs stay diffable without the text being written anywhere.
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

DEFAULT_QUERIES = Path("eval/queries.jsonl")
EXAMPLE_QUERIES = Path("eval/queries.example.jsonl")


def query_id(text: str) -> str:
    """A stable identifier for a query that reveals nothing about it."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def resolve_queries_path(explicit: Path | None = None) -> tuple[Path, bool]:
    """(path, is_example). The private set if present, else the committed example."""
    if explicit is not None:
        path = Path(explicit)
        if not path.exists():
            raise EvalError(f"no query file at {path}")
        return path, path.name == EXAMPLE_QUERIES.name
    if DEFAULT_QUERIES.exists():
        return DEFAULT_QUERIES, False
    if EXAMPLE_QUERIES.exists():
        return EXAMPLE_QUERIES, True
    raise EvalError(
        f"no query file at {DEFAULT_QUERIES} and no {EXAMPLE_QUERIES} to fall back on"
    )


@dataclass
class QueryRecord:
    q: str
    relevant_doc_ids: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class QueryResult:
    query_id: str
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

    def to_json(self, *, include_text: bool = False) -> str:
        """Serialise a run.

        Query text, notes and retrieved titles are dropped unless explicitly
        requested: they are the contents of a personal mailbox, and a results
        file is exactly the kind of artefact that gets committed by accident.
        Metrics and the stable query_id survive, so two runs remain diffable.
        """
        payload = asdict(self)
        payload["redacted"] = not include_text
        if not include_text:
            for result in payload["results"]:
                for field_name in ("q", "note", "titles"):
                    result.pop(field_name, None)
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


def _lookup(conn: sqlite3.Connection, value: str) -> tuple[list[int], bool]:
    """(matching doc ids, whether several matches are expected).

    A content hash naming several documents is not ambiguity: identical bytes
    are the same document, stored more than once. One offer-letter PDF in this
    archive is attached to four messages under three filenames. A source_uid
    matching twice, by contrast, is a broken query file.
    """
    if value.startswith("sha256:"):
        rows = conn.execute(
            "SELECT id FROM docs WHERE content_hash = ?", (value[len("sha256:") :],)
        ).fetchall()
        return [int(r[0]) for r in rows], True

    source, _, uid = value.partition(":")
    if uid and source in KNOWN_SOURCES:
        rows = conn.execute(
            "SELECT id FROM docs WHERE source = ? AND source_uid = ?", (source, uid)
        ).fetchall()
        return [int(r[0]) for r in rows], False

    rows = conn.execute("SELECT id FROM docs WHERE source_uid = ?", (value,)).fetchall()
    return [int(r[0]) for r in rows], False


def resolve_relevant(
    conn: sqlite3.Connection, ids: list[str]
) -> tuple[list[list[int]], list[str]]:
    """Map identifiers to groups of doc ids, one group per label.

    A group is satisfied by retrieving *any* of its members, which is what makes
    a content-hash label behave sensibly: four copies of one PDF are one thing
    to find, not four. Scoring a flat list would demand all four and mark a
    perfect answer as 25% recall — and results are deduplicated by content hash
    anyway, so returning all four is impossible by construction.
    """
    groups: list[list[int]] = []
    unresolved: list[str] = []
    for value in ids:
        matches, many_expected = _lookup(conn, str(value))
        if not matches:
            unresolved.append(str(value))
        elif len(matches) > 1 and not many_expected:
            # Ambiguity is a bug in the query file, not a retrieval result.
            unresolved.append(f"{value} (ambiguous: {len(matches)} docs)")
        else:
            groups.append(sorted(matches))
    return groups, unresolved


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


@dataclass
class VerifyReport:
    """Ground-truth health, checked before any retrieval runs."""

    queries: int
    resolvable: int
    unscoreable: list[tuple[str, list[str]]]   # query, all of its ids
    partial: list[tuple[str, list[str]]]       # query, the ids that failed
    duplicates: list[str]
    total_ids: int
    bad_ids: int

    @property
    def clean(self) -> bool:
        return not self.unscoreable and not self.partial and not self.duplicates


def verify(conn: sqlite3.Connection, records: list[QueryRecord]) -> VerifyReport:
    """Check every relevant_doc_id against the index without searching.

    Ground truth written by something other than the person who owns the
    mailbox can name documents that do not exist. A query whose ids all fail is
    silently skipped by the scorer, so a query set can rot down to a handful of
    scored records while still reporting an average — which looks like a result
    and is not one.
    """
    unscoreable: list[tuple[str, list[str]]] = []
    partial: list[tuple[str, list[str]]] = []
    seen: dict[str, int] = {}
    total_ids = bad_ids = resolvable = 0

    for record in records:
        resolved, unresolved = resolve_relevant(conn, record.relevant_doc_ids)
        total_ids += len(record.relevant_doc_ids)
        bad_ids += len(unresolved)
        for value in record.relevant_doc_ids:
            seen[value] = seen.get(value, 0) + 1
        if resolved:
            resolvable += 1
            if unresolved:
                partial.append((record.q, unresolved))
        else:
            unscoreable.append((record.q, list(record.relevant_doc_ids)))

    duplicates = sorted(value for value, count in seen.items() if count > 1)
    return VerifyReport(
        queries=len(records),
        resolvable=resolvable,
        unscoreable=unscoreable,
        partial=partial,
        duplicates=duplicates,
        total_ids=total_ids,
        bad_ids=bad_ids,
    )


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
        groups, unresolved = resolve_relevant(conn, record.relevant_doc_ids)
        relevant = sorted({doc_id for group in groups for doc_id in group})
        hits = search(
            conn, record.q, mode=mode, limit=k,
            **({} if exclude_categories is None else {"exclude_categories": exclude_categories}),
        )
        retrieved = [h.doc_id for h in hits]
        retrieved_set = set(retrieved)

        relevant_set = set(relevant)
        found = [d for d in retrieved if d in relevant_set]

        # Recall counts groups, not documents: one label is one thing to find.
        satisfied = [g for g in groups if retrieved_set.intersection(g)]
        first_rank = next(
            (i for i, doc_id in enumerate(retrieved, start=1) if doc_id in relevant_set), None
        )
        recall = len(satisfied) / len(groups) if groups else 0.0
        rr = 1.0 / first_rank if first_rank else 0.0

        # A query with no resolvable ground truth is unscored, not scored zero:
        # averaging in a zero for an unfilled record would flatter or damn the
        # index for a reason that has nothing to do with retrieval.
        if groups:
            recalls.append(recall)
            rrs.append(rr)
        elif record.relevant_doc_ids:
            log.warning("query %r: none of its relevant ids exist in the index", record.q)

        results.append(
            QueryResult(
                query_id=query_id(record.q),
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


def write_run(run: EvalRun, results_dir: Path, *, include_text: bool = False) -> Path:
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    stamp = run.started_at.replace(":", "").replace("-", "").split(".")[0]
    path = results_dir / f"{stamp}-{run.mode}-k{run.k}-{run.corpus.fingerprint}.json"
    path.write_text(run.to_json(include_text=include_text), encoding="utf-8")
    return path
