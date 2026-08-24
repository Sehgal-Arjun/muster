"""Connectors.

A connector's only job is to yield ``(source_uid, raw_rfc822_bytes, metadata)``.
It does no parsing, no chunking, no database work. All of that happens once,
downstream, so that Outlook and IMAP inherit every parser fix for free.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class RawMessage:
    """One fetched item, exactly as the provider stored it."""

    source_uid: str  # provider message id, stable across fetches
    raw: bytes  # RFC822 bytes, unmodified
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Connector(Protocol):
    """What Outlook and IMAP will implement later. Deliberately tiny."""

    source: str  # 'gmail' | 'outlook' | 'imap' | 'file'

    def fetch(
        self,
        query: str | None = None,
        *,
        limit: int | None = None,
        skip: Callable[[str], bool] | None = None,
    ) -> Iterator[RawMessage]:
        """Yield messages matching a provider-native query, newest first.

        ``skip`` is consulted with each candidate ``source_uid`` *before* the
        message body is fetched. Providers list ids cheaply and in bulk but
        charge a round trip per body, so a re-sync that already holds most of
        the mailbox should pay for the listing and nothing else.
        """
        ...
