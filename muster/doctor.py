"""Is anything quietly broken?

Almost every failure this index has had was silent. Files were indexed with no
chunks and became unfindable. A hundred thousand chunks sat unembedded because
the job that embeds them asked the wrong question. macOS denied access to a
folder and the walk reported "0 files seen", which reads as "you have no
documents". A sync process wedged at 0% CPU and simply stopped.

None of those raised anything. Each was found by going looking, hours later, and
each was obvious within seconds of asking the right question. This asks all of
them at once.

Checks report rather than repair. A tool that quietly changes an archive while
telling you it is fine is the thing this exists to catch.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from muster.config import Config

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Finding:
    """One question, its answer, and what to do about it."""

    name: str
    status: str
    detail: str
    remedy: str | None = None

    @property
    def bad(self) -> bool:
        return self.status in (WARN, FAIL)


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str, remedy: str | None = None) -> None:
        self.findings.append(Finding(name, status, detail, remedy))

    @property
    def worst(self) -> str:
        if any(f.status == FAIL for f in self.findings):
            return FAIL
        if any(f.status == WARN for f in self.findings):
            return WARN
        return OK

    @property
    def problems(self) -> list[Finding]:
        return [f for f in self.findings if f.bad]


def _scalar(conn: sqlite3.Connection, sql: str, *args):
    return conn.execute(sql, args).fetchone()[0]


def _age(iso: str | None) -> timedelta | None:
    if not iso:
        return None
    try:
        stamp = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return datetime.now(UTC) - stamp


# --- the checks -----------------------------------------------------------


def check_schema(conn: sqlite3.Connection, report: Report) -> None:
    from muster import migrate

    available = {m.version for m in migrate.discover()}
    pending = sorted(available - migrate.applied_versions(conn))
    if pending:
        report.add(
            "schema", FAIL, f"{len(pending)} migration(s) not applied: {', '.join(pending)}",
            "muster init",
        )
    else:
        report.add("schema", OK, "up to date")


def check_unfindable(conn: sqlite3.Connection, report: Report) -> None:
    """Documents with no chunks are in the database and not in the index.

    Chunks are the only thing search reads. A document without any has a row,
    a title, a path — and cannot be found by any query at all.
    """
    # Redacted documents are excluded on purpose. A login code or magic link has
    # its body deliberately left out, so having no chunks is the intended
    # outcome — counting those as breakage is how a health check starts crying
    # wolf and stops being read.
    empty = """
        SELECT count(*) FROM docs d
        WHERE d.origin = 'captured'
          AND json_extract(coalesce(d.meta_json, '{}'), '$.redacted_by') IS NULL
          AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.doc_id = d.id)
    """
    n = _scalar(conn, empty)
    redacted = _scalar(
        conn,
        "SELECT count(*) FROM docs"
        " WHERE json_extract(coalesce(meta_json, '{}'), '$.redacted_by') IS NOT NULL",
    )
    note = f" ({redacted} redacted document(s) excluded, which is intended)" if redacted else ""
    if n:
        report.add(
            "unfindable documents", WARN,
            f"{n} document(s) have no chunks and cannot be found{note}",
            "muster sync files   (it retries documents left with nothing)",
        )
    else:
        report.add("unfindable documents", OK,
                   f"every document is in the index{note}")


def check_embedding(conn: sqlite3.Connection, cfg: Config, report: Report) -> None:
    from muster.embed import count_pending

    pending = count_pending(conn, cfg.embed.model_id)
    total = _scalar(conn, "SELECT count(*) FROM chunks")
    if not total:
        report.add("embedding", WARN, "nothing is chunked yet", "muster sync gmail")
        return
    if pending:
        share = 100 * pending // total
        status = FAIL if share > 50 else WARN
        report.add(
            "embedding", status,
            f"{pending:,} of {total:,} chunks ({share}%) have no vector — "
            "vector and hybrid search cannot see them",
            "muster embed   (or let the scheduled job drain it)",
        )
    else:
        report.add("embedding", OK, f"all {total:,} chunks embedded for {cfg.embed.model_id}")


def check_vector_dimensions(conn: sqlite3.Connection, cfg: Config, report: Report) -> None:
    """A model change with vectors still present is a silent, total failure."""
    row = conn.execute(
        "SELECT DISTINCT length(embedding) FROM vectors WHERE model_id = ? LIMIT 4",
        (cfg.embed.model_id,),
    ).fetchall()
    if len(row) > 1:
        report.add(
            "vector dimensions", FAIL,
            f"{len(row)} different vector sizes stored for {cfg.embed.model_id}",
            "muster embed --rebuild   (the model changed under the stored vectors)",
        )
    elif row:
        report.add("vector dimensions", OK, f"{row[0][0] // 4} dimensions, consistent")
    else:
        report.add("vector dimensions", WARN, f"no vectors for {cfg.embed.model_id}", "muster embed")


def check_fts(conn: sqlite3.Connection, report: Report) -> None:
    """The FTS index is kept in step by triggers; drift means keyword search lies.

    Asked of FTS5 itself. Counting rows cannot answer it: chunks_fts is an
    external-content table, so `count(*)` there reads the content table and is
    equal to it by construction — a comparison that can never fail, which is a
    worse thing to have than no check at all.
    """
    chunks = _scalar(conn, "SELECT count(*) FROM chunks")
    try:
        conn.execute("INSERT INTO chunks_fts (chunks_fts) VALUES ('integrity-check')")
    except sqlite3.DatabaseError as exc:
        report.add(
            "keyword index", FAIL,
            f"FTS5 reports the index is corrupt ({exc}) — keyword search is incomplete",
            "muster reparse   (rebuilds the index from stored originals)",
        )
    else:
        report.add("keyword index", OK, f"{chunks:,} chunks, integrity check passed")


def check_file_roots(cfg: Config, report: Report) -> None:
    """macOS gates Documents and Downloads. Denied looks exactly like empty."""
    denied, missing = [], []
    for root in cfg.files.roots:
        path = Path(root).expanduser()
        if not path.is_dir():
            missing.append(str(path))
            continue
        try:
            os.listdir(path)
        except PermissionError:
            denied.append(str(path))
        except OSError:
            missing.append(str(path))

    if denied:
        report.add(
            "file access", FAIL,
            "macOS denies access to " + ", ".join(denied) + " — nothing there is indexed",
            "System Settings > Privacy & Security > Full Disk Access, "
            "add your terminal and /Applications/Muster.app",
        )
    elif missing:
        report.add("file access", WARN, "no such directory: " + ", ".join(missing),
                   "fix `roots` under [files] in ~/.muster/config.toml")
    else:
        report.add("file access", OK, f"{len(cfg.files.roots)} root(s) readable")


def check_missing_originals(conn: sqlite3.Connection, cfg: Config, report: Report) -> None:
    """Documents whose original has gone, as recorded by a completed walk."""
    marked = _scalar(conn, "SELECT count(*) FROM docs WHERE missing_since IS NOT NULL")
    total = _scalar(conn, "SELECT count(*) FROM docs")

    if marked:
        report.add(
            "missing originals", OK,
            f"{marked} of {total:,} document(s) no longer have their original — "
            "hidden from search, still on record",
            "muster purge --missing   removes them for good",
        )
    else:
        report.add("missing originals", OK, "every document still has its original")


def check_sync_freshness(conn: sqlite3.Connection, report: Report) -> None:
    from muster import syncstate

    state = syncstate.get(conn, "gmail")
    if state is None or not state.last_sync:
        report.add("mail sync", WARN, "has never run", "muster sync --incremental")
        return
    age = _age(state.last_sync)
    if age is None:
        report.add("mail sync", WARN, f"unreadable timestamp: {state.last_sync}")
    elif age > timedelta(days=1):
        report.add(
            "mail sync", WARN,
            f"last ran {age.days} day(s) ago — {state.last_result or 'no result recorded'}",
            "muster schedule install   (or run muster sync --incremental)",
        )
    else:
        hours = int(age.total_seconds() // 3600)
        report.add("mail sync", OK, f"ran {hours}h ago — {state.last_result or 'no result'}")


def check_schedule(report: Report) -> None:
    from muster import scheduling

    if not scheduling.installed():
        report.add("scheduled sync", WARN, "not installed; nothing syncs on its own",
                   "muster schedule install")
        return
    result = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{scheduling.LABEL}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        report.add("scheduled sync", WARN, "the agent file exists but launchd has not loaded it",
                   "muster schedule install")
    else:
        report.add("scheduled sync", OK, f"loaded, every {scheduling.INTERVAL_SECONDS // 60} minutes")


def check_paused(cfg: Config, report: Report) -> None:
    from muster import pausing

    if pausing.is_paused(cfg.home):
        report.add(
            "paused", WARN,
            f"background work is paused (since {pausing.paused_since(cfg.home)}); "
            "nothing syncs or embeds",
            "muster resume",
        )
    else:
        report.add("paused", OK, "background work is allowed to run")


def check_stale_lock(cfg: Config, report: Report) -> None:
    """A lock held by a process that no longer exists blocks every future run."""
    path = cfg.lock_path
    if not path.exists():
        report.add("sync lock", OK, "free")
        return
    try:
        pid = int(path.read_text().strip() or 0)
    except (OSError, ValueError):
        report.add("sync lock", OK, "present but unattributed")
        return
    if not pid:
        report.add("sync lock", OK, "free")
        return
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        # flock is released by the kernel on exit, so this is informational —
        # the file outliving its holder does not actually block anything.
        report.add("sync lock", OK, f"left by pid {pid}, which has exited; the lock is not held")
    except PermissionError:
        report.add("sync lock", OK, f"held by pid {pid}")
    else:
        report.add("sync lock", OK, f"held by pid {pid}, which is running")


def check_server(report: Report) -> None:
    import urllib.error
    import urllib.request

    from muster.api import DEFAULT_HOST, DEFAULT_PORT

    url = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/status"
    try:
        with urllib.request.urlopen(url, timeout=5):
            report.add("server", OK, f"answering on {DEFAULT_HOST}:{DEFAULT_PORT}")
    except (urllib.error.URLError, OSError):
        report.add("server", WARN, "not answering; the panel cannot search",
                   "muster serve   (or packaging/install.sh to run it at login)")


def check_extraction_version(conn: sqlite3.Connection, cfg: Config, report: Report) -> None:
    """Documents read by an older extractor than the one in force.

    Bumping the extractor version is how its output gets corrected, and it makes
    every earlier reading invisible — deliberately. What is not deliberate is
    forgetting to re-run it, at which point a process quietly reports nothing
    outstanding because nothing has been read at the current version.
    """
    from muster import extraction

    current = extraction.EXTRACTOR_VERSION
    stale = _scalar(
        conn,
        "SELECT count(DISTINCT doc_id) FROM doc_extractions WHERE extractor_version < ?",
        current,
    )
    fresh = _scalar(
        conn,
        "SELECT count(DISTINCT doc_id) FROM doc_extractions WHERE extractor_version = ?",
        current,
    )
    if not stale and not fresh:
        report.add("extraction", OK, "nothing extracted yet", None)
    elif stale and not fresh:
        report.add(
            "extraction", WARN,
            f"{stale} document(s) read at an older version; none at v{current} — "
            "every process will report as unexamined",
            "muster process extract <slug>",
        )
    elif stale:
        report.add(
            "extraction", WARN,
            f"{fresh} document(s) at v{current}, {stale} still only at an older version",
            "muster process extract <slug>",
        )
    else:
        report.add("extraction", OK, f"{fresh} document(s) read at v{current}")


def check_judgement_caches(conn: sqlite3.Connection, cfg: Config, report: Report) -> None:
    """The two caches that keep open items a query rather than a generation.

    Both are answers to questions asked of a model, and both are worthless if
    they were recorded against a model that is no longer in use — the numbers
    stay correct, but every screen pays the full cost again.
    """
    from muster import obligations

    model = cfg.extract.model_id
    judged = _scalar(conn, "SELECT count(*) FROM satisfaction WHERE model_id = ?", model)
    other = _scalar(conn, "SELECT count(*) FROM satisfaction WHERE model_id != ?", model)
    obliged = _scalar(
        conn,
        "SELECT count(*) FROM obligations WHERE model_id = ? AND version = ?",
        model, obligations.OBLIGATION_VERSION,
    )
    detail = f"{judged} match(es) and {obliged} obligation(s) cached for {model}"
    if other:
        report.add(
            "judgement cache", WARN,
            f"{detail}; {other} recorded against a different model and unusable",
            "harmless, but they will never be read again",
        )
    else:
        report.add("judgement cache", OK, detail)


def run(cfg: Config, conn: sqlite3.Connection) -> Report:
    """Every check, in the order a reader wants them."""
    report = Report()
    check_schema(conn, report)
    check_fts(conn, report)
    check_unfindable(conn, report)
    check_embedding(conn, cfg, report)
    check_vector_dimensions(conn, cfg, report)
    check_file_roots(cfg, report)
    check_missing_originals(conn, cfg, report)
    check_sync_freshness(conn, report)
    check_schedule(report)
    check_paused(cfg, report)
    check_stale_lock(cfg, report)
    check_extraction_version(conn, cfg, report)
    check_judgement_caches(conn, cfg, report)
    check_server(report)
    return report
