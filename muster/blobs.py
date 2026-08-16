"""Content-addressed blob store.

Every original — a raw RFC822 message, an attachment, a file — is stored once at
``<root>/<first-2-hex>/<full-sha256>`` and never modified. Identical bytes
written twice produce one file. The database references blobs by the path
*relative* to the store root, so the whole ~/.muster directory can be moved
without rewriting rows.

Blobs are written atomically (temp file, then rename) and left read-only, so a
crashed write can never leave a truncated file at a valid content address.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

READ_CHUNK = 1 << 20  # 1 MiB
_BLOB_MODE = 0o444  # immutable by convention, and by permission


class BlobNotFound(KeyError):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while block := fh.read(READ_CHUNK):
            h.update(block)
    return h.hexdigest()


def relpath_for(digest: str) -> str:
    """The store-relative path for a digest. Pure function, no I/O."""
    if len(digest) != 64 or not all(c in "0123456789abcdef" for c in digest):
        raise ValueError(f"not a sha256 hex digest: {digest!r}")
    return f"{digest[:2]}/{digest}"


@dataclass(frozen=True)
class BlobStore:
    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).expanduser())

    # --- locating ---------------------------------------------------------

    def abspath(self, digest_or_relpath: str) -> Path:
        """Absolute path for a digest, or for a store-relative path from the DB."""
        if "/" in digest_or_relpath:
            return self.root / digest_or_relpath
        return self.root / relpath_for(digest_or_relpath)

    def exists(self, digest: str) -> bool:
        return self.abspath(digest).exists()

    # --- writing ----------------------------------------------------------

    def put_blob(self, data: bytes) -> tuple[str, str]:
        """Store bytes. Returns (store-relative path, sha256). Idempotent."""
        digest = sha256_bytes(data)
        rel = relpath_for(digest)
        dest = self.root / rel
        if dest.exists():
            return rel, digest
        self._write_atomic(dest, data)
        return rel, digest

    def put_file(self, src: Path) -> tuple[str, str]:
        """Store a file's contents without loading it all into memory."""
        src = Path(src)
        digest = sha256_file(src)
        rel = relpath_for(digest)
        dest = self.root / rel
        if dest.exists():
            return rel, digest
        with src.open("rb") as fh:
            self._write_atomic(dest, fh)
        return rel, digest

    def _write_atomic(self, dest: Path, source: bytes | object) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".tmp-")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as out:
                if isinstance(source, bytes):
                    out.write(source)
                else:
                    while block := source.read(READ_CHUNK):  # type: ignore[attr-defined]
                        out.write(block)
                out.flush()
                os.fsync(out.fileno())
            os.chmod(tmp, _BLOB_MODE)
            # Another process may have won the race; either copy is identical.
            os.replace(tmp, dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    # --- reading ----------------------------------------------------------

    def get_blob(self, digest_or_relpath: str, *, verify: bool = False) -> bytes:
        path = self.abspath(digest_or_relpath)
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise BlobNotFound(digest_or_relpath) from exc
        if verify:
            digest = digest_or_relpath.rsplit("/", 1)[-1]
            actual = sha256_bytes(data)
            if actual != digest:
                raise ValueError(f"blob corrupt: {digest} contains {actual}")
        return data

    def open_blob(self, digest_or_relpath: str):
        try:
            return self.abspath(digest_or_relpath).open("rb")
        except FileNotFoundError as exc:
            raise BlobNotFound(digest_or_relpath) from exc

    # --- maintenance ------------------------------------------------------

    def iter_digests(self):
        for shard in sorted(self.root.glob("[0-9a-f][0-9a-f]")):
            for blob in sorted(shard.iterdir()):
                if blob.is_file() and not blob.name.startswith(".tmp-"):
                    yield blob.name
