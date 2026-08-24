from __future__ import annotations

import mailbox
import sqlite3
from pathlib import Path

import pytest

from muster.blobs import BlobStore
from muster.config import Config
from muster.connectors.mbox import MboxConnector
from muster.ingest import ingest_message

FIXTURE = Path(__file__).parent / "fixtures" / "sample.mbox"
OWN = {"you@example.com"}


@pytest.fixture
def store(cfg: Config) -> BlobStore:
    return BlobStore(cfg.blob_dir)


def _raws() -> dict[str, bytes]:
    box = mailbox.mbox(str(FIXTURE), factory=None, create=False)
    try:
        return {
            str(box[k].get("Message-ID") or f"key-{k}").strip("<>"): box.get_bytes(k)
            for k in box.keys()
        }
    finally:
        box.close()


def ingest_all(conn, store, *, meta=None):
    results = []
    for uid, raw in _raws().items():
        results.append(
            ingest_message(
                conn, store, source="file", source_uid=uid, raw=raw,
                meta=meta or {}, own_addresses=OWN,
            )
        )
    return results


# --- the headline invariant ----------------------------------------------


def test_reingest_produces_zero_new_rows(conn: sqlite3.Connection, store: BlobStore) -> None:
    first = ingest_all(conn, store)
    assert all(r.created for r in first)

    counts_before = _counts(conn)
    second = ingest_all(conn, store)

    assert all(r.skipped for r in second)
    assert _counts(conn) == counts_before
    assert [r.doc_id for r in second] == [r.doc_id for r in first]


def _counts(conn) -> dict[str, int]:
    return {
        table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("docs", "chunks", "attachments", "vectors")
    }


def test_every_fixture_message_lands(conn: sqlite3.Connection, store: BlobStore) -> None:
    ingest_all(conn, store)
    # 15 messages, plus one doc for the text-bearing PDF attachment.
    assert conn.execute("SELECT count(*) FROM docs").fetchone()[0] == 16
    assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] > 16


def test_broken_messages_do_not_stop_the_run(conn: sqlite3.Connection, store: BlobStore) -> None:
    results = ingest_all(conn, store)
    assert len(results) == 15
    assert all(r.doc_id is not None for r in results)


# --- origin ---------------------------------------------------------------


def test_everything_ingested_is_captured(conn: sqlite3.Connection, store: BlobStore) -> None:
    ingest_all(conn, store)
    origins = {r[0] for r in conn.execute("SELECT DISTINCT origin FROM docs")}
    assert origins == {"captured"}


# --- blobs ----------------------------------------------------------------


def test_raw_original_is_stored_and_recoverable(
    conn: sqlite3.Connection, store: BlobStore
) -> None:
    raws = _raws()
    uid = "ordinary-1@example.com"
    ingest_message(conn, store, source="file", source_uid=uid, raw=raws[uid], own_addresses=OWN)

    row = conn.execute(
        "SELECT blob_path, content_hash FROM docs WHERE source_uid = ?", (uid,)
    ).fetchone()
    assert store.get_blob(row["blob_path"], verify=True) == raws[uid]


# --- attachments ----------------------------------------------------------


def test_pdf_attachment_becomes_its_own_searchable_doc(
    conn: sqlite3.Connection, store: BlobStore
) -> None:
    ingest_all(conn, store)

    attachment = conn.execute(
        "SELECT * FROM attachments WHERE filename = 'Rechnung_2026-0042.pdf'"
    ).fetchone()
    assert attachment is not None
    assert attachment["extracted_doc_id"] is not None

    child = conn.execute(
        "SELECT * FROM docs WHERE id = ?", (attachment["extracted_doc_id"],)
    ).fetchone()
    assert child["title"] == "Rechnung_2026-0042.pdf"
    assert child["mime"] == "application/pdf"
    assert child["origin"] == "captured"

    text = " ".join(
        r[0] for r in conn.execute("SELECT text FROM chunks WHERE doc_id = ?", (child["id"],))
    )
    assert "2026-0042" in text


def test_attachment_filename_is_searchable_from_the_parent(
    conn: sqlite3.Connection, store: BlobStore
) -> None:
    ingest_all(conn, store)
    parent = conn.execute(
        "SELECT id FROM docs WHERE source_uid = 'attachment-5@lieferant.de'"
    ).fetchone()
    first_chunk = conn.execute(
        "SELECT text FROM chunks WHERE doc_id = ? AND ord = 0", (parent["id"],)
    ).fetchone()
    assert "Rechnung_2026-0042.pdf" in first_chunk["text"]


def test_attachment_bytes_are_deduplicated_across_messages(
    conn: sqlite3.Connection, store: BlobStore
) -> None:
    raws = _raws()
    original = raws["attachment-5@lieferant.de"]
    twin = original.replace(b"<attachment-5@lieferant.de>", b"<attachment-5b@lieferant.de>")

    ingest_message(conn, store, source="file", source_uid="a", raw=original, own_addresses=OWN)
    ingest_message(conn, store, source="file", source_uid="b", raw=twin, own_addresses=OWN)

    hashes = [r[0] for r in conn.execute("SELECT content_hash FROM attachments")]
    assert len(hashes) == 2 and len(set(hashes)) == 1, "same bytes, one blob, two references"
    assert len(list(store.iter_digests())) == 3  # two distinct raws + one shared PDF


def test_nameless_attachment_is_stored_without_a_child_doc(
    conn: sqlite3.Connection, store: BlobStore
) -> None:
    raws = _raws()
    uid = "noname-12@example.com"
    ingest_message(conn, store, source="file", source_uid=uid, raw=raws[uid], own_addresses=OWN)

    row = conn.execute("SELECT * FROM attachments").fetchone()
    assert row["filename"] is None
    assert row["extracted_doc_id"] is None


# --- metadata -------------------------------------------------------------


def test_dates_and_sources_are_recorded(conn: sqlite3.Connection, store: BlobStore) -> None:
    ingest_all(conn, store)
    rows = dict(
        conn.execute("SELECT source_uid, date_source FROM docs").fetchall()  # type: ignore[arg-type]
    )
    assert rows["ordinary-1@example.com"] == "header"
    assert rows["baddate-7@example.com"] != "header"


def test_counterparty_uses_configured_identity(
    conn: sqlite3.Connection, store: BlobStore
) -> None:
    ingest_all(conn, store)
    counterparties = dict(
        conn.execute("SELECT source_uid, counterparty FROM docs").fetchall()  # type: ignore[arg-type]
    )
    assert counterparties["ordinary-1@example.com"] == "bob@example.com"
    assert counterparties["outbound-14@example.com"] == "chidi@partner.example"


# --- connector ------------------------------------------------------------


def test_mbox_connector_yields_stable_uids() -> None:
    first = [m.source_uid for m in MboxConnector(FIXTURE).fetch()]
    second = [m.source_uid for m in MboxConnector(FIXTURE).fetch()]
    assert first == second
    assert len(first) == 15
    assert len(set(first)) == 15


def test_mbox_connector_respects_limit() -> None:
    assert len(list(MboxConnector(FIXTURE).fetch(limit=3))) == 3


def test_mbox_connector_honours_skip() -> None:
    """Every connector needs it, or the protocol is a lie."""
    from muster.connectors.mbox import MboxConnector

    all_uids = [m.source_uid for m in MboxConnector(FIXTURE).fetch()]
    known = set(all_uids[:10])

    connector = MboxConnector(FIXTURE)
    got = [m.source_uid for m in connector.fetch(skip=known.__contains__)]

    assert got == all_uids[10:]
    assert connector.skipped_known == 10
