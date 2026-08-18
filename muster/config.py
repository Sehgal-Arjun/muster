"""Configuration loading.

Config lives at ``~/.muster/config.toml`` and is entirely optional: every field
has a default, so a fresh machine works with no file at all. ``MUSTER_HOME``
overrides the data directory, which is what the test suite uses to stay out of
the real one.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_HOME = Path("~/.muster")
CONFIG_FILENAME = "config.toml"

# Kept here rather than in the embedding module so that `muster search` can name
# the current model without importing sentence-transformers.
DEFAULT_EMBED_MODEL = "BAAI/bge-small-en-v1.5"


@dataclass(frozen=True)
class GmailConfig:
    credentials_path: Path
    token_path: Path
    query: str = "newer_than:7d"


@dataclass(frozen=True)
class EmbedConfig:
    model_id: str = DEFAULT_EMBED_MODEL
    batch_size: int = 64
    device: str | None = None  # None => autodetect (mps > cuda > cpu)


@dataclass(frozen=True)
class Config:
    home: Path
    db_path: Path
    blob_dir: Path
    gmail: GmailConfig
    embed: EmbedConfig = field(default_factory=EmbedConfig)
    # Your own addresses, so `counterparty` can name the other party rather
    # than you. Empty is fine: outbound mail is then detected by label alone.
    own_addresses: frozenset[str] = frozenset()

    def ensure_dirs(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.blob_dir.mkdir(parents=True, exist_ok=True)


def _expand(base: Path, value: str | os.PathLike[str]) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p)


def muster_home() -> Path:
    return Path(os.environ.get("MUSTER_HOME", DEFAULT_HOME)).expanduser().resolve()


def load_config(home: Path | None = None) -> Config:
    home = (home or muster_home()).expanduser().resolve()

    raw: dict = {}
    config_file = home / CONFIG_FILENAME
    if config_file.exists():
        with config_file.open("rb") as fh:
            raw = tomllib.load(fh)

    storage = raw.get("storage", {})
    gmail = raw.get("gmail", {})
    embed = raw.get("embed", {})

    return Config(
        home=home,
        db_path=_expand(home, storage.get("db_path", "muster.db")),
        blob_dir=_expand(home, storage.get("blob_dir", "blobs")),
        gmail=GmailConfig(
            credentials_path=_expand(home, gmail.get("credentials_path", "credentials.json")),
            token_path=_expand(home, gmail.get("token_path", "gmail_token.json")),
            query=gmail.get("query", "newer_than:7d"),
        ),
        embed=EmbedConfig(
            model_id=embed.get("model_id", DEFAULT_EMBED_MODEL),
            batch_size=int(embed.get("batch_size", 64)),
            device=embed.get("device"),
        ),
        own_addresses=frozenset(
            a.lower() for a in raw.get("identity", {}).get("addresses", [])
        ),
    )
