from __future__ import annotations

import mailbox
import sqlite3
from pathlib import Path

import pytest

from muster.blobs import BlobStore
from muster.config import Config
from muster.ingest import ingest_message
from muster.search import build_match_query, keyword_search, search

FIXTURE = Path(__file__).parent / "fixtures" / "sample.mbox"
OWN = {"you@example.com"}


@pytest.fixture
def indexed(conn: sqlite3.Connection, cfg: Config) -> sqlite3.Connection:
    store = BlobStore(cfg.blob_dir)
    box = mailbox.mbox(str(FIXTURE), factory=None, create=False)
    try:
        for key in box.keys():
            raw = box.get_bytes(key)
            uid = str(box[key].get("Message-ID") or f"key-{key}").strip("<>")
            ingest_message(
                conn, store, source="file", source_uid=uid, raw=raw, own_addresses=OWN
            )
    finally:
        box.close()
    return conn


def titles(hits) -> list[str]:
    return [h.title for h in hits]


# --- basics ---------------------------------------------------------------


def test_finds_a_distinctive_term(indexed) -> None:
    hits = keyword_search(indexed, "warehouse lease")
    assert hits
    assert "Re: Q2 warehouse lease renewal" in titles(hits)


def test_results_are_ranked_best_first(indexed) -> None:
    hits = keyword_search(indexed, "warehouse lease renewal")
    assert hits[0].title == "Re: Q2 warehouse lease renewal"
    assert all(hits[i].score >= hits[i + 1].score for i in range(len(hits) - 1))


def test_snippet_marks_the_match(indexed) -> None:
    (hit,) = [h for h in keyword_search(indexed, "quetzal") if "quetzal" in h.snippet.lower()]
    assert "[" in hit.snippet and "]" in hit.snippet


def test_limit_is_respected(indexed) -> None:
    assert len(keyword_search(indexed, "the invoice plan", limit=2)) <= 2


def test_no_results_for_absent_term(indexed) -> None:
    assert keyword_search(indexed, "zzzzunfindable") == []


def test_empty_query_returns_nothing(indexed) -> None:
    assert keyword_search(indexed, "") == []
    assert keyword_search(indexed, "   ") == []


# --- the invariants -------------------------------------------------------


def test_derived_docs_are_excluded_by_default(indexed) -> None:
    indexed.execute(
        "INSERT INTO docs (source, source_uid, origin, content_hash, doc_date,"
        " date_source, title, ingested_at) VALUES"
        " ('muster', 'derived-1', 'derived', 'h', '2026-04-01T00:00:00Z', 'header',"
        " 'Derived summary', '2026-04-01T00:00:00Z')"
    )
    doc_id = indexed.execute("SELECT id FROM docs WHERE source_uid='derived-1'").fetchone()[0]
    indexed.execute(
        "INSERT INTO chunks (doc_id, ord, text, text_hash) VALUES (?, 0, ?, 'th')",
        (doc_id, "A conclusion about the warehouse lease that Muster inferred."),
    )

    assert doc_id not in [h.doc_id for h in keyword_search(indexed, "warehouse lease")]
    widened = keyword_search(indexed, "warehouse lease", origin=None)
    assert doc_id in [h.doc_id for h in widened]


def test_one_hit_per_document(indexed) -> None:
    hits = keyword_search(indexed, "transition plan workstream milestones", limit=10)
    doc_ids = [h.doc_id for h in hits]
    assert len(doc_ids) == len(set(doc_ids))


# --- filters --------------------------------------------------------------


def test_since_filter_excludes_older(indexed) -> None:
    assert keyword_search(indexed, "warehouse lease", since="2026-01-01")
    assert not keyword_search(indexed, "warehouse lease", since="2026-06-01")


def test_until_filter_excludes_newer(indexed) -> None:
    assert not keyword_search(indexed, "warehouse lease", until="2026-01-01")
    assert keyword_search(indexed, "warehouse lease", until="2026-12-01")


def test_source_filter(indexed) -> None:
    assert keyword_search(indexed, "warehouse", source="file")
    assert not keyword_search(indexed, "warehouse", source="gmail")


# --- attachments are searchable in their own right ------------------------


def test_attachment_text_is_searchable(indexed) -> None:
    hits = keyword_search(indexed, "2026-0042")
    assert any(h.title == "Rechnung_2026-0042.pdf" for h in hits)


def test_attachment_filename_finds_the_parent(indexed) -> None:
    hits = keyword_search(indexed, "Rechnung_2026-0042.pdf")
    assert "Rechnung 2026-0042" in titles(hits)


# --- query building ------------------------------------------------------


def test_punctuation_does_not_break_fts_syntax(indexed) -> None:
    for query in ['a "b', "invoice -2026", "what? (really)", "AND OR NOT", '"', "*", "^foo"]:
        keyword_search(indexed, query)  # must not raise


def test_terms_are_or_ed_so_long_questions_still_match(indexed) -> None:
    hits = keyword_search(indexed, "what did Bob say about the warehouse lease renewal figure")
    assert "Re: Q2 warehouse lease renewal" in titles(hits)


def test_quoted_phrase_is_preserved() -> None:
    assert build_match_query('"warehouse lease" renewal') == '"warehouse lease" OR "renewal"'


def test_bare_terms_are_quoted_and_or_ed() -> None:
    assert build_match_query("invoice 2026") == '"invoice" OR "2026"'


def test_punctuated_term_contributes_phrase_and_parts() -> None:
    built = build_match_query("Rechnung_2026-0042.pdf")
    assert built.startswith('"Rechnung 2026 0042 pdf"')
    for part in ("Rechnung", "2026", "0042", "pdf"):
        assert f'"{part}"' in built


def test_filename_query_finds_both_the_email_and_the_attachment(indexed) -> None:
    hits = keyword_search(indexed, "Rechnung_2026-0042.pdf")
    found = set(titles(hits))
    assert {"Rechnung 2026-0042", "Rechnung_2026-0042.pdf"} <= found


def test_unknown_mode_is_rejected(indexed) -> None:
    with pytest.raises(ValueError, match="unknown search mode"):
        search(indexed, "x", mode="telepathy")
