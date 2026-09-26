"""One structured summary per document.

Feeding three hundred raw documents to a model is around 300k tokens and has to
be paid again every time anybody asks a question. Extracting once per document
and caching turns that into an 18k-token timeline, and makes the expensive half
idempotent: a crash partway through costs nothing, because everything already
done is already stored.

Every response is validated against the schema before it is stored. A malformed
one is retried and then recorded as a failure — never stored as garbage, and
never silently dropped. "Four documents I could not account for" is an honest
answer; quietly leaving them out is the dangerous one.

The model's job here is narrow and mechanical: read one document, say what was
asked of me, what I sent, what I promised, and what is due. It does no
reconciliation. Deciding whether a request was satisfied is a separate step with
its own evidence, because that is where a wrong answer costs something.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime

log = logging.getLogger(__name__)

# Bump when the prompt or the schema changes. Extractions are keyed on it, so a
# bump re-extracts rather than mixing two contracts in one table.
EXTRACTOR_VERSION = 1

DEFAULT_MODEL = "qwen2.5:14b"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"

# Enough of a document to carry the request; short enough to keep a local model
# fast. Requests live near the top of a message, below the greeting.
MAX_DOC_CHARS = 6000

MAX_ATTEMPTS = 3

LIST_FIELDS = {
    "requests_of_me": ("what", "due"),
    "things_i_sent": ("what", "doc_ref"),
    "things_i_promised": ("what", "when"),
    "deadlines": ("what", "when"),
}

PROMPT = """You extract structured facts from one document belonging to a process.

Return ONLY a JSON object with exactly these keys:

{{"date": string or null,
  "direction": "inbound" or "outbound",
  "counterparty": string or null,
  "requests_of_me": [{{"what": string, "due": string or null}}],
  "things_i_sent": [{{"what": string, "doc_ref": string or null}}],
  "things_i_promised": [{{"what": string, "when": string or null}}],
  "deadlines": [{{"what": string, "when": string or null}}]}}

Definitions, which matter more than fluency:
- requests_of_me: things the OTHER party asked ME to provide, send, sign or do.
- things_i_sent: things I attached, enclosed or confirmed sending.
- things_i_promised: things I said I would do or send. The inverse of
  requests_of_me — what I owe because I offered, not because I was asked.
- deadlines: dates by which something must happen, whoever owes it.

Rules:
- Use [] when there is nothing. Never invent an item to fill a field.
- Quote the document's own wording, shortened. Do not paraphrase into new terms.
- A marketing message, a newsletter or a receipt usually asks nothing: [] is the
  correct and expected answer for most documents.
- direction is "outbound" when I wrote it, "inbound" when I received it.

I am {owner}.

DOCUMENT
--------
{document}
"""


@dataclass
class Extraction:
    doc_id: int
    payload: dict
    model_id: str
    version: int = EXTRACTOR_VERSION

    @property
    def requests(self) -> list[dict]:
        return self.payload.get("requests_of_me") or []

    @property
    def sent(self) -> list[dict]:
        return self.payload.get("things_i_sent") or []

    @property
    def promised(self) -> list[dict]:
        return self.payload.get("things_i_promised") or []

    @property
    def deadlines(self) -> list[dict]:
        return self.payload.get("deadlines") or []


class SchemaError(ValueError):
    """The model returned something that is not the agreed shape."""


def validate(raw: object) -> dict:
    """Coerce a response into the schema, or refuse it.

    Deliberately strict about structure and forgiving about content: a missing
    list becomes empty, but a string where a list belongs is a refusal. Storing a
    half-understood response would put invented structure into every answer built
    on top of it.
    """
    if not isinstance(raw, dict):
        raise SchemaError(f"expected an object, got {type(raw).__name__}")

    out: dict = {}

    date = raw.get("date")
    out["date"] = date if isinstance(date, str) and date.strip() else None

    direction = raw.get("direction")
    out["direction"] = direction if direction in ("inbound", "outbound") else None

    party = raw.get("counterparty")
    out["counterparty"] = party if isinstance(party, str) and party.strip() else None

    for field, (primary, secondary) in LIST_FIELDS.items():
        value = raw.get(field, [])
        if value is None:
            value = []
        if not isinstance(value, list):
            raise SchemaError(f"{field} must be a list, got {type(value).__name__}")
        items = []
        for entry in value:
            if isinstance(entry, str):
                # A bare string where an object belongs is common and harmless
                # to repair: the text is the whole content of the item.
                items.append({primary: entry.strip(), secondary: None})
                continue
            if not isinstance(entry, dict):
                raise SchemaError(f"{field} entries must be objects or strings")
            what = entry.get(primary) or entry.get("what")
            if not isinstance(what, str) or not what.strip():
                continue        # an item with nothing in it is not an item
            other = entry.get(secondary)
            items.append({
                primary: what.strip()[:300],
                secondary: other.strip()[:120] if isinstance(other, str) and other.strip() else None,
            })
        out[field] = items

    return out


def _document_text(conn: sqlite3.Connection, doc_id: int) -> tuple[str, str]:
    """(header block, body) for one document, trimmed for a local model."""
    row = conn.execute(
        "SELECT title, doc_date, sender, counterparty, source, mime, meta_json"
        " FROM docs WHERE id = ?",
        (doc_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no document {doc_id}")

    try:
        meta = json.loads(row["meta_json"] or "{}")
    except (TypeError, ValueError):
        meta = {}
    to = ", ".join(str(a) for a in (meta.get("to") or [])[:5])

    header = "\n".join(
        line for line in (
            f"Subject: {row['title']}" if row["title"] else "",
            f"Date: {row['doc_date']}" if row["doc_date"] else "",
            f"From: {row['sender']}" if row["sender"] else "",
            f"To: {to}" if to else "",
        ) if line
    )

    body = " ".join(
        r[0] for r in conn.execute(
            "SELECT text FROM chunks WHERE doc_id = ? ORDER BY ord LIMIT 4", (doc_id,)
        )
    )
    return header, body[:MAX_DOC_CHARS]


def _ask_model(prompt: str, model: str, timeout: int = 300) -> str:
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "format": "json",
        "stream": False,
        # Zero temperature so the same document gives the same extraction. A
        # cached answer that changes between runs is not a cache.
        "options": {"temperature": 0, "num_predict": 700},
    }).encode()
    request = urllib.request.Request(
        OLLAMA_URL, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response).get("response", "")


def extract_one(
    conn: sqlite3.Connection,
    doc_id: int,
    *,
    model: str = DEFAULT_MODEL,
    owner: str = "the recipient",
    attempts: int = MAX_ATTEMPTS,
    ask=_ask_model,
) -> Extraction:
    """Extract one document, retrying malformed responses."""
    header, body = _document_text(conn, doc_id)
    prompt = PROMPT.format(owner=owner, document=f"{header}\n\n{body}".strip())

    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            raw = ask(prompt, model)
            return Extraction(doc_id=doc_id, payload=validate(json.loads(raw)), model_id=model)
        except (json.JSONDecodeError, SchemaError, urllib.error.URLError, TimeoutError) as exc:
            last = exc
            log.debug("doc %s extraction attempt %d failed: %s", doc_id, attempt, exc)
    raise SchemaError(f"doc {doc_id}: {last}")


# --- cache -----------------------------------------------------------------


def computed_directions(
    conn: sqlite3.Connection, doc_ids: list[int], own: set[str]
) -> dict[int, str]:
    """Direction from the From header, not from the model.

    The model gets this right 72 times in 73 on this archive, which is not the
    problem. The problem is what it does with an outbound document: in 4 of 19 it
    filled `requests_of_me` with things *I* asked of *them*, which would surface
    as "you owe: confirmation of your visa status" — backwards, and confidently
    so. Direction is computable from the sender, so it is computed, and the
    fields that depend on it are then trusted only in the direction that makes
    sense.
    """
    if not doc_ids or not own:
        return {}
    lowered = {a.lower() for a in own}
    out: dict[int, str] = {}
    for start in range(0, len(doc_ids), 500):
        batch = doc_ids[start : start + 500]
        for row in conn.execute(
            "SELECT id, lower(sender) FROM docs"
            f" WHERE id IN ({','.join('?' * len(batch))})", batch
        ):
            out[row[0]] = "outbound" if (row[1] or "") in lowered else "inbound"
    return out


def load(conn: sqlite3.Connection, doc_ids: list[int],
         version: int = EXTRACTOR_VERSION) -> dict[int, Extraction]:
    if not doc_ids:
        return {}
    out: dict[int, Extraction] = {}
    for start in range(0, len(doc_ids), 500):
        batch = doc_ids[start : start + 500]
        for row in conn.execute(
            "SELECT doc_id, payload, model_id FROM doc_extractions"
            f" WHERE extractor_version = ? AND doc_id IN ({','.join('?' * len(batch))})",
            (version, *batch),
        ):
            try:
                out[row[0]] = Extraction(
                    doc_id=row[0], payload=json.loads(row[1]),
                    model_id=row[2], version=version,
                )
            except ValueError:
                continue
    return out


def store(conn: sqlite3.Connection, extraction: Extraction) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO doc_extractions"
        " (doc_id, extractor_version, model_id, payload, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (extraction.doc_id, extraction.version, extraction.model_id,
         json.dumps(extraction.payload), datetime.now(UTC).isoformat(timespec="seconds")),
    )
    conn.execute(
        "DELETE FROM extraction_failures WHERE doc_id = ? AND extractor_version = ?",
        (extraction.doc_id, extraction.version),
    )
    conn.commit()


def record_failure(conn: sqlite3.Connection, doc_id: int, reason: str,
                   attempts: int, version: int = EXTRACTOR_VERSION) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO extraction_failures"
        " (doc_id, extractor_version, reason, attempts, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (doc_id, version, reason[:300], attempts,
         datetime.now(UTC).isoformat(timespec="seconds")),
    )
    conn.commit()


def failures(conn: sqlite3.Connection, doc_ids: list[int],
             version: int = EXTRACTOR_VERSION) -> dict[int, str]:
    if not doc_ids:
        return {}
    return {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT doc_id, reason FROM extraction_failures"
            f" WHERE extractor_version = ? AND doc_id IN ({','.join('?' * len(doc_ids))})",
            (version, *doc_ids),
        )
    }


def pending(conn: sqlite3.Connection, doc_ids: list[int],
            version: int = EXTRACTOR_VERSION) -> list[int]:
    """Documents with neither an extraction nor a recorded failure.

    Resumable by construction: run it again and it picks up exactly where it
    stopped.
    """
    done = set(load(conn, doc_ids, version)) | set(failures(conn, doc_ids, version))
    return [d for d in doc_ids if d not in done]


def extract_set(
    conn: sqlite3.Connection,
    doc_ids: list[int],
    *,
    model: str = DEFAULT_MODEL,
    owner: str = "the recipient",
    version: int = EXTRACTOR_VERSION,
    progress=None,
    should_stop=None,
    ask=_ask_model,
) -> tuple[int, int]:
    """Extract everything outstanding. Returns (extracted, failed)."""
    todo = pending(conn, doc_ids, version)
    extracted = failed = 0
    for index, doc_id in enumerate(todo, start=1):
        if should_stop is not None and should_stop():
            break
        try:
            result = extract_one(conn, doc_id, model=model, owner=owner, ask=ask)
        except (SchemaError, LookupError) as exc:
            record_failure(conn, doc_id, str(exc), MAX_ATTEMPTS, version)
            failed += 1
        else:
            result.version = version
            store(conn, result)
            extracted += 1
        if progress:
            progress(index, len(todo), doc_id)
    return extracted, failed
