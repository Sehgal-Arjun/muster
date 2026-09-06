"""Local files.

The one place this departs from every other connector: it does not copy bytes
into the blob store. A file on disk is already a durable original, and a
content-addressed second copy of a 90MB PDF buys nothing but disk. What is
stored is the path, the hash, the size and the times — enough to find it, notice
it changed, and recognise it after a move.

That makes the hash do double duty. It is still identity, but here it is also
how a file that moved from Downloads to Documents is recognised as the same
document rather than ingested twice under a new path.

macOS records where a downloaded file came from, and reading it is the reason to
do this properly rather than portably: ``kMDItemWhereFroms`` is the join from a
file back to the message that pointed at it.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import plistlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

# Anything larger is a disk image, a video, or a dataset. Reading it costs
# memory and the extractor will find nothing worth indexing.
DEFAULT_MAX_BYTES = 100 * 1024 * 1024

DEFAULT_ROOTS = ("~/Documents", "~/Downloads")

# Directory names that are never a user's documents. Skipped by name at any
# depth, which is what keeps a single node_modules from costing minutes.
DEFAULT_EXCLUDED_DIRS = (
    "node_modules", ".git", "Library", "Caches", ".cache", "venv", ".venv",
    "__pycache__", ".Trash", "site-packages", ".terraform", "DerivedData",
    ".ssh", ".gnupg", ".aws", ".kube", ".docker",
)

# Bundles are directories that macOS presents as files. Descending into one
# produces hundreds of fragments of something the user thinks of as one thing.
DEFAULT_EXCLUDED_SUFFIXES = (
    ".app", ".framework", ".bundle", ".photoslibrary", ".xcodeproj",
    ".sparsebundle", ".fcpbundle", ".imovielibrary",
)

# Video, disk images, archives and binaries: nothing a text extractor can read.
DEFAULT_EXCLUDED_EXTENSIONS = (
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mpg", ".mpeg", ".wmv",
    ".dmg", ".iso", ".sparseimage", ".pkg", ".vmdk",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".mp3", ".wav", ".aiff", ".flac", ".m4a",
    ".so", ".dylib", ".o", ".a", ".class", ".pyc", ".bin", ".exe",
)

# Files whose contents are secrets. Indexing them would put credentials into a
# search index and then into every snippet that matched.
DEFAULT_EXCLUDED_NAMES = (
    ".env", ".netrc", ".htpasswd", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
    "credentials", "secrets.yml", "secrets.yaml", ".pgpass", ".my.cnf",
)
DEFAULT_SECRET_SUFFIXES = (
    ".key", ".pem", ".p12", ".pfx", ".keychain", ".keychain-db", ".kdbx",
    ".1pif", ".opvault", ".agilekeychain", ".jks", ".asc", ".gpg", ".ppk",
)
# A leading-dot file with any of these stems is configuration holding tokens.
DEFAULT_SECRET_PREFIXES = (".env.", ".aws", ".npmrc", ".pypirc", ".dockercfg")


@dataclass(frozen=True)
class FileRecord:
    """One file worth indexing, and everything known about it before reading."""

    path: Path
    size: int
    mtime: str
    birthtime: str | None
    mime: str | None

    @property
    def source_uid(self) -> str:
        return str(self.path)


@dataclass
class WalkStats:
    """Why the walk produced what it did. Silent skipping is how a connector lies."""

    seen: int = 0
    too_big: int = 0
    excluded_type: int = 0
    excluded_secret: int = 0
    excluded_dir: int = 0
    unreadable: int = 0
    roots_missing: list[str] = field(default_factory=list)
    # Roots that exist but cannot be listed. On macOS this is the normal state
    # for Documents and Downloads until the running program is granted access,
    # and it is indistinguishable from an empty folder unless it is reported.
    roots_denied: list[str] = field(default_factory=list)
    walk_errors: int = 0


def _iso(timestamp: float | None) -> str | None:
    if not timestamp:
        return None
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds")


class FileConnector:
    """Walks configured roots and yields the files worth indexing."""

    source = "file"

    def __init__(
        self,
        roots: tuple[str, ...] = DEFAULT_ROOTS,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        excluded_dirs: tuple[str, ...] = DEFAULT_EXCLUDED_DIRS,
        extra_excluded_dirs: tuple[str, ...] = (),
        excluded_extensions: tuple[str, ...] = DEFAULT_EXCLUDED_EXTENSIONS,
        excluded_suffixes: tuple[str, ...] = DEFAULT_EXCLUDED_SUFFIXES,
    ):
        self.roots = tuple(roots)
        self.max_bytes = max_bytes
        self.excluded_dirs = {d.lower() for d in (*excluded_dirs, *extra_excluded_dirs)}
        self.excluded_extensions = {e.lower() for e in excluded_extensions}
        self.excluded_suffixes = tuple(s.lower() for s in excluded_suffixes)
        self.stats = WalkStats()

    # --- exclusion ---------------------------------------------------------

    def is_secret(self, path: Path) -> bool:
        """Whether this looks like credentials rather than a document."""
        name = path.name.lower()
        if name in DEFAULT_EXCLUDED_NAMES:
            return True
        if name.endswith(DEFAULT_SECRET_SUFFIXES):
            return True
        if name.startswith(DEFAULT_SECRET_PREFIXES):
            return True
        # A password manager export is a plain file with an ordinary extension;
        # the name is the only signal there is.
        return "password" in name and name.endswith((".csv", ".txt", ".json", ".xml"))

    def skip_dir(self, path: Path) -> bool:
        name = path.name.lower()
        return name in self.excluded_dirs or name.endswith(self.excluded_suffixes)

    def skip_file(self, path: Path) -> str | None:
        """Why this file is not indexed, or None if it is."""
        name = path.name.lower()
        if name.startswith("."):
            # Dotfiles are configuration, and the ones that are not are still
            # not what anybody searches for.
            return "hidden"
        if name.endswith(self.excluded_suffixes):
            return "bundle"
        if os.path.splitext(name)[1] in self.excluded_extensions:
            return "type"
        if self.is_secret(path):
            return "secret"
        return None

    # --- the walk ----------------------------------------------------------

    def walk(self) -> Iterator[FileRecord]:
        """Every file worth indexing: documents first, images last.

        Not the order the directories happen to be in. Extracting a PDF or a
        docx takes milliseconds; OCR on an image takes seconds, so a single
        folder of screenshots can outweigh every real document on the disk by
        two orders of magnitude. Walked in directory order, one such folder
        starves everything behind it — a CV nine files into another directory
        stays unfindable for an hour because four thousand screenshots happened
        to be listed first.

        Ordering by cost instead makes the index useful within about a minute
        and lets OCR fill in behind it. The listing itself is stat-only and
        cheap, so collecting it before yielding costs a second and a few MB.
        """
        documents: list[FileRecord] = []
        images: list[FileRecord] = []
        for record in self._scan():
            (images if (record.mime or "").startswith("image/") else documents).append(record)
        yield from documents
        yield from images

    def _scan(self) -> Iterator[FileRecord]:
        from muster.extract import is_text_bearing

        self.stats = WalkStats()
        for root in self.roots:
            base = Path(root).expanduser()
            if not base.is_dir():
                self.stats.roots_missing.append(str(base))
                log.warning("root does not exist, skipping: %s", base)
                continue

            # macOS gates Documents, Downloads and Desktop behind a per-program
            # grant. The directory stats fine and lists as empty, so without
            # this check a denied root and an empty one look identical — and
            # "0 files indexed" would read as "you have no documents".
            try:
                os.listdir(base)
            except PermissionError:
                self.stats.roots_denied.append(str(base))
                log.warning("permission denied listing %s", base)
                continue
            except OSError as exc:
                self.stats.roots_missing.append(str(base))
                log.warning("cannot list %s: %s", base, exc)
                continue

            def note(error: OSError) -> None:
                # os.walk swallows these by default, which would hide an entire
                # unreadable subtree inside an otherwise successful walk.
                self.stats.walk_errors += 1
                log.debug("skipping %s: %s", getattr(error, "filename", "?"), error)

            for dirpath, dirnames, filenames in os.walk(
                base, followlinks=False, onerror=note
            ):
                here = Path(dirpath)
                # Pruned in place, so os.walk never descends. Counting them is
                # what makes "we skipped 40k files" visible instead of implied.
                keep = []
                for name in dirnames:
                    if self.skip_dir(here / name):
                        self.stats.excluded_dir += 1
                    else:
                        keep.append(name)
                dirnames[:] = keep

                for name in filenames:
                    path = here / name
                    self.stats.seen += 1
                    reason = self.skip_file(path)
                    if reason == "secret":
                        self.stats.excluded_secret += 1
                        continue
                    if reason is not None:
                        self.stats.excluded_type += 1
                        continue

                    try:
                        stat = path.stat()
                    except OSError:
                        self.stats.unreadable += 1
                        continue
                    if not path.is_file():
                        continue
                    if stat.st_size > self.max_bytes:
                        self.stats.too_big += 1
                        continue

                    mime, _ = mimetypes.guess_type(path.name)
                    if not is_text_bearing(mime, path.name):
                        self.stats.excluded_type += 1
                        continue

                    yield FileRecord(
                        path=path,
                        size=stat.st_size,
                        mtime=_iso(stat.st_mtime) or "",
                        # birthtime is real on APFS and absent elsewhere; the
                        # date priority falls through to mtime when it is.
                        birthtime=_iso(getattr(stat, "st_birthtime", None)),
                        mime=mime,
                    )


def content_hash(path: Path) -> str:
    """sha256 of the file, read in blocks so a large PDF is not held in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _getxattr(path: Path, name: str) -> bytes | None:
    """Read one extended attribute, via libc.

    Not ``os.getxattr`` — that exists only on Linux — and not the ``xattr``
    command, because this runs once per file and a subprocess per file turns a
    walk of thirty thousand documents into minutes of pure process spawning.
    """
    import ctypes
    import ctypes.util

    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    encoded_path = str(path).encode()
    encoded_name = name.encode()

    # macOS getxattr takes position and options that Linux's does not.
    size = libc.getxattr(encoded_path, encoded_name, None, 0, 0, 0)
    if size <= 0:
        return None
    buffer = ctypes.create_string_buffer(size)
    read = libc.getxattr(encoded_path, encoded_name, buffer, size, 0, 0)
    return buffer.raw[:read] if read > 0 else None


def spotlight(path: Path) -> dict:
    """Where a downloaded file came from, and when.

    This is the reason to write a macOS connector rather than a portable one. A
    file in Downloads knows the URL it came from, and often the page that linked
    to it, which is the join back to the message that pointed at the file.

    Read from the extended attributes the downloader wrote, not from ``mdls``.
    Both report the same fields, but the xattr is the thing Spotlight itself
    reads: it needs no index, works on volumes Spotlight ignores — including
    every temporary directory — and costs a syscall instead of a process.

    Best-effort throughout. A file with no download history is the normal case,
    not a failure.
    """
    out: dict = {}

    try:
        raw = _getxattr(path, "com.apple.metadata:kMDItemWhereFroms")
        if raw:
            value = plistlib.loads(raw)
            if isinstance(value, str):
                value = [value]
            cleaned = [str(v) for v in value if v] if isinstance(value, list) else []
            if cleaned:
                out["where_from"] = cleaned
    except Exception as exc:  # noqa: BLE001 — absent or malformed is not an error
        log.debug("no where-from for %s: %s", path, exc)

    try:
        raw = _getxattr(path, "com.apple.metadata:kMDItemDownloadedDate")
        if raw:
            value = plistlib.loads(raw)
            if isinstance(value, list):
                value = value[0] if value else None
            if isinstance(value, datetime):
                out["downloaded_date"] = value.replace(
                    tzinfo=value.tzinfo or UTC
                ).isoformat()
    except Exception as exc:  # noqa: BLE001
        log.debug("no download date for %s: %s", path, exc)

    return out
