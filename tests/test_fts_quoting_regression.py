"""Regression tests for the FTS5 term-quoting bug.

Quoting every term keeps punctuation from being parsed as FTS5 syntax — a query
with a stray bracket must not raise. But quoting alone silently turns any
punctuated term into a *strict phrase*: `Rechnung_2026-0042.pdf` becomes the
four-token sequence "rechnung 2026 0042 pdf", which matched the covering email
(whose chunk literally contains the filename) and missed the invoice itself
(whose text reads "Rechnung Nr 2026-0042", with a token in between).

The eval harness found this as a 0.5 recall score on one query. It looked like
a hard query and was a bug. These tests pin both halves of the fix: punctuation
is still safe, and a punctuated term still contributes its parts.
"""

from __future__ import annotations

import sqlite3

import pytest

from muster.blobs import BlobStore
from muster.ingest import ingest_message
from muster.search import build_match_query, keyword_search


def _message(uid: str, subject: str, body: str) -> bytes:
    return (
        f"Message-ID: <{uid}>\r\n"
        f"Date: Mon, 16 Mar 2026 09:00:00 +0000\r\n"
        f"From: Sender <sender@example.com>\r\n"
        f"To: Sam <you@example.com>\r\n"
        f"Subject: {subject}\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n"
    ).encode()


@pytest.fixture
def indexed(conn: sqlite3.Connection, cfg) -> sqlite3.Connection:
    store = BlobStore(cfg.blob_dir)
    corpus = {
        # The filename appears verbatim, adjacent, as it does in a real
        # "Attachments:" line.
        "covering@example.com": _message(
            "covering@example.com",
            "Invoice attached",
            "Attachments: Rechnung_2026-0042.pdf\n\nPlease find the March invoice attached.",
        ),
        # The same tokens, but separated — the case a phrase query cannot reach.
        "invoice@example.com": _message(
            "invoice@example.com",
            "Rechnung",
            "Rechnung Nr 2026-0042 Betrag 1499 EUR faellig 30.04.2026 for the works.",
        ),
        "unrelated@example.com": _message(
            "unrelated@example.com",
            "Lunch",
            "Shall we get lunch on Thursday instead of Wednesday this week?",
        ),
    }
    for uid, raw in corpus.items():
        ingest_message(conn, store, source="file", source_uid=uid, raw=raw)
    return conn


def uids(hits) -> set[str]:
    return {h.source_uid for h in hits}


# --- the regression itself ------------------------------------------------


def test_filename_query_reaches_both_adjacent_and_separated_tokens(indexed) -> None:
    found = uids(keyword_search(indexed, "Rechnung_2026-0042.pdf", limit=10))
    assert "covering@example.com" in found, "the message carrying the file"
    assert "invoice@example.com" in found, "the invoice itself — the half that regressed"


def test_the_exact_filename_still_ranks_first(indexed) -> None:
    """Adding the parts must not cost the exact match its top position."""
    hits = keyword_search(indexed, "Rechnung_2026-0042.pdf", limit=10)
    assert hits[0].source_uid == "covering@example.com"


@pytest.mark.parametrize(
    "term,expected_tokens",
    [
        ("Rechnung_2026-0042.pdf", ["Rechnung", "2026", "0042", "pdf"]),
        ("invoice-2026.xlsx", ["invoice", "2026", "xlsx"]),
        ("report_final_v2.docx", ["report", "final", "v2", "docx"]),
        ("2026-03-16", ["2026", "03", "16"]),
        ("you@example.com", ["you", "example", "com"]),
        ("bananarepublicfactory.com", ["bananarepublicfactory", "com"]),
    ],
)
def test_punctuated_terms_contribute_phrase_and_parts(term, expected_tokens) -> None:
    built = build_match_query(term)
    assert built.startswith(f'"{" ".join(expected_tokens)}"'), "phrase first, so exact wins"
    for token in expected_tokens:
        assert f'"{token}"' in built, f"{token} must also be searchable alone"


def test_underscore_is_a_separator_just_as_fts5_treats_it() -> None:
    """\\w would keep Rechnung_2026 whole; the unicode61 tokenizer splits it."""
    assert '"Rechnung_2026"' not in build_match_query("Rechnung_2026-0042.pdf")


def test_a_single_word_term_stays_a_single_term() -> None:
    assert build_match_query("invoice") == '"invoice"'


def test_an_explicitly_quoted_phrase_stays_strict() -> None:
    """Quotes are how a user asks for adjacency; that must keep working."""
    assert build_match_query('"warehouse lease"') == '"warehouse lease"'


def test_explicit_phrase_does_not_match_separated_tokens(indexed) -> None:
    assert uids(keyword_search(indexed, '"Rechnung Nr 2026"', limit=10)) == {
        "invoice@example.com"
    }
    assert keyword_search(indexed, '"Rechnung 2026 0042 pdf Betrag"', limit=10) == []


# --- the other half: punctuation must never be FTS5 syntax ----------------


@pytest.mark.parametrize(
    "query",
    [
        'a "b',
        "invoice -2026",
        "what? (really)",
        "AND OR NOT NEAR",
        '"',
        "*",
        "^foo",
        "a:b",
        "(((",
        ")))",
        "NEAR/2",
        "col:value AND other",
        "-",
        "--",
        "50% off!",
        "c++ & c#",
        "«guillemets»",
        "emoji 🤭 query",
        "\\",
        "a\tb\nc",
    ],
)
def test_no_query_can_produce_an_fts5_syntax_error(indexed, query: str) -> None:
    keyword_search(indexed, query, limit=5)


def test_fts5_operators_are_searched_for_not_executed(indexed) -> None:
    """A bare OR must be a term, not an operator with a missing right side."""
    assert build_match_query("OR") == '"OR"'
    keyword_search(indexed, "OR", limit=5)
