"""Keeping one-time credentials out of the index.

A mail archive accumulates login codes, password resets and magic links. They
are the highest-risk content it holds and among the least useful to search:
nobody looks for last March's Spotify code, and every one of them sitting in
plaintext in an FTS index is a liability that grows with the archive.

A rule matches only when **both** the sender and the subject pattern match.
Either alone is far too broad — plenty of real mail comes from no-reply
addresses, and "password" appears in correspondence about policy.

Two modes:

* ``redact`` — the document and its original blob are kept, but nothing
  searchable is written: no body chunks, and digit runs in the title are
  masked. The archive stays complete and re-parseable. **The original raw
  message still contains the secret on disk**, under ``blobs/``.
* ``drop`` — nothing is stored at all: no document row, no blob.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Anything that looks like a code. Senders format them in groups as often as
# not — "Supercell ID Login [987 386]" slipped through a plain \d{4,} — so
# split groups are matched too. Over-masking is cheap here: only the titles of
# already-redacted documents are touched, and those are unsearchable anyway.
_DIGIT_RUN_RE = re.compile(
    r"""(?x)
    \d{4,}                       # 187431
  | \b\d{3}[ .\-]\d{3,}\b        # 987 386
  | \b\d{2}(?:[ .\-]\d{2}){2,}\b  # 12-34-56
    """
)
_TOKEN_RE = re.compile(r"\b(?=\w*\d)(?=\w*[A-Za-z])\w{12,}\b")

REDACTED_MARK = "[redacted]"


@dataclass(frozen=True)
class RedactionMatch:
    rule: str
    mode: str

    @property
    def drops(self) -> bool:
        return self.mode == "drop"


class Redactor:
    """Compiled rules. Built once per run, not per message."""

    def __init__(self, config):
        self.enabled = config.enabled
        self.mode = config.mode
        self._rules: list[tuple[str, re.Pattern[str], re.Pattern[str]]] = []
        for rule in config.rules:
            try:
                self._rules.append(
                    (rule.name, re.compile(rule.sender), re.compile(rule.subject))
                )
            except re.error as exc:
                # A bad pattern must not silently disable redaction for
                # everything else, nor take the run down.
                log.error("redaction rule %r has an invalid pattern (%s); skipped",
                          rule.name, exc)

    def match(self, senders, subject: str | None) -> RedactionMatch | None:
        if not self.enabled or not self._rules:
            return None
        subject = subject or ""
        addresses = [a for a in (senders or []) if a]
        for name, sender_re, subject_re in self._rules:
            if not subject_re.search(subject):
                continue
            if any(sender_re.search(address) for address in addresses):
                return RedactionMatch(rule=name, mode=self.mode)
        return None

    def mask_title(self, title: str | None) -> str:
        """Keep the shape of the subject, lose the secret.

        "187431 – your Spotify login code" stays recognisable as a Spotify
        login code without carrying the code.
        """
        if not title:
            return ""
        masked = _DIGIT_RUN_RE.sub(REDACTED_MARK, title)
        return _TOKEN_RE.sub(REDACTED_MARK, masked)
