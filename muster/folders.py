"""A map of directories, so a search can offer to take you to one.

Searching for "muster" and being shown the emails that mention it, when what
you wanted was the folder, is a small daily annoyance. But folders have no
content and cannot be ranked against documents honestly — a directory named
"invoices" is not more or less relevant than an invoice, it is an answer to a
different question. So they are kept apart: never mixed into results, offered as
one suggestion beside them, and reachable with a key.

Names only. What is inside a directory is the file connector's business.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

# Deep trees are almost never what someone means by "take me to the folder",
# and the cost of scanning grows with every level.
DEFAULT_MAX_DEPTH = 5


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


def scan(
    roots: tuple[str, ...],
    excluded: set[str],
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> list[Folder]:
    """Every directory worth offering, breadth-limited and noise-free."""
    found: list[Folder] = []
    for root in roots:
        base = Path(root).expanduser()
        if not base.is_dir():
            continue
        try:
            os.listdir(base)
        except OSError:
            # Denied or gone. Reported by the file walk already; silence here.
            continue

        base_depth = len(base.parts)
        for dirpath, dirnames, _files in os.walk(base, followlinks=False):
            here = Path(dirpath)
            depth = len(here.parts) - base_depth
            if depth >= max_depth:
                dirnames.clear()
                continue
            # Pruned in place so os.walk never descends into them.
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
                found.append(
                    Folder(
                        path=str(path),
                        name=name,
                        depth=depth + 1,
                        mtime=stamp.isoformat(timespec="seconds") if stamp else None,
                    )
                )
    return found


def replace_all(conn, folders: list[Folder]) -> int:
    """Swap in a fresh map. Directories move and vanish; a diff would not be simpler."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with conn:
        conn.execute("DELETE FROM folders")
        conn.executemany(
            "INSERT OR REPLACE INTO folders (path, name, depth, mtime, seen_at)"
            " VALUES (?, ?, ?, ?, ?)",
            [(f.path, f.name, f.depth, f.mtime, now) for f in folders],
        )
    return len(folders)


def suggest(conn, query: str, limit: int = 3) -> list[Folder]:
    """Directories a query might have meant.

    Only for queries that look like a name. A sentence is a question about
    content, and answering it with a folder would be noise — which is the whole
    reason this is separate from search.
    """
    words = [w for w in query.strip().lower().split() if w]
    if not words or len(words) > 3:
        return []
    needle = " ".join(words)
    compact = needle.replace(" ", "")

    rows = conn.execute(
        """
        SELECT path, name, depth, mtime,
               CASE
                   WHEN lower(name) = :needle THEN 0
                   WHEN lower(name) = :compact THEN 1
                   WHEN lower(name) LIKE :prefix THEN 2
                   ELSE 3
               END AS rank
        FROM folders
        WHERE lower(name) = :needle
           OR lower(name) = :compact
           OR lower(name) LIKE :prefix
           OR lower(name) LIKE :anywhere
        -- Exactness first, then the shallower path, then the one touched most
        -- recently: of two folders called "muster", the live one is wanted.
        ORDER BY rank ASC, depth ASC, mtime DESC
        LIMIT :limit
        """,
        {
            "needle": needle,
            "compact": compact,
            "prefix": f"{needle}%",
            "anywhere": f"%{needle}%",
            "limit": limit,
        },
    ).fetchall()
    return [Folder(path=r[0], name=r[1], depth=r[2], mtime=r[3]) for r in rows]
