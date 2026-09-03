"""Sender-level exclusion.

Gmail's categories are not enough on their own. GitHub notifications land in
``updates`` — the category that also holds receipts and shipping notices, which
is exactly why that category is not filtered by default — and a single
repository's activity can account for most of a month's mail. Silencing it by
category would throw away the receipts too, so the axis has to be the sender.

Two pattern shapes, distinguished by whether the pattern contains an ``@``:

* ``notifications@github.com`` — that address exactly.
* ``substack.com`` — that domain and any subdomain of it, which is how
  newsletter platforms send: each publication gets its own subdomain.

The distinction matters. ``no-reply@spotify.com`` is excluded while other
Spotify mail is kept, and ``listings.example`` stays entirely.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

log = logging.getLogger(__name__)


def normalise(address: str | None) -> str:
    return (address or "").strip().strip("<>").lower()


def domain_of(address: str) -> str:
    return normalise(address).rpartition("@")[2]


class SenderFilter:
    """Compiled exclusion patterns. Built once per run."""

    def __init__(self, patterns: Sequence[str] = ()):
        self.addresses: set[str] = set()
        self.domains: set[str] = set()
        for raw in patterns:
            pattern = normalise(raw)
            if not pattern:
                continue
            if "@" in pattern:
                self.addresses.add(pattern)
            else:
                self.domains.add(pattern.lstrip("."))

    def __bool__(self) -> bool:
        return bool(self.addresses or self.domains)

    def match(self, addresses: Iterable[str] | None) -> str | None:
        """The pattern that excludes this sender, or None."""
        if not self:
            return None
        for raw in addresses or ():
            address = normalise(raw)
            if not address:
                continue
            if address in self.addresses:
                return address
            domain = domain_of(address)
            if not domain:
                continue
            for excluded in self.domains:
                if domain == excluded or domain.endswith("." + excluded):
                    return excluded
        return None

    def sql_predicate(self, column: str = "d.sender") -> tuple[str, dict[str, str]]:
        """A WHERE fragment selecting rows this filter excludes.

        Used by purge to remove mail that was indexed before the sender was
        added to the list.
        """
        clauses: list[str] = []
        params: dict[str, str] = {}
        # The domain half of the address, so a domain pattern matches every
        # local part rather than only a sender literally called "substack.com".
        domain_expr = f"substr({column}, instr({column}, '@') + 1)"
        for i, address in enumerate(sorted(self.addresses)):
            clauses.append(f"{column} = :addr{i}")
            params[f"addr{i}"] = address
        for i, domain in enumerate(sorted(self.domains)):
            clauses.append(f"({domain_expr} = :dom{i} OR {domain_expr} LIKE :domsub{i})")
            params[f"dom{i}"] = domain
            params[f"domsub{i}"] = f"%.{domain}"
        if not clauses:
            return "0", {}
        return "(" + " OR ".join(clauses) + ")", params


def excluded_by_sender(raw: bytes, sender_filter: "SenderFilter") -> bool:
    """Whether a raw message is from an excluded sender.

    Reads just the From header rather than parsing the whole message: sync
    throws these away before anything is stored, so paying for a full MIME parse
    of mail we are about to discard would be the wrong order of work.
    """
    import email
    import email.policy
    import email.utils

    try:
        head = email.message_from_bytes(raw[:8192], policy=email.policy.default)
        addresses = [a for _n, a in email.utils.getaddresses([head.get("From") or ""]) if a]
    except Exception:  # noqa: BLE001 — an unreadable header is not an exclusion
        return False
    return sender_filter.match(addresses) is not None
