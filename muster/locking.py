"""A single-holder lock, so two syncs cannot run at once.

The scheduled job fires every fifteen minutes whether or not anything else is
running, and the panel asks for a refresh whenever it opens. Both write to the
same database, and a backfill can run for hours. Without this the fifteen-minute
timer would stack runs on top of each other until the machine gave up.

``flock`` rather than a pid file: the kernel releases it when the process dies,
so a crashed sync does not leave a lock nobody can clear.
"""

from __future__ import annotations

import errno
import fcntl
import os
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path


class LockHeld(RuntimeError):
    """Somebody else is syncing. Not an error — a reason to do nothing."""


@contextmanager
def sync_lock(path: Path, *, wait: bool = False) -> Iterator[None]:
    """Hold the sync lock for the duration of the block.

    With ``wait=False`` — what the scheduled job and the panel's refresh both
    use — a lock already held raises :class:`LockHeld` immediately rather than
    queueing. A refresh that has to wait for a two-hour backfill is not a
    refresh.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise LockHeld(f"another sync holds {path}") from exc
            raise
        # Written for a human reading the file, not for the locking itself —
        # flock is what actually excludes.
        os.ftruncate(handle, 0)
        os.write(handle, f"{os.getpid()}\n".encode())
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)
