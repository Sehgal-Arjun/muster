"""Incremental sync.

The expensive thing is listing a mailbox. These tests are mostly about proving
that the cheap path stays cheap, and that the fallback fires when — and only
when — the provider says the cursor is too old.
"""

from __future__ import annotations

import sqlite3

import pytest

from muster import incremental, syncstate
from muster.connectors import RawMessage
from muster.connectors.gmail import HistoryTooOld

FIXTURE = __import__("pathlib").Path(__file__).parent / "fixtures" / "sample.mbox"


def _messages(limit: int = 2) -> list[RawMessage]:
    from muster.connectors.mbox import MboxConnector

    out = []
    for message in MboxConnector(FIXTURE).fetch():
        out.append(message)
        if len(out) >= limit:
            break
    return out


class FakeGmail:
    """Records every call, so a test can assert what a sync actually cost."""

    source = "gmail"

    def __init__(self, *, added=None, history_id="9000", expired=False, listing=None):
        self._added = added or []
        self._history_id = history_id
        self._expired = expired
        self._listing = listing or []
        self.calls: list[str] = []
        self.dropped: list[tuple[str, str]] = []
        self.fetched: list[str] = []

    def current_history_id(self):
        self.calls.append("getProfile")
        return self._history_id

    def history_since(self, cursor):
        self.calls.append(f"history.list({cursor})")
        if self._expired:
            raise HistoryTooOld("expired")
        return list(self._added), self._history_id

    def fetch_ids(self, ids, *, skip=None):
        ids = list(ids)
        self.fetched.extend(ids)
        self.calls.append(f"messages.get x{len(ids)}")
        by_id = {m.source_uid: m for m in self._listing}
        for message_id in ids:
            if message_id in by_id:
                yield by_id[message_id]

    def fetch(self, query=None, **kwargs):
        self.calls.append("messages.list")
        skip = kwargs.get("skip")
        for message in self._listing:
            if skip and skip(message.source_uid):
                continue
            yield message


def test_first_run_without_a_cursor_bootstraps_and_fetches_nothing(conn, cfg) -> None:
    """Nothing is known, so the honest move is to mark the point and stop."""
    connector = FakeGmail(history_id="500")
    outcome = incremental.sync(conn, cfg, connector)

    assert outcome.mode == "bootstrap"
    assert connector.fetched == []
    assert syncstate.get(conn, "gmail").cursor == "500"


def test_nothing_new_costs_one_request(conn, cfg) -> None:
    """The whole point: a routine sync is one call, not a mailbox listing."""
    syncstate.set_cursor(conn, "gmail", "100")
    connector = FakeGmail(added=[], history_id="101")

    outcome = incremental.sync(conn, cfg, connector)

    assert connector.calls == ["history.list(100)"]
    assert outcome.summary() == "incremental: nothing new"
    assert syncstate.get(conn, "gmail").cursor == "101"


def test_new_messages_are_fetched_by_id_without_listing(conn, cfg) -> None:
    messages = _messages(2)
    syncstate.set_cursor(conn, "gmail", "100")
    connector = FakeGmail(
        added=[m.source_uid for m in messages], history_id="120", listing=messages
    )

    outcome = incremental.sync(conn, cfg, connector)

    assert "messages.list" not in connector.calls, "listing is what this avoids"
    assert outcome.counts.created == len(messages)
    assert syncstate.get(conn, "gmail").cursor == "120"


def test_ids_already_held_are_not_refetched(conn, cfg) -> None:
    """Gmail reports a message as added on label changes too."""
    messages = _messages(2)
    syncstate.set_cursor(conn, "gmail", "100")
    incremental.sync(
        conn, cfg,
        FakeGmail(added=[m.source_uid for m in messages], history_id="120", listing=messages),
    )

    syncstate.set_cursor(conn, "gmail", "120")
    again = FakeGmail(
        added=[m.source_uid for m in messages], history_id="130", listing=messages
    )
    incremental.sync(conn, cfg, again)

    assert again.fetched == [], "already indexed; fetching them costs a request each"


def test_expired_cursor_falls_back_to_a_listing(conn, cfg) -> None:
    messages = _messages(2)
    syncstate.set_cursor(conn, "gmail", "1")
    connector = FakeGmail(expired=True, history_id="900", listing=messages)

    outcome = incremental.sync(conn, cfg, connector)

    assert outcome.mode == "fallback"
    assert "messages.list" in connector.calls
    assert outcome.counts.created == len(messages)
    # Read after the listing: anything that arrived during it is already held.
    assert syncstate.get(conn, "gmail").cursor == "900"
    assert "history expired" in outcome.summary()


def test_expired_cursor_can_be_made_an_error_instead(conn, cfg) -> None:
    """The scheduled job may prefer to fail loudly than to list a mailbox."""
    syncstate.set_cursor(conn, "gmail", "1")
    with pytest.raises(HistoryTooOld):
        incremental.sync(conn, cfg, FakeGmail(expired=True), allow_fallback=False)


def test_cursor_is_recovered_from_documents_synced_before_the_state_table(conn, cfg) -> None:
    """A mailbox indexed by an older build should not need a full listing."""
    import json

    conn.execute(
        "INSERT INTO docs (source, source_uid, content_hash, doc_date, date_source,"
        " ingested_at, meta_json) VALUES ('gmail', 'm1', 'h', '2026-01-01', 'header',"
        " '2026-01-01', ?)",
        (json.dumps({"history_id": "4242"}),),
    )
    conn.commit()

    connector = FakeGmail(added=[], history_id="4300")
    outcome = incremental.sync(conn, cfg, connector)

    assert connector.calls == ["history.list(4242)"]
    assert outcome.mode == "incremental"


def test_unfetchable_messages_are_reported_not_swallowed(conn, cfg) -> None:
    """A message deleted between being announced and fetched must still show up."""
    syncstate.set_cursor(conn, "gmail", "100")
    connector = FakeGmail(added=["gone-1"], history_id="120", listing=[])
    connector.dropped = [("gone-1", "404")]

    outcome = incremental.sync(conn, cfg, connector)

    assert outcome.unfetchable == 1
    assert "gone from the mailbox" in outcome.summary()
