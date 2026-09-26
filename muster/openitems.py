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

    def as_json(self) -> dict:
        return {
            "kind": self.kind,
            "what": self.what,
            "when": self.when,
            "counterparty": self.counterparty,
            "detail": self.detail,
            "doc_ids": self.doc_ids,
        }


@dataclass
class OpenItemsReport:
    version: int
    process_slug: str
    items: list[OpenItem] = field(default_factory=list)
    # Computed, never generated.
    requests_total: int = 0
    requests_satisfied: int = 0
    documents_considered: int = 0
    documents_unaccounted: list[int] = field(default_factory=list)
    next_deadline: str | None = None
    # Open items for which the archive holds no plausible fulfilment at all, as
    # opposed to ones where a candidate was considered and rejected. The
    # difference matters: the first usually means the thing was done somewhere
    # this index cannot see — a web portal, a phone call — and the second means
    # it probably was not done.
    open_without_candidates: int = 0
    duplicates_collapsed: int = 0

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
                "open_without_candidates": self.open_without_candidates,
                "duplicates_collapsed": self.duplicates_collapsed,
            },
            "next_deadline": self.next_deadline,
            "items": [i.as_json() for i in self.items],
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
) -> tuple[bool, str | None]:
    """Whether `candidate` satisfies `request`. Cached per pair."""
    request_key, candidate_key = _key(request), _key(candidate)
    row = conn.execute(
        "SELECT satisfied, reason FROM satisfaction"
        " WHERE request_key = ? AND candidate_key = ? AND model_id = ?",
        (request_key, candidate_key, model),
    ).fetchone()
    if row is not None:
        return bool(row[0]), row[1]

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


# --- the query -------------------------------------------------------------


def compute(
    conn: sqlite3.Connection,
    result,                       # GatherResult
    *,
    model: str,
    today: date | None = None,
    ask=None,
) -> OpenItemsReport:
    """Open items for a gathered process. A set difference, not a summary."""
    from muster import extraction

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

    # Documents with neither an extraction nor a recorded failure are simply not
    # accounted for, and that is reported rather than hidden.
    unaccounted = sorted(
        set(doc_ids) - set(extractions) - set(failed)
    ) + sorted(failed)

    rows = {
        r["id"]: r
        for r in conn.execute(
            "SELECT id, doc_date, title, counterparty FROM docs"
            f" WHERE id IN ({','.join('?' * len(doc_ids))})", doc_ids
        ) if doc_ids
    } if doc_ids else {}

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

    items: list[OpenItem] = []
    requests_total = requests_satisfied = 0
    without_candidates = 0
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

        for entry in requests:
            requests_total += 1
            what = entry["what"]
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
                ok, reason = satisfies(conn, what, text, model=model, ask=ask)
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
                existing.when = existing.when or entry.get("due")
                collapsed += 1
                continue

            item = OpenItem(
                kind=UNSATISFIED,
                what=what,
                when=entry.get("due"),
                counterparty=got.payload.get("counterparty") or (row["counterparty"] if row else None),
                detail=(
                    "no candidate fulfilment in the archive" if not considered
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
            if any(
                satisfies(conn, what, text, model=model, ask=ask)[0]
                for _oid, text in rank_candidates(what, later)
            ):
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

    deadlines = sorted(i.when for i in items if i.kind == DEADLINE_NEAR and i.when)

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
        documents_unaccounted=unaccounted,
        next_deadline=deadlines[0] if deadlines else None,
        open_without_candidates=without_candidates,
        duplicates_collapsed=collapsed,
    )
