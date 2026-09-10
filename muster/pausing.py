"""A pause switch for everything that runs in the background.

Embedding a large backlog is hours of GPU, and the machine it runs on is the
one the user is trying to work on. Interrupting it was always safe — vectors are
written per batch and anything unembedded is simply picked up next time — but
"safe to kill" is not the same as "easy to stop", and a job you cannot stop is a
job you cannot afford to start.

A file rather than a signal, because the thing being paused is not always the
thing in front of you: a scheduled run fifteen minutes from now has to see it
too, and so does an embed already in flight.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path


def flag_path(home: Path) -> Path:
    return home / "paused"


def is_paused(home: Path) -> bool:
    return flag_path(home).exists()


def paused_since(home: Path) -> str | None:
    try:
        return flag_path(home).read_text().strip() or None
    except OSError:
        return None


def pause(home: Path) -> None:
    """Stop background work. Anything already running stops at a safe point."""
    home.mkdir(parents=True, exist_ok=True)
    flag_path(home).write_text(datetime.now(UTC).isoformat(timespec="seconds") + "\n")


def resume(home: Path) -> bool:
    """Allow background work again. True if it had been paused."""
    path = flag_path(home)
    if path.exists():
        path.unlink()
        return True
    return False
