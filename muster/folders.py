"""A map of directories, so a search can offer to take you to one.

Searching for "muster" and being shown the emails that mention it, when what you
wanted was the folder, is a small daily annoyance. But folders have no content
and cannot be ranked against documents honestly — a directory named "invoices"
is not more or less relevant than an invoice, it is an answer to a different
question. So they are kept apart: never mixed into results, offered as one
suggestion beside them, and reachable with a key.

Matching is on words, not substrings, and over the whole path rather than the
basename. Three things forced that. "MATH213" is one word on disk and "math 213"
is two as typed. A folder called "Sample Exams" is reached by naming the course
above it far more often than by naming itself. And nobody remembers whether they
wrote "past exams" or "exams past" — the words are the memory, the order is not.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

# Deep trees are almost never what someone means by "take me to the folder",
# and the cost of scanning grows with every level.
DEFAULT_MAX_DEPTH = 5

# Long enough to name a folder and its course, short enough that a sentence is
# still treated as a question about content rather than a place.
MAX_QUERY_WORDS = 6

_SPLIT = re.compile(r"[^0-9A-Za-z]+")
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")
_ALPHANUM = re.compile(r"(?<=[A-Za-z])(?=[0-9])|(?<=[0-9])(?=[A-Za-z])")


def words_of(text: str) -> list[str]:
    """Every way a reader might have typed a path segment.

    "MATH213" yields math, 213 and math213, because it is one word on disk and
    two as typed and there is no telling which the reader will use.
    """
    out: list[str] = []
    for piece in _SPLIT.split(text):
        if not piece:
            continue
        out.append(piece)
        for part in _CAMEL.sub(" ", piece).split():
            for atom in _ALPHANUM.sub(" ", part).split():
                out.append(atom)
    # Order-preserving dedupe, case-folded: FTS does its own folding.
    seen: set[str] = set()
    unique = []
    for word in out:
        low = word.lower()
        if low not in seen:
            seen.add(low)
            unique.append(word)
    return unique


@dataclass(frozen=True)
class Folder:
    path: str
    name: str
    depth: int
    mtime: str | None

    @property
    def display(self) -> str:
        """Written the way a person would say it: ~/GitHub/muster."""
        home = str(Path.home())
        return self.path.replace(home, "~", 1) if self.path.startswith(home) else self.path


@dataclass(frozen=True)
class Scanned:
    folder: Folder
    name_text: str
    path_text: str

    @property
    def meaning_text(self) -> str:
        """The folder written as a phrase, for embedding.

        Segments joined the way a path reads, because a model does better with
        "Waterloo / 3A / MATH213 / Sample Exams" than with a bag of tokens.
        """
        parts = [p for p in (self.path_text, self.name_text) if p]
        return " / ".join(parts)


def scan(
    roots: tuple[str, ...],
    excluded: set[str],
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> list[Scanned]:
    """Every directory worth offering, breadth-limited and noise-free."""
    found: list[Scanned] = []
    for root in roots:
        base = Path(root).expanduser()
        if not base.is_dir():
            continue
        try:
            os.listdir(base)
        except OSError:
            # Denied or gone. The file walk reports that already; silence here.
            continue

        base_depth = len(base.parts)
        for dirpath, dirnames, _files in os.walk(base, followlinks=False):
            here = Path(dirpath)
            depth = len(here.parts) - base_depth
            if depth >= max_depth:
                dirnames.clear()
                continue
            dirnames[:] = [
                d for d in dirnames
                if d.lower() not in excluded and not d.startswith(".")
            ]
            for name in dirnames:
                path = here / name
                try:
                    stamp = datetime.fromtimestamp(path.stat().st_mtime, UTC)
                except OSError:
                    stamp = None
                # Ancestors below the root only: every folder shares the root's
                # own segments, so indexing them would match everything equally.
                ancestors = here.parts[base_depth:]
                found.append(
                    Scanned(
                        folder=Folder(
                            path=str(path),
                            name=name,
                            depth=depth + 1,
                            mtime=stamp.isoformat(timespec="seconds") if stamp else None,
                        ),
                        name_text=" ".join(words_of(name)),
                        path_text=" ".join(
                            word for part in ancestors for word in words_of(part)
                        ),
                    )
                )
    return found


def replace_all(conn, scanned: list[Scanned]) -> int:
    """Swap in a fresh map. Directories move and vanish; a diff would not be simpler."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with conn:
        conn.execute("DELETE FROM folders_fts")
        conn.execute("DELETE FROM folders")
        conn.executemany(
            "INSERT INTO folders (path, name, name_text, path_text, depth, mtime,"
            " seen_at, meaning_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (s.folder.path, s.folder.name, s.name_text, s.path_text,
                 s.folder.depth, s.folder.mtime, now, text_hash(s.meaning_text))
                for s in scanned
            ],
        )
        # External content: the index is filled from the table it mirrors.
        conn.execute(
            "INSERT INTO folders_fts (rowid, name_text, path_text)"
            " SELECT id, name_text, path_text FROM folders"
        )
    return len(scanned)


def text_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pending_vectors(conn, model_id: str) -> list[tuple[str, str]]:
    """(hash, text) for folder names with no vector yet, deduplicated."""
    rows = conn.execute(
        """
        SELECT DISTINCT f.meaning_hash, f.path_text, f.name_text
        FROM folders f
        WHERE f.meaning_hash IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM folder_vectors v
              WHERE v.text_hash = f.meaning_hash AND v.model_id = ?
          )
        """,
        (model_id,),
    ).fetchall()
    return [
        (h, " / ".join(p for p in (path_text, name_text) if p))
        for h, path_text, name_text in rows
    ]


def store_vectors(conn, model_id: str, rows: list[tuple[str, bytes]]) -> int:
    if not rows:
        return 0
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO folder_vectors (text_hash, model_id, embedding)"
            " VALUES (?, ?, ?)",
            [(h, model_id, blob) for h, blob in rows],
        )
    return len(rows)


def _match_query(words: list[str]) -> str:
    """Every word must appear, each allowed to be half-typed.

    AND rather than OR: a query is a description of one folder, and matching any
    single word of it would offer whatever happens to share a common term.
    """
    return " AND ".join(f'"{word}"*' for word in words)


# The k in 1/(k + rank), matching the document ranker. Not tuned here either.
RRF_K = 60


def _word_arm(conn, words: list[str], depth: int) -> list[tuple[int, Folder]]:
    """Folders whose path contains every word typed."""
    needle = " ".join(words).lower()
    try:
        rows = conn.execute(
            """
            SELECT f.id, f.path, f.name, f.depth, f.mtime
            FROM folders_fts
            JOIN folders f ON f.id = folders_fts.rowid
            WHERE folders_fts MATCH :match
            ORDER BY
                CASE
                    WHEN lower(f.name) = :needle THEN 0
                    WHEN lower(f.name) LIKE :prefix THEN 1
                    ELSE 2
                END,
                bm25(folders_fts, 8.0, 1.0),
                f.depth ASC,
                f.mtime DESC
            LIMIT :limit
            """,
            {
                "match": _match_query(words),
                "needle": needle,
                "prefix": f"{needle}%",
                "limit": depth,
            },
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 — a malformed query is not an error
        log.debug("folder word match failed for %r: %s", words, exc)
        return []
    return [(r[0], Folder(path=r[1], name=r[2], depth=r[3], mtime=r[4])) for r in rows]


def _meaning_arm(conn, query: str, model_id: str, depth: int) -> list[tuple[int, Folder]]:
    """Folders that mean something close to the query.

    This is what knows a "Sample Test" is a "Sample Exams". Word matching sees
    two different strings; a reader sees one folder.
    """
    from muster.db import load_sqlite_vec
    from muster.embed import encode_query, load_model, pack

    try:
        load_sqlite_vec(conn)
        if not conn.execute(
            "SELECT 1 FROM folder_vectors WHERE model_id = ? LIMIT 1", (model_id,)
        ).fetchone():
            return []  # nothing embedded yet; the word arm stands alone
        embedding = pack(encode_query(load_model(model_id), query, model_id))
        rows = conn.execute(
            """
            SELECT f.id, f.path, f.name, f.depth, f.mtime
            FROM folders f
            JOIN folder_vectors v
              ON v.text_hash = f.meaning_hash AND v.model_id = :model
            ORDER BY vec_distance_cosine(v.embedding, :embedding) ASC
            LIMIT :limit
            """,
            {"model": model_id, "embedding": embedding, "limit": depth},
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 — no model, no vec, no vectors
        log.debug("folder meaning match unavailable: %s", exc)
        return []
    return [(r[0], Folder(path=r[1], name=r[2], depth=r[3], mtime=r[4])) for r in rows]


def _fuse(arms: list[list[tuple[int, Folder]]], limit: int) -> list[Folder]:
    """Reciprocal rank fusion, as the document ranker does it.

    Positions only. A BM25 score and a cosine distance are not on comparable
    scales, and weighting them directly would need a constant nobody can
    justify.
    """
    scores: dict[int, float] = {}
    seen: dict[int, Folder] = {}
    for arm in arms:
        for rank, (folder_id, folder) in enumerate(arm, start=1):
            scores[folder_id] = scores.get(folder_id, 0.0) + 1.0 / (RRF_K + rank)
            seen.setdefault(folder_id, folder)
    ordered = sorted(scores, key=lambda i: (-scores[i], seen[i].depth))
    return [seen[i] for i in ordered[:limit]]


def word_matches(conn, words: list[str], depth: int) -> list[tuple[int, Folder]]:
    """Folders whose path contains every word given. Public for `placing`."""
    return _word_arm(conn, words, depth)


def meaning_matches(conn, text: str, model_id: str, depth: int) -> list[tuple[int, Folder]]:
    """Folders that mean something close to `text`. Public for `placing`."""
    return _meaning_arm(conn, text, model_id, depth)


def fuse(arms: list[list[tuple[int, Folder]]], limit: int) -> list[Folder]:
    """Reciprocal rank fusion over folder arms. Public for `placing`."""
    return _fuse(arms, limit)


def suggest(conn, query: str, limit: int = 3, model_id: str | None = None) -> list[Folder]:
    """Directories a query might have meant, best first.

    Only for queries that look like a name. A sentence is a question about
    content, and answering it with a folder would be noise — which is the whole
    reason this is separate from search.
    """
    words = [w for w in _SPLIT.split(query.strip()) if w]
    if not words or len(words) > MAX_QUERY_WORDS:
        return []

    # Deeper than the limit so fusion has something to work with: a folder the
    # word arm ranks fourth can still win once the meaning arm agrees.
    depth = max(limit * 4, 12)
    arms = [_word_arm(conn, words, depth)]
    if model_id:
        meaning = _meaning_arm(conn, query, model_id, depth)
        if meaning:
            arms.append(meaning)
    return _fuse(arms, limit)
