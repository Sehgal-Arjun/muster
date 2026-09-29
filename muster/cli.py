"""The `muster` command line."""

from __future__ import annotations

import contextlib
import inspect
import logging
from pathlib import Path
from typing import Annotated

import typer

from muster import db, migrate
from muster.blobs import BlobStore
from muster.config import load_config

app = typer.Typer(
    name="muster",
    help="Local-first personal knowledge index.",
    no_args_is_help=True,
    add_completion=False,
)

# invoke_without_command so `muster sync --incremental` is a command in its own
# right: the routine case deserves the short spelling, and naming a provider to
# ask "anything new?" is ceremony.
# How many chunks one scheduled pass will embed before handing the machine
# back. Not tuned: large enough that a backlog drains in hours rather than
# days, small enough that no single run holds the lock for long.
EMBED_PER_RUN = 20_000

sync_app = typer.Typer(name="sync", help="Pull messages from a source.", invoke_without_command=True)
app.add_typer(sync_app)

schedule_app = typer.Typer(
    name="schedule", help="Run the sync on a timer.", no_args_is_help=True
)
app.add_typer(schedule_app)


@sync_app.callback()
def _sync_root(
    ctx: typer.Context,
    incremental: Annotated[
        bool,
        typer.Option("--incremental", "-i", help="Only what arrived since the last sync."),
    ] = False,
) -> None:
    if ctx.invoked_subcommand is not None:
        if incremental:
            typer.echo("--incremental applies to `muster sync`, not a subcommand", err=True)
            raise typer.Exit(1)
        return
    if not incremental:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)
    _run_incremental(quiet=False)


def _run_incremental(*, quiet: bool, hold_lock: bool = True) -> int:
    """One incremental Gmail pass. Returns the number of new documents.

    ``hold_lock=False`` for callers that already hold it. flock is per
    descriptor, so acquiring it twice in one process deadlocks against itself —
    and with LOCK_NB that surfaces as "another sync is running", which would be
    a confusing way to describe a process blocking on its own lock.
    """
    from muster import incremental
    from muster.connectors.gmail import GmailAuthError, GmailConnector
    from muster.locking import LockHeld, sync_lock

    cfg = load_config()
    cfg.ensure_dirs()
    _require_db(cfg)

    conn = db.connect(cfg.db_path)
    try:
        with sync_lock(cfg.lock_path) if hold_lock else contextlib.nullcontext():
            connector = GmailConnector(cfg.gmail.credentials_path, cfg.gmail.token_path)
            outcome = incremental.sync(conn, cfg, connector)
    except LockHeld:
        typer.echo("another sync is already running; nothing to do")
        return 0
    except GmailAuthError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        conn.close()

    if not quiet:
        typer.echo(outcome.summary())
    return outcome.counts.created


@schedule_app.command("install")
def schedule_install() -> None:
    """Install the launchd agent that syncs every 15 minutes."""
    from muster import scheduling

    cfg = load_config()
    try:
        path = scheduling.install(roots=cfg.files.roots)
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        f"installed {path}\n"
        f"  every {scheduling.INTERVAL_SECONDS // 60} minutes, at background priority\n"
        f"  skipped on battery below {scheduling.BATTERY_FLOOR_PERCENT}%\n"
        f"  logs {scheduling.log_dir() / 'sync.log'}\n"
        "  it will not wake the machine; it runs on the next wake instead\n"
        f"  also runs when {', '.join(cfg.files.roots)} change, so a deleted "
        "file is noticed promptly"
    )


@schedule_app.command("uninstall")
def schedule_uninstall() -> None:
    """Remove the scheduled sync."""
    from muster import scheduling

    if scheduling.uninstall():
        typer.echo("removed the scheduled sync")
    else:
        typer.echo("no scheduled sync was installed")


@schedule_app.command("run")
def schedule_run() -> None:
    """One scheduled pass. This is what launchd calls; it is safe to run by hand."""
    from muster import scheduling
    from muster.locking import LockHeld, sync_lock

    from muster import pausing

    cfg = load_config()
    if pausing.is_paused(cfg.home):
        typer.echo(f"skipped: paused since {pausing.paused_since(cfg.home)}")
        return

    power = scheduling.power()
    if power.too_low:
        typer.echo(f"skipped: on battery at {power.percent}%")
        return

    # Held for the whole pass, not probed and released. A first walk of a large
    # Documents folder runs far longer than the fifteen-minute timer, so without
    # this the next run starts on top of the last one — both walking the same
    # tree and writing the same rows.
    try:
        with sync_lock(cfg.lock_path):
            created = _run_incremental(quiet=False, hold_lock=False)
            created += _run_file_pass()

            # Asked of the index, not of this run. "Did this pass create
            # anything" is a different question from "is anything unembedded",
            # and using the first left a backlog of a hundred thousand chunks
            # that no scheduled run would ever have picked up — every one of
            # them created by a sync run from the command line, which does not
            # embed at all.
            #
            # Still cheap when there is nothing to do: one COUNT, and the model
            # is never loaded unless it returns something.
            from muster.embed import count_pending

            conn = db.connect(cfg.db_path)
            try:
                pending = count_pending(conn, cfg.embed.model_id)
            finally:
                conn.close()

            if pending:
                # Bounded per run. A first pass over a large index has hundreds
                # of thousands of chunks waiting, and embedding all of them in
                # one go would hold the sync lock for hours — every fifteen
                # minutes, forever, is a better shape for the same work.
                batch = min(pending, EMBED_PER_RUN)
                typer.echo(f"embedding {batch} of {pending} chunks awaiting vectors")
                _embed_pending(cfg, limit=batch)
            else:
                typer.echo("no chunks awaiting vectors")
    except LockHeld:
        # Not a failure. A backfill that runs for hours would otherwise have a
        # new run stacked on it every fifteen minutes.
        typer.echo("skipped: another sync holds the lock")
        return


def _run_file_pass() -> int:
    """The incremental filesystem pass, for the scheduled job.

    Unchanged files cost a stat each and nothing else, so walking Documents and
    Downloads every fifteen minutes is cheap enough to be unremarkable.
    """
    from muster.connectors.files import FileConnector
    from muster.fileingest import ingest_file

    cfg = load_config()
    if not cfg.files.roots:
        return 0

    connector = FileConnector(
        cfg.files.roots,
        max_bytes=cfg.files.max_bytes,
        extra_excluded_dirs=cfg.files.excluded_dirs,
    )
    conn = db.connect(cfg.db_path)
    created = 0
    try:
        for record in connector.walk():
            try:
                result = ingest_file(conn, record)
            except Exception:  # noqa: BLE001 — one file must not end the pass
                logging.getLogger("muster.sync").exception("failed to index %s", record.path)
                continue
            if result.created or result.updated:
                created += 1
    finally:
        conn.close()
    typer.echo(f"files: {created} new or changed of {connector.stats.seen} seen")
    return created


def _embed_folders(conn, cfg) -> int:
    """Give folder names vectors, so "sample test" can find "Sample Exams".

    Done here rather than during the walk because this is where the model is
    already loaded. Names are short and deduplicated by text, so a rebuilt map
    re-embeds only what actually changed — usually nothing.
    """
    from muster.embed import encode_documents, load_model, pack
    from muster.folders import pending_vectors, store_vectors

    pending = pending_vectors(conn, cfg.embed.model_id)
    if not pending:
        return 0
    model = load_model(cfg.embed.model_id, cfg.embed.device)
    vectors = encode_documents(
        model, [text for _h, text in pending],
        batch_size=cfg.embed.batch_size, model_id=cfg.embed.model_id,
    )
    written = store_vectors(
        conn, cfg.embed.model_id,
        [(h, pack(v)) for (h, _t), v in zip(pending, vectors, strict=True)],
    )
    typer.echo(f"embedded {written} folder name(s)")
    return written


def _embed_pending(cfg, limit: int | None = None) -> None:
    """Embed whatever is waiting.

    Calls the library rather than the `embed` command: invoking a Typer command
    as a function hands it the OptionInfo defaults instead of their values.
    """
    from muster import pausing
    from muster.embed import embed_pending

    conn = db.connect(cfg.db_path)
    try:
        report = embed_pending(
            conn,
            should_stop=lambda: pausing.is_paused(cfg.home),
            model_id=cfg.embed.model_id,
            device=cfg.embed.device,
            batch_size=cfg.embed.batch_size,
            prepend_title=cfg.embed.prepend_title,
            limit=limit,
        )
        if report.stopped:
            typer.echo(f"paused after {report.chunks} chunks; the rest is still waiting")
        else:
            typer.echo(f"embedded {report.chunks} chunks")
        _embed_folders(conn, cfg)
    finally:
        conn.close()

process_app = typer.Typer(
    name="process", help="Processes: the thing a pile of documents is about.",
    no_args_is_help=True,
)
app.add_typer(process_app)


def _open_db():
    cfg = load_config()
    _require_db(cfg)
    return cfg, db.connect(cfg.db_path, load_vec=True)


@process_app.command("new")
def process_new(
    name: Annotated[str, typer.Argument(help="What the process is called.")],
    accept_all: Annotated[
        bool,
        typer.Option("--accept-all", help="Take every proposal without asking."),
    ] = False,
    top: Annotated[
        int, typer.Option("--top", help="Accept the N best proposals without asking.")
    ] = 0,
) -> None:
    """Create a process and propose anchors for it."""
    from muster import processes

    cfg, conn = _open_db()
    try:
        slug = processes.slugify(name)
        if processes.get(conn, slug):
            typer.echo(f"a process named {slug!r} already exists", err=True)
            raise typer.Exit(1)

        proposals = processes.propose(conn, name, own_addresses=cfg.own_addresses)
        if not proposals:
            typer.echo("no recurring identifiers found; creating with no anchors")

        process = processes.create(conn, name, slug)
        typer.echo(f"created {process.slug}")

        accepted = 0
        for index, proposal in enumerate(proposals):
            take = accept_all or (top and index < top)
            if not take and top == 0 and not accept_all:
                take = typer.confirm(f"  {proposal.line()}\n  anchor on this?", default=False)
            if take:
                if processes.add_anchor(conn, process.id, proposal.kind, proposal.value):
                    accepted += 1
                    typer.echo(f"  + {proposal.kind} {proposal.value}")

        typer.echo(f"\n{accepted} anchor(s) of {len(proposals)} proposed")
        if accepted == 0:
            typer.echo("  anchors are sufficient, not necessary — gather still works"
                       " from the seed search alone, just less completely")
    finally:
        conn.close()


@process_app.command("list")
def process_list() -> None:
    """Every process, with how many anchors each has."""
    from muster import processes

    _cfg, conn = _open_db()
    try:
        found = processes.all_processes(conn)
        if not found:
            typer.echo("no processes yet — muster process new \"...\"")
            return
        for process in found:
            kinds = {}
            for anchor in process.anchors:
                kinds[anchor.kind] = kinds.get(anchor.kind, 0) + 1
            summary = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items())) or "no anchors"
            typer.echo(f"  {process.slug:34} {process.name[:32]:34} {summary}")
    finally:
        conn.close()


@process_app.command("show")
def process_show(slug: Annotated[str, typer.Argument(help="Process slug.")]) -> None:
    """One process and its anchors."""
    from muster import processes

    _cfg, conn = _open_db()
    try:
        process = processes.get(conn, slug)
        if process is None:
            typer.echo(f"no process {slug!r}", err=True)
            raise typer.Exit(1)
        typer.echo(f"{process.name}  ({process.slug})")
        typer.echo(f"  created {process.created_at}   updated {process.updated_at}")
        if not process.anchors:
            typer.echo("  no anchors")
        for anchor in process.anchors:
            typer.echo(f"  {anchor.kind:12} {anchor.value}")
    finally:
        conn.close()


@process_app.command("anchor")
def process_anchor(
    action: Annotated[str, typer.Argument(help="add or rm")],
    slug: Annotated[str, typer.Argument(help="Process slug.")],
    kind: Annotated[str, typer.Argument(help="domain|address|thread_uid|ref_pattern|folder|doc_id")],
    value: Annotated[str, typer.Argument(help="The identifier.")],
) -> None:
    """Add or remove one anchor."""
    from muster import processes

    _cfg, conn = _open_db()
    try:
        process = processes.get(conn, slug)
        if process is None:
            typer.echo(f"no process {slug!r}", err=True)
            raise typer.Exit(1)
        if action == "add":
            new = processes.add_anchor(conn, process.id, kind, value)
            typer.echo("added" if new else "already anchored")
        elif action == "rm":
            gone = processes.remove_anchor(conn, process.id, kind, value)
            typer.echo("removed" if gone else "no such anchor")
        else:
            typer.echo("action must be add or rm", err=True)
            raise typer.Exit(1)
    finally:
        conn.close()


@process_app.command("extract")
def process_extract(
    slug: Annotated[str, typer.Argument(help="Process slug.")],
    model: Annotated[str | None, typer.Option("--model", help="Ollama model id.")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Stop after N documents.")] = 0,
) -> None:
    """Extract structured facts from each document in a process. Resumable."""
    from muster import extraction, gather as gather_mod, processes
    from muster.pausing import is_paused

    cfg, conn = _open_db()
    try:
        process = processes.get(conn, slug)
        if process is None:
            typer.echo(f"no process {slug!r}", err=True)
            raise typer.Exit(1)

        result = gather_mod.gather(conn, process)
        doc_ids = result.doc_ids[: limit or None]
        todo = extraction.pending(conn, doc_ids)
        typer.echo(
            f"{result.size} documents gathered; {len(doc_ids) - len(todo)} already"
            f" extracted, {len(todo)} to do"
        )
        if not todo:
            return

        owner = ", ".join(sorted(processes.own_addresses_from_archive(conn))) or "the recipient"

        def report(done: int, total: int, doc_id: int) -> None:
            typer.echo(f"\r  {done}/{total}  doc {doc_id}   ", nl=False)

        extracted, failed = extraction.extract_set(
            conn, doc_ids, model=model or cfg.extract.model_id,
            owner=owner, progress=report,
            should_stop=lambda: is_paused(cfg.home),
        )
        typer.echo(f"\n{extracted} extracted, {failed} failed")
        if failed:
            typer.echo(
                f"  {failed} document(s) could not be read and are recorded as"
                " such rather than dropped"
            )
    finally:
        conn.close()


@process_app.command("suggest")
def process_suggest() -> None:
    """Correspondents carrying real traffic that no process claims. Proposals only."""
    from muster import processes

    _cfg, conn = _open_db()
    try:
        found = processes.unclaimed_clusters(conn)
        if not found:
            typer.echo("nothing unclaimed worth suggesting")
            return
        typer.echo("no process claims these; muster process new \"...\" then anchor on them:")
        for proposal in found:
            typer.echo(f"  {proposal.line()}")
    finally:
        conn.close()


corpus_app = typer.Typer(
    name="corpus", help="Freeze and inspect eval corpora.", no_args_is_help=True
)
app.add_typer(corpus_app)


@app.callback()
def _root(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Log at DEBUG.")] = False,
) -> None:
    from muster.logging_setup import configure

    configure(verbose)


@app.command()
def init() -> None:
    """Create the database and apply any pending migrations."""
    cfg = load_config()
    cfg.ensure_dirs()

    conn = db.connect(cfg.db_path)
    try:
        applied = migrate.migrate(conn)
    finally:
        conn.close()

    typer.echo(f"home    {cfg.home}")
    typer.echo(f"db      {cfg.db_path}")
    typer.echo(f"blobs   {cfg.blob_dir}")
    if applied:
        for version in applied:
            typer.echo(f"applied {version}")
    else:
        typer.echo("applied nothing (schema up to date)")


@app.command()
def status(
    months_shown: Annotated[
        int, typer.Option("--months", help="How many months of history to break down.")
    ] = 18,
    senders: Annotated[
        bool, typer.Option("--senders", help="Break down by sender instead of by month.")
    ] = False,
    top: Annotated[int, typer.Option("--top", help="How many senders to list.")] = 30,
) -> None:
    """Show where the data lives and which migrations are applied."""
    cfg = load_config()
    if not cfg.db_path.exists():
        typer.echo(f"no database at {cfg.db_path} — run `muster init`")
        raise typer.Exit(1)

    conn = db.connect(cfg.db_path)
    try:
        applied = sorted(migrate.applied_versions(conn))
        pending = [m.version for m in migrate.discover() if m.version not in set(applied)]
        counts = {
            table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("docs", "chunks", "vectors", "attachments", "jobs")
        }
        # A document with no chunks matches no query. That is how a parser
        # regression hides, so it gets its own line rather than being inferred.
        # Redacted documents have no chunks on purpose. Counting them here
        # would bury the signal this number exists for: a parser regression
        # that quietly made real mail unsearchable.
        unfindable = conn.execute(
            "SELECT count(*) FROM docs d WHERE d.origin = 'captured'"
            " AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.doc_id = d.id)"
            " AND coalesce(json_extract(d.meta_json, '$.redacted_by'), '') = ''"
        ).fetchone()[0]
        redacted = conn.execute(
            "SELECT count(*) FROM docs d"
            " WHERE coalesce(json_extract(d.meta_json, '$.redacted_by'), '') != ''"
        ).fetchone()[0]
        categories = conn.execute(
            "SELECT COALESCE(category, '-'), count(*) FROM docs GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()
        kinds = conn.execute(
            """
            SELECT CASE
                     WHEN mime = 'message/rfc822' THEN 'message'
                     WHEN source_uid LIKE '%#att%' THEN 'attachment'
                     ELSE COALESCE(mime, 'other')
                   END AS kind,
                   count(*)
            FROM docs WHERE origin = 'captured' GROUP BY 1 ORDER BY 2 DESC
            """
        ).fetchall()
        sender_rows = conn.execute(
            """
            SELECT d.sender,
                   count(DISTINCT d.id) AS docs,
                   count(c.id)          AS chunks
            FROM docs d LEFT JOIN chunks c ON c.doc_id = d.id
            WHERE d.origin = 'captured' AND d.mime = 'message/rfc822'
            GROUP BY d.sender ORDER BY docs DESC LIMIT :top
            """,
            {"top": top},
        ).fetchall()
        months = conn.execute(
            """
            SELECT substr(doc_date, 1, 7) AS month,
                   count(*)                              AS docs,
                   count(DISTINCT content_hash)          AS distinct_bytes,
                   count(DISTINCT COALESCE(thread_uid, source_uid)) AS threads
            FROM docs
            WHERE origin = 'captured' AND mime = 'message/rfc822'
            GROUP BY 1 ORDER BY 1 DESC LIMIT :n
            """,
            {"n": months_shown},
        ).fetchall()
    finally:
        conn.close()

    typer.echo(f"{'db':<13}{cfg.db_path}")
    typer.echo(f"{'applied':<13}{', '.join(applied) or 'none'}")
    if pending:
        typer.echo(f"{'pending':<13}{', '.join(pending)}")
    for table, n in counts.items():
        typer.echo(f"{table:<13}{n}")
    style = typer.style(str(unfindable), fg="yellow") if unfindable else "0"
    typer.echo(f"{'unfindable':<13}{style}  (captured docs with no chunks)")
    typer.echo(f"{'redacted':<13}{redacted}  (credentials, deliberately unindexed)")
    if categories:
        summary = "  ".join(f"{name} {count}" for name, count in categories)
        typer.echo(f"{'categories':<13}{summary}")

    typer.echo("\nby kind")
    for kind, count in kinds:
        typer.echo(f"  {kind:<26}{count:>7}")

    if senders:
        typer.echo(f"\nby sender (top {len(sender_rows)})")
        typer.echo(f"  {'docs':>7}{'chunks':>9}  {'excl':<5}sender")
        from muster.senders import SenderFilter, excluded_by_sender

        active = SenderFilter(cfg.excluded_senders)
        for sender, docs_n, chunks_n in sender_rows:
            mark = "yes" if active.match([sender or ""]) else ""
            line = f"  {docs_n:>7}{chunks_n:>9}  {mark:<5}{(sender or '(unknown)')[:58]}"
            typer.echo(typer.style(line, dim=True) if mark else line)
        return

    if months:
        # distinct_bytes below docs is duplication: the same message stored
        # twice under different uids. Threads well below docs is normal — a
        # thread holds several messages.
        typer.echo(f"\nby month (messages only, newest {len(months)})")
        typer.echo(f"  {'month':<10}{'docs':>7}{'distinct':>10}{'threads':>9}   dup")
        for month, docs_n, distinct_n, threads_n in months:
            dup = docs_n - distinct_n
            flag = typer.style(f"{dup:>5}", fg="yellow") if dup else f"{dup:>5}"
            typer.echo(f"  {month or '?':<10}{docs_n:>7}{distinct_n:>10}{threads_n:>9}{flag}")


def _require_db(cfg):
    if not cfg.db_path.exists():
        typer.echo(f"no database at {cfg.db_path} — run `muster init`", err=True)
        raise typer.Exit(1)


def _drain(
    connector,
    cfg,
    *,
    query: str | None,
    limit: int | None,
    quiet: bool,
    exclude_categories: frozenset[str] = frozenset(),
) -> None:
    """Pull everything a connector yields into the index.

    One message per transaction, and every failure is logged and stepped over:
    a single unparseable message must never cost the rest of the run.
    """
    from muster.categories import is_excluded
    from muster.ingest import ingest_message
    from muster.redaction import Redactor
    from muster.senders import SenderFilter, excluded_by_sender

    conn = db.connect(cfg.db_path)
    store = BlobStore(cfg.blob_dir)
    seen = created = skipped = failed = attachment_docs = chunks = filtered = 0
    redacted = dropped_by_rule = by_sender = 0
    redactor = Redactor(cfg.redaction)
    sender_filter = SenderFilter(cfg.excluded_senders)

    # Every uid already held for this source, so the connector can decline to
    # fetch them. Listing ids is one request per 500; fetching a body is one
    # request each, so re-syncing a mailbox we mostly hold should cost the
    # listing and nothing more.
    known = {
        row[0]
        for row in conn.execute(
            "SELECT source_uid FROM docs WHERE source = ?", (connector.source,)
        )
    }
    if known:
        typer.echo(f"{len(known)} messages already indexed; they will not be fetched")

    def on_page(total: int, pages: int, more: bool) -> None:
        tail = "" if more else " (complete)"
        typer.echo(f"\rlisting: {total} message ids over {pages} pages{tail}", nl=not more)

    fetch_kwargs = {"limit": limit, "skip": known.__contains__}
    if "on_page" in inspect.signature(connector.fetch).parameters:
        fetch_kwargs["on_page"] = on_page

    try:
        for message in connector.fetch(query, **fetch_kwargs):
            seen += 1
            if exclude_categories and is_excluded(
                message.meta.get("labels"), exclude_categories
            ):
                filtered += 1
                continue
            if sender_filter and excluded_by_sender(message.raw, sender_filter):
                by_sender += 1
                continue
            try:
                result = ingest_message(
                    conn,
                    store,
                    source=connector.source,
                    source_uid=message.source_uid,
                    raw=message.raw,
                    meta=message.meta,
                    own_addresses=set(cfg.own_addresses),
                    redactor=redactor,
                    sender_filter=sender_filter,
                )
            except Exception as exc:
                failed += 1
                logging.getLogger("muster.sync").exception(
                    "failed to ingest %s: %s", message.source_uid, exc
                )
                continue

            if result.redacted_by:
                redacted += 1
            if result.dropped:
                dropped_by_rule += 1
                continue
            if result.created:
                created += 1
                attachment_docs += result.attachment_docs
                chunks += result.chunks
                if not quiet:
                    title = (result.warnings and "! " or "  ") + message.source_uid
                    typer.echo(f"{title}  {result.chunks} chunks")
            else:
                skipped += 1
    finally:
        conn.close()

    # Messages the connector could not fetch never reach this loop, so they are
    # reported separately rather than being invisible in the arithmetic below.
    listed = getattr(connector, "listed", seen)
    dropped = list(getattr(connector, "dropped", []))
    known_skipped = getattr(connector, "skipped_known", 0)

    typer.echo(
        f"\n{listed} listed by source"
        f"\n  {known_skipped} already indexed (not fetched)"
        f"\n  {len(dropped)} unfetchable (dropped before parsing)"
        f"\n  {filtered} filtered by category"
        f"\n  {dropped_by_rule} dropped by a redaction rule"
        f"\n  {by_sender} skipped by sender exclusion"
        f"\n  {created} newly indexed"
        f"\n  {skipped} already indexed"
        f"\n  {failed} failed to parse"
        f"\n{chunks} chunks, {attachment_docs} attachment docs"
    )
    if redacted:
        typer.echo(f"  {redacted} redacted (credentials kept out of the index)")
    if listed != (
        known_skipped + len(dropped) + filtered + by_sender
        + dropped_by_rule + created + skipped + failed
    ):
        typer.echo(
            typer.style("  warning: counters do not reconcile", fg="yellow"), err=True
        )
    for uid, reason in dropped[:10]:
        typer.echo(f"  dropped {uid}: {reason}", err=True)


@sync_app.command("gmail")
def sync_gmail(
    query: Annotated[
        str | None, typer.Option("--query", "-q", help="Gmail search query.")
    ] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", "-n", help="Stop after N messages.")
    ] = None,
    quiet: Annotated[bool, typer.Option("--quiet", help="Summary only.")] = False,
    exclude_categories: Annotated[
        str | None,
        typer.Option(
            "--exclude-categories",
            help="Skip these Gmail categories entirely, e.g. 'promotions,social'.",
        ),
    ] = None,
) -> None:
    """Fetch messages from Gmail and index them."""
    from muster.categories import parse_selection
    from muster.connectors.gmail import GmailAuthError, GmailConnector

    cfg = load_config()
    cfg.ensure_dirs()
    _require_db(cfg)

    try:
        excluded = parse_selection(exclude_categories)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc

    connector = GmailConnector(cfg.gmail.credentials_path, cfg.gmail.token_path)
    try:
        _drain(
            connector, cfg, query=query or cfg.gmail.query, limit=limit, quiet=quiet,
            exclude_categories=excluded,
        )
    except GmailAuthError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@sync_app.command("files")
def sync_files(
    roots: Annotated[
        list[str] | None,
        typer.Option("--root", help="Directory to walk. Repeatable; overrides config."),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Re-read every file, ignoring size and mtime.")
    ] = False,
    quiet: Annotated[bool, typer.Option("--quiet", help="Summary only.")] = False,
) -> None:
    """Index local files. Documents and Downloads by default."""
    from muster.connectors.files import FileConnector
    from muster.fileingest import ingest_file
    from muster.locking import LockHeld, sync_lock

    cfg = load_config()
    cfg.ensure_dirs()
    _require_db(cfg)

    connector = FileConnector(
        tuple(roots) if roots else cfg.files.roots,
        max_bytes=cfg.files.max_bytes,
        extra_excluded_dirs=cfg.files.excluded_dirs,
    )
    typer.echo("walking " + ", ".join(connector.roots))

    created = updated = moved = unchanged = empty = failed = 0
    chunks = 0
    by_date_source: dict[str, int] = {}
    # Every path the walk produced, so anything under a root it read and did not
    # produce can be concluded absent — and nothing else can.
    seen_paths: set[str] = set()

    conn = db.connect(cfg.db_path)
    try:
        with sync_lock(cfg.lock_path):
            for record in connector.walk():
                seen_paths.add(record.source_uid)
                try:
                    result = ingest_file(conn, record, force=force)
                except Exception as exc:  # noqa: BLE001 — one file, not the walk
                    failed += 1
                    logging.getLogger("muster.sync").exception(
                        "failed to index %s: %s", record.path, exc
                    )
                    continue

                chunks += result.chunks
                if result.created:
                    created += 1
                    by_date_source[result.date_source] = (
                        by_date_source.get(result.date_source, 0) + 1
                    )
                    if not quiet:
                        typer.echo(f"  {record.path}  {result.chunks} chunks")
                elif result.updated:
                    updated += 1
                elif result.moved:
                    moved += 1
                elif result.unchanged:
                    unchanged += 1
                if result.empty and (result.created or result.updated):
                    empty += 1
    except LockHeld:
        typer.echo("another sync is already running; nothing to do", err=True)
        raise typer.Exit(1) from None
    finally:
        conn.close()

    stats = connector.stats

    # Only when the walk was not limited to some subset of the roots, and only
    # against roots it actually read.
    marked = restored = trashed = 0
    if not roots:
        from muster.missing import sweep_files

        conn = db.connect(cfg.db_path)
        try:
            marked, restored, trashed = sweep_files(
                conn, [Path(r) for r in stats.roots_read], seen_paths=seen_paths
            )
        finally:
            conn.close()

    folders_found = 0
    if not roots:
        from muster.connectors.files import DEFAULT_EXCLUDED_DIRS
        from muster.folders import replace_all, scan

        excluded = {d.lower() for d in (*DEFAULT_EXCLUDED_DIRS, *cfg.files.excluded_dirs)}
        conn = db.connect(cfg.db_path)
        try:
            folders_found = replace_all(
                conn,
                scan(cfg.files.folder_roots, excluded, max_depth=cfg.files.folder_depth),
            )
        finally:
            conn.close()

    typer.echo(
        f"\n{stats.seen} files seen"
        f"\n  {stats.excluded_type} skipped: not indexable"
        f"\n  {stats.excluded_secret} skipped: looks like credentials"
        f"\n  {stats.too_big} skipped: over {cfg.files.max_bytes // (1024 * 1024)}MB"
        f"\n  {stats.excluded_dir} directories pruned"
        f"\n  {stats.unreadable} unreadable"
        f"\n  {created} newly indexed"
        f"\n  {updated} changed and re-indexed"
        f"\n  {moved} recognised as moved"
        f"\n  {unchanged} unchanged"
        f"\n  {failed} failed"
        f"\n{chunks} chunks"
    )
    if marked:
        where = f", {trashed} of them in the Trash" if trashed else ""
        typer.echo(
            f"  {marked} no longer on disk{where} — hidden from search, still on record"
            "\n    muster purge --missing   removes them for good"
        )
    if restored:
        typer.echo(f"  {restored} came back and are searchable again")
    if folders_found:
        typer.echo(f"{folders_found} folders mapped for jump-to suggestions")
    if empty:
        typer.echo(f"  {empty} had no extractable text (indexed as metadata only)")
    if by_date_source:
        ordered = ", ".join(f"{k} {v}" for k, v in sorted(by_date_source.items()))
        typer.echo(f"  dates from: {ordered}")
    for root in stats.roots_missing:
        typer.echo(f"  warning: no such directory: {root}", err=True)
    if stats.walk_errors:
        typer.echo(f"  {stats.walk_errors} directories could not be read", err=True)
    if stats.roots_denied:
        typer.echo(
            typer.style(
                "\nmacOS denied access to:\n  "
                + "\n  ".join(stats.roots_denied)
                + "\n\nThese folders are protected. Nothing was indexed from them —\n"
                "this is a permissions prompt that never appeared, not an empty folder.\n"
                "Grant access in System Settings > Privacy & Security > Full Disk Access\n"
                "for the program running muster (your terminal, or muster itself).",
                fg="yellow",
            ),
            err=True,
        )


@sync_app.command("mbox")
def sync_mbox(
    path: Annotated[Path, typer.Argument(help="An mbox file, Maildir, or .eml.")],
    limit: Annotated[
        int | None, typer.Option("--limit", "-n", help="Stop after N messages.")
    ] = None,
    quiet: Annotated[bool, typer.Option("--quiet", help="Summary only.")] = False,
) -> None:
    """Index a local mailbox — an mbox file, a Maildir, or a single .eml."""
    from muster.connectors.mbox import MboxConnector

    cfg = load_config()
    cfg.ensure_dirs()
    _require_db(cfg)

    if not path.exists():
        typer.echo(f"no such mailbox: {path}", err=True)
        raise typer.Exit(1)

    _drain(MboxConnector(path), cfg, query=None, limit=limit, quiet=quiet)


@app.command()
def embed(
    model: Annotated[
        str | None, typer.Option("--model", help="Override the configured model id.")
    ] = None,
    batch_size: Annotated[
        int | None, typer.Option("--batch-size", help="Texts per forward pass.")
    ] = None,
    device: Annotated[
        str | None, typer.Option("--device", help="mps | cpu | cuda. Default: autodetect.")
    ] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", "-n", help="Stop after N distinct texts.")
    ] = None,
) -> None:
    """Embed chunks that have no vector for the current model. Resumable."""
    from muster import pausing
    from muster.embed import count_pending, embed_pending
    from muster.locking import LockHeld, sync_lock

    cfg = load_config()
    _require_db(cfg)
    model_id = model or cfg.embed.model_id

    # The same lock the scheduled pass holds. Embedding is not a sync, but it
    # competes for the one GPU and writes the same table, and two runs loading
    # bge-m3 at once is slower than either alone.
    try:
        lock = sync_lock(cfg.lock_path)
        lock.__enter__()
    except LockHeld:
        typer.echo("another sync or embed is already running; nothing to do")
        raise typer.Exit(0) from None

    conn = db.connect(cfg.db_path)
    try:
        pending = count_pending(conn, model_id)
        if not pending:
            typer.echo(f"nothing to do — every chunk has a vector for {model_id}")
            return
        typer.echo(f"{pending} chunks pending for {model_id}")

        def report_progress(report, total: int) -> None:
            typer.echo(
                f"\r  {report.groups}/{total} texts  {report.chunks} vectors"
                f"  batch {report.batches}",
                nl=False,
            )

        report = embed_pending(
            conn,
            model_id=model_id,
            device=device or cfg.embed.device,
            batch_size=batch_size or cfg.embed.batch_size,
            limit=limit,
            progress=report_progress,
            # A run started by hand is the one most likely to be in the way, so
            # it honours the switch too — not only the scheduled one.
            should_stop=lambda: pausing.is_paused(cfg.home),
        )
    finally:
        conn.close()
        lock.__exit__(None, None, None)

    typer.echo(
        f"\n{report.chunks} vectors written from {report.groups} distinct texts"
        f" in {report.batches} batches"
        f"\nmodel {report.model_id}  device {report.device}  dim {report.dimensions}"
    )
    if report.stopped:
        typer.echo(
            "\npaused before finishing. nothing was lost — `muster resume` then"
            " re-run, or let the scheduled job carry on."
        )


@app.command()
def doctor(
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show every check, not only problems.")
    ] = False,
) -> None:
    """Check whether anything is quietly broken."""
    from muster import doctor as checks

    cfg = load_config()
    _require_db(cfg)
    conn = db.connect(cfg.db_path)
    try:
        report = checks.run(cfg, conn)
    finally:
        conn.close()

    mark = {checks.OK: ("  ok  ", typer.colors.GREEN),
            checks.WARN: (" warn ", typer.colors.YELLOW),
            checks.FAIL: (" fail ", typer.colors.RED)}

    for finding in report.findings:
        if not verbose and not finding.bad:
            continue
        label, colour = mark[finding.status]
        typer.echo(
            f"{typer.style(label, fg=colour, bold=True)}  "
            f"{finding.name:<22} {finding.detail}"
        )
        if finding.remedy:
            typer.echo(f"{'':8}{'':22} {typer.style('-> ' + finding.remedy, fg=typer.colors.BLUE)}")

    problems = report.problems
    typer.echo()
    if not problems:
        typer.echo(typer.style(f"nothing wrong ({len(report.findings)} checks)", fg=typer.colors.GREEN))
        return
    typer.echo(
        typer.style(
            f"{len(problems)} of {len(report.findings)} checks need attention",
            fg=typer.colors.YELLOW if report.worst == checks.WARN else typer.colors.RED,
        )
    )
    if not verbose:
        typer.echo("  -v shows the checks that passed too")
    # Non-zero so this can gate a script without parsing the output.
    raise typer.Exit(1 if report.worst == checks.FAIL else 0)


@app.command()
def pause() -> None:
    """Stop background work: scheduled syncs, and any embedding in flight."""
    from muster import pausing

    cfg = load_config()
    pausing.pause(cfg.home)
    typer.echo(
        "paused.\n"
        "  a running embed stops after its current batch; nothing is lost\n"
        "  scheduled syncs will skip until you run `muster resume`"
    )


@app.command()
def resume() -> None:
    """Allow background work again. Embedding continues where it stopped."""
    from muster import pausing

    cfg = load_config()
    if pausing.resume(cfg.home):
        typer.echo("resumed. the next scheduled run picks up where it left off")
    else:
        typer.echo("not paused; nothing to do")


@app.command()
def serve(
    host: Annotated[str, typer.Option("--host", help="Interface to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port to bind.")] = 8765,
    unsafe_host: Annotated[
        bool,
        typer.Option(
            "--i-know-what-this-does",
            help="Permit binding a non-loopback interface.",
        ),
    ] = False,
) -> None:
    """Serve the read-only HTTP API for the desktop UI."""
    import ipaddress

    cfg = load_config()
    _require_db(cfg)

    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host in ("localhost",)
    if not loopback and not unsafe_host:
        # There is no authentication. Binding this to a routable address puts
        # an entire personal mailbox on the network for anyone who asks.
        typer.echo(
            f"refusing to bind {host}: the API has no authentication and serves\n"
            "the whole archive. Pass --i-know-what-this-does to override.",
            err=True,
        )
        raise typer.Exit(1)

    try:
        from muster.api import serve as run_server
    except ImportError as exc:
        typer.echo(f"the API extra is not installed: {exc}\n  pip install -e '.[api]'", err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"muster api on http://{host}:{port}  (db {cfg.db_path})")
    run_server(host=host, port=port, cfg=cfg)


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="What to look for.")],
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many results.")] = 10,
    since: Annotated[
        str | None, typer.Option("--since", help="Only docs on or after this ISO date.")
    ] = None,
    until: Annotated[
        str | None, typer.Option("--until", help="Only docs on or before this ISO date.")
    ] = None,
    source: Annotated[
        str | None, typer.Option("--source", help="Restrict to one source.")
    ] = None,
    mode: Annotated[
        str, typer.Option("--mode", help="keyword | vector | hybrid.")
    ] = "keyword",
    include_derived: Annotated[
        bool, typer.Option("--include-derived", help="Also return derived docs.")
    ] = False,
    exclude_categories: Annotated[
        str | None,
        typer.Option(
            "--exclude-categories",
            help="Gmail categories to hide. Default: promotions,social.",
        ),
    ] = None,
    all_categories: Annotated[
        bool, typer.Option("--all-categories", help="Do not hide any category.")
    ] = False,
    min_score: Annotated[
        float | None,
        typer.Option("--min-score", help="Confidence floor. Overrides config."),
    ] = None,
    no_min_score: Annotated[
        bool, typer.Option("--no-min-score", help="Show everything, however weak.")
    ] = False,
) -> None:
    """Search the index."""
    from muster.categories import DEFAULT_EXCLUDED, parse_selection
    from muster.search import search as run_search

    cfg = load_config()
    _require_db(cfg)

    if all_categories:
        excluded = frozenset()
    elif exclude_categories is not None:
        try:
            excluded = parse_selection(exclude_categories)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
    else:
        excluded = DEFAULT_EXCLUDED

    conn = db.connect(cfg.db_path)
    try:
        hits = run_search(
            conn,
            query,
            mode=mode,
            limit=limit,
            since=since,
            until=until,
            source=source,
            origin=None if include_derived else "captured",
            exclude_categories=excluded,
            min_score=0.0 if no_min_score else min_score,
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        conn.close()

    if not hits:
        # A weak match presented like a strong one is worse than nothing: it
        # invites the reader to believe the archive was searched and came back
        # with this.
        typer.echo("no confident matches")
        if not no_min_score:
            typer.echo("  (--no-min-score to see weak matches anyway)", err=True)
        return

    for rank, hit in enumerate(hits, start=1):
        title = hit.title or "(no title)"
        copies = f"  (x{hit.duplicates} identical copies)" if hit.duplicates > 1 else ""
        typer.echo(
            f"{rank:>2}. {typer.style(title[:72], bold=True)}{copies}"
            f"\n    {hit.doc_date[:10]}  {hit.source}  {hit.counterparty or '-'}"
            f"  score {hit.score:.3f}  doc {hit.doc_id}"
            f"\n    {hit.snippet}"
        )


@app.command()
def purge(
    source: Annotated[
        str | None, typer.Option("--source", help="Purge everything from this source.")
    ] = None,
    uid_prefix: Annotated[
        str | None, typer.Option("--uid-prefix", help="Purge uids starting with this.")
    ] = None,
    category: Annotated[
        str | None, typer.Option("--category", help="Purge one Gmail category.")
    ] = None,
    excluded_senders: Annotated[
        bool,
        typer.Option(
            "--excluded-senders",
            help="Purge mail from every sender on the config exclusion list.",
        ),
    ] = False,
    missing: Annotated[
        bool,
        typer.Option(
            "--missing",
            help="Purge documents whose original has gone (deleted, trashed, or unreachable).",
        ),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would go, change nothing.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation.")] = False,
) -> None:
    """Remove documents from the index. Blobs are kept; originals are immutable."""
    from muster.purge import plan as purge_plan
    from muster.purge import purge as run_purge
    from muster.senders import SenderFilter, excluded_by_sender

    cfg = load_config()
    _require_db(cfg)

    sender_filter = SenderFilter(cfg.excluded_senders) if excluded_senders else None
    if excluded_senders and not sender_filter:
        typer.echo("no senders configured under [senders] excluded", err=True)
        raise typer.Exit(1)

    conn = db.connect(cfg.db_path)
    try:
        try:
            preview = purge_plan(
                conn, source=source, uid_prefix=uid_prefix, category=category,
                sender_filter=sender_filter, missing=missing,
            )
        except ValueError as exc:
            typer.echo(
                f"{exc}. Pass --source, --uid-prefix, --category or --missing.", err=True
            )
            raise typer.Exit(1) from exc

        if preview.empty:
            typer.echo("nothing matches")
            return

        typer.echo(
            f"would remove {preview.docs} docs, {preview.chunks} chunks,"
            f" {preview.vectors} vectors, {preview.attachments} attachment rows"
        )
        for sender, count in preview.senders:
            typer.echo(f"  {count:>6}  {sender}")
        for title in preview.titles[:6]:
            typer.echo(f"    {title[:74]}")
        if preview.docs > len(preview.titles):
            typer.echo(f"  … and {preview.docs - len(preview.titles)} more")

        if dry_run:
            typer.echo("\ndry run — nothing changed")
            return
        if not yes and not typer.confirm("\nproceed?"):
            typer.echo("cancelled")
            return

        done = run_purge(
            conn, source=source, uid_prefix=uid_prefix, category=category,
            sender_filter=sender_filter,
            missing=missing,
        )
    finally:
        conn.close()

    typer.echo(
        f"removed {done.docs} docs and {done.chunks} chunks."
        "\nBlobs are untouched — re-run sync to index them again."
    )


@app.command()
def reparse(
    source: Annotated[
        str | None, typer.Option("--source", help="Only this source, e.g. gmail.")
    ] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", "-n", help="Stop after N documents.")
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Rebuild even documents whose text is unchanged.")
    ] = False,
) -> None:
    """Re-extract and re-chunk from stored originals, without re-fetching.

    Ingest is idempotent on source_uid, so parser fixes never reach mail that is
    already indexed. This is how they get there.
    """
    from muster.redaction import Redactor
    from muster.reparse import reparse as run_reparse
    from muster.senders import SenderFilter, excluded_by_sender

    cfg = load_config()
    _require_db(cfg)

    conn = db.connect(cfg.db_path)
    store = BlobStore(cfg.blob_dir)
    try:
        def show(report) -> None:
            typer.echo(
                f"\r  {report.scanned} scanned, {report.changed} changed", nl=False
            )

        report = run_reparse(
            conn,
            store,
            source=source,
            limit=limit,
            own_addresses=set(cfg.own_addresses),
            force=force,
            redactor=Redactor(cfg.redaction),
            sender_filter=SenderFilter(cfg.excluded_senders),
            progress=show,
        )
    finally:
        conn.close()

    typer.echo(
        f"\r{report.scanned} documents scanned"
        f"\n  {report.changed} changed"
        f"\n  {report.unchanged} unchanged (left alone, vectors intact)"
        f"\n  {report.failed} failed"
        f"\nchunks {report.chunks_before} -> {report.chunks_after}"
        f"\nvectors {report.vectors_preserved} preserved by text_hash"
    )
    for uid, reason in report.failures[:10]:
        typer.echo(f"  failed {uid}: {reason}", err=True)
    if report.changed:
        typer.echo("\nrun `muster embed` to fill vectors for the rebuilt chunks")


@corpus_app.command("snapshot")
def corpus_snapshot(
    destination: Annotated[Path, typer.Argument(help="Where to write the frozen corpus.")],
) -> None:
    """Freeze the current index to a standalone file for repeatable evaluation."""
    from muster.evaluate import describe_corpus, snapshot

    cfg = load_config()
    _require_db(cfg)

    conn = db.connect(cfg.db_path)
    try:
        path = snapshot(conn, destination)
    finally:
        conn.close()

    frozen = db.connect(path)
    try:
        info = describe_corpus(frozen, str(path))
    finally:
        frozen.close()

    typer.echo(
        f"wrote {path}"
        f"\nfingerprint {info.fingerprint}"
        f"\n{info.docs} docs, {info.chunks} chunks, {info.vectors} vectors"
    )


@corpus_app.command("info")
def corpus_info(
    path: Annotated[Path, typer.Argument(help="A corpus file.")],
) -> None:
    """Show a corpus's fingerprint and contents."""
    from muster.evaluate import describe_corpus

    if not path.exists():
        typer.echo(f"no corpus at {path}", err=True)
        raise typer.Exit(1)

    conn = db.connect(path)
    try:
        info = describe_corpus(conn, str(path))
    finally:
        conn.close()

    typer.echo(
        f"path        {info.path}"
        f"\nfingerprint {info.fingerprint}"
        f"\ndocs        {info.docs}"
        f"\nchunks      {info.chunks}"
        f"\nvectors     {info.vectors}"
    )


eval_app = typer.Typer(
    name="eval", help="Score retrieval against eval/queries.jsonl.",
    invoke_without_command=True,
)
app.add_typer(eval_app)


@eval_app.command("verify")
def eval_verify(
    queries: Annotated[
        Path | None, typer.Option("--queries", help="JSONL query file.")
    ] = None,
    corpus: Annotated[
        Path | None, typer.Option("--corpus", help="Verify against a frozen corpus.")
    ] = None,
) -> None:
    """Check the query file's ground truth resolves, without running retrieval."""
    from muster.evaluate import EvalError, load_queries, resolve_queries_path, verify

    cfg = load_config()
    db_path = corpus or cfg.db_path
    if not db_path.exists():
        typer.echo(f"no database at {db_path}", err=True)
        raise typer.Exit(1)

    try:
        queries_path, _is_example = resolve_queries_path(queries)
        records = load_queries(queries_path)
    except EvalError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc

    conn = db.connect(db_path)
    try:
        report = verify(conn, records)
    finally:
        conn.close()

    typer.echo(f"{queries_path}  ->  {db_path}")
    typer.echo(
        f"{report.queries} queries, {report.resolvable} scoreable, "
        f"{report.total_ids} ids of which {report.bad_ids} do not resolve"
    )

    if report.unscoreable:
        typer.echo(
            typer.style(
                f"\n{len(report.unscoreable)} queries have NO resolvable id "
                "(silently skipped by the scorer):",
                fg="red",
            )
        )
        for question, ids in report.unscoreable:
            typer.echo(f"  {question[:72]}")
            for value in ids:
                typer.echo(f"      {value}")

    if report.partial:
        typer.echo(
            typer.style(f"\n{len(report.partial)} queries have some bad ids:", fg="yellow")
        )
        for question, ids in report.partial:
            typer.echo(f"  {question[:72]}")
            for value in ids:
                typer.echo(f"      {value}")

    if report.duplicates:
        typer.echo(f"\nids appearing in more than one query: {len(report.duplicates)}")
        for value in report.duplicates[:10]:
            typer.echo(f"  {value}")

    if report.clean:
        typer.echo(typer.style("\nall ground truth resolves", fg="green"))
    else:
        raise typer.Exit(1)


@eval_app.callback(invoke_without_command=True)
def eval_cmd(
    ctx: typer.Context,
    queries: Annotated[
        Path | None,
        typer.Option("--queries", help="JSONL query file. Default: eval/queries.jsonl."),
    ] = None,
    k: Annotated[int, typer.Option("--k", help="Cutoff for recall@k.")] = 10,
    mode: Annotated[str, typer.Option("--mode", help="keyword | vector | hybrid.")] = "keyword",
    results_dir: Annotated[
        Path, typer.Option("--results-dir", help="Where to write the run.")
    ] = Path("eval/results"),
    show: Annotated[int, typer.Option("--show", help="Per-query lines to print.")] = 200,
    corpus: Annotated[
        Path | None,
        typer.Option(
            "--corpus",
            help="Score against a frozen corpus instead of the live index.",
        ),
    ] = None,
    all_categories: Annotated[
        bool, typer.Option("--all-categories", help="Score without the category filter.")
    ] = False,
    include_text: Annotated[
        bool,
        typer.Option(
            "--include-text",
            help="Write query text and result titles into the results file. Private.",
        ),
    ] = False,
) -> None:
    """Score retrieval against eval/queries.jsonl."""
    if ctx.invoked_subcommand is not None:
        return

    from muster.categories import DEFAULT_EXCLUDED
    from muster.evaluate import (
        EXAMPLE_QUERIES,
        EvalError,
        evaluate,
        load_no_answer,
        load_queries,
        probe_no_answer,
        resolve_queries_path,
        write_run,
    )

    cfg = load_config()
    if corpus is None:
        _require_db(cfg)
        db_path = cfg.db_path
    elif not corpus.exists():
        typer.echo(f"no corpus at {corpus} — run `muster corpus snapshot`", err=True)
        raise typer.Exit(1)
    else:
        db_path = corpus

    excluded = frozenset() if all_categories else DEFAULT_EXCLUDED

    try:
        queries_path, is_example = resolve_queries_path(queries)
    except EvalError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc

    if is_example:
        typer.echo(
            typer.style(
                f"using {EXAMPLE_QUERIES} — three synthetic records, not a benchmark.\n"
                "Write your own set at eval/queries.jsonl (gitignored; see eval/README.md).",
                fg="yellow",
            ),
            err=True,
        )

    conn = db.connect(db_path)
    try:
        records = load_queries(queries_path)
        run = evaluate(
            conn, records, mode=mode, k=k,
            corpus_path=str(db_path), exclude_categories=excluded,
        )
        probe = probe_no_answer(
            conn, load_no_answer(), mode=mode, k=k, exclude_categories=excluded
        )
    except (EvalError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        conn.close()

    for result in run.results[:show]:
        if not result.relevant:
            marker, detail = typer.style("skip", fg="yellow"), "no resolvable ground truth"
        elif result.first_relevant_rank:
            marker = typer.style(" hit", fg="green")
            detail = (
                f"rank {result.first_relevant_rank}  "
                f"recall {result.recall_at_k:.2f}  {len(result.hits)}/{len(result.relevant)}"
            )
        else:
            marker, detail = typer.style("miss", fg="red"), f"0/{len(result.relevant)}"
        typer.echo(f"{marker}  {result.q[:60]:<62}{detail}")
        if result.unresolved:
            typer.echo(f"        unresolved ids: {', '.join(map(str, result.unresolved))}")

    if probe.total:
        style = typer.style if probe.leaks else (lambda s, **kw: s)
        typer.echo(
            "\n" + style(
                f"no-answer probe: {probe.silent}/{probe.total} queries correctly "
                "returned nothing",
                fg="red" if probe.leaks else "green",
            )
        )
        for leak in probe.leaks:
            typer.echo(
                f"  leaked  {leak.q[:36]:<38}{leak.returned} results, "
                f"top {leak.top_score} {leak.arms or ''}"
            )

    path = write_run(run, results_dir, include_text=include_text)
    typer.echo(
        f"\nmode {run.mode}  k {run.k}"
        f"\nrecall@{run.k}  {run.recall_at_k:.4f}"
        f"\nMRR        {run.mrr:.4f}"
        f"\nscored     {run.queries_scored}/{run.queries_total} queries"
        f"\ncorpus     {run.corpus.fingerprint}"
        f"  ({run.corpus.docs} docs / {run.corpus.chunks} chunks)"
        f"\n           {run.corpus.path}"
        f"\nqueries    {queries_path}"
        f"\nwrote      {path}"
        + ("  (includes query text — keep it out of git)" if include_text else "  (redacted)")
    )


if __name__ == "__main__":
    app()
