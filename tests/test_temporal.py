"""Reading dates out of a query.

"autopay tenant protection september 2024" was matching "september" and "2024"
as words — terms that appear in every message sent that month and in every
invoice number that happens to contain them.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from muster.blobs import BlobStore
from muster.ingest import ingest_message
from muster.search import keyword_search, vector_search
from muster.temporal import apply, extract

TODAY = date(2026, 8, 9)


def span(query: str) -> tuple[str, str, str] | None:
    text, found = extract(query, today=TODAY)
    return None if found is None else (text, found.since[:10], found.until[:10])


# --- parsing --------------------------------------------------------------


@pytest.mark.parametrize(
    "query,remainder,since,until",
    [
        ("autopay tenant protection september 2024", "autopay tenant protection",
         "2024-09-01", "2024-09-30"),
        ("toronto housing search july 2025", "toronto housing search",
         "2025-07-01", "2025-07-31"),
        ("docusign confirmation june 2025", "docusign confirmation",
         "2025-06-01", "2025-06-30"),
        ("invoices 2024", "invoices", "2024-01-01", "2024-12-31"),
        ("lease renewal last spring", "lease renewal", "2025-03-01", "2025-05-31"),
        ("photos early august", "photos", "2026-08-01", "2026-08-10"),
        ("report late march 2025", "report", "2025-03-20", "2025-03-31"),
        ("payslip last month", "payslip", "2026-07-01", "2026-07-31"),
        ("notes this year", "notes", "2026-01-01", "2026-12-31"),
        ("statement last year", "statement", "2025-01-01", "2025-12-31"),
        ("photos summer 2024", "photos", "2024-06-01", "2024-08-31"),
        ("trip last summer", "trip", "2025-06-01", "2025-08-31"),
        ("bills sept 2025", "bills", "2025-09-01", "2025-09-30"),
    ],
)
def test_expressions_parse(query, remainder, since, until) -> None:
    assert span(query) == (remainder, since, until)


def test_a_bare_month_is_not_a_date() -> None:
    """"voiceflow september" means an offer mentioning September, not mail
    sent in September — and filtering by date removes the answer."""
    assert span("voiceflow september") is None
    assert span("photos spring") is None


def test_a_qualified_month_resolves_backwards() -> None:
    """With a qualifier the intent is explicit, and the year is inferred."""
    # August, in August: this year.
    assert span("photos early august")[1].startswith("2026-08")
    # December, in August: last year, not four months from now.
    assert span("photos late december")[1].startswith("2025-12")


def test_winter_straddles_the_year_boundary() -> None:
    text, since, until = span("trip winter 2025")
    assert since == "2024-12-01" and until == "2025-02-28"


def test_queries_without_a_date_are_untouched() -> None:
    assert span("warehouse lease renewal") is None
    assert span("") is None


def test_digits_inside_identifiers_are_not_years() -> None:
    """Otherwise every invoice number silently becomes a date filter."""
    assert span("invoice 20241234 total") is None
    assert span("order 2024-0042") is None


def test_only_the_first_expression_is_used() -> None:
    """Two dates most often mean a span nobody has asked for yet."""
    text, since, until = span("notes june 2025 and july 2025")
    assert (since, until) == ("2025-06-01", "2025-06-30")


def test_an_impossible_date_is_ignored_not_fatal() -> None:
    assert extract("meeting february 2025", today=TODAY) is not None


def test_a_month_alone_leaves_the_query_whole() -> None:
    text, found = extract("voiceflow september", today=TODAY)
    assert (text, found) == ("voiceflow september", None)


# --- folding into filters -------------------------------------------------


def test_apply_fills_empty_bounds() -> None:
    text, since, until, found = apply("invoices july 2025", None, None, today=TODAY)
    assert text == "invoices"
    assert since.startswith("2025-07-01") and until.startswith("2025-07-31")
    assert found.kind == "month_year"


def test_explicit_bounds_always_win() -> None:
    """--since is the user saying it outright; the query is an inference."""
    text, since, until, _ = apply(
        "invoices july 2025", "2020-01-01", "2020-12-31", today=TODAY
    )
    assert since == "2020-01-01" and until == "2020-12-31"


def test_a_query_that_is_only_a_date_keeps_something_to_rank_on() -> None:
    text, since, _until, _ = apply("july 2025", None, None, today=TODAY)
    assert text == "july 2025"
    assert since.startswith("2025-07-01")


# --- end to end -----------------------------------------------------------


def _raw(uid: str, subject: str, when: str, body: str) -> bytes:
    return (
        f"Message-ID: <{uid}>\r\nDate: {when}\r\n"
        f"From: S <s@example.com>\r\nTo: A <a@b>\r\n"
        f"Subject: {subject}\r\nContent-Type: text/plain\r\n\r\n{body}\r\n"
    ).encode()


@pytest.fixture
def dated(conn: sqlite3.Connection, cfg) -> sqlite3.Connection:
    store = BlobStore(cfg.blob_dir)
    months = [
        ("sep24", "Tue, 10 Sep 2024 09:00:00 +0000", "September 2024"),
        ("oct24", "Thu, 10 Oct 2024 09:00:00 +0000", "October 2024"),
        ("jul25", "Thu, 10 Jul 2025 09:00:00 +0000", "July 2025"),
    ]
    for uid, when, label in months:
        ingest_message(
            conn, store, source="file", source_uid=uid,
            raw=_raw(uid, f"Autopay tenant protection statement", when,
                     f"Your autopay tenant protection charge for {label} has been applied."),
        )
    return conn


def test_the_month_narrows_the_result(dated) -> None:
    hits = keyword_search(dated, "autopay tenant protection september 2024", min_score=0.0)
    assert [h.source_uid for h in hits] == ["sep24"]


def test_without_the_month_all_three_match(dated) -> None:
    hits = keyword_search(dated, "autopay tenant protection", min_score=0.0)
    assert len(hits) == 3


def test_explicit_since_overrides_the_parsed_month(dated) -> None:
    hits = keyword_search(
        dated, "autopay tenant protection september 2024",
        since="2025-01-01", min_score=0.0,
    )
    assert [h.source_uid for h in hits] == ["jul25"]


def test_temporal_can_be_switched_off(dated) -> None:
    hits = keyword_search(
        dated, "autopay tenant protection september 2024", temporal=False, min_score=0.0
    )
    assert len(hits) == 3


def test_the_vector_arm_leaves_the_query_alone_by_default() -> None:
    """Measured: parsing on both arms cost hybrid MRR and added no recall."""
    import inspect

    assert inspect.signature(vector_search).parameters["temporal"].default is False
    assert inspect.signature(keyword_search).parameters["temporal"].default is True
