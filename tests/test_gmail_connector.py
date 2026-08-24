"""Connector tests against a fake Gmail service.

These check the connector's contract — pagination, limits, decoding, and not
dying on one bad message — without touching the network or OAuth.
"""

from __future__ import annotations

import base64

import pytest

from muster.connectors import Connector, RawMessage
from muster.connectors.gmail import GmailConnector


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


class FakeMessages:
    def __init__(self, messages: dict[str, dict], page_size: int = 2):
        self.messages = messages
        self.page_size = page_size
        self.get_calls: list[str] = []

    def list(self, *, userId, q, maxResults=None, pageToken=None):
        ids = list(self.messages)
        start = int(pageToken or 0)
        page = ids[start : start + self.page_size]
        next_token = start + self.page_size
        body = {"messages": [{"id": i} for i in page]}
        if next_token < len(ids):
            body["nextPageToken"] = str(next_token)
        return _Executable(body)

    def get(self, *, userId, id, format):
        self.get_calls.append(id)
        payload = self.messages[id]
        if isinstance(payload, Exception):
            return _Executable(payload)
        return _Executable(payload)


class _Executable:
    def __init__(self, value):
        self.value = value

    def execute(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class FakeService:
    def __init__(self, messages):
        self._messages = FakeMessages(messages)

    def users(self):
        return self

    def messages(self):
        return self._messages


def _payload(mid: str, body: bytes, thread: str = "t1") -> dict:
    return {
        "id": mid,
        "threadId": thread,
        "labelIds": ["INBOX"],
        "internalDate": "1767225600000",
        "historyId": "42",
        "sizeEstimate": len(body),
        "raw": _b64(body),
    }


@pytest.fixture
def connector_factory(tmp_path):
    def make(messages):
        return GmailConnector(
            tmp_path / "credentials.json",
            tmp_path / "token.json",
            service=FakeService(messages),
        )

    return make


def test_gmail_connector_satisfies_protocol(connector_factory) -> None:
    assert isinstance(connector_factory({}), Connector)
    assert connector_factory({}).source == "gmail"


def test_fetch_decodes_raw_and_paginates(connector_factory) -> None:
    messages = {f"m{i}": _payload(f"m{i}", f"Subject: {i}\r\n\r\nbody {i}".encode()) for i in range(5)}
    got = list(connector_factory(messages).fetch("newer_than:7d"))

    assert [m.source_uid for m in got] == [f"m{i}" for i in range(5)]
    assert all(isinstance(m, RawMessage) for m in got)
    assert got[3].raw == b"Subject: 3\r\n\r\nbody 3"


def test_fetch_carries_metadata(connector_factory) -> None:
    messages = {"m0": _payload("m0", b"hi", thread="thread-9")}
    (message,) = list(connector_factory(messages).fetch("q"))

    assert message.meta["thread_uid"] == "thread-9"
    assert message.meta["labels"] == ["INBOX"]
    assert message.meta["internal_date"] == "1767225600000"


def test_limit_stops_early(connector_factory) -> None:
    messages = {f"m{i}": _payload(f"m{i}", b"x") for i in range(10)}
    connector = connector_factory(messages)
    got = list(connector.fetch("q", limit=3))

    assert len(got) == 3
    assert connector.service.messages().get_calls == ["m0", "m1", "m2"]


def test_one_unfetchable_message_does_not_end_the_run(connector_factory, caplog) -> None:
    messages = {
        "good1": _payload("good1", b"a"),
        "bad": RuntimeError("boom"),
        "good2": _payload("good2", b"b"),
    }
    got = list(connector_factory(messages).fetch("q"))

    assert [m.source_uid for m in got] == ["good1", "good2"]


def test_message_without_raw_payload_is_skipped(connector_factory) -> None:
    payload = _payload("m0", b"x")
    del payload["raw"]
    got = list(connector_factory({"m0": payload, "m1": _payload("m1", b"y")}).fetch("q"))

    assert [m.source_uid for m in got] == ["m1"]


def test_missing_credentials_raises_actionable_error(tmp_path) -> None:
    from muster.connectors.gmail import GmailAuthError

    connector = GmailConnector(tmp_path / "nope.json", tmp_path / "token.json")
    with pytest.raises(GmailAuthError, match="OAuth client secret"):
        _ = connector.service


# --- not re-fetching what we already hold ---------------------------------


def test_known_ids_are_never_fetched(connector_factory) -> None:
    """The whole point: listing is bulk, fetching is one round trip each."""
    messages = {f"m{i}": _payload(f"m{i}", b"x") for i in range(6)}
    connector = connector_factory(messages)
    known = {"m0", "m1", "m2", "m4"}

    got = list(connector.fetch("q", skip=known.__contains__))

    assert [m.source_uid for m in got] == ["m3", "m5"]
    assert connector.service.messages().get_calls == ["m3", "m5"], "no wasted fetches"
    assert connector.skipped_known == 4


def test_a_fully_known_mailbox_costs_only_the_listing(connector_factory) -> None:
    messages = {f"m{i}": _payload(f"m{i}", b"x") for i in range(10)}
    connector = connector_factory(messages)

    assert list(connector.fetch("q", skip=lambda _uid: True)) == []
    assert connector.service.messages().get_calls == []
    assert connector.listed == 10, "still listed, just not fetched"
    assert connector.skipped_known == 10


def test_limit_counts_messages_yielded_not_ids_listed(connector_factory) -> None:
    """Otherwise --limit 2 on a mostly-known mailbox fetches nothing at all."""
    messages = {f"m{i}": _payload(f"m{i}", b"x") for i in range(10)}
    connector = connector_factory(messages)
    known = {f"m{i}" for i in range(6)}

    got = list(connector.fetch("q", limit=2, skip=known.__contains__))

    assert [m.source_uid for m in got] == ["m6", "m7"]
    assert connector.service.messages().get_calls == ["m6", "m7"]


def test_skip_is_optional(connector_factory) -> None:
    messages = {f"m{i}": _payload(f"m{i}", b"x") for i in range(3)}
    connector = connector_factory(messages)
    assert len(list(connector.fetch("q"))) == 3
    assert connector.skipped_known == 0


def test_counters_reset_between_runs(connector_factory) -> None:
    messages = {f"m{i}": _payload(f"m{i}", b"x") for i in range(4)}
    connector = connector_factory(messages)
    list(connector.fetch("q", skip=lambda _uid: True))
    assert connector.skipped_known == 4
    list(connector.fetch("q"))
    assert connector.skipped_known == 0
