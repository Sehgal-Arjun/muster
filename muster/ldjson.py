"""Schema.org JSON-LD blocks, rendered as readable text.

Senders embed structured data for mail clients that show rich cards. Alaska
Airlines puts a whole ``FlightReservation`` in the message — confirmation code,
flight number, airports, times — and it arrives twice: once in a
``<script type="application/ld+json">`` in the HTML part, and once sitting bare
in the middle of the plain-text part.

Both paths were failing in different ways. The script tag was dropped whole
with every other script, and the plain-text copy went through as raw JSON,
which is unreadable, unrankable, and long enough to swallow a chunk. It was
also being quietly corrupted on the way: URL collapsing rewrote
``"reservationStatus": "https://schema.org/ReservationConfirmed"`` to
``"reservationStatus": "schema.org"``.

Flight details are exactly what someone searches an archive for, so the block
is parsed and flattened into labelled lines instead. Rendering is generic
rather than per-schema: every ``@type`` gets the same treatment, so a
LodgingReservation or an Order works the day it first arrives without anyone
having written code for it.
"""

from __future__ import annotations

import json
import logging
import re

log = logging.getLogger(__name__)

_SCRIPT_RE = re.compile(
    r"<script[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
# A bare block in a plain-text part: an object whose first key is @context,
# optionally wrapped in an array. Anchored on @context so we never try to
# JSON-parse arbitrary prose that happens to contain a brace.
_BARE_RE = re.compile(r'(?:\[\s*)?\{\s*"@context"', re.IGNORECASE)

# Structural keys that describe the vocabulary rather than the booking.
_SKIP_KEYS = {"@context", "@id", "additionaltype", "url", "image", "logo", "sameas"}
_MAX_DEPTH = 6
_MAX_LINES = 60

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _humanise(key: str) -> str:
    return _CAMEL_RE.sub(" ", key).replace("_", " ").strip().capitalize()


def _scalar(value) -> str | None:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    # schema.org enum URLs carry their meaning in the last path segment:
    # https://schema.org/ReservationConfirmed -> ReservationConfirmed
    if text.lower().startswith(("http://schema.org/", "https://schema.org/")):
        return text.rsplit("/", 1)[-1]
    if text.lower().startswith(("http://", "https://", "data:")):
        return None
    return text


# Keys whose only job is to hold a nested object. Their names say nothing a
# reader wants — "Reservation for: Flight" — so the child's own @type labels it.
_CONTAINER_KEYS = {"reservationfor", "itemoffered", "ordereditem", "aboutitem"}

# Inside a labelled object these identify the thing, so they take the parent's
# label: departureAirport.name becomes "Departure airport: SFO" rather than a
# free-floating "Name: SFO" that could belong to either end of the flight.
_IDENTITY_KEYS = {"name", "iatacode", "identifier", "code"}


def _walk(node, lines: list[str], depth: int = 0, label: str | None = None) -> None:
    if depth > _MAX_DEPTH or len(lines) >= _MAX_LINES:
        return
    if isinstance(node, list):
        for item in node:
            _walk(item, lines, depth, label)
        return
    if not isinstance(node, dict):
        return

    kind = node.get("@type")
    if label is None and isinstance(kind, str):
        heading = _humanise(kind)
        if heading and heading not in lines:
            lines.append(heading)

    def add(line: str) -> None:
        if line not in lines and len(lines) < _MAX_LINES:
            lines.append(line)

    for key, value in node.items():
        if not isinstance(key, str) or key.lower() in _SKIP_KEYS or key == "@type":
            continue
        if isinstance(value, (dict, list)):
            child = None if key.lower() in _CONTAINER_KEYS else _humanise(key)
            _walk(value, lines, depth + 1, child)
            continue
        rendered = _scalar(value)
        if not rendered:
            continue
        if label and key.lower() in _IDENTITY_KEYS:
            add(f"{label}: {rendered}")
        elif label:
            add(f"{label} {_humanise(key).lower()}: {rendered}")
        else:
            add(f"{_humanise(key)}: {rendered}")


def _decode_candidates(text: str):
    """Yield parsed JSON-LD documents found anywhere in ``text``."""
    for match in _SCRIPT_RE.finditer(text):
        payload = match.group(1).strip()
        try:
            yield json.loads(payload)
        except ValueError as exc:
            log.debug("unparseable ld+json script block (%s)", exc)

    decoder = json.JSONDecoder()
    for match in _BARE_RE.finditer(text):
        start = match.start()
        try:
            value, _end = decoder.raw_decode(text, start)
        except ValueError as exc:
            log.debug("unparseable bare ld+json at %d (%s)", start, exc)
            continue
        yield value


def render(text: str) -> str:
    """Readable text for every JSON-LD block in ``text``. '' when there are none."""
    if not text or "@context" not in text:
        return ""
    lines: list[str] = []
    for document in _decode_candidates(text):
        _walk(document, lines)
    return "\n".join(lines)


def strip_bare_blocks(text: str) -> str:
    """Remove bare JSON-LD from a plain-text body, leaving the prose.

    Only the bare form: blocks inside a script tag are already removed with the
    rest of the markup.
    """
    if not text or "@context" not in text:
        return text
    decoder = json.JSONDecoder()
    out = text
    for match in reversed(list(_BARE_RE.finditer(text))):
        start = match.start()
        try:
            _value, end = decoder.raw_decode(text, start)
        except ValueError:
            continue
        out = out[:start] + " " + out[end:]
    return out
