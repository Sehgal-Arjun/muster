"""Local mbox / maildir / .eml connector.

Not a headline feature — it exists because it lets the parser be exercised
against real mail without an OAuth round trip, and because Gmail Takeout
exports are mbox. It uses the standard library ``mailbox`` module and, like
every connector, does no parsing of its own.
"""

from __future__ import annotations

import hashlib
import logging
import mailbox
from collections.abc import Iterator
from pathlib import Path

from muster.connectors import RawMessage

log = logging.getLogger(__name__)


class MboxConnector:
    """Implements the ``Connector`` protocol over a local mailbox file."""

    source = "file"

    def __init__(self, path: Path, *, source_name: str = "file"):
        self.path = Path(path).expanduser().resolve()
        self.source = source_name

    def _box(self):
        if self.path.is_dir():
            return mailbox.Maildir(str(self.path), factory=None, create=False)
        return mailbox.mbox(str(self.path), factory=None, create=False)

    def fetch(self, query: str | None = None, *, limit: int | None = None) -> Iterator[RawMessage]:
        """``query`` is ignored: a local mailbox has no server-side search."""
        if query:
            log.warning("mbox connector ignores query %r", query)

        if self.path.is_file() and self.path.suffix.lower() == ".eml":
            raw = self.path.read_bytes()
            yield RawMessage(source_uid=str(self.path), raw=raw, meta={"path": str(self.path)})
            return

        box = self._box()
        try:
            count = 0
            for key in box.keys():
                if limit is not None and count >= limit:
                    return
                try:
                    raw = box.get_bytes(key)
                except Exception as exc:
                    # A truncated mbox entry must not end the import.
                    log.error("%s[%s]: unreadable (%s)", self.path, key, exc)
                    continue

                # Message-ID would be nicer, but broken messages lack one and
                # the uid has to be stable across re-runs for idempotency.
                digest = hashlib.sha256(raw).hexdigest()
                yield RawMessage(
                    source_uid=f"{self.path}#{digest[:16]}",
                    raw=raw,
                    meta={"path": str(self.path), "mailbox_key": str(key)},
                )
                count += 1
        finally:
            box.close()
