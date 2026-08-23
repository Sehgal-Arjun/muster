"""Sender-level exclusion.

Gmail files GitHub notifications under `updates`, alongside receipts and
shipping notices — the category that is deliberately *not* filtered. So the
category axis cannot separate them and the sender has to.
"""

from __future__ import annotations

import sqlite3

import pytest

from muster.blobs import BlobStore
from muster.ingest import ingest_message
from muster.parser import parse_message
from muster.purge import plan, purge
from muster.search import keyword_search
from muster.senders import SenderFilter

LIST = [
    "notifications@github.com",
    "no-reply-forms@webflow.com",
    "notify@residence.example",
    "noreply@medium.com",
    "no-reply@spotify.com",
    "substack.com",
]


def _raw(sender: str, subject: str, body: str = "Some content about the build.") -> bytes:
    return (
        f"Message-ID: <{abs(hash(sender + subject))}@x>\r\n"
        f"Date: Mon, 16 Mar 2026 09:00:00 +0000\r\n"
        f"From: Sender <{sender}>\r\nTo: A <a@b>\r\n"
        f"Subject: {subject}\r\nContent-Type: text/plain\r\n\r\n{body}\r\n"
    ).encode()


@pytest.fixture
def filt() -> SenderFilter:
    return SenderFilter(LIST)


# --- matching -------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "notifications@github.com",
        "NOTIFICATIONS@GITHUB.COM",
        "no-reply-forms@webflow.com",
        "notify@residence.example",
        "noreply@medium.com",
        "no-reply@spotify.com",
    ],
)
def test_listed_addresses_are_excluded(filt: SenderFilter, address: str) -> None:
    assert filt.match([address])


@pytest.mark.parametrize(
    "address",
    [
        "someone@substack.com",
        "newsletter@weekly.substack.com",
        "hello@deep.nested.substack.com",
    ],
)
def test_a_bare_domain_matches_its_subdomains(filt: SenderFilter, address: str) -> None:
    """Newsletter platforms give every publication its own subdomain."""
    assert filt.match([address])


@pytest.mark.parametrize(
    "address",
    [
        "no-reply@listings.example",          # explicitly kept
        "support@github.com",           # only the notifications address is listed
        "user@spotify.com",            # only no-reply@ is listed
        "someone@notsubstack.com",      # must not match by suffix alone
        "someone@substack.com.evil.io",
    ],
)
def test_everything_else_is_kept(filt: SenderFilter, address: str) -> None:
    assert filt.match([address]) is None


def test_an_empty_list_excludes_nothing() -> None:
    empty = SenderFilter([])
    assert not empty
    assert empty.match(["notifications@github.com"]) is None


def test_the_matching_pattern_is_reported(filt: SenderFilter) -> None:
    assert filt.match(["x@weekly.substack.com"]) == "substack.com"
    assert filt.match(["notifications@github.com"]) == "notifications@github.com"


# --- storage and retrieval -----------------------------------------------


def test_the_sender_is_stored_as_its_own_column(conn, cfg) -> None:
    store = BlobStore(cfg.blob_dir)
    ingest_message(
        conn, store, source="gmail", source_uid="g1",
        raw=_raw("notifications@github.com", "PR run failed"),
    )
    assert conn.execute("SELECT sender FROM docs").fetchone()[0] == "notifications@github.com"


def test_reparse_makes_an_excluded_sender_unsearchable(conn, cfg) -> None:
    """Already-indexed mail must stop matching without being deleted silently."""
    from muster.connectors.mbox import MboxConnector  # noqa: F401
    from muster.reparse import reparse

    store = BlobStore(cfg.blob_dir)
    ingest_message(
        conn, store, source="gmail", source_uid="g1",
        raw=_raw("notifications@github.com", "PR run failed", "The build failed on main."),
    )
    assert keyword_search(conn, "build failed main", min_score=0.0)

    reparse(conn, store, sender_filter=SenderFilter(LIST))

    assert keyword_search(conn, "build failed main", min_score=0.0) == []
    assert conn.execute("SELECT count(*) FROM docs").fetchone()[0] == 1, "not deleted, just quiet"


def test_reparse_leaves_kept_senders_alone(conn, cfg) -> None:
    from muster.reparse import reparse

    store = BlobStore(cfg.blob_dir)
    ingest_message(
        conn, store, source="gmail", source_uid="r1",
        raw=_raw("no-reply@listings.example", "Your viewing is confirmed", "Viewing at 3pm Thursday."),
    )
    reparse(conn, store, sender_filter=SenderFilter(LIST))
    assert keyword_search(conn, "viewing Thursday", min_score=0.0)


# --- purge ----------------------------------------------------------------


@pytest.fixture
def mixed(conn: sqlite3.Connection, cfg) -> sqlite3.Connection:
    store = BlobStore(cfg.blob_dir)
    senders = [
        ("notifications@github.com", "PR run failed"),
        ("notifications@github.com", "Run failed: CI"),
        ("newsletter@weekly.substack.com", "This week"),
        ("no-reply@spotify.com", "Your Wrapped"),
        ("no-reply@listings.example", "New listing"),
        ("bob@example.com", "Warehouse lease"),
    ]
    for i, (sender, subject) in enumerate(senders):
        ingest_message(
            conn, store, source="gmail", source_uid=f"m{i}", raw=_raw(sender, subject)
        )
    return conn


def test_purge_by_excluded_senders(mixed) -> None:
    preview = plan(mixed, sender_filter=SenderFilter(LIST))
    assert preview.docs == 4
    assert dict(preview.senders)["notifications@github.com"] == 2

    purge(mixed, sender_filter=SenderFilter(LIST))
    remaining = sorted(r[0] for r in mixed.execute("SELECT sender FROM docs"))
    assert remaining == ["bob@example.com", "no-reply@listings.example"]


def test_purge_by_sender_keeps_the_ones_you_asked_to_keep(mixed) -> None:
    purge(mixed, sender_filter=SenderFilter(LIST))
    assert mixed.execute(
        "SELECT count(*) FROM docs WHERE sender = 'no-reply@listings.example'"
    ).fetchone()[0] == 1


def test_domain_purge_reaches_subdomains(mixed) -> None:
    purge(mixed, sender_filter=SenderFilter(["substack.com"]))
    assert mixed.execute(
        "SELECT count(*) FROM docs WHERE sender LIKE '%substack.com'"
    ).fetchone()[0] == 0
    assert mixed.execute("SELECT count(*) FROM docs").fetchone()[0] == 5


def test_an_empty_filter_is_not_a_selector(mixed) -> None:
    """An empty exclusion list must not mean 'purge everything'."""
    with pytest.raises(ValueError, match="without a selector"):
        purge(mixed, sender_filter=SenderFilter([]))


# --- config ---------------------------------------------------------------


def test_the_list_comes_from_config(home) -> None:
    from muster.config import load_config

    (home / "config.toml").write_text(
        "[senders]\nexcluded = ['notifications@github.com', 'substack.com']\n"
    )
    cfg = load_config()
    assert cfg.excluded_senders == ("notifications@github.com", "substack.com")


def test_no_config_means_no_exclusions(home) -> None:
    from muster.config import load_config

    assert load_config().excluded_senders == ()


def test_parse_records_the_exclusion_for_audit(filt: SenderFilter) -> None:
    doc = parse_message(_raw("notifications@github.com", "PR"), "x", sender_filter=filt)
    assert doc.excluded_sender == "notifications@github.com"
    assert doc.headers["excluded_sender"] == "notifications@github.com"
