"""The health check.

Every failure this index has had was silent. These tests are mostly about the
checks firing when something is genuinely wrong — and, just as importantly, not
firing when it is not, because a health check that cries wolf stops being read.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from muster import doctor


def _findings(report, name: str):
    return next(f for f in report.findings if f.name == name)


def test_a_clean_index_reports_nothing_wrong(conn, cfg) -> None:
    report = doctor.Report()
    doctor.check_schema(conn, report)
    doctor.check_fts(conn, report)
    doctor.check_unfindable(conn, report)
    assert [f.name for f in report.problems] == []


def test_a_document_with_no_chunks_is_reported(conn, cfg) -> None:
    conn.execute(
        "INSERT INTO docs (source, source_uid, content_hash, doc_date, date_source,"
        " ingested_at) VALUES ('file', '/x/orphan.pdf', 'h1', '2026-01-01', 'mtime', '2026-01-01')"
    )
    conn.commit()

    report = doctor.Report()
    doctor.check_unfindable(conn, report)
    finding = _findings(report, "unfindable documents")
    assert finding.status == doctor.WARN
    assert "1 document" in finding.detail
    assert finding.remedy


def test_redacted_documents_are_not_reported_as_broken(conn, cfg) -> None:
    """A login code has its body left out on purpose; no chunks is correct."""
    conn.execute(
        "INSERT INTO docs (source, source_uid, content_hash, doc_date, date_source,"
        " ingested_at, meta_json) VALUES ('gmail', 'code-1', 'h2', '2026-01-01', 'header',"
        " '2026-01-01', ?)",
        (json.dumps({"redacted_by": "login-code"}),),
    )
    conn.commit()

    report = doctor.Report()
    doctor.check_unfindable(conn, report)
    finding = _findings(report, "unfindable documents")
    assert finding.status == doctor.OK
    assert "redacted" in finding.detail


def _seed_chunks(conn, n: int = 4) -> int:
    cur = conn.execute(
        "INSERT INTO docs (source, source_uid, content_hash, doc_date, date_source,"
        " ingested_at) VALUES ('file', '/x/a.pdf', 'h9', '2026-01-01', 'mtime', '2026-01-01')"
    )
    doc_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO chunks (doc_id, ord, text, text_hash) VALUES (?, ?, ?, ?)",
        [(doc_id, i, f"chunk {i}", f"hash{i}") for i in range(n)],
    )
    conn.commit()
    return doc_id


def test_an_embedding_backlog_is_reported_with_its_share(conn, cfg) -> None:
    _seed_chunks(conn)
    report = doctor.Report()
    doctor.check_embedding(conn, cfg, report)
    finding = _findings(report, "embedding")
    # Chunks present, no vectors: the loud case, and more than half of them.
    assert finding.status == doctor.FAIL
    assert "no vector" in finding.detail


def test_a_denied_root_is_a_failure_not_an_empty_folder(cfg, tmp_path, monkeypatch) -> None:
    """macOS gates Documents and Downloads; denied reads exactly like empty."""
    import os as os_module
    from dataclasses import replace

    real = os_module.listdir

    def denied(path, *a, **k):
        if str(path) == str(tmp_path):
            raise PermissionError(1, "Operation not permitted")
        return real(path, *a, **k)

    monkeypatch.setattr(os_module, "listdir", denied)
    scoped = replace(cfg, files=replace(cfg.files, roots=(str(tmp_path),)))

    report = doctor.Report()
    doctor.check_file_roots(scoped, report)
    finding = _findings(report, "file access")
    assert finding.status == doctor.FAIL
    assert "Full Disk Access" in (finding.remedy or "")


def test_a_missing_root_is_a_warning_not_a_denial(cfg, tmp_path) -> None:
    from dataclasses import replace

    scoped = replace(cfg, files=replace(cfg.files, roots=(str(tmp_path / "nope"),)))
    report = doctor.Report()
    doctor.check_file_roots(scoped, report)
    assert _findings(report, "file access").status == doctor.WARN


def test_being_paused_is_surfaced(cfg) -> None:
    from muster import pausing

    report = doctor.Report()
    doctor.check_paused(cfg, report)
    assert _findings(report, "paused").status == doctor.OK

    pausing.pause(cfg.home)
    try:
        report = doctor.Report()
        doctor.check_paused(cfg, report)
        finding = _findings(report, "paused")
        assert finding.status == doctor.WARN
        assert finding.remedy == "muster resume"
    finally:
        pausing.resume(cfg.home)


def test_a_stale_lock_file_is_not_reported_as_held(cfg) -> None:
    """flock is released by the kernel on exit; the file outliving it blocks nothing."""
    cfg.lock_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.lock_path.write_text("999999\n")
    report = doctor.Report()
    doctor.check_stale_lock(cfg, report)
    finding = _findings(report, "sync lock")
    assert finding.status == doctor.OK
    assert "exited" in finding.detail


def test_never_having_synced_is_reported(conn, cfg) -> None:
    report = doctor.Report()
    doctor.check_sync_freshness(conn, report)
    finding = _findings(report, "mail sync")
    assert finding.status == doctor.WARN
    assert "never" in finding.detail


def test_a_recent_sync_is_fine(conn, cfg) -> None:
    from muster import syncstate

    syncstate.set_cursor(conn, "gmail", "500", result="incremental: nothing new")
    report = doctor.Report()
    doctor.check_sync_freshness(conn, report)
    assert _findings(report, "mail sync").status == doctor.OK


def test_a_healthy_keyword_index_passes(conn, cfg) -> None:
    _seed_chunks(conn)
    report = doctor.Report()
    doctor.check_fts(conn, report)
    assert _findings(report, "keyword index").status == doctor.OK


def test_a_corrupt_keyword_index_is_a_failure(conn, cfg, monkeypatch) -> None:
    """Asked of FTS5 itself.

    Row counts cannot answer this: chunks_fts is external-content, so count(*)
    reads the content table and equals it by construction — a check that can
    never fail.
    """
    _seed_chunks(conn)

    class Corrupt:
        def execute(self, sql, *a):
            if "integrity-check" in sql:
                raise sqlite3.DatabaseError("database disk image is malformed")
            return conn.execute(sql, *a)

    report = doctor.Report()
    doctor.check_fts(Corrupt(), report)
    finding = _findings(report, "keyword index")
    assert finding.status == doctor.FAIL
    assert "corrupt" in finding.detail


def test_the_report_ranks_its_own_severity() -> None:
    report = doctor.Report()
    assert report.worst == doctor.OK
    report.add("a", doctor.OK, "fine")
    assert report.worst == doctor.OK
    report.add("b", doctor.WARN, "hmm")
    assert report.worst == doctor.WARN
    report.add("c", doctor.FAIL, "bad")
    assert report.worst == doctor.FAIL
    assert [f.name for f in report.problems] == ["b", "c"]
