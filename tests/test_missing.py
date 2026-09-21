"""Noticing that an original has gone — and refusing to over-conclude.

The dangerous version of this deletes anything it cannot find. macOS denies
access to Documents and Downloads until a program is granted it, and a walk of
a denied folder reports zero files, which is indistinguishable from an empty
one. Most of these tests are about that.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from muster import missing
from muster.connectors.files import FileConnector
from muster.fileingest import ingest_file


def _index(conn, root: Path, name: str) -> int:
    record = next(r for r in FileConnector((str(root),)).walk() if r.path.name == name)
    return ingest_file(conn, record).doc_id


def _missing_since(conn, doc_id: int):
    return conn.execute("SELECT missing_since FROM docs WHERE id = ?", (doc_id,)).fetchone()[0]


def test_a_deleted_file_is_marked_after_a_completed_walk(conn, tmp_path) -> None:
    doomed = tmp_path / "gone.txt"
    doomed.write_text("here for now")
    kept = tmp_path / "stays.txt"
    kept.write_text("still here")
    gone_id, kept_id = _index(conn, tmp_path, "gone.txt"), _index(conn, tmp_path, "stays.txt")

    doomed.unlink()
    marked, restored, _ = missing.sweep_files(
        conn, [tmp_path], seen_paths={str(kept)}
    )

    assert marked == 1
    assert _missing_since(conn, gone_id)
    assert _missing_since(conn, kept_id) is None


def test_a_denied_root_condemns_nothing(conn, tmp_path) -> None:
    """The failure that would have erased thousands of documents.

    A root that could not be read is not in roots_read, so nothing beneath it
    is judged however absent it looks.
    """
    doomed = tmp_path / "gone.txt"
    doomed.write_text("x")
    doc_id = _index(conn, tmp_path, "gone.txt")
    doomed.unlink()

    # The walk never read this root, so it passes no roots at all.
    marked, _restored, _ = missing.sweep_files(conn, [], seen_paths=set())

    assert marked == 0
    assert _missing_since(conn, doc_id) is None, "a denied root deleted something"


def test_documents_outside_the_roots_that_were_read_are_left_alone(conn, tmp_path) -> None:
    """One root readable and another not must not condemn the other's contents."""
    readable = tmp_path / "readable"
    elsewhere = tmp_path / "elsewhere"
    readable.mkdir()
    elsewhere.mkdir()
    (readable / "a.txt").write_text("a")
    (elsewhere / "b.txt").write_text("b")
    a_id = _index(conn, readable, "a.txt")
    b_id = _index(conn, elsewhere, "b.txt")

    (elsewhere / "b.txt").unlink()
    marked, _restored, _ = missing.sweep_files(
        conn, [readable], seen_paths={str(readable / "a.txt")}
    )

    assert marked == 0
    assert _missing_since(conn, b_id) is None
    assert _missing_since(conn, a_id) is None


def test_a_file_that_comes_back_is_restored(conn, tmp_path) -> None:
    path = tmp_path / "returns.txt"
    path.write_text("here")
    doc_id = _index(conn, tmp_path, "returns.txt")

    path.unlink()
    missing.sweep_files(conn, [tmp_path], seen_paths=set())
    assert _missing_since(conn, doc_id)

    path.write_text("here again")
    _marked, restored, _ = missing.sweep_files(conn, [tmp_path], seen_paths={str(path)})
    assert restored == 1
    assert _missing_since(conn, doc_id) is None


def test_marking_keeps_the_document_and_its_text(conn, tmp_path) -> None:
    """An archive should not lose its record because a file moved."""
    path = tmp_path / "notes.txt"
    path.write_text("warehouse lease renewal")
    doc_id = _index(conn, tmp_path, "notes.txt")
    path.unlink()
    missing.sweep_files(conn, [tmp_path], seen_paths=set())

    assert conn.execute("SELECT count(*) FROM docs WHERE id = ?", (doc_id,)).fetchone()[0] == 1
    assert conn.execute(
        "SELECT count(*) FROM chunks WHERE doc_id = ?", (doc_id,)
    ).fetchone()[0] >= 1


def test_a_marked_document_stops_appearing_in_search(conn, tmp_path) -> None:
    from muster.search import keyword_search

    path = tmp_path / "lease.txt"
    path.write_text("warehouse lease renewal agreement")
    _index(conn, tmp_path, "lease.txt")
    conn.commit()
    assert keyword_search(conn, "warehouse lease", min_score=0.0)

    path.unlink()
    missing.sweep_files(conn, [tmp_path], seen_paths=set())

    assert keyword_search(conn, "warehouse lease", min_score=0.0) == [], (
        "a document whose original is gone was still offered"
    )


def test_marking_twice_keeps_the_first_date(conn, tmp_path) -> None:
    path = tmp_path / "x.txt"
    path.write_text("x")
    doc_id = _index(conn, tmp_path, "x.txt")
    path.unlink()

    missing.sweep_files(conn, [tmp_path], seen_paths=set())
    first = _missing_since(conn, doc_id)
    missing.sweep_files(conn, [tmp_path], seen_paths=set())
    assert _missing_since(conn, doc_id) == first


def test_purge_missing_removes_only_the_marked(conn, tmp_path) -> None:
    from muster.purge import purge

    gone = tmp_path / "gone.txt"
    kept = tmp_path / "kept.txt"
    gone.write_text("a")
    kept.write_text("b")
    gone_id = _index(conn, tmp_path, "gone.txt")
    kept_id = _index(conn, tmp_path, "kept.txt")

    gone.unlink()
    missing.sweep_files(conn, [tmp_path], seen_paths={str(kept)})

    purge(conn, missing=True)
    remaining = {r[0] for r in conn.execute("SELECT id FROM docs")}
    assert gone_id not in remaining
    assert kept_id in remaining


def test_purge_still_refuses_to_run_without_a_selector(conn) -> None:
    from muster.purge import plan

    with pytest.raises(ValueError):
        plan(conn)


def test_a_file_the_walk_skipped_is_not_treated_as_deleted(conn, tmp_path) -> None:
    """The walk skips plenty that is still there.

    Build directories, non-document formats, oversized files and anything
    shaped like a credential are all skipped deliberately. Reading "the walk
    did not yield it" as "deleted" marked 63 documents missing on the first
    real run, every one of them still on disk.
    """
    kept = tmp_path / "still-here.txt"
    kept.write_text("indexed before the exclusions changed")
    doc_id = _index(conn, tmp_path, "still-here.txt")

    # The walk produced nothing at all — as if the file were now excluded.
    marked, _restored, _ = missing.sweep_files(conn, [tmp_path], seen_paths=set())

    assert marked == 0
    assert _missing_since(conn, doc_id) is None


def test_an_unreadable_file_is_not_treated_as_deleted(conn, tmp_path) -> None:
    """Cannot tell is not the same as gone — the denied-root mistake, one level down."""
    locked = tmp_path / "locked"
    locked.mkdir()
    target = locked / "secret.txt"
    target.write_text("x")
    doc_id = _index(conn, tmp_path, "secret.txt")
    locked.chmod(0o000)
    try:
        marked, _restored, _ = missing.sweep_files(conn, [tmp_path], seen_paths=set())
        assert marked == 0
        assert _missing_since(conn, doc_id) is None
    finally:
        locked.chmod(0o755)
