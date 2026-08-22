"""Gmail connector.

Fetches messages with ``format=raw`` so the parser downstream sees exactly the
bytes Gmail stored, not Google's decoded interpretation of them. Nothing here
knows what a MIME part is.

Requires an OAuth client secret ("Desktop app" credentials) at
``~/.muster/credentials.json``. The resulting token is cached at
``~/.muster/gmail_token.json`` and refreshed silently thereafter.
"""

from __future__ import annotations

import base64
import logging
import random
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from muster.connectors import RawMessage

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
DEFAULT_QUERY = "newer_than:7d"
PAGE_SIZE = 500
MAX_ATTEMPTS = 5


class GmailAuthError(RuntimeError):
    pass


def _credentials(credentials_path: Path, token_path: Path):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except ValueError:
            log.warning("cached token at %s is unreadable; re-authorising", token_path)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:  # refresh_token revoked or expired
            log.warning("token refresh failed (%s); re-authorising", exc)
            creds = None

    if not creds or not creds.valid:
        if not credentials_path.exists():
            raise GmailAuthError(
                f"no OAuth client secret at {credentials_path}.\n"
                "Create a Desktop app OAuth client in Google Cloud Console with the\n"
                "Gmail API enabled, download the JSON, and save it there."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
        creds = flow.run_local_server(port=0)

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    token_path.chmod(0o600)
    return creds


def _retrying(call, *, what: str):
    """Gmail rate-limits aggressively; back off rather than losing a run."""
    from googleapiclient.errors import HttpError

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return call()
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            retryable = status in (403, 429, 500, 502, 503, 504)
            if not retryable or attempt == MAX_ATTEMPTS:
                raise
            delay = min(2**attempt, 32) + random.random()
            log.warning("%s: HTTP %s, retrying in %.1fs", what, status, delay)
            time.sleep(delay)
    raise AssertionError("unreachable")


class GmailConnector:
    """Implements the ``Connector`` protocol."""

    source = "gmail"

    def __init__(self, credentials_path: Path, token_path: Path, *, service: Any = None):
        self.credentials_path = Path(credentials_path)
        self.token_path = Path(token_path)
        self._service = service
        # A message Gmail listed but we could not fetch never reaches the
        # caller, so without these counters it would vanish from the run
        # summary entirely and look like it was never there.
        self.listed = 0
        self.dropped: list[tuple[str, str]] = []

    @property
    def service(self):
        if self._service is None:
            from googleapiclient.discovery import build

            creds = _credentials(self.credentials_path, self.token_path)
            self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._service

    def list_ids(self, query: str, limit: int | None = None) -> Iterator[str]:
        messages = self.service.users().messages()
        page_token = None
        seen = 0
        while True:
            page_size = PAGE_SIZE if limit is None else min(PAGE_SIZE, limit - seen)
            response = _retrying(
                lambda: messages.list(
                    userId="me", q=query, maxResults=page_size, pageToken=page_token
                ).execute(),
                what="messages.list",
            )
            for item in response.get("messages", []):
                yield item["id"]
                self.listed += 1
                seen += 1
                if limit is not None and seen >= limit:
                    return
            page_token = response.get("nextPageToken")
            if not page_token:
                return

    def fetch(self, query: str | None = None, *, limit: int | None = None) -> Iterator[RawMessage]:
        query = query or DEFAULT_QUERY
        self.listed = 0
        self.dropped = []
        messages = self.service.users().messages()

        for message_id in self.list_ids(query, limit):
            try:
                payload = _retrying(
                    lambda mid=message_id: messages.get(
                        userId="me", id=mid, format="raw"
                    ).execute(),
                    what=f"messages.get {message_id}",
                )
            except Exception as exc:
                # One unfetchable message must not end the run.
                log.error("skipping %s: %s", message_id, exc)
                self.dropped.append((message_id, str(exc)))
                continue

            raw = payload.get("raw")
            if not raw:
                log.error("skipping %s: no raw payload", message_id)
                self.dropped.append((message_id, "no raw payload"))
                continue

            yield RawMessage(
                source_uid=message_id,
                raw=base64.urlsafe_b64decode(raw),
                meta={
                    "thread_uid": payload.get("threadId"),
                    "labels": payload.get("labelIds", []),
                    "internal_date": payload.get("internalDate"),
                    "history_id": payload.get("historyId"),
                    "size_estimate": payload.get("sizeEstimate"),
                },
            )
