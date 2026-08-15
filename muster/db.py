"""SQLite connection handling.

One database file, no ORM. Every connection sets the same pragmas: foreign keys
are off by default in SQLite and the schema leans on ON DELETE CASCADE, so
forgetting them would silently orphan rows.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def connect(db_path: Path, *, load_vec: bool = False) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")

    if load_vec:
        load_sqlite_vec(conn)
    return conn


def load_sqlite_vec(conn: sqlite3.Connection) -> None:
    """Load the sqlite-vec extension into an open connection."""
    import sqlite_vec

    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit transaction. Connections run in autocommit, so this is opt-in."""
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
