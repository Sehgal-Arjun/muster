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
  "preview": true,
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

`preview` is true when `GET /thumb/{doc_id}` will return an image — PDFs and
images. Carried on the hit so a result list does not fire a request per row to
discover that most documents have nothing to show.

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

## `GET /file/{id}`

The bytes of an indexed file, read from where it lives on disk. Documents from
the filesystem connector keep no blob — the file *is* the original — so this is
what `doc.blob.url` points at for them.

Takes a document id, never a path: there is no string a caller can supply that
makes this read something that was not indexed. Mail is refused even when stored
under the same source, because a message that came out of an mbox is not a
document on disk.

**404** if the id is not a filesystem document. **410** if the file has been
moved or deleted since the last walk — the index outlives the file, and that is
the honest answer rather than a 500. Supports range requests, so a PDF streams.

`Cache-Control: private, no-cache` — unlike a blob, a path is not a hash and the
bytes behind it can change.

---

## `GET /thumb/{id}`

A small PNG preview: the first page of a PDF, or a downscaled image. `size` is
the long edge, 16–512, default 96.

Rendered on demand and cached by content hash, so it is immutable once made.
**404** when the document has nothing worth showing, which is what `preview` on
a hit tells the UI in advance.

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

## `POST /sync/refresh`

The one endpoint that writes. Starts an incremental Gmail sync and returns
immediately:

```json
{ "job": "sync-1-1775000000", "state": "running", "started": true }
```

`started` is `false` when a sync was already in flight — the job in flight is
returned rather than a second one queued behind it. Poll it:

```
GET /sync/jobs/{job}
{ "job": "sync-1-…", "state": "done", "result": "incremental: 3 indexed, 7 chunks", "error": null }
```

`state` is `running`, `done`, `skipped` or `failed`. `skipped` means the
scheduled job holds the sync lock, which is not an error. `result` is the same
one-line summary `muster sync --incremental` prints.

It can only pull new mail. Embed, reparse and purge stay on the command line:
they run for hours and destroy work, and a button that starts them silently is a
mistake waiting to happen. Pulling mail is neither — it is bounded by what has
arrived.

The panel calls this when it opens, at most once every two minutes, and never
waits on it.

---

## Processes and gather

A process is the thing a pile of documents is *about*. Search answers "find me
the thing"; these answer "what is the state of the thing".

### `GET /process`

```json
{ "processes": [ { "slug": "j-1-visa-application", "name": "J-1 visa application",
                   "created_at": "…", "updated_at": "…",
                   "anchors": [ { "kind": "domain", "value": "sponsor.example" } ] } ] }
```

### `POST /process/propose?name=…` and `POST /process/confirm`

Two steps, because the accept/reject decision is the whole reason anchors can be
trusted. An anchor is permanent and pulls in documents forever, so it should be
something somebody chose rather than a threshold somebody set.

`propose` creates nothing and returns candidates:

```json
{ "slug": "tum-enrolment", "name": "TUM enrolment",
  "proposals": [ { "id": "domain:sponsor.example", "kind": "domain",
                   "value": "sponsor.example", "count": 31,
                   "example": "J-1 Visa Sponsorship Approved" } ] }
```

`confirm` takes `{ "name": …, "anchors": ["domain:sponsor.example", …] }` and
creates the process with exactly those.

`POST /process/{slug}/anchor` and `DELETE /process/{slug}/anchor?kind=&value=`
add and remove anchors afterwards; both return the resulting anchor list, and the
next gather reflects them immediately.

### `POST /process/{slug}/dismiss`

`{ "what": "the portal declaration", "verdict": "done" }`, or `{"undo": true}`.

Records that the user settled an item themselves. **Stored as evidence, not as a
delete**: most obligations in a real process are discharged somewhere this index
cannot see — a web portal, a phone call — and an absence would simply be
re-derived as open on the next run. The item moves to `open_items.settled` with
its date, keeps its citations, and is keyed on the request's words rather than
its wording, so settling one phrasing settles the reminder that restated it.

`known_docs` records how large the set was at the time. If documents arrive
afterwards, the item comes back marked `contradicted` rather than being silently
kept or silently dropped.

### `GET /process/{slug}/gather` — the artifact

Versioned, because other features consume it. Open items are one renderer over
this, not the only one.

```json
{
  "version": 1,
  "process": { "slug": "…", "name": "…" },
  "anchors": [ { "kind": "domain", "value": "sponsor.example" } ],
  "doc_ids": [ 5867, 3135, … ],
  "inclusions": [ { "doc_id": 5867, "reason": "seed_only", "via": null },
                  { "doc_id": 3135, "reason": "anchor:domain", "via": "sponsor.example" } ],
  "seed_only": [ 5867 ],
  "capped": false, "dropped": 0, "depth_reached": 2, "size": 88
}
```

`doc_ids` is **chronological by `doc_date`, never by relevance**: state is
temporal, and relevance ordering destroys the sequence that shows a deposit was
requested in April and paid in May.

`inclusions` explains every document. `reason` is `anchor:<kind>`, `edge:<name>`
or `seed_only`. Documents included by the seed alone are also listed under
`seed_only` — that count is how a missing anchor announces itself.

Expansion follows exact joins only (threads, anchored counterparties,
attachments, download provenance, reference numbers, folders), so the same
inputs give the same set every time. Nothing uses similarity.

`dropped` is how many documents the cap discarded, keeping the most recent.

### `GET /process/{slug}`

Timeline, open items, and what could not be accounted for.

```json
{
  "process": { … },
  "gather": { "version": 1, "size": 88, "capped": false, "dropped": 0,
              "depth_reached": 2, "seed_only": 8 },
  "open_items": {
    "version": 1,
    "counts": { "requests_total": 68, "requests_satisfied": 2, "requests_open": 66,
                "documents_considered": 88, "documents_unaccounted": 0,
                "open_without_candidates": 53, "duplicates_collapsed": 13 },
    "next_deadline": "2025-12-25",
    "items": [ { "kind": "unsatisfied", "what": "proof of health insurance",
                 "when": null, "counterparty": "clerk@sponsor.example",
                 "detail": "no candidate fulfilment in the archive",
                 "doc_ids": [ 2303, 2308 ] } ],
    "unaccounted_doc_ids": []
  },
  "extraction": { "model_id": "qwen2.5:14b", "version": 1, "pending": 0 },
  "timeline": [ { "doc_id": 5867, "date": "…", "title": "…", "source": "gmail",
                  "kind": "message", "counterparty": "…",
                  "reason": "seed_only", "via": null } ]
}
```

Every count is computed. Nothing on this endpoint asks a model how many of
something there are; the only model judgement in the pipeline is whether one
named thing satisfies one named request, and that is cached per pair.

`items[].kind` is `deadline_near`, `unsatisfied` or `unanswered`, kept distinct
because flattening them loses what makes each actionable. Every item cites the
documents it came from.

Three counts exist to stop the report being confidently wrong:

- `documents_unaccounted` — documents in the set the extractor could not read.
- `documents_unread` / `documents_unexamined` — attempted and failed, versus never
  attempted. One is a broken document, the other is unfinished work.
- `overdue` — deadlines already past. Not `next_deadline`, which is the next one
  *coming*; a date that has gone is either missed or moot.
- `asks_skipped` — obligations found in attachments rather than correspondence,
  and declined. A photographed consular form lists every document anyone might
  need; none of it was asked of this reader.
- `not_obliged` — requests judged to be invitations, surveys or advice rather than
  obligations. Reported because it is a filter, and a filter nobody can see is
  indistinguishable from a bug.
  Reported, never silently omitted.
- `open_without_candidates` — open items for which the archive holds *no*
  plausible fulfilment, as against ones checked and rejected. On a real process
  this was 53 of 66: those were done through a web portal this index cannot see,
  and without the distinction the answer reads as "you owe 66 things".
- `extraction.pending` — documents not yet examined at all.
- `settled_by_me` / `settled_contradicted` — items the user closed by hand, and
  how many of those have new documents bearing on them.
- `awaiting_confirmation` — items whose deadline passed with no activity since.
  Each carries `needs_confirmation` and `last_activity`, so a client can ask
  once — "due 17 Aug, nothing since 2 Apr; done, or still open?" — rather than
  repeating the line forever, which is how a list teaches somebody to stop
  reading it.

Anything the model concludes is derived and stays out of search.

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

## `GET /home`

What is going on, across everything — the panel's resting state, shown instead
of an empty screen when nothing has been typed.

```json
{
  "processes": [
    {
      "slug": "warehouse-lease",
      "name": "Warehouse lease",
      "documents": 194,
      "anchors": 3,
      "open": 12,
      "settled_by_me": 1,
      "awaiting_confirmation": 2,
      "next_deadline": "2026-09-01",
      "estimated": false,
      "extraction_pending": 0,
      "last_activity": "2026-08-08T10:00:07+00:00"
    }
  ],
  "index": {
    "documents": 11075,
    "missing": 0,
    "last_sync": "2026-08-08T10:02:11+00:00",
    "last_result": "ok",
    "embedding_ready": true
  }
}
```

Sorted soonest-deadline-first; a process with no deadline falls to the end.

Every summary here is read from the *same payload* `GET /process/{slug}` serves,
so the overview and the detail screen cannot disagree about a number, and opening
a process the overview has already listed is a cache read rather than a rebuild.
Views are reconstructed by a background thread at startup and again after a sync
or an extraction finishes; they are invalidated by any new document, any
extraction, an anchor change, or a dismissal. Measured warm: `/home` **39ms**,
opening a process **6–10ms**.

`open` is the number of items the process view would list — after duplicates are
collapsed and dismissals applied — and not `counts.requests_open`, which is
larger and answers a different question. Two screens disagreeing about the same
quantity is worse than one of them being approximate.

Built from cached judgements only, so drawing the panel never waits on an
inference. A process nobody has opened yet has no cached verdicts, so every
request counts as open and `estimated` is `true`: the number is an upper bound
that says so. Opening the process once makes its judgements, and every overview
afterwards is exact and free.

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
