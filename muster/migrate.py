"""Migration runner.

Numbered ``.sql`` files in ``migrations/``, applied in filename order, each one
recorded in ``schema_migrations``. A migration and its bookkeeping row commit
together, so an interrupted run leaves the database either fully before or
fully after a given migration — never half-applied and unrecorded.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_PACKAGED = Path(__file__).resolve().parent / "migrations"
_REPO_ROOT = Path(__file__).resolve().parent.parent / "migrations"


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


def migrations_dir() -> Path:
    override = os.environ.get("MUSTER_MIGRATIONS")
    if override:
        return Path(override).expanduser().resolve()
    if _PACKAGED.is_dir():
        return _PACKAGED
    if _REPO_ROOT.is_dir():
        return _REPO_ROOT
    raise MigrationError("no migrations directory found")


def discover(directory: Path | None = None) -> list[Migration]:
    directory = directory or migrations_dir()
    files = sorted(p for p in directory.glob("*.sql") if p.is_file())
    return [Migration(version=p.stem, path=p) for p in files]


def _ensure_bookkeeping(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )


def applied_versions(conn: sqlite3.Connection) -> set[str]:
    _ensure_bookkeeping(conn)
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {r[0] for r in rows}


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def migrate(conn: sqlite3.Connection, directory: Path | None = None) -> list[str]:
    """Apply every unapplied migration. Returns the versions applied."""
    done = applied_versions(conn)
    pending = [m for m in discover(directory) if m.version not in done]

    applied: list[str] = []
    for migration in pending:
        now = datetime.now(UTC).isoformat()
        record = (
            "INSERT INTO schema_migrations (version, applied_at) VALUES "
            f"({_quote(migration.version)}, {_quote(now)});"
        )
        # executescript() commits any open transaction before running, so the
        # transaction control has to live inside the script itself.
        script = f"BEGIN;\n{migration.sql}\n{record}\nCOMMIT;"
        try:
            conn.executescript(script)
        except sqlite3.Error as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise MigrationError(f"{migration.version} failed: {exc}") from exc
        applied.append(migration.version)

    return applied
