"""The filesystem connector.

The exclusions matter more than the happy path: this walks a real home
directory, and the difference between a good default and a bad one is whether
the index quietly fills with node_modules, disk images and ssh keys.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from muster.connectors.files import FileConnector, DEFAULT_MAX_BYTES
from muster.fileingest import ingest_file, resolve_date


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "Documents").mkdir()
    (tmp_path / "Documents" / "lease.txt").write_text("warehouse lease renewal 2026")
    (tmp_path / "Documents" / "notes.md").write_text("# Notes\nrent review")
    return tmp_path


def walk(root: Path, **kwargs) -> list[Path]:
    connector = FileConnector((str(root),), **kwargs)
    return sorted(r.path for r in connector.walk())


def test_walks_configured_roots(tree: Path) -> None:
    found = walk(tree)
    assert {p.name for p in found} == {"lease.txt", "notes.md"}


def test_leaves_the_rest_of_the_home_directory_alone(tmp_path: Path) -> None:
    """Only what is configured. This is the whole safety property."""
    (tmp_path / "Documents").mkdir()
    (tmp_path / "Documents" / "keep.txt").write_text("keep")
    (tmp_path / "Elsewhere").mkdir()
    (tmp_path / "Elsewhere" / "private.txt").write_text("not asked for")

    found = walk(tmp_path / "Documents")
    assert [p.name for p in found] == ["keep.txt"]


@pytest.mark.parametrize(
    "directory", ["node_modules", ".git", "Library", "Caches", ".ssh", "__pycache__"],
)
def test_noise_directories_are_pruned(tmp_path: Path, directory: str) -> None:
    nested = tmp_path / directory / "deep"
    nested.mkdir(parents=True)
    (nested / "thing.txt").write_text("should not be indexed")
    (tmp_path / "real.txt").write_text("should be")

    assert [p.name for p in walk(tmp_path)] == ["real.txt"]


def test_app_bundles_are_not_descended_into(tmp_path: Path) -> None:
    """A .app is one thing to a user, not four hundred."""
    inside = tmp_path / "Thing.app" / "Contents"
    inside.mkdir(parents=True)
    (inside / "readme.txt").write_text("bundle guts")
    (tmp_path / "real.txt").write_text("real")

    assert [p.name for p in walk(tmp_path)] == ["real.txt"]


@pytest.mark.parametrize(
    "name",
    ["movie.mp4", "disk.dmg", "image.iso", "archive.zip", "song.mp3", "lib.dylib"],
)
def test_media_and_binaries_are_skipped(tmp_path: Path, name: str) -> None:
    (tmp_path / name).write_bytes(b"x" * 32)
    assert walk(tmp_path) == []


@pytest.mark.parametrize(
    "name",
    [
        ".env", "id_rsa", "server.key", "cert.pem", "vault.kdbx", "login.keychain",
        "backup.1pif", ".netrc", "passwords.csv", "secrets.yml",
    ],
)
def test_secrets_are_never_indexed(tmp_path: Path, name: str) -> None:
    (tmp_path / name).write_text("hunter2")
    connector = FileConnector((str(tmp_path),))
    assert list(connector.walk()) == []
    assert connector.stats.excluded_secret >= 1 or connector.stats.excluded_type >= 1


def test_oversized_files_are_skipped_and_counted(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_bytes(b"x" * 2048)
    connector = FileConnector((str(tmp_path),), max_bytes=1024)
    assert list(connector.walk()) == []
    assert connector.stats.too_big == 1


def test_a_missing_root_is_reported_not_fatal(tmp_path: Path) -> None:
    connector = FileConnector((str(tmp_path / "nope"),))
    assert list(connector.walk()) == []
    assert connector.stats.roots_missing


def test_the_default_size_cap_is_100mb() -> None:
    assert DEFAULT_MAX_BYTES == 100 * 1024 * 1024


# --- ingest ---------------------------------------------------------------


def _record(root: Path, name: str):
    connector = FileConnector((str(root),))
    return next(r for r in connector.walk() if r.path.name == name)


def test_a_file_is_indexed_without_copying_its_bytes(conn, tree: Path) -> None:
    """The point of this connector: the file on disk is the original."""
    result = ingest_file(conn, _record(tree / "Documents", "lease.txt"))

    assert result.created and result.chunks >= 1
    row = conn.execute("SELECT * FROM docs WHERE id = ?", (result.doc_id,)).fetchone()
    assert row["blob_path"] is None, "a file must not be copied into the blob store"
    assert row["source"] == "file"

    import json
    meta = json.loads(row["meta_json"])
    assert meta["path"].endswith("lease.txt")
    assert meta["size"] > 0 and meta["mtime"]


def test_an_unchanged_file_is_not_re_read(conn, tree: Path) -> None:
    record = _record(tree / "Documents", "lease.txt")
    ingest_file(conn, record)
    again = ingest_file(conn, record)
    assert again.unchanged and not again.created


def test_a_changed_file_is_re_indexed_and_old_chunks_go(conn, tree: Path) -> None:
    path = tree / "Documents" / "lease.txt"
    first = ingest_file(conn, _record(tree / "Documents", "lease.txt"))

    path.write_text("entirely different text about a boat")
    os.utime(path, (0, 0))  # force a different mtime
    second = ingest_file(conn, _record(tree / "Documents", "lease.txt"))

    assert second.updated and second.doc_id == first.doc_id
    texts = [
        r[0] for r in conn.execute(
            "SELECT text FROM chunks WHERE doc_id = ?", (first.doc_id,)
        )
    ]
    assert any("boat" in t for t in texts)
    assert not any("warehouse" in t for t in texts), "stale chunks survived"


def test_a_moved_file_is_recognised_rather_than_indexed_twice(conn, tree: Path) -> None:
    """Identity is the hash, because the path is the one name a file can lose."""
    source = tree / "Documents" / "lease.txt"
    first = ingest_file(conn, _record(tree / "Documents", "lease.txt"))

    moved_to = tree / "Documents" / "archive"
    moved_to.mkdir()
    source.rename(moved_to / "lease.txt")

    result = ingest_file(conn, _record(moved_to, "lease.txt"))
    assert result.moved and result.doc_id == first.doc_id
    assert conn.execute("SELECT count(*) FROM docs WHERE source='file'").fetchone()[0] == 1


def test_date_falls_back_through_the_priority_order(conn, tree: Path) -> None:
    """No exif and no pdf metadata here, so it must land on a file time."""
    record = _record(tree / "Documents", "lease.txt")
    _date, source = resolve_date(record)
    assert source in ("birthtime", "mtime")


def test_pdf_creation_date_beats_the_file_times(tmp_path: Path) -> None:
    pymupdf = pytest.importorskip("pymupdf")
    path = tmp_path / "doc.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Agreement to lease")
    doc.set_metadata({"creationDate": "D:20240915120000Z"})
    doc.save(path)
    doc.close()

    record = _record(tmp_path, "doc.pdf")
    date, source = resolve_date(record)
    assert source == "pdfmeta"
    assert date.startswith("2024-09-15")


def test_a_root_that_cannot_be_read_is_reported_not_treated_as_empty(
    tmp_path: Path, monkeypatch
) -> None:
    """macOS gates Documents and Downloads; denied must not read as 'no files'."""
    import os as os_module

    (tmp_path / "thing.txt").write_text("x")
    real_listdir = os_module.listdir

    def denied(path, *args, **kwargs):
        if str(path) == str(tmp_path):
            raise PermissionError(1, "Operation not permitted")
        return real_listdir(path, *args, **kwargs)

    monkeypatch.setattr(os_module, "listdir", denied)

    connector = FileConnector((str(tmp_path),))
    assert list(connector.walk()) == []
    assert connector.stats.roots_denied == [str(tmp_path)]
    assert connector.stats.roots_missing == [], "denied is not the same as missing"


def test_an_unreadable_subtree_is_counted_rather_than_swallowed(tmp_path: Path) -> None:
    """os.walk hides these by default, which loses a whole subtree silently."""
    (tmp_path / "ok.txt").write_text("readable")
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "hidden.txt").write_text("unreachable")
    locked.chmod(0o000)
    try:
        connector = FileConnector((str(tmp_path),))
        names = [r.path.name for r in connector.walk()]
        assert names == ["ok.txt"]
        assert connector.stats.walk_errors >= 1
    finally:
        locked.chmod(0o755)


# --- spotlight ------------------------------------------------------------


def _set_where_from(path: Path, urls: list[str], when=None) -> None:
    """Write the attributes a browser writes when it saves a download."""
    import plistlib
    import subprocess

    payload = plistlib.dumps(urls, fmt=plistlib.FMT_BINARY)
    subprocess.run(
        ["xattr", "-w", "-x", "com.apple.metadata:kMDItemWhereFroms",
         payload.hex(), str(path)],
        check=True,
    )
    if when is not None:
        stamp = plistlib.dumps([when], fmt=plistlib.FMT_BINARY)
        subprocess.run(
            ["xattr", "-w", "-x", "com.apple.metadata:kMDItemDownloadedDate",
             stamp.hex(), str(path)],
            check=True,
        )


def test_where_from_is_read_without_spotlight(tmp_path: Path) -> None:
    """The join from a download back to the message that pointed at it.

    Read from the xattr rather than mdls precisely so it works here: a temp
    directory is not Spotlight-indexed, and mdls reports nothing for it.
    """
    from muster.connectors.files import spotlight

    path = tmp_path / "lease.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    _set_where_from(path, ["https://example.com/lease.pdf", "https://mail.google.com/"])

    assert spotlight(path)["where_from"] == [
        "https://example.com/lease.pdf",
        "https://mail.google.com/",
    ]


def test_downloaded_date_is_read(tmp_path: Path) -> None:
    from datetime import datetime

    from muster.connectors.files import spotlight

    path = tmp_path / "lease.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    _set_where_from(path, ["https://example.com/x"], when=datetime(2026, 3, 4, 10, 0, 0))

    assert spotlight(path)["downloaded_date"].startswith("2026-03-04")


def test_a_file_with_no_download_history_is_not_an_error(tmp_path: Path) -> None:
    from muster.connectors.files import spotlight

    path = tmp_path / "plain.txt"
    path.write_text("written here, never downloaded")
    assert spotlight(path) == {}


def test_provenance_reaches_the_document(conn, tmp_path: Path) -> None:
    """What the viewer renders as 'Downloaded from'."""
    import json

    from muster.connectors.files import FileConnector

    (tmp_path / "lease.txt").write_text("warehouse lease")
    _set_where_from(tmp_path / "lease.txt", ["https://example.com/lease.pdf"])

    record = next(iter(FileConnector((str(tmp_path),)).walk()))
    result = ingest_file(conn, record)

    meta = json.loads(
        conn.execute("SELECT meta_json FROM docs WHERE id = ?", (result.doc_id,)).fetchone()[0]
    )
    assert meta["where_from"] == ["https://example.com/lease.pdf"]


def test_documents_are_yielded_before_images(tmp_path: Path) -> None:
    """OCR costs seconds and extraction costs milliseconds.

    In directory order one folder of screenshots starves every real document
    behind it, which is how a CV stayed unfindable for an hour while four
    thousand images were OCR'd first.
    """
    shots = tmp_path / "screenshots"
    shots.mkdir()
    for i in range(5):
        (shots / f"shot{i}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    papers = tmp_path / "papers"
    papers.mkdir()
    (papers / "cv.pdf").write_bytes(b"%PDF-1.4\n")
    (papers / "notes.txt").write_text("text")

    kinds = [
        "image" if (r.mime or "").startswith("image/") else "doc"
        for r in FileConnector((str(tmp_path),)).walk()
    ]
    assert kinds.count("doc") == 2 and kinds.count("image") == 5
    assert kinds[:2] == ["doc", "doc"], f"images were not deferred: {kinds}"


def test_the_walk_still_reports_everything_it_skipped(tmp_path: Path) -> None:
    """Ordering must not lose the stats, which are gathered during the scan."""
    (tmp_path / "keep.txt").write_text("x")
    (tmp_path / "movie.mp4").write_bytes(b"\x00")
    (tmp_path / "id_rsa").write_text("secret")

    connector = FileConnector((str(tmp_path),))
    assert [r.path.name for r in connector.walk()] == ["keep.txt"]
    assert connector.stats.seen == 3
    assert connector.stats.excluded_secret == 1


def test_config_can_exclude_a_directory_the_defaults_do_not_know_about(
    tmp_path: Path,
) -> None:
    """Machine-specific junk: added to the defaults, not replacing them."""
    junk = tmp_path / "controlcenter"
    junk.mkdir()
    (junk / "IMG_0001.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (junk / "note.txt").write_text("still not wanted")
    nested = tmp_path / "deep" / "controlcenter"
    nested.mkdir(parents=True)
    (nested / "IMG_0002.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "real.txt").write_text("wanted")

    connector = FileConnector((str(tmp_path),), extra_excluded_dirs=("controlcenter",))
    assert [r.path.name for r in connector.walk()] == ["real.txt"]

    # The built-in exclusions must still apply alongside it.
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.txt").write_text("noise")
    connector = FileConnector((str(tmp_path),), extra_excluded_dirs=("controlcenter",))
    assert [r.path.name for r in connector.walk()] == ["real.txt"]


def test_a_file_with_no_extractable_text_is_still_findable_by_name(
    conn, tmp_path: Path
) -> None:
    """Chunks are the only thing that reaches the index.

    A photograph OCRs to nothing, and a document with no chunks has no row in
    the search index at all — so "passport photo" could not find
    passport_photo.png even though the name says exactly what it is.
    """
    from muster.fileingest import name_text

    image = tmp_path / "passport_photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

    record = _record(tmp_path, "passport_photo.png")
    result = ingest_file(conn, record)

    assert result.chunks == 1
    assert result.empty is True, "it still reports that no text was extracted"
    text = conn.execute(
        "SELECT text FROM chunks WHERE doc_id = ?", (result.doc_id,)
    ).fetchone()[0]
    assert text == "passport photo.png"
    assert name_text(Path("Sam_Reyes-CV.pdf")) == "Sam Reyes CV.pdf"


def test_the_name_chunk_is_reachable_through_search(conn, tmp_path: Path) -> None:
    from muster.search import keyword_search

    (tmp_path / "passport_photo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    ingest_file(conn, _record(tmp_path, "passport_photo.png"))
    conn.commit()

    hits = keyword_search(conn, "passport photo", min_score=0.0)
    assert any("passport" in (h.title or "").lower() for h in hits)


def test_a_document_left_with_no_chunks_is_retried_once(conn, tmp_path: Path) -> None:
    """Unchanged means "nothing to do", not "leave it broken"."""
    (tmp_path / "note.txt").write_text("some text")
    record = _record(tmp_path, "note.txt")
    first = ingest_file(conn, record)

    # Simulate the broken state: indexed, but nothing reached the index.
    conn.execute("DELETE FROM chunks WHERE doc_id = ?", (first.doc_id,))
    conn.commit()

    again = ingest_file(conn, record)
    assert not again.unchanged, "size and mtime matched, so it skipped a broken doc"
    assert again.chunks >= 1

    # And having healed it, the next pass is a plain skip again.
    assert ingest_file(conn, record).unchanged is True


# --- what counts as a document -------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["report.pdf", "notes.md", "letter.docx", "memo.rtf", "essay.txt",
     "deck.pages", "scan.jpg", "shot.png", "slides.html"],
)
def test_documents_are_indexed(tmp_path: Path, name: str) -> None:
    (tmp_path / name).write_bytes(b"content")
    assert [r.path.name for r in FileConnector((str(tmp_path),)).walk()] == [name]


@pytest.mark.parametrize(
    "name",
    ["all.min.js", "fontawesome.js", "style.css", "spec.c", "main.py",
     "data.json", "tsconfig.ts", "Makefile", "schema.sql"],
)
def test_code_is_not_a_document(tmp_path: Path, name: str) -> None:
    """The brief asked for pdf, docx, txt, md, rtf, pages and images.

    Treating "is it text" as the test swept in build output: one bundled
    fontawesome.js alone produced 718 chunks, and minified libraries came to a
    quarter of everything waiting to be embedded.
    """
    (tmp_path / name).write_text("x = 1")
    assert list(FileConnector((str(tmp_path),)).walk()) == []


@pytest.mark.parametrize("directory", [".next", "dist", "build", "out", "target", "coverage"])
def test_build_output_directories_are_pruned(tmp_path: Path, directory: str) -> None:
    """Bundled files are named after the packages they inline, not placed under
    node_modules, so excluding that name alone does not reach them."""
    nested = tmp_path / directory / "static"
    nested.mkdir(parents=True)
    (nested / "bundle.pdf").write_bytes(b"%PDF-")
    (tmp_path / "real.pdf").write_bytes(b"%PDF-")

    assert [r.path.name for r in FileConnector((str(tmp_path),)).walk()] == ["real.pdf"]


def test_the_allowlist_runs_after_the_secret_check(tmp_path: Path) -> None:
    """A .pdf named like a credential is still refused."""
    (tmp_path / "id_rsa.pdf").write_bytes(b"%PDF-")
    connector = FileConnector((str(tmp_path),))
    assert list(connector.walk()) == []
    assert connector.stats.excluded_secret == 1
