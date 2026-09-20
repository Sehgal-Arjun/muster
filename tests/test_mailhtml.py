"""Showing a message as it was sent, without trusting it.

An email body is arbitrary HTML written by a stranger and stored for years.
Everything here is about what must not survive the trip to a webview.
"""

from __future__ import annotations

import pytest

from muster import mailhtml

pytest.importorskip("lxml")


def _wrap(body: str) -> bytes:
    return (
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        "Subject: test\r\n\r\n" + body
    ).encode()


def test_scripts_do_not_survive() -> None:
    out = mailhtml.render(_wrap("<p>hello</p><script>alert(1)</script>"))
    assert "hello" in out.html
    assert "<script" not in out.html.lower()
    assert "alert(1)" not in out.html, "script contents were pasted into the body"


@pytest.mark.parametrize("tag", ["iframe", "object", "embed", "form", "link", "meta", "base"])
def test_navigating_and_loading_elements_are_removed(tag: str) -> None:
    out = mailhtml.render(_wrap(f"<p>text</p><{tag}></{tag}>"))
    assert f"<{tag}" not in out.html.lower()


def test_event_handlers_are_stripped() -> None:
    out = mailhtml.render(_wrap('<p onclick="steal()" onmouseover="x()">text</p>'))
    assert "onclick" not in out.html.lower()
    assert "onmouseover" not in out.html.lower()
    assert "text" in out.html


def test_javascript_urls_are_stripped() -> None:
    out = mailhtml.render(_wrap('<a href="javascript:alert(1)">click</a>'))
    assert "javascript:" not in out.html.lower()
    assert "click" in out.html


def test_remote_images_are_removed_and_counted() -> None:
    """A tracking pixel that fails to load has still been requested."""
    out = mailhtml.render(
        _wrap('<p>hi</p><img src="https://track.example/p.gif"><img src="cid:logo">')
    )
    assert out.images_removed == 1
    assert "track.example" not in out.html
    assert "cid:logo" in out.html, "images carried in the message cost nothing to show"


def test_links_open_outside_and_cannot_reach_back() -> None:
    out = mailhtml.render(_wrap('<a href="https://example.com">x</a>'))
    assert 'target="_blank"' in out.html
    assert "noopener" in out.html


def test_a_plain_text_message_reports_no_html() -> None:
    raw = b"Content-Type: text/plain; charset=utf-8\r\nSubject: t\r\n\r\njust words"
    out = mailhtml.render(raw)
    assert out.is_html is False
    assert out.html == ""


def test_an_unparseable_message_does_not_raise() -> None:
    out = mailhtml.render(b"\xff\xfe not a message at all")
    assert out.is_html is False


def test_the_multipart_html_part_is_preferred() -> None:
    raw = (
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/alternative; boundary="b"\r\n\r\n'
        b"--b\r\nContent-Type: text/plain\r\n\r\nplain version\r\n"
        b'--b\r\nContent-Type: text/html\r\n\r\n<p>rich version</p>\r\n'
        b"--b--\r\n"
    )
    out = mailhtml.render(raw)
    assert out.is_html is True
    assert "rich version" in out.html
