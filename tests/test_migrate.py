from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from muster import db, migrate
from muster.config import Config


def test_migrate_applies_and_is_idempotent(cfg: Config) -> None:
    conn = db.connect(cfg.db_path)
    first = migrate.migrate(conn)
    assert "001_initial" in first

    second = migrate.migrate(conn)
    assert second == [], "re-running migrations must be a no-op"

    versions = migrate.applied_versions(conn)
    assert versions == {m.version for m in migrate.discover()}
    conn.close()


def test_expected_tables_exist(conn: sqlite3.Connection) -> None:
    names = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    }
    for table in (
        "schema_migrations",
        "docs",
        "chunks",
        "chunks_fts",
        "vectors",
        "attachments",
        "jobs",
        "journal",
    ):
        assert table in names, f"missing {table}"


def test_docs_origin_defaults_to_captured(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO docs (source, source_uid, content_hash, doc_date, date_source, ingested_at)"
        " VALUES ('gmail', 'abc', 'deadbeef', '2026-01-01T00:00:00Z', 'header', '2026-01-01T00:00:00Z')"
    )
    row = conn.execute("SELECT origin FROM docs").fetchone()
    assert row["origin"] == "captured"


def test_docs_source_uid_is_unique(conn: sqlite3.Connection) -> None:
    args = ("gmail", "same-uid", "hash", "2026-01-01T00:00:00Z", "header", "2026-01-01T00:00:00Z")
    sql = (
        "INSERT INTO docs (source, source_uid, content_hash, doc_date, date_source, ingested_at)"
        " VALUES (?, ?, ?, ?, ?, ?)"
    )
    conn.execute(sql, args)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, args)


def test_fts_triggers_track_chunks(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO docs (id, source, source_uid, content_hash, doc_date, date_source, ingested_at)"
        " VALUES (1, 'gmail', 'm1', 'h', '2026-01-01T00:00:00Z', 'header', '2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO chunks (id, doc_id, ord, text, text_hash) VALUES (1, 1, 0, 'salamander invoice', 'th')"
    )
    hits = conn.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'salamander'").fetchall()
    assert [h[0] for h in hits] == [1]

    conn.execute("UPDATE chunks SET text = 'newt invoice' WHERE id = 1")
    assert conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'salamander'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'newt'").fetchone()[0] == 1

    conn.execute("DELETE FROM chunks WHERE id = 1")
    assert conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'newt'").fetchone()[0] == 0


def test_cascade_delete_removes_chunks_and_vectors(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO docs (id, source, source_uid, content_hash, doc_date, date_source, ingested_at)"
        " VALUES (1, 'gmail', 'm1', 'h', '2026-01-01T00:00:00Z', 'header', '2026-01-01T00:00:00Z')"
    )
    conn.execute("INSERT INTO chunks (id, doc_id, ord, text, text_hash) VALUES (1, 1, 0, 'x', 'th')")
    conn.execute("INSERT INTO vectors (chunk_id, model_id, embedding) VALUES (1, 'm', X'00')")

    conn.execute("DELETE FROM docs WHERE id = 1")
    assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM vectors").fetchone()[0] == 0


def test_partial_migration_is_not_recorded(tmp_path: Path) -> None:
    bad = tmp_path / "migrations"
    bad.mkdir()
    (bad / "001_bad.sql").write_text(
        "CREATE TABLE good (id INTEGER PRIMARY KEY);\nCREATE TABLE oops (;\n"
    )
    conn = db.connect(tmp_path / "bad.db")
    with pytest.raises(migrate.MigrationError):
        migrate.migrate(conn, bad)

    assert migrate.applied_versions(conn) == set()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "good" not in tables, "a failed migration must roll back entirely"
    conn.close()


def test_sqlite_vec_loads(cfg: Config) -> None:
    conn = db.connect(cfg.db_path, load_vec=True)
    version = conn.execute("SELECT vec_version()").fetchone()[0]
    assert version
    conn.close()
