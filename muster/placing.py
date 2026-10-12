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
import re
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

# Words too common to be evidence of anything. Deliberately tiny: this is not a
# stopword list for retrieval, it is a filter over the handful of terms a
# document repeats most, and every entry earns its place by being a word that
# appears in every second document ever written.
STOPWORDS = frozenset("""
the a an and or of to in for on at by with from as is are was were be been this
that these those it its will would should could can may not no if then than so
your you my our their his her they we he she i me us them
page pdf doc docx file document date name please thank thanks dear sincerely
der die das den dem des ein eine einen einem und oder von zu im am bei mit
uhr sie ihr ihre sich nicht auch aus nach vor wird werden sind haben hat
le la les un une des du de et ou pour dans sur avec par est sont
el los las una para con por que se del
""".split())

# How many of a document's own words are worth matching folders against. Six is
# enough to carry a course code, a counterparty and a subject; more is the middle
# of the document, which is about content rather than category.
CONTENT_TERMS = 6

# A term has to be repeated to count as what the document is *about*. One mention
# is a passing reference, and passing references match the wrong folder
# confidently.
MIN_TERM_COUNT = 2

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


def content_terms(text: str, limit: int = CONTENT_TERMS) -> list[str]:
    """The words a document is about, as far as counting can tell.

    Frequency over its own words, which is crude and has one great virtue: it can
    be shown to you. "text mentions ENVS 205" is a claim you can check against the
    file in a second, where "reads like what is filed here" is a claim about a
    cosine distance that you cannot check at all.

    Identifiers are kept whatever their count — MATH213, ENVS205, SE465. A code
    appearing once is still a code, and it is usually the whole answer. Written as
    two words in the document ("ENVS 205") and as one on disk, so the pair is
    joined — but only when the letters are capitalised, or "about 166" becomes an
    identifier and matches a folder with total confidence.
    """
    if not text:
        return []
    counts: dict[str, int] = {}
    identifiers: list[str] = []
    # Tokenised here rather than through folders.words_of, which dedupes — it is
    # built for path segments, where a repeat means nothing. Frequency is the
    # whole signal here, and deduping made every count exactly one.
    # Unicode-aware: `[0-9A-Za-z]+` cut "Schlüsselabholung" into "schl" and
    # "sselabholung", which is what every accented word in the archive became.
    tokens = re.findall(r"[^\W_]+", text, re.UNICODE)
    for index, word in enumerate(tokens):
        # "ENVS 205" is one name written as two words, and the folder it belongs
        # to is called ENVS205. Joining the pair is the difference between reading
        # a course code and reading two unrelated tokens.
        if (
            word.isalpha() and word.isupper() and 2 <= len(word) <= 5
            and index + 1 < len(tokens)
            and tokens[index + 1].isdigit()
            and 2 <= len(tokens[index + 1]) <= 4
        ):
            joined = f"{word}{tokens[index + 1]}"
            if joined not in identifiers:
                identifiers.append(joined)
    for word in tokens:
        low = word.lower()
        if len(low) < 3 or low in STOPWORDS or low.isdigit():
            continue
        # A letters-then-digits token: a course code, a model number, a case
        # number. Rare in prose and decisive when present.
        # Mixed letters and digits, both of them substantially: MATH213 and
        # A1234567 qualify, "2026a" and "by2000" are a year with something stuck
        # to it and matched folders full of confident nonsense.
        digits = sum(c.isdigit() for c in low)
        letters = sum(c.isalpha() for c in low)
        if digits >= 2 and letters >= 2:
            if word not in identifiers:
                identifiers.append(word)
            continue
        counts[low] = counts.get(low, 0) + 1

    repeated = sorted(
        (w for w, n in counts.items() if n >= MIN_TERM_COUNT),
        key=lambda w: (-counts[w], -len(w), w),
    )
    # Identifiers first: they are the strongest thing a document can say about
    # where it belongs.
    return (identifiers + repeated)[:limit]


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


# How many folders the judgement is shown. Measured, not guessed: with eight
# options the model picked the right German housing folder and said why; with
# twenty-four — the same eight plus project directories — it abstained. A long
# list is not more recall, it is less attention.
SHORTLIST = 6

# Whose families are worth adding: all of them. Taking only the top few meant the
# children of `Housing` were never offered when `Housing` ranked fourth — and
# `Housing/TUM` was the answer.
RELATIVES_OF = SHORTLIST

# Everything the judgement sees, after families. Beyond about ten it stops
# choosing.
SHORTLIST_TOTAL = 10


def _with_relatives(
    conn: sqlite3.Connection, found: list[folders.Folder]
) -> list[folders.Folder]:
    """The shortlist, plus the parents and children of what is on it.

    The arms are good at recognising a *category* and poor at choosing inside it:
    for a German letter about collecting keys they named `Housing`, which is right,
    and could not reach `Housing/TUM`, which is the answer. Offering the family of
    every folder they named costs one query and puts the answer in the room.
    """
    out: list[folders.Folder] = list(found)
    seen = {f.path for f in found}
    for folder in found[:RELATIVES_OF]:
        for row in conn.execute(
            "SELECT path, name, depth, mtime FROM folders"
            " WHERE path = ? OR (path LIKE ? AND path NOT LIKE ?)",
            (
                str(Path(folder.path).parent),
                f"{folder.path}/%",
                f"{folder.path}/%/%",     # children, not descendants
            ),
        ):
            if row["path"] in seen:
                continue
            # A bare root — ~/Documents, ~/GitHub — is where things go when
            # nobody has decided, which is the opposite of what this is for.
            if row["depth"] <= 1:
                continue
            seen.add(row["path"])
            out.append(folders.Folder(
                path=row["path"], name=row["name"],
                depth=row["depth"], mtime=row["mtime"],
            ))
    return out[:SHORTLIST_TOTAL]


def candidates(
    conn: sqlite3.Connection,
    path: str | Path,
    *,
    model_id: str | None = None,
    limit: int = MAX_CANDIDATES,
    reason_with: str | None = None,
    ask=None,
) -> list[Candidate]:
    """Where this file plausibly belongs, best first, with reasons.

    With `reason_with` (an extraction model), the arms become a shortlist and one
    cached judgement picks from it. That is the only part of this that knows
    anything about the world, and it is why a German letter from a Munich student
    housing office can reach a folder called TUM.
    """
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

    # What the document says, matched against folder names the same way its own
    # name is. This is the arm that files "ENVS 205 Assignment 7.pdf" under
    # ENVS205 by reading it rather than by trusting the filename to be tidy.
    terms = content_terms(text)
    if terms:
        merged: dict[str, tuple[int, folders.Folder]] = {}
        best_rank: dict[str, int] = {}
        for term in terms:
            for rank, (folder_id, folder) in enumerate(
                folders.word_matches(conn, [term], depth), start=1
            ):
                if folder.path not in best_rank or rank < best_rank[folder.path]:
                    best_rank[folder.path] = rank
                    merged[folder.path] = (folder_id, folder)
                why.setdefault(folder.path, []).append(f"text mentions {term}")
                # A folder whose own name is a word the document keeps using is
                # not a coincidence worth hedging about.
                if term.lower() in {w.lower() for w in folders.words_of(folder.name)}:
                    strong.add(folder.path)
        if merged:
            arms.append([
                merged[path] for path in sorted(best_rank, key=lambda p: best_rank[p])
            ])

    if model_id and (text or name_words):
        query = f"{target.stem} {text}".strip()
        arm = folders.meaning_matches(conn, query, model_id, depth)
        if arm:
            arms.append(arm)
            for _id, folder in arm:
                why.setdefault(folder.path, []).append("reads like what is filed here")

    neighbour_arm: list[tuple[int, folders.Folder]] = []
    neighbour_paths: set[str] = set()
    counted, total_of_kind = _neighbours(conn, target.suffix, depth)
    total_of_kind = total_of_kind or 1
    for folder_path, count in counted:
        found = _folder_row(conn, folder_path)
        if found is None:
            continue      # outside the indexed roots, or below the depth limit
        neighbour_arm.append(found)
        neighbour_paths.add(folder_path)
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

    # Kept before the filter below, because the judgement is supposed to see what
    # the arms *found*, not what they could justify. The filter used to run first
    # and leave it two folders to choose between — both of them wrong — which is
    # why a German housing letter was offered a past-exam directory while the same
    # question, asked by hand with the unfiltered list, answered correctly.
    all_scores = dict(scores)

    # Agreement, or a single arm with something substantial to say. Everything
    # else is dropped: no suggestion is a usable answer, and five ties are not.
    scores = {
        path: score for path, score in scores.items()
        if named_by[path] >= MIN_ARMS or path in strong
    }

    # Never suggest where it already is.
    here = str(target.parent)
    scores.pop(here, None)

    ordered = sorted(scores, key=lambda p: (-scores[p], seen[p].depth))
    best = scores[ordered[0]] if ordered else 0.0
    ordered_all = sorted(all_scores, key=lambda p: (-all_scores[p], seen[p].depth))

    # Before filtering for confidence: the judgement gets the wide list, because
    # the answer the arms cannot justify is exactly the one it is here to find.
    if reason_with:
        # Only folders some arm found *about* this file, plus their families. A
        # folder that merely holds pdfs says nothing about what this pdf is, and
        # four of them crowding the list is what made the judgement abstain on a
        # German housing letter whose answer was sitting at number six.
        about = [
            p for p in ordered_all
            if p not in neighbour_paths or named_by[p] > 1
        ][:SHORTLIST]
        shortlist = _with_relatives(conn, [seen[p] for p in about])
        chosen, verdict = choose(
            conn, name=target.name, text=text, options=shortlist,
            model=reason_with, ask=ask,
        )
        # Looked up in the shortlist rather than in `seen`: the folders added as
        # relatives were never scored by an arm, and requiring a score threw away
        # exactly the answers this step exists to find.
        offered = {f.path: f for f in shortlist}
        if chosen and chosen in offered:
            # Named as a judgement rather than a measurement, because it is one,
            # and because you should know which of the two to distrust.
            first = Candidate(
                folder=offered[chosen],
                # A judgement has no score. Reported as the reason it is: read.
                score=scores.get(chosen, 0.0),
                reasons=(
                    f"read it: {verdict}" if verdict else "read it and chose this",
                    *dict.fromkeys(why.get(chosen, [])),
                ),
            )
            rest = [
                Candidate(
                    folder=seen[p], score=scores[p],
                    reasons=tuple(dict.fromkeys(why.get(p, ["similar folder"]))),
                )
                for p in ordered
                if p != chosen and (named_by[p] >= MIN_ARMS or p in strong)
            ][: max(limit - 1, 0)]
            return [first, *rest]

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


# The question's own version. Not in the table as a column but mixed into its key:
# a changed prompt is a different question, and without this the cache answered the
# new question with the old answer — I improved the prompt, re-ran it, and got the
# previous abstention back three times before noticing.
CHOICE_VERSION = 2

CHOICE_PROMPT = """A file has arrived and needs filing. Which of these folders does it belong in?

FILE: {name}

WHAT IT SAYS (any language, first page only):
{text}

FOLDERS:
{options}

Think about what the document is for and who it is from, not only its words. A
letter from a university's housing office belongs with that university's housing,
whatever language it is written in.

Answer with the number of the best folder. Nothing is moved by your answer: it is
shown as a suggestion and a person confirms it, so a plausible folder they can
reject is far more useful than abstaining. Answer null only when none of the
folders is even close.

Return ONLY {{"folder": <number or null>, "why": "<eight words, naming what in the document decided it>"}}."""


def _choice_keys(name: str, text: str, options: list[str]) -> tuple[str, str]:
    import hashlib

    def digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    return (
        digest(f"{name}\n{text}"),
        digest(f"v{CHOICE_VERSION}\n" + "\n".join(options)),
    )


def choose(
    conn: sqlite3.Connection,
    *,
    name: str,
    text: str,
    options: list[folders.Folder],
    model: str,
    ask=None,
    cached_only: bool = False,
) -> tuple[str | None, str | None]:
    """Which of these folders the document belongs in, if any. Cached.

    The arms are good at recall and know nothing about the world. This knows that
    TUM is a university in Munich, and that is the entire reason it exists.

    Returns (folder path or None, reason). "None of these" is a real answer and is
    cached like any other: a wrong folder is worse than no answer, because a file
    in the wrong place is lost twice.
    """
    if not options:
        return None, None
    paths = [f.path for f in options]
    text_key, options_key = _choice_keys(name, text, paths)
    row = conn.execute(
        "SELECT chosen, reason FROM placement_choices"
        " WHERE text_key = ? AND options_key = ? AND model_id = ?",
        (text_key, options_key, model),
    ).fetchone()
    if row is not None:
        return row["chosen"], row["reason"]
    if cached_only:
        return None, None

    if ask is None:
        from muster.extraction import _ask_model as ask  # noqa: PLC0415

    chosen: str | None = None
    reason: str | None = None
    try:
        import json

        listing = "\n".join(
            f"{i}. {f.display}" for i, f in enumerate(options, start=1)
        )
        raw = ask(
            CHOICE_PROMPT.format(name=name, text=text[:1200], options=listing), model
        )
        parsed = json.loads(raw)
        index = parsed.get("folder")
        if isinstance(index, int) and 1 <= index <= len(options):
            chosen = paths[index - 1]
            got = parsed.get("why")
            reason = got.strip()[:120] if isinstance(got, str) else None
    except Exception as exc:  # noqa: BLE001 — no answer is an answer here
        log.debug("placement choice failed for %s: %s", name, exc)
        return None, None

    # An abstention is not cached. "None of these" almost always means the
    # shortlist was wrong rather than that no answer exists, and caching it turns
    # a bad shortlist into a permanent one — which is exactly what happened while
    # building this: an improved question kept reading back the old "no".
    if chosen:
        conn.execute(
            "INSERT OR REPLACE INTO placement_choices"
            " (text_key, options_key, model_id, chosen, reason, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (text_key, options_key, model, chosen, reason,
             datetime.now(UTC).isoformat(timespec="seconds")),
        )
        conn.commit()
    return chosen, reason


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
