"""The one loop that turns fetched messages into indexed documents.

Extracted so the full sync and the incremental sync cannot drift. They differ
only in how the messages are discovered — a mailbox listing versus a history
cursor — and everything after that point (category filtering, sender exclusion,
redaction, ingest, counting) has to behave identically or the two paths will
quietly index different things.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from muster.blobs import BlobStore
from muster.config import Config
from muster.connectors import RawMessage

log = logging.getLogger(__name__)


@dataclass
class Counts:
    """What a run did, in the terms the summary reports."""

    seen: int = 0
    created: int = 0
    skipped: int = 0
    failed: int = 0
    filtered: int = 0
    by_sender: int = 0
    redacted: int = 0
    dropped_by_rule: int = 0
    chunks: int = 0
    attachment_docs: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        """Whether anything reached the index. Drives "was there work to do"."""
        return self.created > 0


def ingest_stream(
    conn,
    store: BlobStore,
    cfg: Config,
    source: str,
    messages: Iterable[RawMessage],
    *,
    exclude_categories: frozenset[str] = frozenset(),
    on_message: Callable[[RawMessage, object], None] | None = None,
) -> Counts:
    """Ingest everything ``messages`` yields.

    One message per transaction, and every failure is logged and stepped over: a
    single unparseable message must never cost the rest of the run.
    """
    from muster.categories import is_excluded
    from muster.ingest import ingest_message
    from muster.redaction import Redactor
    from muster.senders import SenderFilter, excluded_by_sender

    counts = Counts()
    redactor = Redactor(cfg.redaction)
    sender_filter = SenderFilter(cfg.excluded_senders)

    for message in messages:
        counts.seen += 1
        if exclude_categories and is_excluded(message.meta.get("labels"), exclude_categories):
            counts.filtered += 1
            continue
        if sender_filter and excluded_by_sender(message.raw, sender_filter):
            counts.by_sender += 1
            continue

        try:
            result = ingest_message(
                conn,
                store,
                source=source,
                source_uid=message.source_uid,
                raw=message.raw,
                meta=message.meta,
                own_addresses=set(cfg.own_addresses),
                redactor=redactor,
                sender_filter=sender_filter,
            )
        except Exception as exc:  # noqa: BLE001 — one bad message, not the run
            counts.failed += 1
            counts.failures.append((message.source_uid, str(exc)))
            log.exception("failed to ingest %s: %s", message.source_uid, exc)
            continue

        if result.redacted_by:
            counts.redacted += 1
        if result.dropped:
            counts.dropped_by_rule += 1
            continue
        if result.created:
            counts.created += 1
            counts.attachment_docs += result.attachment_docs
            counts.chunks += result.chunks
        else:
            counts.skipped += 1
        if on_message:
            on_message(message, result)

    return counts
