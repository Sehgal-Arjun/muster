"""Gather: the complete set for a process.

Recall is the goal and determinism is the requirement. A set that changes between
runs cannot support a claim about what is outstanding, and a set that quietly
drops a document produces a confidently wrong answer about what somebody is owed.
"""

from __future__ import annotations

import json

import pytest

from muster import gather as G
from muster import processes


def _mail(conn, *, uid, title, when, counterparty=None, thread=None, sender=None):
    cursor = conn.execute(
        "INSERT INTO docs (source, source_uid, content_hash, mime, doc_date,"
        " date_source, ingested_at, title, counterparty, sender, thread_uid)"
        " VALUES ('gmail', ?, ?, 'message/rfc822', ?, 'header', '2026-01-01', ?, ?, ?, ?)",
        (uid, f"h-{uid}", when, title, counterparty, sender, thread),
    )
    conn.commit()
    return cursor.lastrowid


def _file(conn, *, uid, title, when, path=None, where_from=None, downloaded=None):
    meta = {"path": path or f"/x/{title}"}
    if where_from:
        meta["where_from"] = where_from
    if downloaded:
        meta["downloaded_date"] = downloaded
    cursor = conn.execute(
        "INSERT INTO docs (source, source_uid, content_hash, mime, doc_date,"
        " date_source, ingested_at, title, meta_json)"
        " VALUES ('file', ?, ?, 'application/pdf', ?, 'birthtime', '2026-01-01', ?, ?)",
        (uid, f"h-{uid}", when, title, json.dumps(meta)),
    )
    conn.commit()
    return cursor.lastrowid


def _chunk(conn, doc_id, text):
    conn.execute(
        "INSERT INTO chunks (doc_id, ord, text, text_hash) VALUES (?, 0, ?, ?)",
        (doc_id, text, f"t-{doc_id}"),
    )
    conn.commit()


@pytest.fixture
def visa(conn):
    """A small process with one anchored domain and one stray."""
    process = processes.create(conn, "Visa application")
    processes.add_anchor(conn, process.id, "domain", "sponsor.example")
    ids = {
        "a": _mail(conn, uid="m1", title="Visa application received", when="2026-03-01T09:00:00+00:00",
                   counterparty="clerk@sponsor.example", thread="T1"),
        "b": _mail(conn, uid="m2", title="Please send proof of insurance", when="2026-04-01T09:00:00+00:00",
                   counterparty="clerk@sponsor.example", thread="T2"),
        "stray": _mail(conn, uid="m3", title="Visa application question", when="2026-02-01T09:00:00+00:00",
                       counterparty="friend@gmail.com", thread="T9"),
    }
    for key, doc_id in ids.items():
        _chunk(conn, doc_id, f"visa application {key}")
    return processes.get(conn, process.slug), ids


# --- the properties that matter -------------------------------------------


def test_gather_is_deterministic(conn, visa) -> None:
    """Same inputs, same set, same order — every time."""
    process, _ids = visa
    first = G.gather(conn, process)
    second = G.gather(conn, process)
    assert first.doc_ids == second.doc_ids
    assert {d: i.reason for d, i in first.inclusions.items()} == {
        d: i.reason for d, i in second.inclusions.items()
    }


def test_an_anchor_is_sufficient_on_its_own(conn, visa) -> None:
    """A document matching an anchor is in, whether or not the seed found it."""
    process, ids = visa
    result = G.gather(conn, process, seed_limit=0)
    assert ids["a"] in result.doc_ids
    assert result.inclusions[ids["a"]].reason == "anchor:domain"


def test_an_anchor_is_not_necessary(conn, visa) -> None:
    """The stray has no anchor and still belongs — a visa email from a personal
    address is exactly the document that must not be lost."""
    process, ids = visa
    result = G.gather(conn, process)
    assert ids["stray"] in result.doc_ids
    assert result.inclusions[ids["stray"]].reason == "seed_only"


def test_seed_only_documents_are_surfaced(conn, visa) -> None:
    """Their count is the signal that an anchor is missing."""
    process, ids = visa
    result = G.gather(conn, process)
    assert ids["stray"] in result.seed_only


def test_ordering_is_chronological_not_by_relevance(conn, visa) -> None:
    """State is temporal: relevance ordering destroys the sequence that shows a
    thing was asked for in April and sent in May."""
    process, _ids = visa
    result = G.gather(conn, process)
    rows = {r["id"]: r for r in G._rows_for(conn, set(result.doc_ids))}
    dates = [rows[d]["doc_date"] for d in result.doc_ids]
    assert dates == sorted(dates)


def test_every_document_records_why_it_is_included(conn, visa) -> None:
    process, _ids = visa
    result = G.gather(conn, process)
    for doc_id in result.doc_ids:
        inclusion = result.inclusions[doc_id]
        assert inclusion.reason
        assert inclusion.reason.startswith(("anchor:", "edge:")) or inclusion.reason == "seed_only"


def test_the_cap_is_reported_when_it_binds(conn, visa) -> None:
    """A silently truncated set is a silently wrong answer."""
    process, _ids = visa
    for i in range(12):
        doc_id = _mail(conn, uid=f"bulk{i}", title=f"bulk {i}",
                       when=f"2026-05-{i + 1:02d}T09:00:00+00:00",
                       counterparty="clerk@sponsor.example")
        _chunk(conn, doc_id, "visa bulk")
    result = G.gather(conn, process, max_docs=5)
    assert result.capped is True
    assert result.size == 5, "the cap did not actually bound the set"
    assert result.dropped > 0, "documents were discarded without saying so"

    # And the ones kept are the most recent: a question about a process is
    # nearly always a question about where it stands now.
    rows = {r["id"]: r for r in G._rows_for(conn, set(result.doc_ids))}
    assert min(rows[d]["doc_date"] for d in result.doc_ids) >= "2026-05"


def test_the_artifact_is_serialisable_and_versioned(conn, visa) -> None:
    """Other features consume this, so its shape is a contract."""
    process, _ids = visa
    body = G.gather(conn, process).as_json()
    assert body["version"] == G.GATHER_VERSION
    assert set(body) >= {
        "version", "process", "anchors", "doc_ids", "inclusions",
        "seed_only", "capped", "depth_reached", "size",
    }
    json.dumps(body)  # must round-trip


# --- each edge, proven to fire --------------------------------------------


def test_the_thread_edge_pulls_in_the_rest_of_a_conversation(conn, visa) -> None:
    process, ids = visa
    sibling = _mail(conn, uid="sib", title="Re: application received",
                    when="2026-03-02T09:00:00+00:00",
                    counterparty="someone@elsewhere.example", thread="T1")
    _chunk(conn, sibling, "unrelated words entirely")
    result = G.gather(conn, process)
    assert sibling in result.doc_ids
    assert result.inclusions[sibling].reason == "edge:thread"


def test_the_attachment_edge_pulls_in_extracted_documents(conn, visa) -> None:
    process, ids = visa
    extracted = _mail(conn, uid="att-doc", title="insurance.pdf",
                      when="2026-04-02T09:00:00+00:00")
    conn.execute(
        "INSERT INTO attachments (doc_id, filename, mime, content_hash, blob_path,"
        " extracted_doc_id) VALUES (?, 'insurance.pdf', 'application/pdf', 'h', 'p', ?)",
        (ids["b"], extracted),
    )
    conn.commit()
    result = G.gather(conn, process)
    assert extracted in result.doc_ids
    assert result.inclusions[extracted].reason == "edge:attachment"


def test_the_where_from_edge_pulls_in_downloads_from_an_anchored_domain(conn, visa) -> None:
    process, _ids = visa
    saved = _file(conn, uid="wf", title="form.pdf", when="2026-04-03T09:00:00+00:00",
                  where_from=["https://portal.sponsor.example/form.pdf"])
    result = G.gather(conn, process)
    assert saved in result.doc_ids
    assert result.inclusions[saved].reason == "edge:where_from"


def test_the_download_time_edge_uses_the_recorded_download_time(conn, visa) -> None:
    """Not doc_date. For a file that is its EXIF or birthtime, which says nothing
    about when it arrived — comparing it to email timestamps made this edge
    contribute half a real set."""
    process, _ids = visa
    near = _file(conn, uid="near", title="saved.pdf",
                 when="2019-01-01T00:00:00+00:00",          # old capture date
                 downloaded="2026-04-01T09:30:00+00:00")    # right after the email
    far = _file(conn, uid="far", title="other.pdf",
                when="2026-04-01T09:00:00+00:00",           # doc_date matches exactly
                downloaded="2020-01-01T00:00:00+00:00")     # but downloaded years before
    result = G.gather(conn, process)
    assert near in result.doc_ids, "a file downloaded beside an anchored email was missed"
    assert result.inclusions[near].reason == "edge:downloaded_near"
    assert far not in result.doc_ids, "doc_date was used instead of the download time"


def test_a_file_with_no_download_time_is_not_guessed_at(conn, visa) -> None:
    process, _ids = visa
    unknown = _file(conn, uid="unknown", title="mystery.pdf",
                    when="2026-04-01T09:05:00+00:00")
    result = G.gather(conn, process)
    assert unknown not in result.doc_ids


def test_the_ref_pattern_anchor_matches_body_text(conn) -> None:
    process = processes.create(conn, "Case")
    processes.add_anchor(conn, process.id, "ref_pattern", "AA00FCH0WH")
    doc_id = _mail(conn, uid="ref", title="Confirmation", when="2026-06-01T09:00:00+00:00")
    _chunk(conn, doc_id, "Your application id is AA00FCH0WH and is now complete.")
    result = G.gather(conn, processes.get(conn, process.slug), seed_limit=0)
    assert doc_id in result.doc_ids
    assert result.inclusions[doc_id].reason == "anchor:ref_pattern"


def test_the_folder_anchor_takes_everything_beneath_it(conn) -> None:
    process = processes.create(conn, "Taxes")
    processes.add_anchor(conn, process.id, "folder", "/x/Taxes")
    inside = _file(conn, uid="in", title="return.pdf", when="2026-01-01T00:00:00+00:00",
                   path="/x/Taxes/2026/return.pdf")
    outside = _file(conn, uid="out", title="other.pdf", when="2026-01-01T00:00:00+00:00",
                    path="/x/Elsewhere/other.pdf")
    result = G.gather(conn, processes.get(conn, process.slug), seed_limit=0)
    assert inside in result.doc_ids
    assert outside not in result.doc_ids


def test_a_missing_original_is_not_gathered(conn, visa) -> None:
    """Documents whose file is gone are hidden from search; they must not
    reappear here."""
    process, ids = visa
    conn.execute("UPDATE docs SET missing_since = '2026-05-01' WHERE id = ?", (ids["a"],))
    conn.commit()
    assert ids["a"] not in G.gather(conn, process).doc_ids
