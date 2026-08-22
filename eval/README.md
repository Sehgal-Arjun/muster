# Evaluation

Retrieval changes are otherwise unfalsifiable. Chunk size, quote stripping,
OR-vs-AND, the RRF constant — each of these either moves recall@10 and MRR on a
real query set, or it does not, and without the numbers every future decision
about retrieval is taste.

```sh
muster corpus snapshot ~/.muster/eval-corpus.db    # freeze the corpus first
muster eval --corpus ~/.muster/eval-corpus.db
muster eval --corpus ~/.muster/eval-corpus.db --mode hybrid --k 10
```

**Pin the corpus.** Scoring against the live index means the numbers move
whenever mail arrives, which looks like progress and is not. Every run records
a corpus fingerprint — a hash of the captured documents' `(source, source_uid,
content_hash)` triples — in both the results JSON and the results filename.
**Two runs are only comparable if their fingerprints match.** The fingerprint is
stable across reparse and re-embedding, because those change derived content and
not which mail is in the index.

Each run prints a per-query line and writes a timestamped JSON file to
`eval/results/` (gitignored) holding the full retrieved list per query, so two
runs can be diffed to see *which* queries moved rather than only the average.

## Your query set is private, and stays local

| file | committed? | what it is |
|---|---|---|
| `queries.example.jsonl` | **yes** | three synthetic records against `tests/fixtures/sample.mbox`, showing the format |
| `queries.jsonl` | **no — gitignored** | your real set. Names real senders, subjects and message ids |
| `queries.local.jsonl` | **no — gitignored** | a second private set, if you want to keep experiments apart |
| `results/` | **no — gitignored** | run output, redacted by default |

`muster eval` reads `queries.jsonl` when it exists and falls back to
`queries.example.jsonl` otherwise, printing a notice so a fallback run is never
mistaken for a real one. Neither the queries you write nor the mail they match
should ever reach the repository.

**Results files are redacted.** A query like *"what did the clinic say about the
biopsy"*, together with the subject lines it retrieved, is as revealing as the
mailbox. So `results/*.json` records metrics, document ids and a stable
`query_id` — the first twelve hex of the query's SHA-256 — and omits the query
text, your notes, and the retrieved titles. Runs stay diffable because the
`query_id` is stable. `--include-text` opts back in when you are debugging a
specific query; the file it writes is gitignored, but do not go out of your way
to share it.

## The query file

`queries.jsonl`, one JSON object per line:

```json
{"q": "warehouse lease renewal price", "relevant_doc_ids": ["msgid@example.com"], "note": "why this query is here"}
```

- **`q`** — what you would actually type. Do not write queries that read like
  keyword lists; the point is to measure real use.
- **`relevant_doc_ids`** — every document that genuinely answers the query.
  **Strings only; row ids are rejected**, because they change whenever the index
  is rebuilt and a query file that silently rots is worse than no query file.
  Three forms are accepted:

  | form | example | when |
  |---|---|---|
  | `source:uid` | `gmail:19fe48042d9ff694` | preferred — never ambiguous |
  | bare uid | `19fe48042d9ff694` | fine while unique across sources |
  | `sha256:<hash>` | `sha256:d87a5390…` | keyed on the raw bytes |

  For Gmail the uid is the API message id; for mbox it is the Message-ID. An
  attachment's own doc has the uid `<parent-uid>#att0`. A uid matching more than
  one document is reported as ambiguous rather than guessed at.
- **`note`** — why the query is in the set, and what it is meant to stress. This
  is the part you will thank yourself for in six months when a number moves.

A record whose `relevant_doc_ids` resolve to nothing is **skipped, not scored
zero**: averaging a zero for a record you have not filled in yet would damn the
index for a reason that has nothing to do with retrieval. The summary line
reports how many of the queries were actually scored.

## Filling in the other 95

There are five seed records here, all pointing at `tests/fixtures/sample.mbox`
(`muster sync mbox tests/fixtures/sample.mbox` to make them resolve). They exist
to show the format and to prove the harness works; they are not a benchmark —
five queries over fifteen messages cannot tell you anything.

The rest have to come from you, by hand, against your real mail. A
workable routine:

1. Think of something you actually needed to find recently. Write the query the
   way you would have typed it, before you knew the answer.
2. `muster search "..."` and read the results.
3. Find the documents that truly answer it — including any the search missed.
   `muster search --limit 50`, date filters, and searching for a
   remembered fragment all help. The ones retrieval *missed* are the valuable
   records; a query set built only from what search already finds measures
   nothing.
4. Add the record with every relevant uid, and a note saying what it stresses.

Aim for a spread rather than a hundred of the same shape:

- **Literal** — a distinctive phrase you remember verbatim. BM25 should ace it.
- **Paraphrase** — the idea, in words the message never used. Vectors should win.
- **Entity** — a person, company, invoice number, address.
- **Filename** — an attachment by name, and by its contents.
- **Temporal** — "the quote from last spring", paired with `--since`/`--until`.
- **Multilingual** — German queries against German mail, and cross-language.
- **Multi-answer** — a thread or a recurring bill where several docs are relevant.
- **Near-miss** — a query where an obvious wrong document competes.

Record the recall@10 and MRR in the commit message whenever the number moves,
along with what changed. The absolute value matters far less than the direction.
