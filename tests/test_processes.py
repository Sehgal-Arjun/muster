"""Processes and their anchors.

Anchors are exact joins, and the tests that matter are about what must *not* be
proposed: an anchor is permanent once accepted, and a bad one pulls in unrelated
documents forever.
"""

from __future__ import annotations

import json

import pytest

from muster import processes


def _doc(conn, *, title, counterparty=None, sender=None, thread=None,
         source="gmail", path=None, labels=None, doc_id=None, text=None):
    meta = {}
    if path:
        meta["path"] = path
    if labels:
        meta["labels"] = labels
    cursor = conn.execute(
        "INSERT INTO docs (source, source_uid, content_hash, doc_date, date_source,"
        " ingested_at, title, counterparty, sender, thread_uid, meta_json)"
        " VALUES (?, ?, ?, '2026-01-01T00:00:00+00:00', 'header', '2026-01-01', ?, ?, ?, ?, ?)",
        (source, f"uid-{doc_id or title}", f"hash-{doc_id or title}", title,
         counterparty, sender, thread, json.dumps(meta)),
    )
    new_id = cursor.lastrowid
    if text:
        conn.execute(
            "INSERT INTO chunks (doc_id, ord, text, text_hash, title)"
            " VALUES (?, 0, ?, ?, ?)",
            (new_id, text, f"th-{new_id}", title),
        )
    conn.commit()
    return new_id


# --- storage ---------------------------------------------------------------


def test_a_process_round_trips(conn) -> None:
    made = processes.create(conn, "TUM enrolment")
    assert made.slug == "tum-enrolment"
    found = processes.get(conn, "tum-enrolment")
    assert found and found.name == "TUM enrolment" and found.anchors == []


def test_anchors_are_unique_per_process(conn) -> None:
    made = processes.create(conn, "Visa")
    assert processes.add_anchor(conn, made.id, "domain", "example.org") is True
    assert processes.add_anchor(conn, made.id, "domain", "example.org") is False
    assert len(processes.get(conn, "visa").anchors) == 1


def test_removing_an_anchor(conn) -> None:
    made = processes.create(conn, "Visa")
    processes.add_anchor(conn, made.id, "domain", "example.org")
    assert processes.remove_anchor(conn, made.id, "domain", "example.org") is True
    assert processes.remove_anchor(conn, made.id, "domain", "example.org") is False


def test_anchor_kinds_are_constrained(conn) -> None:
    import sqlite3

    made = processes.create(conn, "Visa")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO process_anchors (process_id, kind, value, created_at)"
            " VALUES (?, 'vibes', 'x', '2026-01-01')",
            (made.id,),
        )


def test_deleting_a_process_takes_its_anchors(conn) -> None:
    made = processes.create(conn, "Visa")
    processes.add_anchor(conn, made.id, "domain", "example.org")
    processes.delete(conn, "visa")
    assert conn.execute("SELECT count(*) FROM process_anchors").fetchone()[0] == 0


# --- what must not be proposed --------------------------------------------


def test_a_phone_number_is_not_a_reference_number() -> None:
    """It identifies an office, not a process, and would pull in everything
    that office ever touched. This archive proposed 555-301-0142."""
    from muster.processes import _looks_like_phone

    assert _looks_like_phone("555-301-0142")
    assert _looks_like_phone("888-555-0199")
    assert _looks_like_phone("279-6700")
    assert not _looks_like_phone("AA00FCH0WH")
    assert not _looks_like_phone("21-4471-99")
    assert not _looks_like_phone("TUM-20260114")


def test_generic_mail_domains_are_never_proposed(conn) -> None:
    """A gmail address distinguishes nothing; anchoring on it includes everyone."""
    for i in range(4):
        _doc(conn, title=f"visa note {i}", counterparty="someone@gmail.com",
             doc_id=f"g{i}", text="visa application")
    proposals = processes.propose(conn, "visa")
    assert not [p for p in proposals if p.value == "gmail.com"]


def test_own_addresses_are_never_proposed(conn) -> None:
    """They match everything the user was party to, which is no anchor at all."""
    for i in range(3):
        _doc(conn, title=f"visa {i}", counterparty="me@work.example",
             doc_id=f"o{i}", text="visa")
    proposals = processes.propose(
        conn, "visa", own_addresses=frozenset({"me@work.example"})
    )
    assert not [p for p in proposals if p.value == "me@work.example"]


def test_own_address_is_derived_from_sent_mail(conn) -> None:
    """Config is easy to leave incomplete; SENT mail names the sender exactly."""
    for i in range(5):
        _doc(conn, title=f"sent {i}", sender="me@work.example",
             labels=["SENT"], doc_id=f"s{i}")
    assert processes.own_addresses_from_archive(conn) == {"me@work.example"}


def test_a_single_occurrence_is_not_a_pattern(conn) -> None:
    _doc(conn, title="visa one", counterparty="unique@rare.example",
         doc_id="r1", text="visa")
    for i in range(3):
        _doc(conn, title=f"visa {i}", counterparty="often@common.example",
             doc_id=f"c{i}", text="visa")
    values = {p.value for p in processes.propose(conn, "visa")}
    assert "common.example" in values
    assert "rare.example" not in values


def test_proposals_are_ordered_by_evidence(conn) -> None:
    for i in range(6):
        _doc(conn, title=f"visa many {i}", counterparty="a@many.example",
             doc_id=f"m{i}", text="visa")
    for i in range(2):
        _doc(conn, title=f"visa few {i}", counterparty="b@few.example",
             doc_id=f"f{i}", text="visa")
    proposals = processes.propose(conn, "visa")
    counts = [p.count for p in proposals]
    assert counts == sorted(counts, reverse=True)


def test_proposals_are_stable_across_runs(conn) -> None:
    """An anchor list that changes between runs could never be trusted."""
    for i in range(4):
        _doc(conn, title=f"visa {i}", counterparty="a@stable.example",
             doc_id=f"st{i}", text="visa application form")
    first = [(p.kind, p.value, p.count) for p in processes.propose(conn, "visa")]
    second = [(p.kind, p.value, p.count) for p in processes.propose(conn, "visa")]
    assert first == second


def test_suggest_never_creates_anything(conn) -> None:
    for i in range(5):
        _doc(conn, title=f"note {i}", counterparty=f"x{i}@unclaimed.example",
             doc_id=f"u{i}")
    before = conn.execute("SELECT count(*) FROM processes").fetchone()[0]
    processes.unclaimed_clusters(conn, min_docs=1)
    assert conn.execute("SELECT count(*) FROM processes").fetchone()[0] == before


def test_suggest_skips_domains_already_anchored(conn) -> None:
    made = processes.create(conn, "Claimed")
    processes.add_anchor(conn, made.id, "domain", "claimed.example")
    for i in range(5):
        _doc(conn, title=f"c {i}", counterparty=f"x{i}@claimed.example", doc_id=f"cl{i}")
    values = {p.value for p in processes.unclaimed_clusters(conn, min_docs=1)}
    assert "claimed.example" not in values
