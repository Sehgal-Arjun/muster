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
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/muster init      # creates the db, applies migrations; safe to re-run
.venv/bin/muster status
```

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
