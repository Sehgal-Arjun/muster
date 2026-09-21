"""Incremental Gmail sync.

A routine sync should cost one request. Listing a mailbox to discover that
nothing has changed is the expensive way to learn nothing, and at fifteen-minute
intervals it is most of what the scheduler would ever do.

``users.history.list`` answers "what arrived after this point" directly, and
with nothing new it returns a cursor and no history at all. The catch is that
Gmail keeps history for roughly a week: past that the cursor is refused, and the
only honest recovery is the mailbox listing this exists to avoid. So the date
query is not the old path — it is the fallback, and it has to stay correct.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from muster import syncstate
from muster.blobs import BlobStore
from muster.config import Config
from muster.pipeline import Counts, ingest_stream

log = logging.getLogger(__name__)

SOURCE = "gmail"


@dataclass
class Outcome:
    """What a run did, in terms a one-line summary can be written from."""

    mode: str  # 'incremental' | 'bootstrap' | 'fallback'
    counts: Counts = field(default_factory=Counts)
    cursor: str | None = None
    considered: int = 0  # ids the provider reported as new
    # Reported by the provider but not fetchable — usually a message deleted
    # between being announced and being asked for. Counted because a silent
    # drop at the connector is invisible in every other number here.
    unfetchable: int = 0
    # Deleted or trashed in Gmail since the last sync. Hidden from search but
    # kept on record; `muster purge --missing` is what actually removes them.
    removed: int = 0
    fallback_reason: str | None = None

    def summary(self) -> str:
        if self.mode == "bootstrap":
            return (
                "bootstrapped from the current mailbox state; "
                "the next sync will be incremental"
            )
        where = {
            "incremental": "incremental",
            "fallback": f"full listing ({self.fallback_reason})",
        }[self.mode]
        if not self.considered and not self.counts.created and not self.removed:
            return f"{where}: nothing new"
        parts = [f"{self.counts.created} indexed"]
        if self.counts.skipped:
            parts.append(f"{self.counts.skipped} already held")
        if self.counts.filtered:
            parts.append(f"{self.counts.filtered} filtered")
        if self.counts.by_sender:
            parts.append(f"{self.counts.by_sender} excluded sender")
        if self.counts.failed:
            parts.append(f"{self.counts.failed} failed")
        if self.unfetchable:
            parts.append(f"{self.unfetchable} gone from the mailbox")
        if self.removed:
            parts.append(f"{self.removed} deleted in Gmail")
        return f"{where}: {', '.join(parts)}, {self.counts.chunks} chunks"


def sync(
    conn,
    cfg: Config,
    connector,
    *,
    exclude_categories: frozenset[str] = frozenset(),
    allow_fallback: bool = True,
) -> Outcome:
    """Bring the index up to date, as cheaply as the provider allows."""
    from muster.connectors.gmail import HistoryTooOld

    store = BlobStore(cfg.blob_dir)
    state = syncstate.get(conn, SOURCE)
    cursor = state.cursor if state else None

    if not cursor:
        # A mailbox synced before this table existed still knows where it got
        # to — every message kept the historyId it was fetched with.
        cursor = syncstate.highest_indexed_history_id(conn)

    if not cursor:
        outcome = Outcome(mode="bootstrap", cursor=connector.current_history_id())
        syncstate.set_cursor(conn, SOURCE, outcome.cursor, result=outcome.summary())
        return outcome

    try:
        added, removed, new_cursor = connector.changes_since(cursor)
    except HistoryTooOld as exc:
        if not allow_fallback:
            raise
        log.info("history expired, falling back to a listing: %s", exc)
        return _full(conn, store, cfg, connector, exclude_categories, reason="history expired")

    # A message added and then deleted inside one history window is gone. The
    # deletion is the later fact, and fetching it would cost a request to store
    # something we are about to hide.
    if removed:
        gone = set(removed)
        added = [message_id for message_id in added if message_id not in gone]

    outcome = Outcome(mode="incremental", considered=len(added))

    if removed:
        from muster.missing import mark_missing

        ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM docs WHERE source = ? AND source_uid IN (%s)"
                % ",".join("?" * len(removed)),
                (SOURCE, *removed),
            )
        ]
        outcome.removed = mark_missing(conn, ids, reason="deleted in Gmail")

    if added:
        # Gmail reports a message as added for label changes too, so most of
        # these are usually already held. Checking locally is free; fetching to
        # find out is a request each.
        known = {
            row[0]
            for row in conn.execute(
                "SELECT source_uid FROM docs WHERE source = ?", (SOURCE,)
            )
        }
        fresh = [mid for mid in added if mid not in known]
        outcome.counts = ingest_stream(
            conn, store, cfg, SOURCE,
            connector.fetch_ids(fresh),
            exclude_categories=exclude_categories,
        )
        outcome.unfetchable = len(getattr(connector, "dropped", []))

    outcome.cursor = new_cursor
    syncstate.set_cursor(conn, SOURCE, new_cursor, result=outcome.summary())
    return outcome


def _full(conn, store, cfg, connector, exclude_categories, *, reason: str) -> Outcome:
    """The date-query path, used when the cursor is too old to be answered."""
    known = {
        row[0]
        for row in conn.execute("SELECT source_uid FROM docs WHERE source = ?", (SOURCE,))
    }
    counts = ingest_stream(
        conn, store, cfg, SOURCE,
        connector.fetch(cfg.gmail.query, skip=known.__contains__),
        exclude_categories=exclude_categories,
    )
    # Read the cursor *after* the listing: anything that arrived during it is
    # already indexed, and starting the next run before that point would only
    # re-examine messages we hold.
    outcome = Outcome(
        mode="fallback", counts=counts, cursor=connector.current_history_id(),
        considered=counts.seen, fallback_reason=reason,
        unfetchable=len(getattr(connector, "dropped", [])),
    )
    syncstate.set_cursor(conn, SOURCE, outcome.cursor, result=outcome.summary())
    return outcome
