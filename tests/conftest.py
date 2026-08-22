from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from muster import db, migrate
from muster.config import Config, load_config


# The real index is a personal mailbox that may be mid-sync. Nothing in the
# suite may reach it, including code paths that call load_config() indirectly
# — vector_search does, several layers down from a test that never mentions it.
REAL_HOME = Path("~/.muster").expanduser().resolve()


@pytest.fixture(autouse=True, scope="session")
def _never_touch_the_real_index(tmp_path_factory) -> Path:
    """Point MUSTER_HOME away from ~/.muster for the whole session.

    Belt and braces with the per-test `home` fixture: this one covers tests
    that never ask for isolation because they do not look like they need it.
    """
    fallback = tmp_path_factory.mktemp("session-muster-home")
    os.environ["MUSTER_HOME"] = str(fallback)
    return fallback


@pytest.fixture(autouse=True)
def _assert_real_index_untouched(request):
    """Fail loudly if a test resolved config to the real home."""
    yield
    from muster.config import muster_home

    if muster_home() == REAL_HOME:
        pytest.fail(
            f"{request.node.nodeid} left MUSTER_HOME pointing at {REAL_HOME}; "
            "tests must never write to the live index"
        )


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated MUSTER_HOME so tests never touch ~/.muster."""
    h = tmp_path / "muster-home"
    h.mkdir()
    monkeypatch.setenv("MUSTER_HOME", str(h))
    return h


@pytest.fixture
def cfg(home: Path) -> Config:
    c = load_config()
    c.ensure_dirs()
    return c


@pytest.fixture
def conn(cfg: Config) -> sqlite3.Connection:
    c = db.connect(cfg.db_path)
    migrate.migrate(c)
    yield c
    c.close()
