"""Normalising extracted text before it becomes chunks.

Marketing email is the adversary here. It pads preheaders with hundreds of
zero-width characters, wraps every link in 200-plus characters of base64
tracking, and repeats the same destination eighty times down a page. All three
wreck retrieval in the same way: they inflate chunk budgets with tokens that
carry no meaning, distort BM25's term statistics, and pull embeddings toward
the centroid of "generic promotional email".

Everything here runs on both plain-text and HTML-derived bodies, so a tracking
URL pasted into a plain-text message is handled the same way.
"""

from __future__ import annotations

import re

# The set the archive actually contains, as preheader padding: zero-width
# space/non-joiner/joiner, combining grapheme joiner, soft hyphen, BOM, and
# word joiner. Deliberately not a blanket Cf category strip — bidi marks change
# how real text reads, and removing them would corrupt Arabic and Hebrew.
INVISIBLE_CHARS = "​‌‍͏­﻿⁠"
_INVISIBLE_RE = re.compile(f"[{INVISIBLE_CHARS}]")

# Trailing characters excluded so a URL at the end of a sentence keeps its
# punctuation out of the domain.
_URL_RE = re.compile(
    r"""(?xi)
    \b
    (?: https?://  | www\. )
    [^\s<>"'`\]\)]*
    [^\s<>"'`\]\).,;:!?]     # never end on sentence punctuation
    """
)

# Multi-label public suffixes common enough to matter. A full Public Suffix
# List would need a dependency and a refresh cadence; this covers the archive
# and degrades gracefully — an unlisted suffix keeps one label too few, which
# costs precision on a domain query but never loses the document.
_MULTI_SUFFIXES = frozenset(
    """
    co.uk org.uk gov.uk ac.uk me.uk net.uk sch.uk
    com.au net.au org.au edu.au gov.au id.au
    co.nz net.nz org.nz govt.nz
    co.jp or.jp ne.jp ac.jp go.jp
    com.br com.mx com.ar com.co com.pe com.tr com.cn com.hk com.tw com.sg
    co.in net.in org.in gov.in
    co.za org.za
    com.pl com.ua com.ru
    """.split()
)

MIN_CONTENT_CHARS = 24
MIN_ALNUM_RATIO = 0.5


def strip_invisibles(text: str) -> str:
    """Remove zero-width padding. Whitespace runs are collapsed afterwards."""
    return _INVISIBLE_RE.sub("", text)


def registrable_domain(host: str) -> str:
    """`click.email.bananarepublicfactory.com` -> `bananarepublicfactory.com`.

    Tracking subdomains are per-campaign noise; the registrable domain is the
    part a person would actually search for.
    """
    host = host.strip().strip(".").lower()
    if not host:
        return ""
    # Strip credentials and port.
    host = host.rsplit("@", 1)[-1].split(":", 1)[0]
    labels = [label for label in host.split(".") if label]
    if len(labels) <= 2:
        return ".".join(labels)
    if ".".join(labels[-2:]) in _MULTI_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _host_of(url: str) -> str:
    without_scheme = re.sub(r"(?i)^https?://", "", url)
    return without_scheme.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]


def collapse_urls(text: str) -> str:
    """Replace every URL with its registrable domain.

    Runs of the same domain — a marketing email with eighty links to one
    destination — collapse to a single mention, so term frequency reflects that
    the message talked about one sender rather than eighty.
    """

    def replace(match: re.Match[str]) -> str:
        return registrable_domain(_host_of(match.group(0))) or " "

    text = _URL_RE.sub(replace, text)
    # A scheme with no host left behind, e.g. `href="https://"`.
    text = re.sub(r"(?i)\bhttps?://(?![\w-])", " ", text)
    # Collapse immediately repeated domains, allowing whitespace and the
    # punctuation that separates list items between them.
    return re.sub(
        r"\b((?:[\w-]+\.)+[a-z]{2,})(?:[\s,;|·•\-–—]*\1\b)+",
        r"\1",
        text,
        flags=re.IGNORECASE,
    )


def collapse_whitespace(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def clean_text(text: str) -> str:
    """Full normalisation, in the order the transformations depend on."""
    if not text:
        return ""
    text = strip_invisibles(text)
    text = collapse_urls(text)
    return collapse_whitespace(text)


def content_score(text: str) -> tuple[int, float]:
    """(alphanumeric character count, share of characters that carry meaning)."""
    if not text:
        return 0, 0.0
    alnum = sum(1 for ch in text if ch.isalnum())
    meaningful = sum(1 for ch in text if ch.isalnum() or ch.isspace())
    return alnum, meaningful / len(text)


def is_low_content(
    text: str,
    *,
    min_chars: int = MIN_CONTENT_CHARS,
    min_alnum_ratio: float = MIN_ALNUM_RATIO,
) -> bool:
    """Whether a chunk is too thin or too punctuation-heavy to be worth indexing."""
    alnum, ratio = content_score(text)
    return alnum < min_chars or ratio < min_alnum_ratio


def filter_chunks(chunks: list[str]) -> list[str]:
    """Drop chunks too thin or too punctuation-heavy to be worth indexing.

    This may legitimately return nothing. Keeping a junk chunk purely so the
    document stays findable would put the junk in the index; the caller has a
    better answer, which is to fall back to the title
    (see :func:`muster.ingest.build_chunks`).
    """
    return [c for c in chunks if not is_low_content(c)]
