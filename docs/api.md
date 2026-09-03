# Muster HTTP API

A thin read-only wrapper over the retrieval functions in `muster/search.py`.
It reimplements nothing: `/search` calls `muster.search.search()`, `/status`
reads the same counts as `muster status`. Anything that changes ranking belongs
in the library, behind the eval harness, not here.

**Started with `muster serve`.** Binds `127.0.0.1:8765` by default. No
authentication, no TLS, no CORS beyond `tauri://localhost` and
`http://localhost:*`. It serves the full contents of a personal mailbox and
must never listen on a routable interface; `--host` exists but refuses anything
other than a loopback address unless `--i-know-what-this-does` is passed.

## Conventions

| | |
|---|---|
| Content type | `application/json; charset=utf-8`, except `/blob/{hash}` |
| Dates | ISO 8601 with offset, exactly as stored: `2026-02-28T19:34:00+00:00` |
| Doc ids | integers, **not stable** across a rebuild — see [Identity](#identity) |
| Errors | HTTP status + `{"error": {"code": "...", "message": "..."}}` |

Error codes: `bad_request`, `not_found`, `unavailable` (503, e.g. no vectors
for the configured model), `internal`.

### Identity

Three ways to name a document, in ascending order of durability. The UI should
prefer `source_uid` for anything it persists and never store `id`.

| field | example | stable across |
|---|---|---|
| `id` | `2391` | this database only — a purge and re-sync renumbers |
| `source_uid` | `gmail:19aa00bb11cc22dd#att18` | re-ingest; the provider's own id |
| `content_hash` | `11111111…` (sha256) | everything; it *is* the bytes |

---

## `GET /search`

| param | type | default | notes |
|---|---|---|---|
| `q` | string | required | free text; a date inside it becomes a filter |
| `mode` | `keyword` \| `vector` \| `hybrid` | `hybrid` | |
| `limit` | int 1–100 | `10` | |
| `since` / `until` | ISO date | — | explicit bounds **override** any date parsed from `q` |
| `min_score` | float | config | `0` disables the confidence floor |
| `all_categories` | bool | `false` | include promotions and social |

### Response

```json
{
  "query": {
    "raw": "warehouse lease renewal september 2024",
    "searched": "warehouse lease renewal",
    "mode": "hybrid",
    "limit": 10,
    "temporal": {
      "matched": "september 2024",
      "since": "2024-09-01T00:00:00+00:00",
      "until": "2024-09-30T23:59:59+00:00"
    },
    "filters": { "since": null, "until": null, "excluded_categories": ["promotions", "social"] }
  },
  "results": [ /* Hit */ ],
  "empty_reason": null,
  "took_ms": 41
}
```

`query.searched` is what actually reached the ranker. It differs from `raw`
whenever a date was lifted out, and the UI should show that — otherwise the
user cannot tell why "september 2024" returned nothing from August.

### Hit

```json
{
  "rank": 1,
  "doc_id": 2391,
  "title": "Offer Letter - Application.pdf",
  "snippet": "… On behalf of [Northwind], Inc. I am pleased to [offer] you the position of [Fullstack] [Developer] …",
  "date": "2026-02-28T19:34:00+00:00",
  "source": "gmail",
  "kind": "attachment",
  "mime": "application/pdf",
  "counterparty": "careers@example.com",
  "sender": { "name": "Northwind Careers", "address": "careers@example.com" },
  "category": null,
  "attachment_count": 0,
  "content_hash": "1111111111111111111111111111111111111111111111111111111111111111",
  "sources": [
    { "doc_id": 2391, "source": "gmail", "source_uid": "19aa00bb11cc22dd#att18",
      "title": "Offer Letter - Application.pdf", "date": "2026-02-28T19:34:00+00:00" },
    { "doc_id": 2369, "source": "gmail", "source_uid": "19aa00bb11cc9f31#att14",
      "title": "Offer Letter - Application.pdf", "date": "2026-02-28T19:34:00+00:00" },
    { "doc_id": 5433, "source": "gmail", "source_uid": "19bb44cc55dd66ee#att2",
      "title": "InternshipOfferLetter.pdf", "date": "2025-07-31T10:02:00+00:00" },
    { "doc_id": 2353, "source": "gmail", "source_uid": "19aa00bb11ccff40#att2",
      "title": "Offer.pdf", "date": "2026-02-28T19:34:00+00:00" }
  ],
  "score": {
    "value": 0.0164,
    "scale": "rrf",
    "of_best": 1.0,
    "arms": { "keyword": 1, "vector": 4 }
  }
}
```

**`sources` is always a list**, length ≥ 1, best-scoring copy first. One entry
per copy of these exact bytes **in this result list** — not in the index. If the
confidence floor cut two of four copies, `sources.length` is 2, because a result
list should describe itself rather than the database behind it. The UI renders
`(xN)` from `sources.length`. A filename that differs between copies — as above,
the same PDF arrived under three names — is worth showing in the actions menu.

`snippet` marks matched terms with `[` and `]`. These are FTS5 delimiters, not
markup; the UI must escape the surrounding text before substituting spans, or a
message body containing `<script>` becomes one.

`kind` is `message` | `attachment` | `other`, derived from mime and uid shape,
so the UI can pick an icon without parsing either.

`score.scale` names what `value` means, because the three modes are not
comparable and a bare number invites the user to compare them:

| scale | mode | range | reading |
|---|---|---|---|
| `bm25` | keyword | 0 → ~45 | higher is better; magnitude tracks index size |
| `cosine` | vector | −1 → 1 | similarity |
| `rrf` | hybrid | ~0.008 → ~0.033 | rank fusion; **magnitude is meaningless** |

`of_best` is `value / results[0].value` — scale-free. A list where everything
sits near `1.0` is a flat tail, which is what a query the index cannot answer
looks like.

`arms` is the rank each arm gave the document *before* fusion, and it is the
real answer to "is retrieval guessing". `{"keyword": 1, "vector": 4}` means both
arms found it independently — strong. `{"vector": 1}` alone means BM25 found
nothing and the result rests entirely on semantic similarity. RRF collapses that
distinction into a single number that cannot express it, so it is carried
alongside. `null` in single-arm modes, which have nothing to agree about.

### Nothing cleared the floor

`results` is `[]` and `empty_reason` is populated. Never an empty list with no
explanation: ten confident-looking wrong answers are worse than none, and so is
silence that looks like an empty mailbox.

```json
{
  "code": "below_confidence_floor",
  "message": "3 documents matched but none scored above the confidence floor.",
  "matched": 3,
  "best_score": 7.21,
  "floor": 12.64,
  "retry": { "min_score": 0 }
}
```

| code | meaning |
|---|---|
| `empty_query` | `q` was blank or had no searchable terms |
| `no_matches` | nothing matched at all, floor or no floor |
| `below_confidence_floor` | candidates existed; all were below the floor |
| `no_vectors_for_model` | `vector` mode, nothing embedded — 503, not empty |

`retry` is a params object the UI can resubmit verbatim to show the weak
matches anyway, which is what the footer hint should offer.

---

## `GET /doc/{id}`

```json
{
  "id": 5430,
  "source": "gmail",
  "source_uid": "19bb44cc55dd66ee",
  "kind": "message",
  "title": "Credit reports and offer letters",
  "date": "2025-07-31T10:02:00+00:00",
  "date_source": "header",
  "mime": "message/rfc822",
  "category": null,
  "sender": { "name": "Dana Okonkwo", "address": "dana@example.com" },
  "recipients": { "to": ["you@example.com"], "cc": [] },
  "counterparty": "landlord@example.com",
  "thread_uid": "19bb44cc55dd66ee",
  "thread": {
    "uid": "19bb44cc55dd66ee",
    "count": 3,
    "messages": [
      { "doc_id": 5428, "title": "Credit reports and offer letters",
        "date": "2025-07-29T09:00:00+00:00",
        "sender": { "name": "Dana Okonkwo", "address": "dana@example.com" },
        "current": false }
    ]
  },
  "provenance": null,
  "text": "Hi, attached are the credit reports and offer letters…",
  "chunk_count": 3,
  "redacted_by": null,
  "excluded_sender": null,
  "blob": {
    "hash": "33333333…", "bytes": 184320, "mime": "message/rfc822",
    "url": "/blob/33333333…", "filename": "Credit reports and offer letters.eml"
  },
  "attachments": [
    { "filename": "CreditReport.pdf", "mime": "application/pdf",
      "content_hash": "22222222…", "bytes": 91234,
      "url": "/blob/22222222…", "doc_id": 5431 }
  ],
  "copies": [ /* same shape as Hit.sources; other docs with this content_hash */ ]
}
```

`text` is the indexed body, reconstructed rather than concatenated from chunks —
chunks overlap by 15%, so joining them repeats every boundary. For a message it
comes from re-parsing the stored blob, which is cheap and guarantees the text
matches what was indexed; for an attachment doc it is the extracted text.

`attachments[].doc_id` is the attachment's own searchable document where one
exists, and `null` where the file carried no text. Following it is how the UI
gets from a message to the PDF the query actually matched.

`recipients` comes from the stored headers. It is not `counterparty`, which is a
single address chosen for ranking — a reader needs to know whether a message was
addressed to them or to three hundred people, and that is not recoverable from
one address.

`thread` is context, not a mailbox: the conversation's messages oldest first,
capped at 50, with `current` marking this one. It is `null` when the thread holds
only this message, because a conversation of one is not context.

`provenance` is present only for documents that are files on disk. `where_from`
and `downloaded_date` are Spotlight's record of the download (`kMDItemWhereFroms`,
`kMDItemDownloadedDate`) and are the join from a downloaded file back to the
message that pointed at it. `null` for mail, which has neither.

`redacted_by` non-null means the body was deliberately not indexed — a login
code or magic link. `text` is empty and the title is masked. The UI should say
so rather than render an empty document.

**404** if the id does not exist. Ids are not stable; a UI holding one across a
re-sync should expect this and fall back to `source_uid`.

---

## `GET /blob/{hash}`

The original bytes, unmodified. `hash` is a full 64-character lowercase sha256;
anything else is **400**, not 404 — the store is content-addressed and a
malformed hash is a caller bug, not a missing file. The hash is validated
against `blobs.relpath_for()` before touching the filesystem, so no caller-
supplied string ever reaches a path join.

```
200  Content-Type: application/pdf
     Content-Length: 91234
     Content-Disposition: inline; filename="CreditReport.pdf"
     Cache-Control: private, max-age=31536000, immutable
     ETag: "11111111…"
```

Immutable caching is safe here in a way it rarely is: the URL *is* the hash of
the body, so the response can never change.

Mime and filename are resolved from whichever `docs` or `attachments` row
references the hash; `application/octet-stream` and the bare hash when nothing
does. `?download=1` switches to `Content-Disposition: attachment`.

**Range requests are supported** — without them a PDF viewer refuses to stream.

---

## `GET /status`

Same numbers as `muster status`, for the UI's footer and empty state.

```json
{
  "database": "/Users/you/.muster/muster.db",
  "blob_dir": "/Users/you/.muster/blobs",
  "schema": { "applied": ["001_initial", "…", "005_fts_party_column"], "pending": [] },
  "counts": { "docs": 8038, "chunks": 16078, "vectors": 16078, "attachments": 681 },
  "health": { "unfindable": 0, "redacted": 94, "chunks_awaiting_vectors": 0 },
  "by_kind": { "message": 5961, "attachment": 310 },
  "by_category": { "updates": 5074, "personal": 495, "promotions": 470, "uncategorised": 217 },
  "date_range": { "earliest": "2023-08-09T04:35:54+00:00", "latest": "2026-08-09T05:30:32+00:00" },
  "embedding": { "model_id": "BAAI/bge-m3", "dimensions": 1024 },
  "search_defaults": {
    "modes": ["keyword", "vector", "hybrid"],
    "default_mode": "hybrid",
    "excluded_categories": ["promotions", "social"]
  },
  "corpus_fingerprint": "81af6e3cf91dc885"
}
```

`blob_dir` is the root of the content-addressed store. A blob's path is
`{blob_dir}/{hash[:2]}/{hash}` — the UI needs it to reveal a file in Finder,
which takes a path and not a URL.

`chunks_awaiting_vectors` above zero means the vector arm is incomplete and
hybrid is quietly degraded. The UI should surface it; it is the single most
useful "why is search bad right now" signal.

---

## Decisions on record

**`sources` counts this result list, not the index.** A list that claims four
copies while showing the two that survived the floor is describing a database
the user cannot see. Costs nothing: the copies are already in hand.

**Arm ranks come from the fusion step, not a second query.** `reciprocal_rank_fusion`
has both ranked lists in front of it and was discarding the provenance on the
last line; it now carries it through. No extra retrieval, no doubled cost.

**`/doc/{id}` re-parses the blob for `text`.** ~5 ms and exact. Storing the body
at ingest would be a schema change duplicating bytes already on disk.

## What I am not building

No write endpoints. Sync, embed, reparse and purge stay on the CLI: they are
long-running, they mutate an archive that is expensive to rebuild, and a UI
button that silently starts a two-hour backfill is a mistake waiting to happen.
`/status` is enough for the UI to tell the user to go and run one.
