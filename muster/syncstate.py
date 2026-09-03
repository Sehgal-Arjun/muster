"""Where each source got to, so the next run can resume.

Small enough to be obvious, separate because both the CLI and the API read it
and neither should be reaching into the other's tables.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class SyncState:
    source: str
    account: str
    cursor: str | None
    last_sync: str | None
    last_result: str | None


def get(conn: sqlite3.Connection, source: str, account: str = "") -> SyncState | None:
    row = conn.execute(
        "SELECT source, account, cursor, last_sync, last_result FROM sync_state"
        " WHERE source = ? AND account = ?",
        (source, account),
    ).fetchone()
    return SyncState(*row) if row else None


def set_cursor(
    conn: sqlite3.Connection,
    source: str,
    cursor: str | None,
    *,
    account: str = "",
    result: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO sync_state (source, account, cursor, last_sync, last_result)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (source, account) DO UPDATE SET
            cursor = excluded.cursor,
            last_sync = excluded.last_sync,
            last_result = excluded.last_result
        """,
        (source, account, cursor, datetime.now(UTC).isoformat(timespec="seconds"), result),
    )
    conn.commit()


def highest_indexed_history_id(conn: sqlite3.Connection) -> str | None:
    """The newest Gmail historyId already in the index.

    Every message was stored with the historyId it had when fetched, so a
    mailbox synced before this table existed can still resume incrementally
    instead of being listed from scratch. Compared as an integer because that is
    what Gmail's ordering is; returned as text because that is what the API wants.
    """
    row = conn.execute(
        "SELECT max(CAST(json_extract(meta_json, '$.history_id') AS INTEGER))"
        " FROM docs WHERE source = 'gmail' AND meta_json IS NOT NULL"
    ).fetchone()
    return str(row[0]) if row and row[0] else None
