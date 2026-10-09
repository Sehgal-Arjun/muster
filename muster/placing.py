"""Where a file belongs, and moving it there.

Something arrives in Downloads with a name like `Arjun Sehgal-6193.pdf` and stays
there for a year, because deciding where it goes means remembering what is already
on disk. That is the one thing an index of the filesystem is good at.

Three kinds of evidence, all of them explainable, because a suggestion you cannot
argue with is one you cannot trust:

- **the name** — "MATH213 midterm.pdf" says most of it, and folder names are
  matched on words over the whole path, so a course above a folder counts
- **what the file says** — its text against each folder's name as a phrase, which
  is what knows a "Sample Test" belongs with "Sample Exams"
- **its neighbours** — folders that already hold files of the same kind. The only
  arm that is pure counting, and often the most convincing: "four other PDFs live
  here" is a fact about your filing, not an opinion about the file

Then the count of suggestions is decided by the evidence rather than fixed: one
when one is clearly ahead, several when several are close, none when nothing is.

This module contains the only code in Muster that writes to your files. It moves
nothing without being told to, refuses to overwrite anything, and records what it
did so it can be undone.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from muster import folders

log = logging.getLogger(__name__)

# How much of a file's text is worth reading to place it. The first page says
# what a document is; the rest says what it contains.
TEXT_BUDGET = 1500

# A candidate has to be at least this fraction of the leader's score to be worth
# showing. Below it, offering the folder is worse than offering nothing.
KEEP_RATIO = 0.55

# The leader is shown alone when it is this far ahead. "One obvious answer" is a
# claim about the gap, not about the top score.
CLEAR_LEAD = 1.6

MAX_CANDIDATES = 5

# A folder named by one arm alone, halfway down its list, is not evidence — and
# five of those look exactly like five suggestions. Measured on real downloads:
# every candidate scored 1/(60+rank) from a single arm and the whole list was a
# tie, while the two files that *did* have agreeing arms scored double and were
# obviously right. So a candidate has to earn its place: two arms naming it, or
# one arm with something substantial to say.
MIN_ARMS = 2

# What "substantial" means for the neighbours arm. Not a raw count: "20 other png
# files here" is true of the same four folders for every png in existence, which
# makes it a fact about the archive rather than a suggestion about this file. It
# only nominates a folder on its own when that folder is where files of this kind
# actually live — most of them, not merely more than anywhere else.
DOMINANT_SHARE = 0.5


@dataclass(frozen=True)
class Candidate:
    folder: folders.Folder
    score: float
    # Why this folder, in the words of the evidence that produced it. Never
    # omitted: an unexplained suggestion about where to put a file is a guess
    # wearing a confident face.
    reasons: tuple[str, ...]

    def as_json(self) -> dict:
        return {
            "path": self.folder.path,
            "display": self.folder.display,
            "name": self.folder.name,
            "score": round(self.score, 5),
            "reasons": list(self.reasons),
        }


def file_text(path: Path, budget: int = TEXT_BUDGET) -> str:
    """What the file says, as far as it is cheap to find out.

    Reuses the ingest parser, so a PDF placed here reads the same way it reads in
    search. A file nothing can parse is not an error: the name and the neighbours
    still have plenty to say.
    """
    try:
        import mimetypes

        from muster.extract import extract_text

        # Only as much as the budget needs: placing a 40MB PDF should not read
        # all of it to learn that it is a lease.
        data = path.open("rb").read(2_000_000)
        mime, _ = mimetypes.guess_type(path.name)
        return extract_text(data, mime, path.name)[:budget]
    except Exception as exc:  # noqa: BLE001 — unparseable is a normal case here
        log.debug("no text for %s: %s", path, exc)
        return ""


def _neighbours(
    conn: sqlite3.Connection, suffix: str, limit: int
) -> tuple[list[tuple[str, int]], int]:
    """Folders already holding indexed files of this kind, and how many there are.

    Returns the best `limit` folders and the total count across all of them, so a
    share can be computed honestly.

    Counting, not judgement. It is also the arm that knows about folders the
    other two cannot describe — a directory called "2026" says nothing about its
    contents and may still be exactly where this belongs.
    """
    if not suffix:
        return [], 0
    # Counted in Python rather than in SQL: dirname is three string functions
    # deep in SQLite and one obvious expression here, and this arm is the one
    # that has to be beyond argument.
    counts: dict[str, int] = {}
    for row in conn.execute(
        "SELECT COALESCE(json_extract(meta_json, '$.path'), source_uid) AS path"
        " FROM docs WHERE source = 'file' AND missing_since IS NULL"
        "   AND lower(COALESCE(json_extract(meta_json, '$.path'), source_uid)) LIKE ?",
        (f"%{suffix.lower()}",),
    ):
        path = row["path"]
        if not path:
            continue
        parent = str(Path(path).parent)
        counts[parent] = counts.get(parent, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    # The total is over every folder, not the ones returned. A share computed
    # against a truncated list flatters whichever folders made the cut.
    return ranked[:limit], sum(counts.values())


def _folder_row(conn: sqlite3.Connection, path: str) -> tuple[int, folders.Folder] | None:
    row = conn.execute(
        "SELECT id, path, name, depth, mtime FROM folders WHERE path = ?", (path,)
    ).fetchone()
    if row is None:
        return None
    return row["id"], folders.Folder(
        path=row["path"], name=row["name"], depth=row["depth"], mtime=row["mtime"]
    )


def candidates(
    conn: sqlite3.Connection,
    path: str | Path,
    *,
    model_id: str | None = None,
    limit: int = MAX_CANDIDATES,
) -> list[Candidate]:
    """Where this file plausibly belongs, best first, with reasons."""
    target = Path(path).expanduser()
    name_words = folders.words_of(target.stem)
    text = file_text(target)

    depth = max(limit * 4, 12)
    arms: list[list[tuple[int, folders.Folder]]] = []
    why: dict[str, list[str]] = {}
    # Folders that stand on one arm alone, and may still be worth offering.
    strong: set[str] = set()

    if name_words:
        arm = folders.word_matches(conn, name_words, depth)
        if arm:
            arms.append(arm)
            wanted = {w.lower() for w in name_words}
            for _id, folder in arm:
                why.setdefault(folder.path, []).append(
                    f"name matches {' '.join(name_words[:4])}"
                )
                # The folder's own name carries a word from the file's name —
                # "MATH213 midterm.pdf" into "MATH213". That is enough on its own.
                if wanted & {w.lower() for w in folders.words_of(folder.name)}:
                    strong.add(folder.path)

    if model_id and (text or name_words):
        query = f"{target.stem} {text}".strip()
        arm = folders.meaning_matches(conn, query, model_id, depth)
        if arm:
            arms.append(arm)
            for _id, folder in arm:
                why.setdefault(folder.path, []).append("reads like what is filed here")

    neighbour_arm: list[tuple[int, folders.Folder]] = []
    counted, total_of_kind = _neighbours(conn, target.suffix, depth)
    total_of_kind = total_of_kind or 1
    for folder_path, count in counted:
        found = _folder_row(conn, folder_path)
        if found is None:
            continue      # outside the indexed roots, or below the depth limit
        neighbour_arm.append(found)
        kind = target.suffix.lstrip(".").lower() or "file"
        why.setdefault(folder_path, []).append(
            f"{count} other {kind} file{'s' if count > 1 else ''} here"
        )
        if count / total_of_kind >= DOMINANT_SHARE:
            strong.add(folder_path)
    if neighbour_arm:
        arms.append(neighbour_arm)

    if not arms:
        return []

    # Fused by position, like every other ranking here: a BM25 score, a cosine
    # distance and a file count are not on comparable scales, and weighting them
    # directly would need three constants nobody can justify.
    scores: dict[str, float] = {}
    seen: dict[str, folders.Folder] = {}
    named_by: dict[str, int] = {}
    for arm in arms:
        for rank, (_id, folder) in enumerate(arm, start=1):
            scores[folder.path] = scores.get(folder.path, 0.0) + 1.0 / (folders.RRF_K + rank)
            named_by[folder.path] = named_by.get(folder.path, 0) + 1
            seen.setdefault(folder.path, folder)

    # Agreement, or a single arm with something substantial to say. Everything
    # else is dropped: no suggestion is a usable answer, and five ties are not.
    scores = {
        path: score for path, score in scores.items()
        if named_by[path] >= MIN_ARMS or path in strong
    }

    # Never suggest where it already is.
    here = str(target.parent)
    scores.pop(here, None)
    if not scores:
        return []

    ordered = sorted(scores, key=lambda p: (-scores[p], seen[p].depth))
    best = scores[ordered[0]]

    # One when one is clearly ahead; several when several are close. The number of
    # suggestions is evidence about the file, not a layout decision.
    if len(ordered) > 1 and best >= CLEAR_LEAD * scores[ordered[1]]:
        ordered = ordered[:1]
    else:
        ordered = [p for p in ordered if scores[p] >= KEEP_RATIO * best][:limit]

    return [
        Candidate(
            folder=seen[p],
            score=scores[p],
            reasons=tuple(dict.fromkeys(why.get(p, ["similar folder"]))),
        )
        for p in ordered
    ]


class MoveRefused(Exception):
    """A move that must not happen silently, and will not happen at all."""


@dataclass(frozen=True)
class Move:
    doc_id: int | None
    from_path: str
    to_path: str


def move(
    conn: sqlite3.Connection,
    path: str | Path,
    destination: str | Path,
) -> Move:
    """Move one file into one folder, and tell the index it moved.

    The index part is not bookkeeping. A file document's identity is its path, so
    a move that nothing records looks like a deletion followed by the arrival of
    an identical file: the sweep marks the old path gone, and the next walk
    ingests the same bytes as a second document. Updating the path in place keeps
    it one document that moved.
    """
    source = Path(path).expanduser()
    folder = Path(destination).expanduser()

    if not source.is_file():
        raise MoveRefused(f"not a file: {source}")
    if not folder.is_dir():
        raise MoveRefused(f"not a folder: {folder}")
    target = folder / source.name
    if target.exists():
        # Refused rather than renamed. Muster does not get to invent
        # "invoice (2).pdf" on somebody's behalf.
        raise MoveRefused(f"already exists: {target}")
    if target == source:
        raise MoveRefused("already there")

    shutil.move(str(source), str(target))

    doc_id = _repoint(conn, str(source), str(target))
    conn.execute(
        "INSERT INTO file_moves (doc_id, from_path, to_path, moved_at)"
        " VALUES (?, ?, ?, ?)",
        (doc_id, str(source), str(target),
         datetime.now(UTC).isoformat(timespec="seconds")),
    )
    conn.commit()
    return Move(doc_id=doc_id, from_path=str(source), to_path=str(target))


def _repoint(conn: sqlite3.Connection, old: str, new: str) -> int | None:
    """Point the document at where the file now is. Returns the document, if any."""
    row = conn.execute(
        "SELECT id, meta_json FROM docs WHERE source = 'file'"
        " AND (source_uid = ? OR json_extract(meta_json, '$.path') = ?)",
        (old, old),
    ).fetchone()
    if row is None:
        return None      # never indexed: nothing to correct, and not a problem

    import json

    meta = {}
    try:
        meta = json.loads(row["meta_json"] or "{}")
    except (TypeError, ValueError):
        meta = {}
    meta["path"] = new
    conn.execute(
        # missing_since cleared: if the sweep had already marked it gone, the file
        # is demonstrably here — we just moved it.
        "UPDATE docs SET source_uid = ?, meta_json = ?, missing_since = NULL WHERE id = ?",
        (new, json.dumps(meta), row["id"]),
    )
    return int(row["id"])


def undo(conn: sqlite3.Connection, move_id: int) -> Move:
    """Put a file back. Muster made the move, so Muster can take it back."""
    row = conn.execute(
        "SELECT id, doc_id, from_path, to_path, undone_at FROM file_moves WHERE id = ?",
        (move_id,),
    ).fetchone()
    if row is None:
        raise MoveRefused(f"no move {move_id}")
    if row["undone_at"]:
        raise MoveRefused("already undone")

    current, back = Path(row["to_path"]), Path(row["from_path"])
    if not current.is_file():
        raise MoveRefused(f"not where it was left: {current}")
    if back.exists():
        raise MoveRefused(f"something is there now: {back}")
    if not back.parent.is_dir():
        raise MoveRefused(f"the folder it came from is gone: {back.parent}")

    shutil.move(str(current), str(back))
    _repoint(conn, str(current), str(back))
    conn.execute(
        "UPDATE file_moves SET undone_at = ? WHERE id = ?",
        (datetime.now(UTC).isoformat(timespec="seconds"), move_id),
    )
    conn.commit()
    return Move(doc_id=row["doc_id"], from_path=str(current), to_path=str(back))
