from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from muster.blobs import BlobNotFound, BlobStore, relpath_for


@pytest.fixture
def store(tmp_path: Path) -> BlobStore:
    return BlobStore(tmp_path / "blobs")


def test_put_returns_sharded_path_and_digest(store: BlobStore) -> None:
    data = b"hello muster"
    rel, digest = store.put_blob(data)

    assert digest == hashlib.sha256(data).hexdigest()
    assert rel == f"{digest[:2]}/{digest}"
    assert (store.root / rel).read_bytes() == data


def test_put_is_deduplicated(store: BlobStore) -> None:
    rel_a, digest_a = store.put_blob(b"same bytes")
    rel_b, digest_b = store.put_blob(b"same bytes")

    assert (rel_a, digest_a) == (rel_b, digest_b)
    assert list(store.iter_digests()) == [digest_a]
    assert sum(1 for _ in store.root.rglob("*") if _.is_file()) == 1


def test_distinct_bytes_get_distinct_blobs(store: BlobStore) -> None:
    _, a = store.put_blob(b"one")
    _, b = store.put_blob(b"two")
    assert a != b
    assert sorted(store.iter_digests()) == sorted([a, b])


def test_get_blob_by_digest_or_relpath(store: BlobStore) -> None:
    rel, digest = store.put_blob(b"payload")
    assert store.get_blob(digest) == b"payload"
    assert store.get_blob(rel) == b"payload"
    assert store.get_blob(rel, verify=True) == b"payload"


def test_get_missing_blob_raises(store: BlobStore) -> None:
    with pytest.raises(BlobNotFound):
        store.get_blob("0" * 64)


def test_empty_bytes_are_storable(store: BlobStore) -> None:
    rel, digest = store.put_blob(b"")
    assert digest == hashlib.sha256(b"").hexdigest()
    assert store.get_blob(rel) == b""


def test_blobs_are_read_only(store: BlobStore) -> None:
    rel, _ = store.put_blob(b"immutable")
    path = store.root / rel
    assert path.stat().st_mode & 0o222 == 0, "blobs must not be writable"
    with pytest.raises(PermissionError):
        path.open("wb")


def test_put_file_matches_put_blob(store: BlobStore, tmp_path: Path) -> None:
    src = tmp_path / "doc.bin"
    src.write_bytes(b"x" * (1 << 20) + b"tail")

    rel_file, digest_file = store.put_file(src)
    rel_bytes, digest_bytes = store.put_blob(src.read_bytes())

    assert (rel_file, digest_file) == (rel_bytes, digest_bytes)
    assert store.get_blob(digest_file, verify=True) == src.read_bytes()


def test_no_temp_files_left_behind(store: BlobStore) -> None:
    store.put_blob(b"a")
    store.put_blob(b"b")
    assert not [p for p in store.root.rglob(".tmp-*")]


def test_verify_detects_corruption(store: BlobStore) -> None:
    rel, digest = store.put_blob(b"trustworthy")
    path = store.root / rel
    path.chmod(0o644)
    path.write_bytes(b"tampered")

    with pytest.raises(ValueError, match="corrupt"):
        store.get_blob(digest, verify=True)


def test_relpath_rejects_non_digests() -> None:
    for bad in ("", "abc", "z" * 64, "A" * 64):
        with pytest.raises(ValueError):
            relpath_for(bad)
