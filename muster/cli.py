"""The `muster` command line."""

from __future__ import annotations

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

sync_app = typer.Typer(name="sync", help="Pull messages from a source.", no_args_is_help=True)
app.add_typer(sync_app)


@app.callback()
def _root(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Log at DEBUG.")] = False,
) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


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
def status() -> None:
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
    finally:
        conn.close()

    typer.echo(f"{'db':<13}{cfg.db_path}")
    typer.echo(f"{'applied':<13}{', '.join(applied) or 'none'}")
    if pending:
        typer.echo(f"{'pending':<13}{', '.join(pending)}")
    for table, n in counts.items():
        typer.echo(f"{table:<13}{n}")


def _require_db(cfg):
    if not cfg.db_path.exists():
        typer.echo(f"no database at {cfg.db_path} — run `muster init`", err=True)
        raise typer.Exit(1)


def _drain(connector, cfg, *, query: str | None, limit: int | None, quiet: bool) -> None:
    """Pull everything a connector yields into the index.

    One message per transaction, and every failure is logged and stepped over:
    a single unparseable message must never cost the rest of the run.
    """
    from muster.ingest import ingest_message

    conn = db.connect(cfg.db_path)
    store = BlobStore(cfg.blob_dir)
    seen = created = skipped = failed = attachment_docs = chunks = 0

    try:
        for message in connector.fetch(query, limit=limit):
            seen += 1
            try:
                result = ingest_message(
                    conn,
                    store,
                    source=connector.source,
                    source_uid=message.source_uid,
                    raw=message.raw,
                    meta=message.meta,
                    own_addresses=set(cfg.own_addresses),
                )
            except Exception as exc:
                failed += 1
                logging.getLogger("muster.sync").exception(
                    "failed to ingest %s: %s", message.source_uid, exc
                )
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

    typer.echo(
        f"\n{seen} seen, {created} new, {skipped} already indexed, {failed} failed"
        f"\n{chunks} chunks, {attachment_docs} attachment docs"
    )


@sync_app.command("gmail")
def sync_gmail(
    query: Annotated[
        str | None, typer.Option("--query", "-q", help="Gmail search query.")
    ] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", "-n", help="Stop after N messages.")
    ] = None,
    quiet: Annotated[bool, typer.Option("--quiet", help="Summary only.")] = False,
) -> None:
    """Fetch messages from Gmail and index them."""
    from muster.connectors.gmail import GmailAuthError, GmailConnector

    cfg = load_config()
    cfg.ensure_dirs()
    _require_db(cfg)

    connector = GmailConnector(cfg.gmail.credentials_path, cfg.gmail.token_path)
    try:
        _drain(connector, cfg, query=query or cfg.gmail.query, limit=limit, quiet=quiet)
    except GmailAuthError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


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
) -> None:
    """Search the index."""
    from muster.search import search as run_search

    cfg = load_config()
    _require_db(cfg)

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
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        conn.close()

    if not hits:
        typer.echo("no results")
        return

    for rank, hit in enumerate(hits, start=1):
        title = hit.title or "(no title)"
        typer.echo(
            f"{rank:>2}. {typer.style(title[:72], bold=True)}"
            f"\n    {hit.doc_date[:10]}  {hit.source}  {hit.counterparty or '-'}"
            f"  score {hit.score:.3f}  doc {hit.doc_id}"
            f"\n    {hit.snippet}"
        )


if __name__ == "__main__":
    app()
