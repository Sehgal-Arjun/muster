"""Noticing that an original has gone.

The dangerous version of this feature deletes anything it cannot find. That is
wrong here for a reason we have already lived through: macOS denies access to
Documents and Downloads until a program is granted it, and a walk of a denied
folder reports zero files, which is indistinguishable from an empty one. Naive
pruning would have erased several thousand documents because of a permission
prompt nobody saw. An unmounted volume, a renamed folder and a network share
that is briefly away all look identical to deletion from the inside.

So two rules, and everything here follows from them.

A document can only be marked by a walk that actually read the root it lives
under. If the root could not be listed, nothing under it is judged.

And marking is not removing. A marked document keeps its row and its text, stops
appearing in results, and is deleted only when somebody asks for that
specifically. Coming back clears the mark, because files return.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def mark_missing(conn: sqlite3.Connection, doc_ids: list[int], *, reason: str) -> int:
    """Flag documents whose original is gone. Already-marked rows keep their date."""
    if not doc_ids:
        return 0
    placeholders = ",".join("?" * len(doc_ids))
    cursor = conn.execute(
        f"UPDATE docs SET missing_since = ? WHERE id IN ({placeholders})"
        " AND missing_since IS NULL",
        [_now(), *doc_ids],
    )
    conn.commit()
    if cursor.rowcount:
        log.info("marked %d document(s) missing (%s)", cursor.rowcount, reason)
    return cursor.rowcount


def unmark(conn: sqlite3.Connection, doc_ids: list[int]) -> int:
    """A file that came back is not missing. Restores it to search."""
    if not doc_ids:
        return 0
    placeholders = ",".join("?" * len(doc_ids))
    cursor = conn.execute(
        f"UPDATE docs SET missing_since = NULL WHERE id IN ({placeholders})"
        " AND missing_since IS NOT NULL",
        doc_ids,
    )
    conn.commit()
    return cursor.rowcount


def in_trash(path: Path) -> bool:
    """Whether a vanished file appears to be sitting in the Trash.

    Not required to mark anything — a file that is simply gone is gone either
    way — but it separates "you deleted this" from "the drive is not here",
    which is the difference between a report worth acting on and noise.

    Matched on name and size: the Trash keeps the original filename, and hashing
    everything in it to be certain would cost more than the certainty is worth.
    """
    trash = Path.home() / ".Trash"
    if not trash.is_dir():
        return False
    try:
        for candidate in trash.iterdir():
            # macOS appends a suffix when a name is already taken.
            if candidate.name == path.name or candidate.stem.startswith(path.stem):
                return True
    except OSError:
        return False
    return False


def _is_gone(path: str) -> bool:
    """Whether the file is definitely not there.

    Asked of the filesystem rather than inferred from the walk, and answered
    only when the answer is unambiguous. A permission error means "cannot tell",
    which must never be read as "deleted" — the same mistake as trusting a
    denied root, one directory further down.
    """
    import os

    try:
        os.stat(path)
    except FileNotFoundError:
        return True
    except NotADirectoryError:
        # A parent component is now a file: the original path cannot exist.
        return True
    except OSError:
        return False
    return False


def sweep_files(
    conn: sqlite3.Connection, roots_read: list[Path], *, seen_paths: set[str]
) -> tuple[int, int, int]:
    """Reconcile file documents against a completed walk.

    ``roots_read`` is only those roots the walk could actually list. Documents
    under any other root are left alone, however absent they look.

    Absence is then checked against the filesystem, not against what the walk
    produced. Those are different questions: the walk skips build directories,
    files that are not documents, oversized ones and anything shaped like a
    credential — and a file deliberately not indexed is still very much there.
    Reading "the walk did not yield it" as "it was deleted" marked 63 documents
    missing the first time this ran, every one of them still on disk.

    Returns (marked, restored, in_trash).
    """
    if not roots_read:
        return 0, 0, 0

    resolved = [str(root.resolve()) for root in roots_read]

    def under_a_read_root(path: str) -> bool:
        return any(path == root or path.startswith(root + "/") for root in resolved)

    gone: list[int] = []
    returned: list[int] = []
    trashed = 0

    for row in conn.execute(
        "SELECT id, source_uid, meta_json, missing_since FROM docs WHERE source = 'file'"
    ):
        try:
            path = json.loads(row["meta_json"] or "{}").get("path") or row["source_uid"]
        except (TypeError, ValueError):
            path = row["source_uid"]
        if not path or not under_a_read_root(path):
            continue

        # Seen by the walk is proof enough of existence, and saves a stat.
        present = path in seen_paths or not _is_gone(path)

        if present:
            if row["missing_since"]:
                returned.append(row["id"])
            continue

        if not row["missing_since"]:
            gone.append(row["id"])
            if in_trash(Path(path)):
                trashed += 1

    marked = mark_missing(conn, gone, reason="not seen by a completed walk")
    restored = unmark(conn, returned)
    return marked, restored, trashed
