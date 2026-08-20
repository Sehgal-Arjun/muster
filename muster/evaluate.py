"""Retrieval evaluation.

A change to chunking, quote stripping or ranking either moves these numbers or
it does not. Without that, every future decision about retrieval is taste.

Queries live in ``eval/queries.jsonl``, one JSON object per line::

    {"q": "...", "relevant_doc_ids": [...], "note": "..."}

``relevant_doc_ids`` accepts integers (matched against ``docs.id``) or strings
(matched against ``docs.source_uid``). Prefer strings: row ids change when the
index is rebuilt, and a query file that silently rots is worse than none.
"""

from __future__ import annotations

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
    relevant_doc_ids: list[int | str] = field(default_factory=list)
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
class EvalRun:
    mode: str
    k: int
    started_at: str
    queries_total: int
    queries_scored: int
    recall_at_k: float
    mrr: float
    results: list[QueryResult]
    corpus: dict[str, int]

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
        records.append(
            QueryRecord(
                q=str(raw["q"]),
                relevant_doc_ids=list(raw.get("relevant_doc_ids") or []),
                note=str(raw.get("note", "")),
            )
        )
    return records


def resolve_relevant(
    conn: sqlite3.Connection, ids: list[int | str]
) -> tuple[list[int], list[str]]:
    """Map a record's ids to live doc ids, reporting the ones that do not exist."""
    resolved: list[int] = []
    unresolved: list[str] = []
    for value in ids:
        if isinstance(value, bool):
            unresolved.append(repr(value))
        elif isinstance(value, int):
            row = conn.execute("SELECT id FROM docs WHERE id = ?", (value,)).fetchone()
            (resolved if row else unresolved).append(value if row else str(value))
        else:
            row = conn.execute(
                "SELECT id FROM docs WHERE source_uid = ?", (str(value),)
            ).fetchone()
            if row:
                resolved.append(int(row[0]))
            else:
                unresolved.append(str(value))
    return resolved, unresolved


def evaluate(
    conn: sqlite3.Connection,
    records: list[QueryRecord],
    *,
    mode: str = "keyword",
    k: int = DEFAULT_K,
) -> EvalRun:
    results: list[QueryResult] = []
    recalls: list[float] = []
    rrs: list[float] = []

    for record in records:
        relevant, unresolved = resolve_relevant(conn, record.relevant_doc_ids)
        hits = search(conn, record.q, mode=mode, limit=k)
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

    corpus = {
        "docs": conn.execute("SELECT count(*) FROM docs WHERE origin='captured'").fetchone()[0],
        "chunks": conn.execute("SELECT count(*) FROM chunks").fetchone()[0],
        "vectors": conn.execute("SELECT count(*) FROM vectors").fetchone()[0],
    }

    return EvalRun(
        mode=mode,
        k=k,
        started_at=datetime.now(UTC).isoformat(),
        queries_total=len(records),
        queries_scored=len(recalls),
        recall_at_k=round(sum(recalls) / len(recalls), 4) if recalls else 0.0,
        mrr=round(sum(rrs) / len(rrs), 4) if rrs else 0.0,
        results=results,
        corpus=corpus,
    )


def write_run(run: EvalRun, results_dir: Path) -> Path:
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    stamp = run.started_at.replace(":", "").replace("-", "").split(".")[0]
    path = results_dir / f"{stamp}-{run.mode}-k{run.k}.json"
    path.write_text(run.to_json(), encoding="utf-8")
    return path
