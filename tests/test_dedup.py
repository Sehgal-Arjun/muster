"""Collapsing byte-identical results.

One offer-letter PDF in the real archive is attached to four messages under
three filenames. Searching for it returned all four, taking ranks one to four
of a ten-result list — six slots doing the work of nine.
"""

from __future__ import annotations

import sqlite3

import pytest

from muster.blobs import BlobStore
from muster.ingest import ingest_message
from muster.search import Hit, collapse_duplicates, keyword_search

BODY = (
    "VOICEFLOW, INC. 24 July 2025. On behalf of the Company I am pleased to "
    "offer you the position of FullStack Developer starting in September."
)


def _raw(uid: str, subject: str, body: str) -> bytes:
    return (
        f"Message-ID: <{uid}>\r\nDate: Mon, 16 Mar 2026 09:00:00 +0000\r\n"
        f"From: S <s@example.com>\r\nTo: A <a@b>\r\n"
        f"Subject: {subject}\r\nContent-Type: text/plain\r\n\r\n{body}\r\n"
    ).encode()


@pytest.fixture
def duplicated(conn: sqlite3.Connection, cfg) -> sqlite3.Connection:
    store = BlobStore(cfg.blob_dir)
    # The same bytes arriving four times, plus one genuinely different message.
    same = _raw("offer@x", "Offer letter", BODY)
    for i in range(4):
        ingest_message(conn, store, source="gmail", source_uid=f"copy{i}", raw=same)
    ingest_message(
        conn, store, source="gmail", source_uid="other",
        raw=_raw("other@x", "Different offer", "A FullStack Developer offer from someone else."),
    )
    return conn


def _hit(doc_id: int, score: float, content_hash: str | None) -> Hit:
    return Hit(
        doc_id=doc_id, chunk_id=doc_id, title=f"doc {doc_id}", source="gmail",
        source_uid=str(doc_id), doc_date="2026-01-01T00:00:00Z", counterparty=None,
        score=score, snippet="", content_hash=content_hash,
    )


# --- the unit -------------------------------------------------------------


def test_identical_hashes_collapse_to_one(cfg) -> None:
    hits = [_hit(1, 9.0, "aaa"), _hit(2, 8.0, "aaa"), _hit(3, 7.0, "bbb")]
    out = collapse_duplicates(hits)
    assert [h.doc_id for h in out] == [1, 3]


def test_the_survivor_is_the_best_scoring_copy(cfg) -> None:
    hits = [_hit(1, 9.0, "aaa"), _hit(2, 8.0, "aaa")]
    assert collapse_duplicates(hits)[0].doc_id == 1


def test_the_count_of_copies_is_reported(cfg) -> None:
    hits = [_hit(1, 9.0, "aaa"), _hit(2, 8.0, "aaa"), _hit(3, 7.0, "aaa"), _hit(4, 6.0, "bbb")]
    out = collapse_duplicates(hits)
    assert out[0].duplicates == 3
    assert out[1].duplicates == 1


def test_distinct_documents_are_untouched(cfg) -> None:
    hits = [_hit(1, 9.0, "aaa"), _hit(2, 8.0, "bbb"), _hit(3, 7.0, "ccc")]
    assert [h.doc_id for h in collapse_duplicates(hits)] == [1, 2, 3]


def test_a_missing_hash_never_merges(cfg) -> None:
    """Two documents with no hash recorded are not thereby the same document."""
    hits = [_hit(1, 9.0, None), _hit(2, 8.0, None)]
    assert len(collapse_duplicates(hits)) == 2


def test_dedup_can_be_switched_off(cfg, home) -> None:
    (home / "config.toml").write_text("[search]\ndedupe_by_content = false\n")
    hits = [_hit(1, 9.0, "aaa"), _hit(2, 8.0, "aaa")]
    assert len(collapse_duplicates(hits)) == 2


# --- end to end -----------------------------------------------------------


def test_four_copies_do_not_take_four_slots(duplicated) -> None:
    hits = keyword_search(duplicated, "fullstack developer offer", min_score=0.0)
    uids = [h.source_uid for h in hits]

    assert len(uids) == 2, f"one per distinct file, got {uids}"
    assert "other" in uids
    assert sum(h.duplicates for h in hits) == 5


def test_the_freed_slots_go_to_different_documents(duplicated) -> None:
    """The point of collapsing: room for something you have not already seen."""
    hits = keyword_search(duplicated, "fullstack developer offer", limit=2, min_score=0.0)
    hashes = {h.content_hash for h in hits}
    assert len(hashes) == len(hits), "no two results are the same file"


def test_ranking_still_puts_the_best_match_first(duplicated) -> None:
    hits = keyword_search(duplicated, "voiceflow september", min_score=0.0)  # noqa: E501
    assert hits[0].content_hash is not None
    assert hits[0].duplicates >= 1
