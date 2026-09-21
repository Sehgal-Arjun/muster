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
#
# bge-m3 over bge-small-en-v1.5, decided by measurement on a real 23-query set
# plus a 30-pair EN/DE probe:
#
#   model                  hybrid r@10   hybrid MRR   EN->DE acc@1
#   bge-small-en-v1.5           0.5652       0.4543          0.233
#   multilingual-e5-base        0.6087       0.4686          0.900
#   bge-m3                      0.6522       0.4700          1.000
#
# The English gain alone would not obviously be worth 14x the embedding time.
# The cross-lingual number is what settles it: bge-small cannot see German at
# all, and German correspondence is coming. Query-time cost is not a factor —
# m3 loads in 2.6s warm and encodes a query faster than bge-small does.
DEFAULT_EMBED_MODEL = "BAAI/bge-m3"


@dataclass(frozen=True)
class GmailConfig:
    credentials_path: Path
    token_path: Path
    query: str = "newer_than:7d"


@dataclass(frozen=True)
class FilesConfig:
    """Which parts of the disk are indexed.

    Documents and Downloads only, by default and deliberately. A home directory
    holds far more that is not a document than is, and the cost of indexing the
    rest is paid on every walk forever.
    """

    roots: tuple[str, ...] = ("~/Documents", "~/Downloads")
    max_bytes: int = 100 * 1024 * 1024
    # Where to look for *directories* to offer as jump targets. Wider than the
    # roots above on purpose: the folder you want to open is often one nothing
    # inside is indexed from — a code checkout, say — and recording a name
    # costs nothing next to reading a file.
    folder_roots: tuple[str, ...] = ("~",)
    folder_depth: int = 5
    # Added to the connector's built-in list rather than replacing it: the
    # defaults cover what is junk on every Mac, and this covers what is junk on
    # this one. Matched by directory name at any depth.
    excluded_dirs: tuple[str, ...] = ()


@dataclass(frozen=True)
class EmbedConfig:
    model_id: str = DEFAULT_EMBED_MODEL
    batch_size: int = 64
    device: str | None = None  # None => autodetect (mps > cuda > cpu)
    # Prepend the document title to its first chunk before encoding. Affects
    # the vector arm only — the stored chunk text, and therefore BM25, is
    # untouched, so the two can be measured independently.
    prepend_title: bool = False


@dataclass(frozen=True)
class SearchConfig:
    """Ranking knobs.

    The score floors are per-mode because the scales are unrelated: BM25 is
    unbounded, cosine similarity sits in [-1, 1], and RRF compresses everything
    into a narrow band just above 1/(k+1). A single number could not mean the
    same thing in all three.
    """

    title_weight: float = 1.0
    text_weight: float = 1.0
    # Sender display name and addresses. Some correspondents appear in the
    # archive only here — the From line is the sole copy of their name.
    party_weight: float = 1.0
    # Applied to each arm's native score. Below this, a result is noise, and
    # ten confident-looking wrong answers are worse than none.
    # BM25 per discriminating clause at a 1000-chunk index, scaled to the real
    # index and query at search time. Measured on a 1447-chunk index: queries
    # with a real answer score 4.9-13.8, queries without one 1.8-3.7.
    min_score_keyword: float = 4.0
    # CALIBRATED FOR BAAI/bge-m3. Cosine distributions are a property of the
    # model, not of the index: this number is meaningless under a different
    # one and must be re-swept whenever `embed.model_id` changes. It was left
    # at a bge-small value once already, which put it *below* the level bge-m3
    # assigns to gibberish, so the arm rejected nothing and hybrid served ten
    # confident wrong answers for "zzzqqxx nonexistent".
    #
    # Measured on 8038 documents, 31 real queries and 8 no-answer probes:
    #
    #   gibberish tops out at 0.511 (digit runs excepted, see below)
    #   real queries: min 0.428, median 0.605, max 0.767
    #   hybrid recall holds at 0.7419 up to 0.55 and falls to 0.6452 at 0.58
    #
    # 0.55 sits in the gap, takes the no-answer probe from 2/8 silent to 7/8,
    # and improves hybrid MRR (0.5148 -> 0.5239). The eighth probe is
    # "1234567890 0987654321" at 0.630: digit strings genuinely sit near
    # reference numbers in embedding space, and a floor above that would also
    # be above the median real query. The keyword arm is what refuses it.
    min_score_vector: float = 0.55
    min_score_hybrid: float = 0.0  # arms are already floored before fusion
    # Drop results below this fraction of the best hit. Scale-free, and what
    # removes the flat tail that RRF in particular produces.
    min_score_ratio: float = 0.25
    # Unequal so RRF can never tie two arms at rank 1. Not a relevance weight:
    # any value below 1.0 scores identically on the real query set, so this
    # only decides ties, and it decides them toward exact lexical evidence.
    rrf_keyword_weight: float = 1.0
    rrf_vector_weight: float = 0.9
    # Collapse results that are byte-identical. Four copies of one PDF held
    # ranks 1-4 of a ten-result list before this.
    dedupe_by_content: bool = True


# Automated senders whose volume drowns real correspondence. Gmail files GitHub
# under `updates`, alongside receipts and shipping notices, so the category axis
# cannot separate them — the sender can.
DEFAULT_EXCLUDED_SENDERS: tuple[str, ...] = ()


@dataclass(frozen=True)
class RedactionRule:
    """Both patterns must match. Either alone is far too broad."""

    name: str
    sender: str
    subject: str


# One-time codes and magic links: the highest-risk content in a mail archive
# and the least useful to search. Overridable in full from config.
DEFAULT_REDACTION_RULES = (
    RedactionRule(
        name="one-time-credentials",
        sender=r"(?i)(?:^|[.<@_-])(?:no-?reply|do-?not-?reply|verify|accounts?|security)\b",
        subject=(
            r"(?i)\b(?:code|verification|verify|sign[- ]?in|log[- ]?in|login|"
            r"secure link|magic link|one[- ]?time|otp|passcode|password|"
            r"authenticate|confirm your (?:email|account)|2fa)\b"
        ),
    ),
)


@dataclass(frozen=True)
class RedactionConfig:
    enabled: bool = True
    mode: str = "redact"  # 'redact' keeps the doc and blob; 'drop' indexes nothing
    rules: tuple[RedactionRule, ...] = DEFAULT_REDACTION_RULES


@dataclass(frozen=True)
class Config:
    home: Path
    db_path: Path
    blob_dir: Path
    gmail: GmailConfig
    embed: EmbedConfig = field(default_factory=EmbedConfig)
    files: FilesConfig = field(default_factory=FilesConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    redaction: RedactionConfig = field(default_factory=RedactionConfig)
    # Addresses ("notifications@github.com") or bare domains ("substack.com",
    # matching subdomains too). Empty by default: this is a personal list.
    excluded_senders: tuple[str, ...] = DEFAULT_EXCLUDED_SENDERS
    # Your own addresses, so `counterparty` can name the other party rather
    # than you. Empty is fine: outbound mail is then detected by label alone.
    own_addresses: frozenset[str] = frozenset()

    @property
    def lock_path(self) -> Path:
        """The sync lock, one per home — two mailboxes still serialise."""
        return self.home / "sync.lock"

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
    files = raw.get("files", {})
    search = raw.get("search", {})
    redaction = raw.get("redaction", {})

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
            prepend_title=bool(embed.get("prepend_title", False)),
        ),
        files=FilesConfig(
            roots=tuple(str(r) for r in files.get("roots", FilesConfig.roots)),
            max_bytes=int(files.get("max_bytes", FilesConfig.max_bytes)),
            excluded_dirs=tuple(str(d) for d in files.get("excluded_dirs", ())),
            folder_roots=tuple(str(r) for r in files.get("folder_roots", FilesConfig.folder_roots)),
            folder_depth=int(files.get("folder_depth", FilesConfig.folder_depth)),
        ),
        search=SearchConfig(
            title_weight=float(search.get("title_weight", 1.0)),
            party_weight=float(search.get("party_weight", 1.0)),
            text_weight=float(search.get("text_weight", 1.0)),
            min_score_keyword=float(search.get("min_score_keyword", 4.0)),
            min_score_vector=float(search.get("min_score_vector", 0.45)),
            min_score_hybrid=float(search.get("min_score_hybrid", 0.0)),
            min_score_ratio=float(search.get("min_score_ratio", 0.25)),
            rrf_keyword_weight=float(search.get("rrf_keyword_weight", 1.0)),
            rrf_vector_weight=float(search.get("rrf_vector_weight", 0.9)),
            dedupe_by_content=bool(search.get("dedupe_by_content", True)),
        ),
        redaction=_redaction_config(redaction),
        excluded_senders=tuple(
            str(a) for a in raw.get("senders", {}).get("excluded", DEFAULT_EXCLUDED_SENDERS)
        ),
        own_addresses=frozenset(
            a.lower() for a in raw.get("identity", {}).get("addresses", [])
        ),
    )


def _redaction_config(raw: dict) -> RedactionConfig:
    rules = raw.get("rules")
    if rules is None:
        parsed = DEFAULT_REDACTION_RULES
    else:
        parsed = tuple(
            RedactionRule(
                name=str(rule.get("name", f"rule-{i}")),
                sender=str(rule.get("sender", "")),
                subject=str(rule.get("subject", "")),
            )
            for i, rule in enumerate(rules)
        )
    mode = str(raw.get("mode", "redact")).lower()
    if mode not in ("redact", "drop"):
        raise ValueError(f"redaction.mode must be 'redact' or 'drop', not {mode!r}")
    return RedactionConfig(
        enabled=bool(raw.get("enabled", True)), mode=mode, rules=parsed
    )
