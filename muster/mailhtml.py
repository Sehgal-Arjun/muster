"""The HTML of a message, made safe to display.

The indexed text of an email is not the email. It is deliberately mangled for
retrieval — links reduced to bare domains, quoted replies removed, navigation
flattened into prose — which is right for a ranker and wrong for a reader, who
gets a wall of "spotify.com spotify.com spotify.com" where a message used to be.

The original is still in the blob store, so the message can be shown as it was
sent. What it cannot be is trusted: an email body is arbitrary HTML from a
stranger. Everything executable comes out here, before it reaches a webview.

Remote images are dropped rather than blocked at load time. A tracking pixel
that fails to load has still been asked for, and asking is the whole point of
one; a reader should not have to rely on a network policy to avoid telling a
marketer they opened their mail six months later.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Elements that execute, navigate, or load. None of them belong in a message
# being read inside an application.
DANGEROUS_TAGS = (
    "script", "style", "iframe", "frame", "frameset", "object", "embed",
    "applet", "form", "input", "button", "select", "textarea", "link",
    "meta", "base", "noscript", "svg", "math",
)

# Anything that runs on an event, plus the attributes that fetch.
EVENT_ATTR = re.compile(r"^on", re.I)
URL_ATTRS = ("href", "src", "action", "formaction", "background", "poster")
SAFE_SCHEMES = ("http:", "https:", "mailto:", "tel:", "cid:", "#")


@dataclass
class Rendered:
    html: str
    images_removed: int = 0
    # False when the message had no HTML part and the caller should fall back.
    is_html: bool = True


def _lxml():
    from lxml import html as lxml_html

    return lxml_html


def extract_html_part(raw: bytes) -> str | None:
    """The best HTML body in a message, or None if it is plain text only."""
    import email
    import email.policy

    try:
        message = email.message_from_bytes(raw, policy=email.policy.default)
    except Exception as exc:  # noqa: BLE001 — an unparseable message is not fatal
        log.warning("cannot parse message for display: %s", exc)
        return None

    try:
        part = message.get_body(preferencelist=("html",))
    except Exception:  # noqa: BLE001 — malformed multiparts raise here
        part = None
    if part is None:
        return None
    try:
        return part.get_content()
    except Exception as exc:  # noqa: BLE001 — bad charset, truncated base64
        log.warning("cannot decode html part: %s", exc)
        return None


def sanitise(source: str) -> Rendered:
    """Strip everything that executes, navigates on its own, or phones home."""
    lxml_html = _lxml()
    if not source or not source.strip():
        return Rendered(html="", is_html=False)

    try:
        tree = lxml_html.fromstring(source)
    except Exception as exc:  # noqa: BLE001
        log.warning("cannot parse message html: %s", exc)
        return Rendered(html="", is_html=False)

    for element in tree.iter():
        tag = element.tag if isinstance(element.tag, str) else ""
        if tag.lower() in DANGEROUS_TAGS:
            # drop_tree, not drop_tag: the contents of a <script> are the
            # dangerous part, and keeping them would paste code into the body.
            element.drop_tree()

    images_removed = 0
    for element in tree.iter():
        if not isinstance(element.tag, str):
            continue
        for name, value in list(element.attrib.items()):
            if EVENT_ATTR.match(name):
                del element.attrib[name]
                continue
            if name.lower() in URL_ATTRS:
                cleaned = (value or "").strip().replace("\n", "").replace("\t", "")
                if not cleaned.lower().startswith(SAFE_SCHEMES):
                    del element.attrib[name]
        if element.tag.lower() == "img":
            src = (element.get("src") or "").strip().lower()
            # cid: and data: are carried in the message itself and cost nothing
            # to show. Anything fetched over the network is a beacon.
            if src.startswith(("http://", "https://")):
                element.drop_tree()
                images_removed += 1

    # Links open outside; a message must never be able to replace the view it is
    # being read in.
    for anchor in tree.iter("a"):
        anchor.set("target", "_blank")
        anchor.set("rel", "noopener noreferrer nofollow")

    body = tree.find("body")
    html = lxml_html.tostring(
        body if body is not None else tree, encoding="unicode", method="html"
    )
    return Rendered(html=html, images_removed=images_removed, is_html=True)


def render(raw: bytes) -> Rendered:
    """A message's HTML body, safe to hand to a webview."""
    source = extract_html_part(raw)
    if source is None:
        return Rendered(html="", is_html=False)
    return sanitise(source)
