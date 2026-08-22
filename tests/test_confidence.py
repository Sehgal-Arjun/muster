"""The confidence floor.

Ten confident-looking wrong answers are worse than none: a ranked list with
scores next to it implies the archive was searched and this is what it had.
So results that only clear the noise level are suppressed and the command says
so instead.

The floor is not a raw BM25 number. BM25 magnitude scales with log(collection
size) and with how many query terms are rare enough to matter, so a constant
would mean different things on different indexes and queries. The configured
value is
BM25 per discriminating clause at a 1000-chunk index, scaled at search time.
"""

from __future__ import annotations

import sqlite3

import pytest

from muster.blobs import BlobStore
from muster.ingest import ingest_message
from muster.search import _scaled_keyword_floor, keyword_search

BODIES = {
    "lease": ("Warehouse lease", "Confirming the warehouse lease renewal at 4200 EUR per month "
              "starting 1 July, with the landlord wanting a decision by 30 April."),
    "solar": ("Solaranlage Angebot", "Anbei das Angebot fuer die Solaranlage, Gesamtpreis "
              "18400 EUR inklusive Montage und acht Wochen Lieferzeit."),
    "invoice": ("Rechnung", "Rechnung Nr 2026-0042 Betrag 1499 EUR faellig 30.04.2026."),
    "lunch": ("Lunch", "Shall we get lunch on Thursday instead of Wednesday this week."),
    "standup": ("Standup", "The standup moved to nine fifteen so the team can join from home."),
    "policy": ("Policy", "Please review the updated expenses policy before the end of the month."),
}


def _raw(uid: str, subject: str, body: str) -> bytes:
    return (
        f"Message-ID: <{uid}>\r\nDate: Mon, 16 Mar 2026 09:00:00 +0000\r\n"
        f"From: S <s@example.com>\r\nTo: A <a@example.com>\r\n"
        f"Subject: {subject}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n"
    ).encode()


# Filler so the index is big enough for IDF to mean something. Below roughly a
# hundred chunks every term looks rare and BM25 cannot separate anything, which
# would make a test of the floor a test of nothing.
FILLER_TOPICS = [
    "quarterly planning session notes and the actions assigned to each team",
    "office move logistics including desk allocation and parking arrangements",
    "supplier onboarding checklist and the documents finance needs first",
    "training budget approvals for the coming year across the department",
    "server maintenance window and the services affected during the outage",
]


@pytest.fixture
def indexed(conn: sqlite3.Connection, cfg) -> sqlite3.Connection:
    store = BlobStore(cfg.blob_dir)
    for uid, (subject, body) in BODIES.items():
        ingest_message(
            conn, store, source="file", source_uid=f"{uid}@example.com",
            raw=_raw(f"{uid}@example.com", subject, body),
        )
    for i in range(120):
        topic = FILLER_TOPICS[i % len(FILLER_TOPICS)]
        ingest_message(
            conn, store, source="file", source_uid=f"filler{i}@example.com",
            raw=_raw(f"filler{i}@example.com", f"Filler {i}", f"Message {i} about {topic}."),
        )
    return conn


# --- the behaviour the user sees ------------------------------------------


def test_a_query_with_a_real_answer_returns_it(indexed) -> None:
    hits = keyword_search(indexed, "warehouse lease renewal")
    assert hits
    assert hits[0].source_uid == "lease@example.com"


def test_a_query_with_no_answer_returns_nothing(indexed) -> None:
    """Not 'the ten least-bad documents'."""
    assert keyword_search(indexed, "lost jacket") == []
    assert keyword_search(indexed, "grandmother birthday present") == []


def test_the_weak_tail_is_cut_not_just_reordered(indexed) -> None:
    unfiltered = keyword_search(indexed, "the lease policy month week", min_score=0.0)
    filtered = keyword_search(indexed, "the lease policy month week")
    assert len(unfiltered) > len(filtered)


def test_the_floor_can_be_switched_off(indexed) -> None:
    assert keyword_search(indexed, "training budget approvals", min_score=0.0) != []


def test_an_explicit_floor_overrides_the_configured_one(indexed) -> None:
    assert keyword_search(indexed, "warehouse lease renewal", min_score=1e6) == []


# --- why the floor is not a constant --------------------------------------


def test_the_floor_scales_with_collection_size(indexed) -> None:
    """The same configured value has to mean the same thing on any index."""
    small = _scaled_keyword_floor(indexed, 8.0, '"warehouse"')

    indexed.execute(
        "INSERT INTO docs (id, source, source_uid, content_hash, doc_date, date_source,"
        " ingested_at) VALUES (9999, 'file', 'bulk', 'h', '2026-01-01T00:00:00Z',"
        " 'header', '2026-01-01T00:00:00Z')"
    )
    indexed.executemany(
        "INSERT INTO chunks (doc_id, ord, text, text_hash) VALUES (9999, ?, ?, ?)",
        [(i, f"filler chunk number {i}", f"h{i}") for i in range(2000)],
    )
    large = _scaled_keyword_floor(indexed, 8.0, '"warehouse"')
    assert large > small


def test_the_floor_does_not_depend_on_query_length(indexed) -> None:
    """Stopword clauses contribute 0.0 to BM25, so the sum does not grow.

    Measured: "warehouse lease renewal" and the same question wrapped in eight
    more words score identically. A length normalisation would punish the
    politely phrased one for words that added nothing.
    """
    short = keyword_search(indexed, "warehouse lease renewal", min_score=0.0)
    long = keyword_search(
        indexed, "what did they say about the warehouse lease renewal figure again",
        min_score=0.0,
    )
    assert short[0].score == pytest.approx(long[0].score, rel=0.01)


def test_a_long_question_still_finds_its_answer(indexed) -> None:
    hits = keyword_search(
        indexed, "what did they say about the warehouse lease renewal figure again"
    )
    assert hits and hits[0].source_uid == "lease@example.com"


def test_a_one_word_query_still_finds_its_answer(indexed) -> None:
    assert keyword_search(indexed, "Solaranlage")


def test_zero_configured_floor_disables_scaling(indexed) -> None:
    assert _scaled_keyword_floor(indexed, 0.0, '"warehouse"') == 0.0


# --- the relative floor ---------------------------------------------------


def test_results_far_below_the_best_are_dropped(indexed) -> None:
    """Scale-free companion: the shape of a bad query is a flat tail."""
    hits = keyword_search(indexed, "warehouse lease renewal month week policy")
    assert hits
    weakest, strongest = hits[-1].score, hits[0].score
    assert weakest >= strongest * 0.25
