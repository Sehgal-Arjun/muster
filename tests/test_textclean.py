from __future__ import annotations

import pytest

from muster.htmltext import html_to_text
from muster.textclean import (
    INVISIBLE_CHARS,
    clean_text,
    collapse_urls,
    filter_chunks,
    is_low_content,
    registrable_domain,
    strip_invisibles,
)

# --- invisible characters -------------------------------------------------


@pytest.mark.parametrize(
    "char,name",
    [
        ("​", "zero-width space"),
        ("‌", "zero-width non-joiner"),
        ("‍", "zero-width joiner"),
        ("͏", "combining grapheme joiner"),
        ("­", "soft hyphen"),
        ("﻿", "byte order mark"),
        ("⁠", "word joiner"),
    ],
)
def test_every_listed_invisible_is_removed(char: str, name: str) -> None:
    assert strip_invisibles(f"a{char}b") == "ab", name


def test_preheader_padding_collapses_to_nothing() -> None:
    """The real shape: a headline followed by 900 zero-width characters."""
    padded = "On Sale This Week" + ("‌ " * 900)
    assert clean_text(padded) == "On Sale This Week"


def test_bidi_marks_are_left_alone() -> None:
    """Blanket Cf stripping would corrupt right-to-left text."""
    text = "‫مرحبا‬"
    assert strip_invisibles(text) == text


# --- URLs -----------------------------------------------------------------


@pytest.mark.parametrize(
    "host,expected",
    [
        ("click.email.bananarepublicfactory.com", "bananarepublicfactory.com"),
        ("www.oldnavy.ca", "oldnavy.ca"),
        ("bananarepublicfactory.com", "bananarepublicfactory.com"),
        ("a.b.c.d.example.co.uk", "example.co.uk"),
        ("shop.example.com.au", "example.com.au"),
        ("localhost", "localhost"),
        ("mail.google.com:443", "google.com"),
    ],
)
def test_registrable_domain(host: str, expected: str) -> None:
    assert registrable_domain(host) == expected


def test_tracking_url_becomes_a_bare_domain() -> None:
    url = (
        "https://click.email.oldnavy.ca/?qs=ABB7InYiOjEsImQiOjQ5MzB9AAYAAAAAAsp73zzR"
        "Ej-6MoHVYRXNWacbIQetJTN7l5jPjuBw49tp69DaNiSxTqv1RBOZdjJEqVl"
    )
    assert collapse_urls(f"Shop now {url} today") == "Shop now oldnavy.ca today"


def test_domain_survives_so_domain_queries_still_hit() -> None:
    cleaned = clean_text("See https://click.email.example.com/x/y?q=1")
    assert "example.com" in cleaned


def test_repeated_links_to_one_destination_collapse() -> None:
    """A marketing email with eighty links must not read as eighty mentions."""
    urls = " ".join(f"https://click.email.shop.com/a{i}" for i in range(80))
    assert collapse_urls(urls) == "shop.com"


def test_distinct_domains_are_all_kept() -> None:
    out = collapse_urls("https://a.example.com/1 https://b.other.com/2")
    assert "example.com" in out and "other.com" in out


def test_trailing_punctuation_is_not_part_of_the_domain() -> None:
    assert collapse_urls("see https://example.com/path.") == "see example.com."


# --- low content ----------------------------------------------------------


def test_punctuation_soup_is_low_content() -> None:
    assert is_low_content("=== ||| --- >>> *** ___ +++ ~~~ ^^^ %%%")


def test_short_fragment_is_low_content() -> None:
    assert is_low_content("View | Unsub")


def test_real_prose_is_not_low_content() -> None:
    assert not is_low_content(
        "Confirming the warehouse lease renewal at 4200 EUR per month."
    )


def test_filter_drops_junk_but_keeps_prose() -> None:
    chunks = ["=== ||| ---", "Confirming the lease renewal at 4200 EUR per month.", "· · ·"]
    assert filter_chunks(chunks) == [chunks[1]]


def test_filter_may_legitimately_return_nothing() -> None:
    """Keeping junk to stay findable would index the junk; the caller uses the title."""
    assert filter_chunks(["=== ||| ---", "· · ·"]) == []


def test_filter_on_empty_input_returns_empty() -> None:
    assert filter_chunks([]) == []


# --- HTML -----------------------------------------------------------------


def test_office_metadata_does_not_leak_as_text() -> None:
    """The '96' bug: <o:PixelsPerInch>96</o:PixelsPerInch> in a conditional comment."""
    html = (
        "<html><head><title>t</title></head><body>"
        "<!--[if gte mso 9]><xml><o:OfficeDocumentSettings>"
        "<o:PixelsPerInch>96</o:PixelsPerInch><o:AllowPNG/>"
        "</o:OfficeDocumentSettings></xml><![endif]-->"
        "<p>Match day ready</p></body></html>"
    )
    out = html_to_text(html)
    assert out.strip() == "Match day ready"
    assert "96" not in out


def test_conditional_comment_residue_leaves_no_stray_brackets() -> None:
    html = "<div><!--[if !mso]><!--><span>Real text</span><!--<![endif]--></div>"
    out = html_to_text(html)
    assert "]" not in out and "[" not in out
    assert "Real text" in out


def test_script_and_style_are_dropped() -> None:
    html = "<style>.a{color:red}</style><script>var x=1</script><p>Body</p>"
    out = html_to_text(html)
    assert "color" not in out and "var x" not in out
    assert "Body" in out


def test_large_document_is_converted_rather_than_refused() -> None:
    """talon gives up past 419 tags; two thirds of real mail is bigger than that."""
    html = "<html><body>" + "<div><span>x</span></div>" * 800 + "<p>needle</p></body></html>"
    assert "needle" in html_to_text(html)


def test_anchor_href_is_emitted_then_reduced_to_a_domain() -> None:
    html = '<a href="https://click.email.skillshare.com/abc123">Skillshare</a>'
    assert clean_text(html_to_text(html)) == "Skillshare skillshare.com"


def test_no_markdown_link_syntax_is_produced() -> None:
    html = '<p>Read <a href="https://example.com/a">Skillshare</a> now</p>'
    out = clean_text(html_to_text(html))
    assert "](" not in out
    assert "[" not in out


def test_block_tags_become_line_breaks() -> None:
    assert html_to_text("<p>one</p><p>two</p>").splitlines() == ["one", "two"]


def test_image_alt_text_is_kept() -> None:
    assert "Company logo" in html_to_text('<img src="x.png" alt="Company logo">')


def test_unparseable_html_returns_empty_rather_than_raising() -> None:
    assert html_to_text("") == ""
    assert html_to_text("   ") == ""


# --- regressions found against the real archive ---------------------------


def test_xml_declaration_does_not_lose_the_body() -> None:
    """A CIBC alert opened with this and silently indexed as an empty document.

    lxml refuses a str carrying an encoding declaration; the exception was
    caught and the body became ''. Real bank statements went unfindable.
    """
    html = (
        '<?xml version="1.0" encoding="ISO-8859-1"?><html><head/><body>'
        "<table><tr><td>Your eStatement for the current month is now available."
        "</td></tr></table></body></html>"
    )
    out = html_to_text(html)
    assert "eStatement" in out
    assert "current month is now available" in out


def test_mis_declared_charset_does_not_lose_the_body() -> None:
    html = '<?xml version="1.0" encoding="WINDOWS-1252"?><html><body><p>Grüße</p></body></html>'
    assert "Grüße" in html_to_text(html)


def test_bare_scheme_with_no_host_is_removed() -> None:
    assert "http" not in collapse_urls('<a href="https://">x</a> https://')


def test_table_layout_text_is_extracted() -> None:
    """Bank and receipt email is almost entirely nested tables."""
    html = "<table><tr><td><table><tr><td>Balance below $100</td></tr></table></td></tr></table>"
    assert "Balance below $100" in html_to_text(html)
