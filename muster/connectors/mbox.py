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
import re
from collections.abc import Iterator
from pathlib import Path

from muster.connectors import RawMessage

log = logging.getLogger(__name__)


_MESSAGE_ID_RE = re.compile(rb"^Message-ID:\s*<([^>\r\n]+)>", re.IGNORECASE | re.MULTILINE)


def _uid_for(raw: bytes) -> str:
    """A uid that survives moving the mailbox file.

    Message-ID first, so the same message imported from two exports is one doc
    and eval queries can name messages portably. Broken messages often lack
    one, hence the content hash — which is still stable across re-runs, which
    is what idempotency needs.
    """
    match = _MESSAGE_ID_RE.search(raw[:8192])
    if match:
        return match.group(1).decode("utf-8", errors="replace").strip()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class MboxConnector:
    """Implements the ``Connector`` protocol over a local mailbox file."""

    source = "file"

    def __init__(self, path: Path, *, source_name: str = "file"):
        self.path = Path(path).expanduser().resolve()
        self.source = source_name
        self.skipped_known = 0

    def _box(self):
        if self.path.is_dir():
            return mailbox.Maildir(str(self.path), factory=None, create=False)
        return mailbox.mbox(str(self.path), factory=None, create=False)

    def fetch(
        self,
        query: str | None = None,
        *,
        limit: int | None = None,
        skip=None,
    ) -> Iterator[RawMessage]:
        """``query`` is ignored: a local mailbox has no server-side search."""
        if query:
            log.warning("mbox connector ignores query %r", query)

        if self.path.is_file() and self.path.suffix.lower() == ".eml":
            raw = self.path.read_bytes()
            uid = str(self.path)
            if skip is None or not skip(uid):
                yield RawMessage(source_uid=uid, raw=raw, meta={"path": uid})
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

                uid = _uid_for(raw)
                if skip is not None and skip(uid):
                    self.skipped_known += 1
                    continue
                yield RawMessage(
                    source_uid=uid,
                    raw=raw,
                    meta={"path": str(self.path), "mailbox_key": str(key)},
                )
                count += 1
        finally:
            box.close()
