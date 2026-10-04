"""Whether being asked amounts to being obliged.

The extractor finds what a document asks for. Most of what an institution sends
asks for something in the imperative and obliges nobody: "join us for a webinar",
"you are welcome to attend", "consider requesting an extension", "complete this
brief 4-minute survey". Counting those as outstanding work is how a list of
twelve items comes to contain nine that are not owed — at which point it stops
being read, which costs more than the three real ones were worth.

So this asks one narrow question per distinct request and caches the answer, the
same shape as the satisfaction check: deterministic where it can be, a model only
where a judgement is genuinely required, and never asked twice.

Failing safe means keeping the item. An unanswerable request stays on the list,
because a missed obligation is a worse error than a tolerated suggestion.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import UTC, datetime

log = logging.getLogger(__name__)

# 1: the first wording of the question. Bump when the prompt changes, since a
# different prompt is a different question and the old answers are not to it.
OBLIGATION_VERSION = 1

PROMPT = """Does this oblige the reader to do something?

REQUEST: {request}
FROM: {context}

Answer YES only if a specific person or organisation is waiting on the reader for
this, and not doing it has a consequence.

Answer NO for:
- invitations and offers — "join us", "you are welcome to", "feel free to",
  "if you are interested", an optional meeting or event
- calls to action in bulk mail — surveys, sign-ups, registrations, reviews,
  "download the app", unless the document itself calls it mandatory or required
- advice and recommendations — "consider", "make sure to", "plan carefully",
  "keep copies", "do not ignore"
- generic instructions, or a line from a checklist of what anyone might need
- anything already done, or a step somebody else performs

Return ONLY {{"obliged": true or false, "reason": "<six words>"}}."""


def _key(text: str) -> str:
    return hashlib.sha256(" ".join(text.lower().split()).encode("utf-8")).hexdigest()


class Uncached(Exception):
    """No cached answer, and the caller asked not to wait for one."""


def is_obligation(
    conn: sqlite3.Connection,
    request: str,
    *,
    context: str = "",
    model: str,
    ask=None,
    cached_only: bool = False,
) -> tuple[bool, str | None]:
    """Whether `request` is owed by the reader. Cached per wording."""
    key = _key(request)
    row = conn.execute(
        "SELECT obliged, reason FROM obligations"
        " WHERE request_key = ? AND model_id = ? AND version = ?",
        (key, model, OBLIGATION_VERSION),
    ).fetchone()
    if row is not None:
        return bool(row[0]), row[1]

    if cached_only:
        raise Uncached()

    if ask is None:
        from muster.extraction import _ask_model as ask  # noqa: PLC0415

    # Kept on the list if the question cannot be answered: a missed obligation is
    # a worse error than a suggestion nobody minded seeing.
    obliged, reason = True, None
    try:
        raw = ask(
            PROMPT.format(request=request[:300], context=(context or "unknown")[:120]),
            model,
        )
        parsed = json.loads(raw)
        # A reply that does not answer the question is not a "no". Anything but
        # an explicit false keeps the item, for the same reason an exception does.
        obliged = bool(parsed.get("obliged", True))
        got = parsed.get("reason")
        reason = got.strip()[:120] if isinstance(got, str) else None
    except Exception as exc:  # noqa: BLE001 — an unanswerable request stays open
        log.debug("obligation check failed: %s", exc)
        return True, None

    conn.execute(
        "INSERT OR REPLACE INTO obligations"
        " (request_key, model_id, version, obliged, reason, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (key, model, OBLIGATION_VERSION, int(obliged), reason,
         datetime.now(UTC).isoformat(timespec="seconds")),
    )
    conn.commit()
    return obliged, reason
