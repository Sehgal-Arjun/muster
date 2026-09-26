"""Per-document extraction.

The model is allowed to be wrong. What it is not allowed to do is store garbage,
silently drop a document, or change its answer between runs.
"""

from __future__ import annotations

import json

import pytest

from muster import extraction


def _doc(conn, *, uid="d1", title="Missing items", sender="clerk@sponsor.example",
         body="Please send proof of insurance by 30 April."):
    cursor = conn.execute(
        "INSERT INTO docs (source, source_uid, content_hash, mime, doc_date,"
        " date_source, ingested_at, title, sender)"
        " VALUES ('gmail', ?, ?, 'message/rfc822', '2026-04-01T09:00:00+00:00',"
        " 'header', '2026-04-01', ?, ?)",
        (uid, f"h-{uid}", title, sender),
    )
    doc_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO chunks (doc_id, ord, text, text_hash) VALUES (?, 0, ?, ?)",
        (doc_id, body, f"t-{doc_id}"),
    )
    conn.commit()
    return doc_id


GOOD = json.dumps({
    "date": "2026-04-01", "direction": "inbound", "counterparty": "clerk@sponsor.example",
    "requests_of_me": [{"what": "proof of insurance", "due": "30 April 2026"}],
    "things_i_sent": [], "things_i_promised": [], "deadlines": [],
})


# --- validation ------------------------------------------------------------


def test_a_valid_response_round_trips() -> None:
    out = extraction.validate(json.loads(GOOD))
    assert out["requests_of_me"] == [{"what": "proof of insurance", "due": "30 April 2026"}]


def test_missing_lists_become_empty() -> None:
    out = extraction.validate({"direction": "inbound"})
    assert out["requests_of_me"] == []
    assert out["deadlines"] == []


def test_a_bare_string_where_an_object_belongs_is_repaired() -> None:
    """Common, and the text is the whole content of the item."""
    out = extraction.validate({"requests_of_me": ["send the form"]})
    assert out["requests_of_me"] == [{"what": "send the form", "due": None}]


def test_a_string_where_a_list_belongs_is_refused() -> None:
    """Half-understanding a response would put invented structure downstream."""
    with pytest.raises(extraction.SchemaError):
        extraction.validate({"requests_of_me": "send the form"})


def test_a_non_object_is_refused() -> None:
    with pytest.raises(extraction.SchemaError):
        extraction.validate(["not", "an", "object"])


def test_an_unknown_direction_becomes_none() -> None:
    assert extraction.validate({"direction": "sideways"})["direction"] is None


def test_empty_items_are_dropped_not_stored() -> None:
    out = extraction.validate({"requests_of_me": [{"what": "  ", "due": "x"}, {}]})
    assert out["requests_of_me"] == []


# --- the cache -------------------------------------------------------------


def test_extraction_is_stored_and_loaded(conn) -> None:
    doc_id = _doc(conn)
    result = extraction.extract_one(conn, doc_id, ask=lambda _p, _m: GOOD)
    extraction.store(conn, result)
    loaded = extraction.load(conn, [doc_id])
    assert loaded[doc_id].requests[0]["what"] == "proof of insurance"


def test_pending_is_resumable(conn) -> None:
    """A crash halfway through must cost nothing."""
    a, b = _doc(conn, uid="a"), _doc(conn, uid="b")
    assert set(extraction.pending(conn, [a, b])) == {a, b}
    extraction.store(conn, extraction.extract_one(conn, a, ask=lambda _p, _m: GOOD))
    assert extraction.pending(conn, [a, b]) == [b]


def test_a_malformed_response_is_retried_then_recorded_not_stored(conn) -> None:
    doc_id = _doc(conn)
    calls = []

    def broken(_prompt, _model):
        calls.append(1)
        return "definitely not json"

    extracted, failed = extraction.extract_set(conn, [doc_id], ask=broken)
    assert (extracted, failed) == (0, 1)
    assert len(calls) == extraction.MAX_ATTEMPTS, "it did not retry"
    assert extraction.load(conn, [doc_id]) == {}, "garbage was stored"
    assert doc_id in extraction.failures(conn, [doc_id]), "the failure was not recorded"


def test_a_recorded_failure_is_not_retried_forever(conn) -> None:
    doc_id = _doc(conn)
    extraction.extract_set(conn, [doc_id], ask=lambda _p, _m: "nope")
    assert extraction.pending(conn, [doc_id]) == []


def test_a_later_success_clears_the_failure(conn) -> None:
    doc_id = _doc(conn)
    extraction.extract_set(conn, [doc_id], ask=lambda _p, _m: "nope")
    extraction.store(conn, extraction.extract_one(conn, doc_id, ask=lambda _p, _m: GOOD))
    assert extraction.failures(conn, [doc_id]) == {}


def test_versions_do_not_mix(conn) -> None:
    """A prompt change is a version bump, not a silent reinterpretation."""
    doc_id = _doc(conn)
    result = extraction.extract_one(conn, doc_id, ask=lambda _p, _m: GOOD)
    extraction.store(conn, result)
    assert extraction.load(conn, [doc_id], version=99) == {}
    assert extraction.pending(conn, [doc_id], version=99) == [doc_id]


def test_direction_is_computed_from_the_sender_not_the_model(conn) -> None:
    """The model is reliable about direction and unreliable about what follows:
    on real data it filled requests_of_me with my own asks on a fifth of
    outbound documents."""
    mine = _doc(conn, uid="mine", sender="me@example.com")
    theirs = _doc(conn, uid="theirs", sender="clerk@sponsor.example")
    directions = extraction.computed_directions(
        conn, [mine, theirs], {"me@example.com"}
    )
    assert directions[mine] == "outbound"
    assert directions[theirs] == "inbound"


def test_extraction_stops_when_asked(conn) -> None:
    docs = [_doc(conn, uid=f"s{i}") for i in range(4)]
    extracted, _failed = extraction.extract_set(
        conn, docs, ask=lambda _p, _m: GOOD, should_stop=lambda: True
    )
    assert extracted == 0
