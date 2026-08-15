from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from muster import db, migrate
from muster.config import Config, load_config


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
