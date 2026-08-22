"""`muster status` breakdowns.

A month with twice its neighbours' volume is either real or duplication, and
the two need different responses. The month table reports distinct content
hashes next to the document count so the answer is visible rather than
inferred.
"""

from __future__ import annotations

import sqlite3

import pytest
from typer.testing import CliRunner

from muster.blobs import BlobStore
from muster.cli import app
from muster.ingest import ingest_message

runner = CliRunner()


def _raw(uid: str, subject: str, date: str, body: str = "Some body text here.") -> bytes:
    return (
        f"Message-ID: <{uid}>\r\nDate: {date}\r\n"
        f"From: S <s@example.com>\r\nTo: A <a@example.com>\r\n"
        f"Subject: {subject}\r\nContent-Type: text/plain\r\n\r\n{body}\r\n"
    ).encode()


@pytest.fixture
def populated(conn: sqlite3.Connection, cfg) -> sqlite3.Connection:
    store = BlobStore(cfg.blob_dir)
    for i in range(5):
        ingest_message(
            conn, store, source="gmail", source_uid=f"jul{i}",
            raw=_raw(f"jul{i}", f"July {i}", "Tue, 07 Jul 2026 09:00:00 +0000", f"July body {i}"),
            meta={"labels": ["CATEGORY_PROMOTIONS"]},
        )
    for i in range(2):
        ingest_message(
            conn, store, source="gmail", source_uid=f"aug{i}",
            raw=_raw(f"aug{i}", f"August {i}", "Sat, 01 Aug 2026 09:00:00 +0000", f"Aug body {i}"),
        )
    return conn


def _run(cfg):
    return runner.invoke(app, ["status", "--months", "12"])


def test_status_breaks_down_by_kind(populated, cfg) -> None:
    result = _run(cfg)
    assert result.exit_code == 0
    assert "by kind" in result.stdout
    assert "message" in result.stdout


def test_status_breaks_down_by_month(populated, cfg) -> None:
    result = _run(cfg)
    assert "by month" in result.stdout
    assert "2026-07" in result.stdout
    assert "2026-08" in result.stdout


def test_a_clean_month_reports_no_duplication(populated, cfg) -> None:
    """Five distinct July messages: docs and distinct-bytes must agree."""
    result = _run(cfg)
    july = next(line for line in result.stdout.splitlines() if "2026-07" in line)
    fields = july.split()
    assert fields[1] == "5", july
    assert fields[2] == "5", "distinct content hashes should equal document count"
    assert fields[4] == "0", "and therefore zero duplicates"


def test_duplication_is_visible_when_it_exists(conn, cfg) -> None:
    """The same bytes under two uids is what duplication actually looks like."""
    store = BlobStore(cfg.blob_dir)
    raw = _raw("dup", "Duplicated", "Tue, 07 Jul 2026 09:00:00 +0000")
    ingest_message(conn, store, source="gmail", source_uid="a", raw=raw)
    ingest_message(conn, store, source="gmail", source_uid="b", raw=raw)

    result = _run(cfg)
    july = next(line for line in result.stdout.splitlines() if "2026-07" in line)
    fields = july.split()
    assert fields[1] == "2" and fields[2] == "1", july
    assert fields[4] == "1", "one duplicate flagged"


def test_attachment_docs_are_counted_separately(conn, cfg) -> None:
    import mailbox
    from pathlib import Path

    store = BlobStore(cfg.blob_dir)
    fixture = Path(__file__).parent / "fixtures" / "sample.mbox"
    box = mailbox.mbox(str(fixture), factory=None, create=False)
    try:
        for key in box.keys():
            uid = str(box[key].get("Message-ID") or f"k{key}").strip("<>")
            ingest_message(conn, store, source="file", source_uid=uid, raw=box.get_bytes(key))
    finally:
        box.close()

    result = _run(cfg)
    kinds = result.stdout.split("by kind")[1]
    assert "attachment" in kinds
    assert "message" in kinds


def test_unfindable_documents_are_reported(conn, cfg) -> None:
    conn.execute(
        "INSERT INTO docs (source, source_uid, content_hash, doc_date, date_source,"
        " ingested_at) VALUES ('gmail', 'x', 'h', '2026-07-01T00:00:00Z', 'header',"
        " '2026-07-01T00:00:00Z')"
    )
    result = _run(cfg)
    line = next(line for line in result.stdout.splitlines() if "unfindable" in line)
    assert "1" in line


def test_months_option_bounds_the_table(populated, cfg) -> None:
    result = runner.invoke(app, ["status", "--months", "1"])
    months = [line for line in result.stdout.splitlines() if line.strip().startswith("2026-")]
    assert len(months) == 1
