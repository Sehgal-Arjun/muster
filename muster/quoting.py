"""Reducing a message body to what its sender actually typed.

Two passes, in this order:

1. **Quoted replies** — talon, backed by our own regex sweep for the openers it
   misses. Only ``talon.quotations`` is imported: ``talon.signature`` pulls
   scikit-learn (47 MB) for a classifier we do not use, and ``talon.init()``
   would load it, so we call ``register_xpath_extensions()`` directly instead.
2. **Signatures** — a local heuristic, deliberately conservative. A trailing
   block of at most four lines, following a ``--`` delimiter or a closing
   token, is removed only if it also contains a phone number or a URL. A
   sign-off with a name and nothing else stays: the name is searchable signal,
   and a false strip loses information that no later phase can recover.

The corpus is substantially German, so the openers and closings below are
bilingual.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

_xpath_registered = False


def _quotations():
    """Import talon lazily and register its xpath extensions exactly once."""
    global _xpath_registered
    from talon import quotations

    if not _xpath_registered:
        quotations.register_xpath_extensions()
        _xpath_registered = True
    return quotations


# Openers that begin a quoted original. Anchored per-line, case-insensitive.
# talon catches most of these; the duplication is deliberate, because talon
# gives up silently and a missed opener drags a whole thread into every chunk.
QUOTE_OPENERS = [
    # English
    r"^\s*On\b.{0,200}\bwrote:\s*$",
    r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$",
    r"^\s*-{2,}\s*Forwarded message\s*-{2,}\s*$",
    r"^\s*_{10,}\s*$",
    # German
    r"^\s*Am\b.{0,200}\bschrieb\b.{0,200}:\s*$",
    r"^\s*Am\b.{0,120}\bum\b.{0,60}\bschrieb\b.*$",
    r"^\s*-{2,}\s*Urspr[uü]ngliche Nachricht\s*-{2,}\s*$",
    r"^\s*-{2,}\s*Weitergeleitete Nachricht\s*-{2,}\s*$",
    r"^\s*Gesendet von meinem\b.*$",
]
_QUOTE_OPENER_RE = re.compile("|".join(QUOTE_OPENERS), re.IGNORECASE | re.MULTILINE)

# A header block pasted inline by Outlook. One of these alone is too weak — a
# body can legitimately start "From: the team" — so we require two adjacent.
_HEADER_LINE_RE = re.compile(
    r"^\s*(?:From|To|Sent|Subject|Cc|Von|An|Gesendet|Betreff|Kopie)\s*:\s*\S",
    re.IGNORECASE,
)

_CLOSINGS = [
    # English
    "regards", "best regards", "kind regards", "warm regards", "best wishes",
    "best", "thanks", "thank you", "thanks again", "many thanks", "cheers",
    "sincerely", "yours", "yours sincerely", "yours faithfully", "all the best",
    # German
    "viele grüße", "viele gruesse", "mit freundlichen grüßen",
    "mit freundlichen gruessen", "beste grüße", "beste gruesse",
    "liebe grüße", "liebe gruesse", "freundliche grüße", "herzliche grüße",
    "danke", "vielen dank", "lg", "vg", "mfg",
]
_CLOSING_RE = re.compile(
    r"^\s*(?:" + "|".join(re.escape(c) for c in _CLOSINGS) + r")\s*[,.!;:]*\s*$",
    re.IGNORECASE,
)

_SIG_DELIM_RE = re.compile(r"^\s*(?:--|__|—)\s*$")

# Loose on purpose: a false positive only means we strip a block that already
# looked like a signature on two other counts.
_PHONE_RE = re.compile(r"(?:\+\d[\d\s()./-]{7,}\d)|(?:\b\d{3,5}[\s./-]\d{3,}[\s./-]?\d*\b)")
_URL_RE = re.compile(r"(?:https?://|www\.)\S", re.IGNORECASE)

MAX_SIGNATURE_LINES = 4
_SIGNATURE_SEARCH_WINDOW = 12  # lines from the end worth considering


def strip_quotes(text: str) -> str:
    """Remove quoted originals from a plain-text body."""
    from muster.textclean import strip_invisibles

    # Invisibles go first: they break the line anchors the openers rely on, but
    # removing them cannot change line structure the way full cleaning would.
    text = strip_invisibles(text)
    if not text.strip():
        return text

    try:
        text = _quotations().extract_from_plain(text)
    except Exception as exc:  # talon raises on some pathological inputs
        log.debug("talon extract_from_plain failed (%s); using regex fallback", exc)

    return _truncate_at_quote(text)


def strip_quotes_html(html: str) -> str:
    """Remove quoted originals from an HTML body, returning plain text."""
    if not html.strip():
        return ""

    try:
        quotations = _quotations()
        stripped = quotations.extract_from_html(html)
    except Exception as exc:
        log.debug("talon extract_from_html failed (%s); converting raw html", exc)
        stripped = html

    from muster.textclean import strip_invisibles

    return _truncate_at_quote(strip_invisibles(html_to_text(stripped)))


def _truncate_at_quote(text: str) -> str:
    """Cut at the earliest quote opener talon left behind."""
    lines = text.split("\n")

    cut = None
    match = _QUOTE_OPENER_RE.search(text)
    if match:
        cut = text[: match.start()].count("\n")

    # Two adjacent pasted header lines mark an inline forward.
    for i in range(len(lines) - 1):
        if _HEADER_LINE_RE.match(lines[i]) and _HEADER_LINE_RE.match(lines[i + 1]):
            cut = i if cut is None else min(cut, i)
            break

    # A run of '>' quoting that reaches the end of the body.
    for i, line in enumerate(lines):
        if line.lstrip().startswith(">"):
            rest = [ln for ln in lines[i:] if ln.strip()]
            if all(ln.lstrip().startswith(">") for ln in rest):
                cut = i if cut is None else min(cut, i)
            break

    if cut is not None:
        lines = lines[:cut]

    return "\n".join(lines).strip()


def strip_signature(text: str) -> tuple[str, str | None]:
    """Split a trailing signature off. Returns (body, signature_or_None)."""
    lines = text.split("\n")
    start = len(lines) - _SIGNATURE_SEARCH_WINDOW

    best: int | None = None
    for i in range(max(0, start), len(lines)):
        line = lines[i]
        if _SIG_DELIM_RE.match(line):
            best = i + 1  # RFC 3676 delimiter is not part of the signature
        elif _CLOSING_RE.match(line):
            best = i  # the sign-off itself belongs to the block
    if best is None or best >= len(lines):
        return text.strip(), None

    block = lines[best:]
    if len([ln for ln in block if ln.strip()]) > MAX_SIGNATURE_LINES:
        return text.strip(), None

    blob = "\n".join(block)
    if not (_PHONE_RE.search(blob) or _URL_RE.search(blob)):
        return text.strip(), None

    body = "\n".join(lines[:best]).strip()
    return body, blob.strip()


def html_to_text(html: str) -> str:
    """HTML to plain text. See :mod:`muster.htmltext` for why not talon's."""
    from muster.htmltext import html_to_text as _html_to_text

    return _html_to_text(html)


def _collapse_blank_lines(text: str) -> str:
    from muster.textclean import collapse_whitespace

    return collapse_whitespace(text)


def clean_body(text: str, *, is_html: bool = False) -> str:
    """The full reduction: quotes out, signature out, text normalised.

    Normalisation runs last so that quote and signature detection still see the
    original line structure — collapsing whitespace first would merge the very
    line breaks those heuristics anchor on.
    """
    from muster.textclean import clean_text

    text = strip_quotes_html(text) if is_html else strip_quotes(text)
    body, _signature = strip_signature(text)
    return clean_text(body)
