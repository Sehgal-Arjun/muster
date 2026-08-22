from __future__ import annotations

import sqlite3

import pytest

from muster.blobs import BlobStore
from muster.categories import (
    DEFAULT_EXCLUDED,
    category_of,
    is_excluded,
    normalise,
    parse_selection,
)
from muster.ingest import ingest_message
from muster.search import keyword_search

PROMO = (
    b"Message-ID: <promo-1@shop.example>\r\n"
    b"Date: Mon, 16 Mar 2026 09:00:00 +0000\r\n"
    b"From: Shop <deals@shop.example>\r\n"
    b"To: Sam <you@example.com>\r\n"
    b"Subject: Half price warehouse clearance\r\n"
    b"Content-Type: text/plain\r\n\r\n"
    b"Our warehouse clearance sale starts today with everything half price.\r\n"
)
REAL = (
    b"Message-ID: <real-1@example.com>\r\n"
    b"Date: Mon, 16 Mar 2026 10:00:00 +0000\r\n"
    b"From: Bob <bob@example.com>\r\n"
    b"To: Sam <you@example.com>\r\n"
    b"Subject: Warehouse lease\r\n"
    b"Content-Type: text/plain\r\n\r\n"
    b"Confirming the warehouse lease renewal at 4200 EUR per month.\r\n"
)


# --- label parsing --------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("CATEGORY_PROMOTIONS", "promotions"),
        ("promotions", "promotions"),
        ("PROMOTIONS", "promotions"),
    ],
)
def test_normalise_accepts_every_spelling(raw: str, expected: str) -> None:
    assert normalise(raw) == expected


def test_category_of_reads_gmail_labels() -> None:
    assert category_of(["INBOX", "CATEGORY_PROMOTIONS"]) == "promotions"
    assert category_of(["INBOX", "CATEGORY_SOCIAL"]) == "social"
    assert category_of(["INBOX", "UNREAD"]) is None
    assert category_of([]) is None
    assert category_of(None) is None


def test_multiple_categories_resolve_to_the_most_exclusion_worthy() -> None:
    assert category_of(["CATEGORY_UPDATES", "CATEGORY_PROMOTIONS"]) == "promotions"


def test_updates_is_not_excluded_by_default() -> None:
    """Receipts and shipping notices live in updates, and those are wanted."""
    assert "updates" not in DEFAULT_EXCLUDED
    assert DEFAULT_EXCLUDED == {"promotions", "social"}


def test_is_excluded() -> None:
    assert is_excluded(["CATEGORY_PROMOTIONS"], DEFAULT_EXCLUDED)
    assert not is_excluded(["CATEGORY_UPDATES"], DEFAULT_EXCLUDED)
    assert not is_excluded(["INBOX"], DEFAULT_EXCLUDED)


def test_parse_selection() -> None:
    assert parse_selection("promotions,social") == {"promotions", "social"}
    assert parse_selection("CATEGORY_PROMOTIONS") == {"promotions"}
    assert parse_selection(None) == frozenset()
    with pytest.raises(ValueError, match="unknown categories"):
        parse_selection("promotions,nonsense")


# --- storage --------------------------------------------------------------


def test_labels_land_in_meta_json_and_the_category_column(conn, cfg) -> None:
    import json

    store = BlobStore(cfg.blob_dir)
    ingest_message(
        conn, store, source="gmail", source_uid="p1", raw=PROMO,
        meta={"labels": ["INBOX", "CATEGORY_PROMOTIONS"], "thread_uid": "t1"},
    )
    row = conn.execute("SELECT category, meta_json FROM docs WHERE source_uid='p1'").fetchone()

    assert row["category"] == "promotions"
    assert "CATEGORY_PROMOTIONS" in json.loads(row["meta_json"])["labels"]


def test_non_gmail_docs_have_a_null_category(conn, cfg) -> None:
    store = BlobStore(cfg.blob_dir)
    ingest_message(conn, store, source="file", source_uid="f1", raw=REAL)
    assert conn.execute("SELECT category FROM docs").fetchone()["category"] is None


# --- retrieval ------------------------------------------------------------


@pytest.fixture
def mixed(conn: sqlite3.Connection, cfg) -> sqlite3.Connection:
    store = BlobStore(cfg.blob_dir)
    ingest_message(
        conn, store, source="gmail", source_uid="p1", raw=PROMO,
        meta={"labels": ["CATEGORY_PROMOTIONS"]},
    )
    ingest_message(conn, store, source="gmail", source_uid="r1", raw=REAL, meta={"labels": ["INBOX"]})
    return conn


def test_promotions_are_hidden_by_default(mixed) -> None:
    uids = {h.source_uid for h in keyword_search(mixed, "warehouse", min_score=0.0)}
    assert "r1" in uids
    assert "p1" not in uids


def test_promotions_are_reachable_when_asked_for(mixed) -> None:
    uids = {h.source_uid for h in keyword_search(mixed, "warehouse", exclude_categories=frozenset(), min_score=0.0)}
    assert {"p1", "r1"} <= uids


def test_uncategorised_docs_always_pass_the_filter(conn, cfg) -> None:
    """NULL means 'not categorised' — every mbox doc and all personal mail."""
    store = BlobStore(cfg.blob_dir)
    ingest_message(conn, store, source="file", source_uid="f1", raw=REAL)
    assert keyword_search(conn, "warehouse", min_score=0.0)


def test_social_is_hidden_too(conn, cfg) -> None:
    store = BlobStore(cfg.blob_dir)
    ingest_message(
        conn, store, source="gmail", source_uid="s1", raw=PROMO,
        meta={"labels": ["CATEGORY_SOCIAL"]},
    )
    assert keyword_search(conn, "warehouse", min_score=0.0) == []


def test_sync_can_drop_excluded_categories_before_ingest(conn, cfg) -> None:
    """--exclude-categories keeps them out of the index entirely, not just search."""
    from muster.categories import is_excluded as excluded

    messages = [
        ("p1", PROMO, ["CATEGORY_PROMOTIONS"]),
        ("r1", REAL, ["INBOX"]),
    ]
    store = BlobStore(cfg.blob_dir)
    for uid, raw, labels in messages:
        if excluded(labels, frozenset({"promotions"})):
            continue
        ingest_message(conn, store, source="gmail", source_uid=uid, raw=raw, meta={"labels": labels})

    assert [r[0] for r in conn.execute("SELECT source_uid FROM docs")] == ["r1"]
