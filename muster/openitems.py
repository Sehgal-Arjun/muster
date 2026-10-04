"""What is still outstanding in a process.

This is a **query, not a generation**. An item is open when a request has no
matching fulfilment — a set difference over cached extractions, in ordinary SQL
and Python, deterministic and testable. Counts, dates and totals are computed.
Nothing here asks a model how many of something there are.

The model has exactly one job: decide whether a particular thing satisfies a
particular request. Does `AOK_confirmation.pdf` satisfy "proof of health
insurance"? That is a small judgement about two short strings, made one pair at a
time and cached, and it is the only place a model's opinion enters.

Three kinds are kept apart, because flattening them loses the distinction that
makes each actionable:

- `deadline_near`  — a date is approaching, whoever owes it
- `unsatisfied`    — something was asked of me and nothing matches it
- `unanswered`     — someone asked me something and no reply of mine followed

And the count that matters most is the one for documents this could *not*
classify. A silent omission is the dangerous failure; loud uncertainty is fine.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

log = logging.getLogger(__name__)

OPEN_ITEMS_VERSION = 1

# A deadline within this window is worth raising. Past deadlines are always
# raised — a missed one is more urgent than an approaching one, not less.
DEADLINE_HORIZON = timedelta(days=30)

# A deadline this far in the past is history rather than a deadline. Recent ones
# are still raised — a missed deadline is more urgent than an approaching one —
# but a date from two years ago in a finished process is noise, and calling it
# "next" is worse than not showing it.
DEADLINE_STALE = timedelta(days=90)

# A request older than this with no reply is stale rather than open: processes
# end, and a two-year-old unanswered question is usually not a live obligation.
STALE_AFTER = timedelta(days=365)

DEADLINE_NEAR = "deadline_near"
UNSATISFIED = "unsatisfied"
UNANSWERED = "unanswered"


@dataclass
class OpenItem:
    kind: str
    what: str
    # Every item cites its sources. An open item that cannot be traced back to a
    # document is an assertion, and assertions are what this design exists to
    # avoid making.
    doc_ids: list[int]
    when: str | None = None
    counterparty: str | None = None
    detail: str | None = None
    # Set when the user settled this themselves. The item stays in history
    # rather than disappearing, because "done on the 4th" is a fact and an
    # absence would simply be re-derived as open.
    settled: str | None = None
    settled_at: str | None = None
    # True when documents have arrived since it was settled. Shown, not acted
    # on: the dismissal was a judgement about what was known at the time.
    contradicted: bool = False
    # True when the deadline has passed with no activity since. Asked once.
    needs_confirmation: bool = False
    last_activity: str | None = None

    @property
    def identity(self) -> str:
        return " ".join(sorted(_tokens(self.what))) or self.what.strip().lower()

    def as_json(self) -> dict:
        return {
            "kind": self.kind,
            "what": self.what,
            "when": self.when,
            "counterparty": self.counterparty,
            "detail": self.detail,
            "doc_ids": self.doc_ids,
            "key": self.identity,
            "settled": self.settled,
            "settled_at": self.settled_at,
            "contradicted": self.contradicted,
            "needs_confirmation": self.needs_confirmation,
            "last_activity": self.last_activity,
        }


@dataclass
class _Uncached(Exception):
    """A pair the cache cannot answer, raised only in cached_only mode."""


@dataclass
class OpenItemsReport:
    version: int
    process_slug: str
    items: list[OpenItem] = field(default_factory=list)
    # Computed, never generated.
    requests_total: int = 0
    requests_satisfied: int = 0
    documents_considered: int = 0
    # Two different things, kept apart because one is a failure and the other is
    # only unfinished work. Reporting them as one number meant the panel saying
    # "194 documents could not be read" about 194 documents nothing had tried to
    # read yet — a broken index, in a screen whose whole job is being trustworthy.
    documents_unread: list[int] = field(default_factory=list)
    documents_unexamined: list[int] = field(default_factory=list)
    next_deadline: str | None = None
    # Open items for which the archive holds no plausible fulfilment at all, as
    # opposed to ones where a candidate was considered and rejected. The
    # difference matters: the first usually means the thing was done somewhere
    # this index cannot see — a web portal, a phone call — and the second means
    # it probably was not done.
    open_without_candidates: int = 0
    # Asked for, but not owed: invitations, surveys, advice. Counted rather than
    # hidden, because a filter nobody can see is indistinguishable from a bug.
    not_obliged: int = 0
    # Deadlines already past, which are not "next" but are not nothing either.
    overdue: int = 0
    # Requests and deadlines found in attachments rather than correspondence.
    # Reported because it is a deliberate exclusion, not an absence of data.
    asks_skipped: int = 0
    duplicates_collapsed: int = 0
    # Settled by the user rather than by evidence. Kept out of the open list and
    # counted separately, so the header stays true.
    settled_items: list = field(default_factory=list)
    # True when the report was built from cached judgements alone and some pair
    # had no cached answer. The open count is then an upper bound, and says so.
    estimated: bool = False

    @property
    def documents_unaccounted(self) -> list[int]:
        """Everything contributing nothing to the numbers above, for either reason."""
        return self.documents_unexamined + self.documents_unread

    @property
    def by_kind(self) -> dict[str, list[OpenItem]]:
        out: dict[str, list[OpenItem]] = {}
        for item in self.items:
            out.setdefault(item.kind, []).append(item)
        return out

    def as_json(self) -> dict:
        return {
            "version": self.version,
            "process": self.process_slug,
            "counts": {
                "requests_total": self.requests_total,
                "requests_satisfied": self.requests_satisfied,
                "requests_open": self.requests_total - self.requests_satisfied,
                "documents_considered": self.documents_considered,
                "documents_unaccounted": len(self.documents_unaccounted),
                # Attempted and failed, versus never attempted.
                "documents_unread": len(self.documents_unread),
                "documents_unexamined": len(self.documents_unexamined),
                "open_without_candidates": self.open_without_candidates,
                "not_obliged": self.not_obliged,
                "overdue": self.overdue,
                "asks_skipped": self.asks_skipped,
                "duplicates_collapsed": self.duplicates_collapsed,
                "settled_by_me": len(self.settled_items),
                "settled_contradicted": sum(
                    1 for i in self.settled_items if i.contradicted
                ),
                "awaiting_confirmation": sum(
                    1 for i in self.items if i.needs_confirmation
                ),
            },
            "estimated": self.estimated,
            "next_deadline": self.next_deadline,
            "items": [i.as_json() for i in self.items],
            "settled": [i.as_json() for i in self.settled_items],
            "unaccounted_doc_ids": self.documents_unaccounted,
        }


# --- dates -----------------------------------------------------------------

_MONTHS = {
    m: i for i, m in enumerate(
        ["january", "february", "march", "april", "may", "june", "july",
         "august", "september", "october", "november", "december"], start=1)
}
# Not \b at the end: a document timestamp is 2026-04-01T09:00:00+00:00, and a
# word boundary fails between "1" and "T". With \b this matched no stored date at
# all, so every request looked like it arrived today and nothing was ever stale.
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})(?!\d)")
_DMY = re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})\b")
_MDY = re.compile(r"\b([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})\b")


def parse_when(text: str | None) -> date | None:
    """A date out of a phrase, or nothing.

    Deliberately narrow. "as soon as possible" and "before you travel" are real
    answers to "when" and are not dates; inventing one for them would put a
    fabricated deadline in front of the user, which is exactly the failure this
    module is shaped to avoid.
    """
    if not text:
        return None
    match = _ISO.search(text)
    if match:
        year, month, day = (int(g) for g in match.groups())
        try:
            return date(year, month, day)
        except ValueError:
            return None
    match = _DMY.search(text)
    if match:
        day, name, year = match.groups()
        month = _MONTHS.get(name.lower()[:9]) or _MONTHS.get(name.lower()[:3] + "")
        month = month or next((v for k, v in _MONTHS.items() if k.startswith(name.lower()[:3])), None)
        if month:
            try:
                return date(int(year), month, int(day))
            except ValueError:
                return None
    match = _MDY.search(text)
    if match:
        name, day, year = match.groups()
        month = next((v for k, v in _MONTHS.items() if k.startswith(name.lower()[:3])), None)
        if month:
            try:
                return date(int(year), month, int(day))
            except ValueError:
                return None
    return None


# How many candidate fulfilments to put to the model per request.
#
# Asking about every pair is quadratic: 74 requests against ~100 candidates is
# 7,400 model calls, which on this archive did not finish inside ten minutes. The
# candidates are ranked by word overlap first — deterministic, free — and only the
# best few are actually asked about. A candidate sharing no word at all with the
# request is not a plausible fulfilment, and refusing to ask about it costs
# nothing.
MAX_CANDIDATES_PER_REQUEST = 5

_WORDS = re.compile(r"[a-z0-9]+")

# Words that appear in everything and carry no evidence of a match.
_STOP = frozenset({
    "the", "a", "an", "of", "to", "for", "and", "or", "in", "on", "at", "by",
    "with", "your", "my", "our", "please", "send", "provide", "copy", "form",
    "document", "attached", "signed", "complete", "completed", "file", "pdf",
})


def _tokens(text: str) -> set[str]:
    return {w for w in _WORDS.findall(text.lower()) if w not in _STOP and len(w) > 2}


def rank_candidates(request: str, candidates: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Plausible fulfilments for a request, best first.

    Word overlap, so it is deterministic and the same request ranks the same
    candidates every run. Only a prefilter — the judgement about whether a
    candidate actually satisfies the request is still the model's, and is still
    made one pair at a time.
    """
    wanted = _tokens(request)
    if not wanted:
        return []
    scored = []
    for index, (doc_id, text) in enumerate(candidates):
        shared = wanted & _tokens(text)
        if not shared:
            continue
        # Overlap first; original order as the tie-break, so the earliest
        # plausible fulfilment wins rather than an arbitrary one.
        scored.append((-len(shared), index, doc_id, text))
    scored.sort()
    return [(doc_id, text) for _n, _i, doc_id, text in scored[:MAX_CANDIDATES_PER_REQUEST]]


# --- the semantic match ----------------------------------------------------


def _key(*parts: str) -> str:
    return hashlib.sha256("␟".join(p.strip().lower() for p in parts).encode()).hexdigest()[:32]


MATCH_PROMPT = """Does the thing provided satisfy the thing requested?

REQUESTED: {request}
PROVIDED:  {candidate}

Answer ONLY with JSON: {{"satisfied": true or false, "reason": "<8 words>"}}

Say true only if the provided thing plainly is what was asked for, or clearly
contains it. A different document about the same process does not count. If you
are unsure, say false — an unsatisfied item shown to someone who has already
done it costs a moment, and a satisfied item that was never done costs them the
thing itself.
"""


def satisfies(
    conn: sqlite3.Connection,
    request: str,
    candidate: str,
    *,
    model: str,
    ask=None,
    cached_only: bool = False,
) -> tuple[bool, str | None]:
    """Whether `candidate` satisfies `request`. Cached per pair.

    ``cached_only`` answers from the cache and never asks. It is what makes a
    summary of every process affordable: the first look at a process pays for
    its judgements once, and every overview afterwards is free. An uncached pair
    reads as unsatisfied, so a summary can overstate what is open — which is why
    a report built this way says that it did.
    """
    request_key, candidate_key = _key(request), _key(candidate)
    row = conn.execute(
        "SELECT satisfied, reason FROM satisfaction"
        " WHERE request_key = ? AND candidate_key = ? AND model_id = ?",
        (request_key, candidate_key, model),
    ).fetchone()
    if row is not None:
        return bool(row[0]), row[1]

    if cached_only:
        raise _Uncached()

    if ask is None:
        from muster.extraction import _ask_model as ask  # noqa: PLC0415

    verdict, reason = False, None
    try:
        raw = ask(MATCH_PROMPT.format(request=request[:300], candidate=candidate[:300]), model)
        parsed = json.loads(raw)
        verdict = bool(parsed.get("satisfied"))
        got = parsed.get("reason")
        reason = got.strip()[:120] if isinstance(got, str) else None
    except Exception as exc:  # noqa: BLE001 — an unanswerable pair is unsatisfied
        log.debug("satisfaction check failed: %s", exc)
        return False, None

    conn.execute(
        "INSERT OR REPLACE INTO satisfaction"
        " (request_key, candidate_key, model_id, satisfied, reason, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (request_key, candidate_key, model, int(verdict), reason,
         datetime.now(UTC).isoformat(timespec="seconds")),
    )
    conn.commit()
    return verdict, reason


# --- dismissal -------------------------------------------------------------
#
# Most obligations in a real process are discharged somewhere this index cannot
# see. An item that no evidence can ever satisfy will sit in the list forever,
# and a list with permanent residents stops being read.


def item_key(what: str) -> str:
    """The identity of an obligation, independent of how it was worded.

    Token-based and order-independent, matching how duplicate requests are
    collapsed: settling "a copy of your passport" also settles the reminder that
    restated it, because they are one obligation.
    """
    return " ".join(sorted(_tokens(what))) or what.strip().lower()


def dismiss(
    conn: sqlite3.Connection,
    process_id: int,
    what: str,
    *,
    verdict: str = "done",
    note: str | None = None,
    known_docs: int = 0,
) -> None:
    """Record that the user settled this themselves.

    Stored, not deleted. "I did this on the 4th" is a fact the reconstruction
    should carry — an absence would just be re-derived as open on the next run.
    """
    conn.execute(
        "INSERT OR REPLACE INTO item_dismissals"
        " (process_id, item_key, verdict, note, settled_at, known_docs)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (process_id, item_key(what), verdict, note,
         datetime.now(UTC).isoformat(timespec="seconds"), known_docs),
    )
    conn.commit()


def undismiss(conn: sqlite3.Connection, process_id: int, what: str) -> bool:
    cursor = conn.execute(
        "DELETE FROM item_dismissals WHERE process_id = ? AND item_key = ?",
        (process_id, item_key(what)),
    )
    conn.commit()
    return cursor.rowcount > 0


def dismissals(conn: sqlite3.Connection, process_id: int) -> dict[str, sqlite3.Row]:
    return {
        row["item_key"]: row
        for row in conn.execute(
            "SELECT * FROM item_dismissals WHERE process_id = ?", (process_id,)
        )
    }


# --- the query -------------------------------------------------------------


def compute(
    conn: sqlite3.Connection,
    result,                       # GatherResult
    *,
    model: str,
    today: date | None = None,
    ask=None,
    process_id: int | None = None,
    cached_only: bool = False,
) -> OpenItemsReport:
    """Open items for a gathered process. A set difference, not a summary."""
    from muster import extraction, obligations

    today = today or datetime.now(UTC).date()
    doc_ids = list(result.doc_ids)
    extractions = extraction.load(conn, doc_ids)
    failed = extraction.failures(conn, doc_ids)

    # Direction is computed from the From header rather than read from the
    # extraction. See extraction.computed_directions: the model is reliable about
    # direction and unreliable about what follows from it, filling requests_of_me
    # with my own asks on a fifth of outbound documents.
    from muster.processes import own_addresses_from_archive

    direction = extraction.computed_directions(
        conn, doc_ids, own_addresses_from_archive(conn)
    )

    # Neither kind is hidden, and they are not the same kind. A document with a
    # recorded failure was read and could not be understood; one with nothing
    # recorded has simply not been looked at yet, and will be the moment
    # extraction runs.
    unexamined = sorted(set(doc_ids) - set(extractions) - set(failed))
    unread = sorted(failed)

    rows = {
        r["id"]: r
        for r in conn.execute(
            "SELECT id, doc_date, title, counterparty, mime FROM docs"
            f" WHERE id IN ({','.join('?' * len(doc_ids))})", doc_ids
        ) if doc_ids
    } if doc_ids else {}

    def is_correspondence(doc_id: int) -> bool:
        """Whether a document is somebody writing to somebody.

        Obligations are read from correspondence only, and deliberately not from
        attachments. Measured on a real process: a photographed consular form
        produced twelve "requests", an application-instructions document seven,
        because both are generic checklists listing every document anyone might
        ever need. None of it was asked of this reader in particular.

        Asymmetric on purpose — the *evidence* side still uses attachments, since
        a file I sent is exactly how I discharge an obligation.
        """
        row = rows.get(doc_id)
        return bool(row) and row["mime"] == "message/rfc822"

    # Everything I sent or promised, in document order, as candidate fulfilments.
    provided: list[tuple[int, str]] = []
    for doc_id in doc_ids:
        got = extractions.get(doc_id)
        if not got:
            continue
        # Only a document I sent can evidence my having sent something.
        if direction.get(doc_id, "inbound") == "outbound":
            for entry in got.sent:
                provided.append((doc_id, entry["what"]))
            # An attachment on a message I sent is evidence of my sending it.
            # On a message I received it is evidence of theirs, which does not
            # discharge anything I owe.
            for row in conn.execute(
                "SELECT filename FROM attachments WHERE doc_id = ? AND filename IS NOT NULL",
                (doc_id,),
            ):
                provided.append((doc_id, row[0]))

    # Position lookup rather than list.index in a loop: the latter is quadratic
    # in the size of the set, which on 300 documents is felt.
    position = {doc_id: i for i, doc_id in enumerate(doc_ids)}

    settled = dismissals(conn, process_id) if process_id else {}

    items: list[OpenItem] = []
    settled_items: list[OpenItem] = []
    requests_total = requests_satisfied = 0
    without_candidates = 0
    # Obligations declined because their document was not correspondence.
    asks_skipped = 0
    # Requests the model judged to be invitations, advice or marketing.
    not_obliged = 0
    estimated = False
    # The same requirement restated in two documents is one obligation, not two.
    # A checklist repeated in a reminder is the common case.
    seen_requests: dict[str, OpenItem] = {}
    collapsed = 0

    for doc_id in doc_ids:
        got = extractions.get(doc_id)
        if not got:
            continue
        row = rows.get(doc_id)
        doc_date = parse_when(row["doc_date"] if row else None) or today

        # A request inside a message I wrote is something I asked of somebody
        # else, not something owed by me.
        if direction.get(doc_id, "inbound") == "outbound":
            requests = []
        else:
            requests = got.requests

        if not is_correspondence(doc_id):
            # A checklist in an attachment is not a request made of anybody.
            asks_skipped += len(requests) + len(got.deadlines)
            continue

        for entry in requests:
            what = entry["what"]
            # Being asked is not being obliged. An institution's mail is mostly
            # invitations and advice in the imperative, and a list where most
            # entries are not owed stops being read.
            try:
                obliged, _why = obligations.is_obligation(
                    conn, what, context=(row["title"] if row else "") or "",
                    model=model, ask=ask, cached_only=cached_only,
                )
            except obligations.Uncached:
                estimated = True
                obliged = True      # kept, and the report says the count is soft
            if not obliged:
                not_obliged += 1
                continue

            requests_total += 1
            # Only things provided *after* the request can satisfy it. Order is
            # why gather is chronological: a receipt from last year does not
            # answer a question asked this morning.
            here = position[doc_id]
            later = [
                (other_id, text) for other_id, text in provided
                if position[other_id] >= here
            ]
            matched: tuple[int, str, str | None] | None = None
            for other_id, text in rank_candidates(what, later):
                try:
                    ok, reason = satisfies(
                        conn, what, text, model=model, ask=ask, cached_only=cached_only
                    )
                except _Uncached:
                    estimated = True
                    continue
                if ok:
                    matched = (other_id, text, reason)
                    break
            if matched:
                requests_satisfied += 1
                continue

            age = today - doc_date
            if age > STALE_AFTER:
                continue     # a year-old unanswered request is not a live one

            considered = rank_candidates(what, later)
            if not considered:
                without_candidates += 1

            key = " ".join(sorted(_tokens(what))) or what.strip().lower()
            existing = seen_requests.get(key)
            if existing is not None:
                # Same obligation, another document: cite both rather than
                # listing it twice.
                existing.doc_ids.append(doc_id)
                merged = parse_when((entry.get("due") or "").strip())
                existing.when = existing.when or (merged.isoformat() if merged else None)
                collapsed += 1
                continue

            # Only a real date goes in the date field. "14 days from receipt" is
            # a condition, not a deadline, and putting it where a date belongs
            # made it sort as though it were one and render as though it parsed.
            due_raw = (entry.get("due") or "").strip() or None
            due_date = parse_when(due_raw) if due_raw else None
            item = OpenItem(
                kind=UNSATISFIED,
                what=what,
                when=due_date.isoformat() if due_date else None,
                counterparty=got.payload.get("counterparty") or (row["counterparty"] if row else None),
                detail=(
                    # The unparsed phrase survives as prose, where it is honest.
                    f"due {due_raw}" if due_raw and not due_date
                    else "no candidate fulfilment in the archive" if not considered
                    else f"asked in {(row['title'] or 'a message')[:60]}"
                ),
                doc_ids=[doc_id],
            )

            seen_requests[key] = item
            items.append(item)

        for entry in got.deadlines:
            when = parse_when(entry.get("when"))
            if when is None:
                continue
            if today - when > DEADLINE_STALE:
                continue  # history, not a deadline
            if when - today <= DEADLINE_HORIZON:
                items.append(OpenItem(
                    kind=DEADLINE_NEAR,
                    what=entry["what"],
                    when=when.isoformat(),
                    counterparty=got.payload.get("counterparty"),
                    detail="overdue" if when < today else "approaching",
                    doc_ids=[doc_id],
                ))

        # Something I promised, in a message I wrote, that no later document
        # shows me doing. A promise attributed to an inbound message is the
        # other party's promise, not mine.
        promised = got.promised if direction.get(doc_id, "inbound") == "outbound" else []
        for entry in promised:
            what = entry["what"]
            later = [
                (other_id, text) for other_id, text in provided
                if position[other_id] > position[doc_id]
            ]
            fulfilled = False
            for _oid, text in rank_candidates(what, later):
                try:
                    if satisfies(conn, what, text, model=model, ask=ask,
                                 cached_only=cached_only)[0]:
                        fulfilled = True
                        break
                except _Uncached:
                    estimated = True
            if fulfilled:
                continue
            if today - doc_date > STALE_AFTER:
                continue
            items.append(OpenItem(
                kind=UNANSWERED,
                what=what,
                when=entry.get("when"),
                counterparty=got.payload.get("counterparty"),
                detail="I said I would do this",
                doc_ids=[doc_id],
            ))

    # Dismissals apply to every kind, not only to unsatisfied requests. A
    # deadline the user has dealt with is as settled as a document they sent, and
    # handling only one kind meant a dismissal was stored and then ignored.
    if settled:
        still_open: list[OpenItem] = []
        for item in items:
            record = settled.get(item.identity)
            if record is None:
                still_open.append(item)
                continue
            item.settled = record["verdict"]
            item.settled_at = record["settled_at"]
            # Documents arriving after it was settled may bear on it. Surfaced
            # rather than assumed either way: the dismissal was a judgement about
            # what was known at the time, not a permanent silence.
            item.contradicted = len(doc_ids) > (record["known_docs"] or 0)
            settled_items.append(item)
        items = still_open

    # An item whose deadline has passed with nothing happening since is asked
    # about once rather than repeated every time the process is opened. Nagging
    # is how a list teaches somebody to stop reading it; a single direct question
    # — "due 17 Aug, nothing since 2 Apr; done, or still open?" — is answerable.
    last_activity = max(
        (rows[d]["doc_date"] for d in doc_ids if d in rows and rows[d]["doc_date"]),
        default=None,
    )
    for item in items:
        due = parse_when(item.when)
        if due is None or due >= today:
            continue
        # Activity *after* the deadline means the process moved on and the
        # question answers itself.
        moved_on = any(
            (rows[d]["doc_date"] or "") [:10] > due.isoformat()
            for d in doc_ids if d in rows
        )
        if moved_on:
            continue
        item.needs_confirmation = True
        item.last_activity = last_activity[:10] if last_activity else None

    deadlines = sorted(i.when for i in items if i.kind == DEADLINE_NEAR and i.when)
    # "Next" means the next one coming, not the earliest one on record. A date
    # that has passed is either missed or moot, and either way it is not next;
    # counted separately so it is still visible.
    upcoming = [d for d in deadlines if d >= today.isoformat()]
    overdue = len(deadlines) - len(upcoming)

    # Overdue first, then soonest; unsatisfied before promised, since something
    # asked of you outranks something you offered.
    order = {DEADLINE_NEAR: 0, UNSATISFIED: 1, UNANSWERED: 2}
    items.sort(key=lambda i: (order.get(i.kind, 9), i.when or "9999", i.what))

    return OpenItemsReport(
        version=OPEN_ITEMS_VERSION,
        process_slug=result.process_slug,
        items=items,
        requests_total=requests_total,
        requests_satisfied=requests_satisfied,
        documents_considered=len(extractions),
        documents_unread=unread,
        documents_unexamined=unexamined,
        next_deadline=upcoming[0] if upcoming else None,
        overdue=overdue,
        asks_skipped=asks_skipped,
        open_without_candidates=without_candidates,
        not_obliged=not_obliged,
        duplicates_collapsed=collapsed,
        settled_items=settled_items,
        estimated=estimated,
    )
