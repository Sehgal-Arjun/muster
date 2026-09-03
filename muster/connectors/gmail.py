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
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from muster.connectors import RawMessage

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
DEFAULT_QUERY = "newer_than:7d"
PAGE_SIZE = 500
MAX_ATTEMPTS = 5


class HistoryTooOld(RuntimeError):
    """The stored historyId is older than Gmail is willing to answer for.

    Gmail keeps history for roughly a week. Past that the only honest answer is
    "list the mailbox again", which is what the caller falls back to.
    """


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
        # Ids the caller already holds. Counted rather than fetched: this is
        # the difference between a re-sync taking minutes and taking hours.
        self.skipped_known = 0

    @property
    def service(self):
        if self._service is None:
            from googleapiclient.discovery import build

            creds = _credentials(self.credentials_path, self.token_path)
            self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._service

    def current_history_id(self) -> str | None:
        """The mailbox's history cursor right now. One request.

        Used to start incremental sync from "everything up to here is already
        indexed" after a full pass, and to bootstrap when nothing is stored.
        """
        profile = _retrying(
            lambda: self.service.users().getProfile(userId="me").execute(),
            what="users.getProfile",
        )
        history_id = profile.get("historyId")
        return str(history_id) if history_id is not None else None

    def history_since(self, start_history_id: str) -> tuple[list[str], str | None]:
        """Message ids added since ``start_history_id``, and the new cursor.

        This is the whole point of incremental sync: with nothing new it is a
        single request that returns a cursor and no history at all, rather than
        a listing of the mailbox.

        Raises :class:`HistoryTooOld` when Gmail has expired the cursor, which
        is a routine event after about a week and not a failure.
        """
        from googleapiclient.errors import HttpError

        added: list[str] = []
        seen: set[str] = set()
        page_token = None
        newest = start_history_id

        while True:
            try:
                response = _retrying(
                    lambda: self.service.users()
                    .history()
                    .list(
                        userId="me",
                        startHistoryId=start_history_id,
                        # Deletions and label changes are not what this index
                        # tracks; asking for them only makes the response bigger.
                        historyTypes=["messageAdded"],
                        pageToken=page_token,
                    )
                    .execute(),
                    what="users.history.list",
                )
            except HttpError as exc:
                if exc.resp.status == 404:
                    raise HistoryTooOld(
                        f"Gmail has expired history id {start_history_id}"
                    ) from exc
                raise

            newest = str(response.get("historyId", newest))
            for record in response.get("history", []):
                for item in record.get("messagesAdded", []):
                    message_id = item.get("message", {}).get("id")
                    if message_id and message_id not in seen:
                        seen.add(message_id)
                        added.append(message_id)

            page_token = response.get("nextPageToken")
            if not page_token:
                return added, newest

    def fetch_ids(self, message_ids: Iterable[str], *, skip=None) -> Iterator[RawMessage]:
        """Fetch specific messages by id, for the incremental path.

        ``fetch`` discovers ids by listing; here they are already known, so the
        listing — the expensive half — is skipped entirely.
        """
        self.listed = 0
        self.dropped = []
        self.skipped_known = 0
        for message_id in message_ids:
            self.listed += 1
            if skip is not None and skip(message_id):
                self.skipped_known += 1
                continue
            message = self._get_one(message_id)
            if message is not None:
                yield message

    def _get_one(self, message_id: str) -> RawMessage | None:
        messages = self.service.users().messages()
        try:
            payload = _retrying(
                lambda: messages.get(userId="me", id=message_id, format="raw").execute(),
                what=f"messages.get {message_id}",
            )
        except Exception as exc:
            log.error("skipping %s: %s", message_id, exc)
            self.dropped.append((message_id, str(exc)))
            return None

        raw = payload.get("raw")
        if not raw:
            log.error("skipping %s: no raw payload", message_id)
            self.dropped.append((message_id, "no raw payload"))
            return None

        return RawMessage(
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

    def list_ids(
        self, query: str, limit: int | None = None, on_page=None
    ) -> Iterator[str]:
        """Page through message ids.

        ``on_page`` is called after each page with the running total. Listing a
        large mailbox takes minutes and produces nothing until the first fetch,
        which is indistinguishable from a hang.
        """
        messages = self.service.users().messages()
        page_token = None
        seen = 0
        pages = 0
        while True:
            page_size = PAGE_SIZE if limit is None else min(PAGE_SIZE, limit - seen)
            response = _retrying(
                lambda: messages.list(
                    userId="me", q=query, maxResults=page_size, pageToken=page_token
                ).execute(),
                what="messages.list",
            )
            batch = response.get("messages", [])
            pages += 1
            if on_page:
                on_page(seen + len(batch), pages, bool(response.get("nextPageToken")))
            for item in batch:
                yield item["id"]
                self.listed += 1
                seen += 1
                if limit is not None and seen >= limit:
                    return
            page_token = response.get("nextPageToken")
            if not page_token:
                return

    def fetch(
        self,
        query: str | None = None,
        *,
        limit: int | None = None,
        skip=None,
        on_page=None,
    ) -> Iterator[RawMessage]:
        query = query or DEFAULT_QUERY
        self.listed = 0
        self.dropped = []
        self.skipped_known = 0
        messages = self.service.users().messages()

        # The limit counts messages actually yielded, not ids listed. Listing
        # is one request per 500 ids; fetching is one per message. Capping the
        # listing would mean `--limit 20` on a known mailbox fetching nothing.
        yielded = 0
        for message_id in self.list_ids(query, None, on_page=on_page):
            if skip is not None and skip(message_id):
                self.skipped_known += 1
                continue
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
                    # Kept per message so a future incremental sync can resume
                    # from the highest one seen, instead of listing at all.
                    "history_id": payload.get("historyId"),
                    "size_estimate": payload.get("sizeEstimate"),
                },
            )
            yielded += 1
            if limit is not None and yielded >= limit:
                return
