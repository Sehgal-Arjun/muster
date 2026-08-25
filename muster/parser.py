"""RFC822 bytes to structured rows.

Every connector's output funnels through here, so this is where all the mail
weirdness is allowed to live: malformed MIME, charsets that do not exist,
missing Date headers, Content-Transfer-Encoding that lies.

The contract is that :func:`parse_message` always returns a ``ParsedMessage``.
Anything it could not understand is recorded in ``warnings`` and the run
continues. Losing one field of one message is acceptable; losing the message,
or the run, is not.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import Message
from typing import Any

from muster import ldjson
from muster.extract import decode_text
from muster.quoting import clean_body

log = logging.getLogger(__name__)

NO_SUBJECT = "(no subject)"

# Multipart containers that are structure, not content.
_CONTAINER_TYPES = {"multipart"}


@dataclass
class ParsedAttachment:
    filename: str | None
    mime: str | None
    data: bytes

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass
class ParsedMessage:
    source_uid: str
    content_hash: str
    body: str
    title: str
    doc_date: str
    date_source: str
    counterparty: str | None
    thread_uid: str | None
    mime: str
    attachments: list[ParsedAttachment] = field(default_factory=list)
    headers: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # Set when a redaction rule fired. The body is already emptied by then;
    # this records why, so the decision is auditable from meta_json.
    redacted_by: str | None = None
    # Set when the sender is on the exclusion list. Distinct from redaction:
    # nothing here is sensitive, it is simply not worth indexing.
    excluded_sender: str | None = None


# --- header helpers -------------------------------------------------------


def _load(raw: bytes) -> tuple[Message, list[str]]:
    """Parse bytes into a Message, degrading to compat32 if the modern policy chokes."""
    warnings: list[str] = []
    try:
        return email.message_from_bytes(raw, policy=email.policy.default), warnings
    except Exception as exc:
        warnings.append(f"policy.default failed ({exc}); parsed with compat32")
        try:
            return email.message_from_bytes(raw), warnings
        except Exception as exc2:  # pragma: no cover - email is extremely tolerant
            warnings.append(f"compat32 also failed ({exc2}); empty message")
            return email.message_from_string(""), warnings


def _header(msg: Message, name: str) -> str:
    """A header as a plain str, with RFC2047 decoded and errors swallowed."""
    try:
        value = msg.get(name)
    except Exception:
        return ""
    if value is None:
        return ""
    try:
        text = str(value)
    except Exception:
        return ""
    if "=?" in text:
        try:
            parts = email.header.decode_header(text)
            text = "".join(
                chunk.decode(enc or "utf-8", errors="replace") if isinstance(chunk, bytes) else chunk
                for chunk, enc in parts
            )
        except Exception:
            pass
    return re.sub(r"\s+", " ", text).strip()


def _addresses(msg: Message, name: str) -> list[str]:
    raw = _header(msg, name)
    if not raw:
        return []
    try:
        return [addr.lower() for _display, addr in email.utils.getaddresses([raw]) if addr]
    except Exception:
        return []


def _display_names(msg: Message, name: str) -> list[str]:
    """The human-readable part of an address header.

    "Mira Halvorsen <m.halvorsen@university.example>" carries the only copy of that
    name in the whole archive; the address tokenises as one word and never
    matches the two-word query someone would type.
    """
    raw = _header(msg, name)
    if not raw:
        return []
    try:
        pairs = email.utils.getaddresses([raw])
    except Exception:
        return []
    out: list[str] = []
    for display, address in pairs:
        display = (display or "").strip().strip("\"'")
        # A display name that just repeats the address adds nothing.
        if display and display.lower() != (address or "").lower():
            out.append(display)
    return out


def resolve_date(
    msg: Message, meta: dict[str, Any], warnings: list[str]
) -> tuple[str, str]:
    """(iso8601, date_source). Ordered by how much we trust the origin."""
    raw = _header(msg, "Date")
    if raw:
        try:
            parsed = email.utils.parsedate_to_datetime(raw)
            if parsed is not None:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                return parsed.astimezone(UTC).isoformat(), "header"
        except (TypeError, ValueError) as exc:
            warnings.append(f"unparseable Date header {raw!r} ({exc})")

    # Gmail's internalDate: the provider's own receipt timestamp, in ms.
    internal = meta.get("internal_date")
    if internal:
        try:
            stamp = datetime.fromtimestamp(int(internal) / 1000, tz=UTC)
            return stamp.isoformat(), "provider"
        except (TypeError, ValueError, OSError) as exc:
            warnings.append(f"unusable internal_date {internal!r} ({exc})")

    for received in reversed(msg.get_all("Received") or []):
        _, _, tail = str(received).rpartition(";")
        if tail.strip():
            try:
                parsed = email.utils.parsedate_to_datetime(tail.strip())
                if parsed is not None:
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=UTC)
                    return parsed.astimezone(UTC).isoformat(), "received"
            except (TypeError, ValueError):
                continue

    warnings.append("no usable date; fell back to ingestion time")
    return datetime.now(UTC).isoformat(), "ingest"


def resolve_counterparty(
    from_addrs: list[str],
    to_addrs: list[str],
    cc_addrs: list[str],
    own: set[str],
    labels: list[str],
) -> str | None:
    """The other party: the sender, unless we sent it, in which case a recipient."""
    sender = from_addrs[0] if from_addrs else None
    outbound = "SENT" in labels or (sender is not None and sender in own)

    if outbound:
        for addr in to_addrs + cc_addrs:
            if addr not in own:
                return addr
        return to_addrs[0] if to_addrs else sender
    if sender and sender in own:
        for addr in to_addrs + cc_addrs:
            if addr not in own:
                return addr
    return sender


def resolve_thread(msg: Message, meta: dict[str, Any]) -> str | None:
    """Provider thread id if we have one; otherwise the root of References."""
    provider = meta.get("thread_uid")
    if provider:
        return str(provider)

    references = _header(msg, "References")
    if references:
        ids = re.findall(r"<[^>]+>", references)
        if ids:
            return ids[0]
    in_reply_to = _header(msg, "In-Reply-To")
    if in_reply_to:
        ids = re.findall(r"<[^>]+>", in_reply_to)
        if ids:
            return ids[0]
    return _header(msg, "Message-ID") or None


# --- body and attachments -------------------------------------------------


def _part_bytes(part: Message, warnings: list[str]) -> bytes:
    try:
        payload = part.get_payload(decode=True)
    except Exception as exc:
        warnings.append(f"undecodable part ({exc})")
        return b""
    if payload is None:
        # A part whose transfer encoding lied; take the raw string.
        try:
            raw = part.get_payload()
            return raw.encode("utf-8", errors="replace") if isinstance(raw, str) else b""
        except Exception:
            return b""
    return payload


def _part_text(part: Message, warnings: list[str]) -> str:
    data = _part_bytes(part, warnings)
    if not data:
        return ""
    charset = None
    try:
        charset = part.get_content_charset()
    except Exception as exc:
        warnings.append(f"unreadable charset ({exc})")
    return decode_text(data, charset)


def _content_type(part: Message) -> str:
    try:
        return (part.get_content_type() or "").lower()
    except Exception:
        return "text/plain"


def _filename(part: Message) -> str | None:
    try:
        name = part.get_filename()
    except Exception:
        return None
    if not name:
        return None
    if "=?" in name:
        try:
            chunks = email.header.decode_header(name)
            name = "".join(
                c.decode(enc or "utf-8", errors="replace") if isinstance(c, bytes) else c
                for c, enc in chunks
            )
        except Exception:
            pass
    # Defensive: a filename from an untrusted message must never be a path.
    return name.replace("\x00", "").strip().rsplit("/", 1)[-1].rsplit("\\", 1)[-1] or None


def _disposition(part: Message) -> str:
    try:
        return (part.get_content_disposition() or "").lower()
    except Exception:
        return ""


def _walk(msg: Message, warnings: list[str]):
    try:
        yield from msg.walk()
    except Exception as exc:
        warnings.append(f"MIME walk aborted ({exc})")


def split_parts(
    msg: Message, warnings: list[str]
) -> tuple[list[str], list[str], list[ParsedAttachment]]:
    plain: list[str] = []
    html: list[str] = []
    attachments: list[ParsedAttachment] = []

    for part in _walk(msg, warnings):
        ctype = _content_type(part)
        if ctype.split("/", 1)[0] in _CONTAINER_TYPES:
            if part.is_multipart():
                continue
            # Declared multipart but the boundary never appeared, so the body
            # sits directly on the container. Read it rather than lose it.
            warnings.append(f"{ctype} with no parts; treating payload as text")
            text = _part_text(part, warnings)
            if text.strip():
                plain.append(text)
            continue

        filename = _filename(part)
        disposition = _disposition(part)
        is_attachment = disposition == "attachment" or (
            filename is not None and ctype not in ("text/plain", "text/html")
        )

        if is_attachment or (filename and disposition == "inline"):
            data = _part_bytes(part, warnings)
            if data:
                attachments.append(ParsedAttachment(filename=filename, mime=ctype, data=data))
            continue

        if ctype == "text/plain":
            plain.append(_part_text(part, warnings))
        elif ctype == "text/html":
            html.append(_part_text(part, warnings))
        elif ctype.startswith("text/"):
            plain.append(_part_text(part, warnings))
        else:
            data = _part_bytes(part, warnings)
            if data:
                attachments.append(ParsedAttachment(filename=filename, mime=ctype, data=data))

    return plain, html, attachments


# --- entry point ----------------------------------------------------------


def parse_message(
    raw: bytes,
    source_uid: str,
    meta: dict[str, Any] | None = None,
    *,
    own_addresses: set[str] | None = None,
    redactor=None,
    sender_filter=None,
) -> ParsedMessage:
    """Turn RFC822 bytes into a ParsedMessage. Never raises."""
    meta = meta or {}
    own = {a.lower() for a in (own_addresses or set())}
    content_hash = hashlib.sha256(raw).hexdigest()

    msg, warnings = _load(raw)

    plain, html, attachments = split_parts(msg, warnings)

    # text/plain wins; HTML is converted only when it is all we were sent.
    joined_plain = "\n\n".join(p for p in plain if p.strip()).strip()
    joined_html_all = "\n".join(h for h in html if h.strip())

    # JSON-LD is read from the untouched part text, before scripts are stripped
    # from the HTML and before URL collapsing rewrites the schema.org enum URLs
    # the block uses to state its own values. Both parts are scanned because
    # senders duplicate the block across them; the renderer deduplicates.
    structured = ldjson.render(f"{joined_plain}\n{joined_html_all}")
    if structured:
        warnings.append("rendered a JSON-LD block")

    if joined_plain:
        body = clean_body(ldjson.strip_bare_blocks(joined_plain), is_html=False)
        body_mime = "text/plain"
    else:
        joined_html = joined_html_all
        body = clean_body(joined_html, is_html=True) if joined_html else ""
        body_mime = "text/html" if joined_html else "text/plain"
        if not body and joined_html:
            warnings.append("html body produced no text")

    # Structured data leads: a flight's confirmation code and route are the
    # part of the message a person is most likely to search for.
    if structured:
        body = f"{structured}\n\n{body}".strip()

    subject = _header(msg, "Subject") or NO_SUBJECT
    from_addrs = _addresses(msg, "From")
    from_names = _display_names(msg, "From")
    to_addrs = _addresses(msg, "To")
    cc_addrs = _addresses(msg, "Cc")
    labels = list(meta.get("labels") or [])

    doc_date, date_source = resolve_date(msg, meta, warnings)

    # Redaction runs after parsing and before anything is indexed: the decision
    # needs the resolved sender and subject, and must reach every downstream
    # consumer, including reparse.
    redacted_by = None
    if redactor is not None:
        hit = redactor.match(from_addrs, subject)
        if hit is not None:
            redacted_by = hit.rule
            body = ""
            subject = redactor.mask_title(subject)
            attachments = []
            warnings.append(f"redacted by rule {hit.rule!r}")

    excluded_sender = sender_filter.match(from_addrs) if sender_filter is not None else None
    if excluded_sender:
        warnings.append(f"sender excluded by {excluded_sender!r}")

    if not redacted_by and not body.strip() and not attachments:
        warnings.append("empty body and no attachments")

    return ParsedMessage(
        source_uid=source_uid,
        content_hash=content_hash,
        body=body,
        title=subject,
        doc_date=doc_date,
        date_source=date_source,
        counterparty=resolve_counterparty(from_addrs, to_addrs, cc_addrs, own, labels),
        thread_uid=resolve_thread(msg, meta),
        mime=body_mime,
        attachments=attachments,
        headers={
            "from": from_addrs,
            "from_names": from_names,
            "to": to_addrs,
            "cc": cc_addrs,
            "subject": subject,
            "message_id": _header(msg, "Message-ID") or None,
            "in_reply_to": _header(msg, "In-Reply-To") or None,
            "labels": labels,
            "redacted_by": redacted_by,
            "excluded_sender": excluded_sender,
        },
        warnings=warnings,
        redacted_by=redacted_by,
        excluded_sender=excluded_sender,
    )
