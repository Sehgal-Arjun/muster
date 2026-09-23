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
            "INSERT INTO folders (path, name, name_text, path_text, depth, mtime, seen_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (s.folder.path, s.folder.name, s.name_text, s.path_text,
                 s.folder.depth, s.folder.mtime, now)
                for s in scanned
            ],
        )
        # External content: the index is filled from the table it mirrors.
        conn.execute(
            "INSERT INTO folders_fts (rowid, name_text, path_text)"
            " SELECT id, name_text, path_text FROM folders"
        )
    return len(scanned)


def _match_query(words: list[str]) -> str:
    """Every word must appear, each allowed to be half-typed.

    AND rather than OR: a query is a description of one folder, and matching any
    single word of it would offer whatever happens to share a common term.
    """
    return " AND ".join(f'"{word}"*' for word in words)


def suggest(conn, query: str, limit: int = 3) -> list[Folder]:
    """Directories a query might have meant, best first.

    Only for queries that look like a name. A sentence is a question about
    content, and answering it with a folder would be noise — which is the whole
    reason this is separate from search.
    """
    words = [w for w in _SPLIT.split(query.strip()) if w]
    if not words or len(words) > MAX_QUERY_WORDS:
        return []

    needle = " ".join(words).lower()
    try:
        rows = conn.execute(
            """
            SELECT f.path, f.name, f.depth, f.mtime
            FROM folders_fts
            JOIN folders f ON f.id = folders_fts.rowid
            WHERE folders_fts MATCH :match
            ORDER BY
                -- An exact or leading match on the folder's own name is what
                -- somebody typing a name meant; everything else is inference.
                CASE
                    WHEN lower(f.name) = :needle THEN 0
                    WHEN lower(f.name) LIKE :prefix THEN 1
                    ELSE 2
                END,
                -- Then BM25, with the name weighted well above the ancestors it
                -- happens to sit under.
                bm25(folders_fts, 8.0, 1.0),
                f.depth ASC,
                f.mtime DESC
            LIMIT :limit
            """,
            {
                "match": _match_query(words),
                "needle": needle,
                "prefix": f"{needle}%",
                "limit": limit,
            },
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 — a malformed query is not an error
        log.debug("folder suggestion failed for %r: %s", query, exc)
        return []

    return [Folder(path=r[0], name=r[1], depth=r[2], mtime=r[3]) for r in rows]
