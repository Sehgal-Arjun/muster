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
    from muster.embed import count_pending, embed_pending

    cfg = load_config()
    _require_db(cfg)
    model_id = model or cfg.embed.model_id

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
        )
    finally:
        conn.close()

    typer.echo(
        f"\n{report.chunks} vectors written from {report.groups} distinct texts"
        f" in {report.batches} batches"
        f"\nmodel {report.model_id}  device {report.device}  dim {report.dimensions}"
    )


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


@app.command("eval")
def eval_cmd(
    queries: Annotated[
        Path, typer.Option("--queries", help="JSONL query file.")
    ] = Path("eval/queries.jsonl"),
    k: Annotated[int, typer.Option("--k", help="Cutoff for recall@k.")] = 10,
    mode: Annotated[str, typer.Option("--mode", help="keyword | vector | hybrid.")] = "keyword",
    results_dir: Annotated[
        Path, typer.Option("--results-dir", help="Where to write the run.")
    ] = Path("eval/results"),
    show: Annotated[int, typer.Option("--show", help="Per-query lines to print.")] = 200,
) -> None:
    """Score retrieval against eval/queries.jsonl."""
    from muster.evaluate import EvalError, evaluate, load_queries, write_run

    cfg = load_config()
    _require_db(cfg)

    conn = db.connect(cfg.db_path)
    try:
        records = load_queries(queries)
        run = evaluate(conn, records, mode=mode, k=k)
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

    path = write_run(run, results_dir)
    typer.echo(
        f"\nmode {run.mode}  k {run.k}"
        f"\nrecall@{run.k}  {run.recall_at_k:.4f}"
        f"\nMRR        {run.mrr:.4f}"
        f"\nscored     {run.queries_scored}/{run.queries_total} queries"
        f"  over {run.corpus['docs']} docs / {run.corpus['chunks']} chunks"
        f"\nwrote      {path}"
    )


if __name__ == "__main__":
    app()
