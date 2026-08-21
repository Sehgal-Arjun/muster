# muster

A local-first personal knowledge index. It ingests email and files, stores the
originals immutably, and lets you search everything you have ever received.

Everything is one SQLite file plus a content-addressed blob store. No services,
no ORM, no frameworks.

## Layout

```
~/.muster/
  config.toml        optional; every field has a default
  muster.db          the index
  blobs/ab/abcd…     content-addressed originals, never modified
  credentials.json   Google OAuth client secret
  gmail_token.json   cached OAuth token
```

Set `MUSTER_HOME` to point all of that somewhere else.

## Setup

```sh
make install               # venv, deps, and the talon workaround below
.venv/bin/muster init      # creates the db, applies migrations; safe to re-run
.venv/bin/muster status
```

Use `make install`, not a bare `pip install -e .`. talon's published metadata
hard-requires `cchardet`, whose C extension cannot build on Python 3.11+, so
talon has to go in with `--no-deps` alongside `faust-cchardet`. `pyproject.toml`
lists talon's real dependencies by hand and explains why; the Makefile asserts
afterwards that talon imports with its scikit-learn classifier switched off.

## Gmail

One-time setup:

1. In Google Cloud Console, create a project and enable the **Gmail API**.
2. Create an **OAuth client ID** of type *Desktop app*; download the JSON.
3. Save it as `~/.muster/credentials.json`.
4. Add your own address as a test user on the OAuth consent screen.

```sh
muster sync gmail --query "newer_than:7d" --limit 50
```

The first run opens a browser for consent and caches a token at
`~/.muster/gmail_token.json` (mode 0600); later runs refresh it silently. The
scope is `gmail.readonly`.

## Local mail

No OAuth needed — useful for Takeout exports, and for exercising the parser:

```sh
muster sync mbox ~/takeout/All\ mail.mbox
muster sync mbox tests/fixtures/sample.mbox
```

## What ingestion does

Per message, in one transaction:

1. The raw RFC822 bytes go to the blob store, content-addressed.
2. `text/plain` wins; `text/html` is converted only when it is all there is.
3. Quoted replies go (talon plus our own English and German openers), then a
   conservative signature strip — a trailing block of ≤4 lines after a `--`
   delimiter or a sign-off, removed only if it also holds a phone or URL.
4. The body is chunked to ~512 tokens with 15% overlap on paragraph breaks.
5. Attachments become blobs. Text-bearing ones (PDF via PyMuPDF, OCR via
   `ocrmac` when there is no text layer) also become their own `docs` rows, so
   an invoice is searchable in its own right. Their filenames are appended to
   the parent's first chunk, because that is how people remember email.

Re-running is a no-op: a message is identified by `(source, source_uid)`, and an
existing row means the whole message is skipped. Malformed MIME, invented
charsets and missing headers are logged to `docs.meta_json` and stepped over —
a bad message never ends a run.

## Search

```sh
muster search "warehouse lease renewal" --limit 10 --since 2026-01-01
muster search "..." --mode keyword | vector | hybrid
```

- **keyword** — BM25 over FTS5. Terms are OR-ed, and a punctuated term
  contributes both its phrase and its parts, so `Rechnung_2026-0042.pdf` finds
  the covering email *and* the invoice.
- **vector** — `bge-small-en-v1.5` via sqlite-vec cosine distance. Needs
  `muster embed` first.
- **hybrid** — both arms fused with Reciprocal Rank Fusion at k=60. Falls back
  to keyword alone when nothing is embedded.

All three stay available so they remain comparable under `muster eval`; a mode
that cannot be measured against the others is a mode nobody can reason about.

```sh
make install-embed        # adds sentence-transformers (pulls torch)
muster embed              # resumable; skips chunks that already have a vector
```

`muster embed` is safe to interrupt — each batch commits — and identical chunk
text is encoded once and written for every chunk that shares its `text_hash`.
Switching models inserts alongside the old vectors rather than replacing them.

### Where it stands

On the 15-message fixture corpus (5 seed queries, k=10):

| mode | recall@10 | MRR |
|---|---|---|
| keyword | 0.8000 | 0.8000 |
| vector | **1.0000** | **0.8333** |
| hybrid | 0.8000 | 0.8000 |

Do not read much into this. Five queries over sixteen documents measures
almost nothing, and hybrid is being punished by an artefact of the tiny corpus:
the one query keyword misses is answered by the vector arm at rank 6, and with
only sixteen documents the keyword arm's confident noise outranks it in the
fusion. Tuning the RRF constant against five queries would be overfitting.
The number that matters is the one after `eval/queries.jsonl` is filled in.

## Configuration

`~/.muster/config.toml`, all optional:

```toml
[storage]
db_path = "muster.db"      # relative paths resolve against ~/.muster
blob_dir = "blobs"

[gmail]
credentials_path = "credentials.json"
token_path = "gmail_token.json"
query = "newer_than:7d"

[embed]
model_id = "BAAI/bge-small-en-v1.5"
batch_size = 64
# device = "mps"           # default: autodetect

[identity]
addresses = ["you@example.com"]   # so `counterparty` names the other party
```

## Schema

Numbered `.sql` files in `migrations/`, applied in filename order by
`muster init` and recorded in `schema_migrations`. Migrations are additive;
each applies atomically with its bookkeeping row.

Three invariants the schema exists to enforce:

- **Originals are immutable.** The database holds blob paths, never bytes.
- **Re-embedding never re-parses.** `vectors` is keyed `(chunk_id, model_id)`,
  so a new model is an insert alongside the old rows. `chunks.text_hash` lets
  unchanged text skip re-embedding.
- **Captured and derived content are separate.** `docs.origin` is `'captured'`
  or `'derived'`; retrieval filters to `'captured'` by default.
