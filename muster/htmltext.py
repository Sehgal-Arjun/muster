"""HTML to plain text.

Replaces talon's converter, which refuses any document with more than 419 tags
and returns None. Two thirds of real marketing email crosses that line, so in
practice almost every promotional message was falling through to a regex
fallback that stripped tags with ``<[^>]+>``. That fallback could not see
conditional comments, so Microsoft Office metadata leaked into the index as
bare text — a chunk beginning "96" was ``<o:PixelsPerInch>96</o:PixelsPerInch>``
— and comment residue left stray brackets glued to the next URL.

lxml is already a dependency (talon needs it), has no size limit, and can
actually remove comments and namespaced Office elements rather than trying to
pattern-match around them.
"""

from __future__ import annotations

import logging
import re
import warnings

log = logging.getLogger(__name__)

# Content that is markup, styling or client-specific metadata, never prose.
_DROP_TAGS = ("script", "style", "head", "title", "meta", "link", "noscript", "xml")

# Office namespaces: o:, v:, w:, m:, x:. These live inside conditional comments
# and hold layout metadata that reads as numbers when flattened.
_OFFICE_TAG_RE = re.compile(r"^(?:[ovwmx]:|st\d)", re.IGNORECASE)

_BLOCK_TAGS = frozenset(
    """
    p div br hr tr td th table thead tbody tfoot li ul ol dl dt dd
    h1 h2 h3 h4 h5 h6 blockquote pre section article header footer
    nav aside form fieldset figure figcaption address center
    """.split()
)


def _lxml():
    from lxml import etree, html

    return etree, html


def html_to_text(source: str) -> str:
    """Convert an HTML body to plain text, or return '' if it cannot be parsed.

    Anchor targets are emitted inline as raw URLs. They are reduced to bare
    domains later, by the same pass that handles URLs in plain-text bodies, so
    there is one place where link handling is decided.
    """
    if not source or not source.strip():
        return ""

    try:
        etree, html = _lxml()
    except ImportError:  # pragma: no cover - lxml is a hard dependency
        log.warning("lxml unavailable; cannot convert HTML")
        return ""

    try:
        with warnings.catch_warnings():
            # html5lib's DataLossWarning fires on essentially every real
            # marketing email and says nothing actionable.
            warnings.simplefilter("ignore")
            # Parse bytes, not str, with the encoding pinned. Plenty of real
            # mail opens with <?xml version="1.0" encoding="ISO-8859-1"?>, and
            # lxml refuses a str that carries an encoding declaration. The
            # payload was already decoded correctly upstream using the MIME
            # charset, so UTF-8 here is the truth and the declaration is stale.
            parser = html.HTMLParser(encoding="utf-8", recover=True)
            tree = html.fromstring(source.encode("utf-8", errors="replace"), parser=parser)
    except Exception as exc:
        # Losing a body silently is how a bank alert becomes unfindable, so
        # this is a warning rather than a debug line.
        log.warning("lxml could not parse HTML (%s); body lost", exc)
        return ""

    try:
        # Comments first: conditional comments wrap the Office metadata, and
        # removing the comment removes the whole block with it.
        etree.strip_elements(tree, etree.Comment, *_DROP_TAGS, with_tail=False)
        for element in tree.iter():
            tag = element.tag
            if isinstance(tag, str) and _OFFICE_TAG_RE.match(tag):
                parent = element.getparent()
                if parent is not None:
                    parent.remove(element)
    except Exception as exc:
        log.debug("HTML cleanup failed (%s); continuing with what parsed", exc)

    return _flatten(tree)


def _flatten(tree) -> str:
    parts: list[str] = []

    def emit_break() -> None:
        if parts and parts[-1] != "\n":
            parts.append("\n")

    for element in tree.iter():
        tag = element.tag if isinstance(element.tag, str) else ""
        tag = tag.rsplit("}", 1)[-1].lower()

        if tag in _BLOCK_TAGS:
            emit_break()

        if element.text and element.text.strip():
            parts.append(element.text.strip())
            parts.append(" ")

        if tag == "a":
            href = (element.get("href") or "").strip()
            # mailto: and tel: are contact details worth keeping as-is;
            # javascript: and anchors carry nothing.
            if href and not href.lower().startswith(("javascript:", "#", "data:")):
                parts.append(href)
                parts.append(" ")
        elif tag == "img":
            alt = (element.get("alt") or "").strip()
            if alt:
                parts.append(alt)
                parts.append(" ")

        if tag in _BLOCK_TAGS:
            emit_break()

        if element.tail and element.tail.strip():
            parts.append(element.tail.strip())
            parts.append(" ")

    from muster.textclean import collapse_whitespace

    return collapse_whitespace("".join(parts))
