/**
 * The shapes in docs/api.md, and the client that fetches them.
 *
 * These types are hand-written against that document rather than generated,
 * so a mismatch shows up as a compile error here rather than as `undefined`
 * three components deep.
 */

export type Mode = "keyword" | "vector" | "hybrid";
export type Kind = "message" | "attachment" | "other";
export type Scale = "bm25" | "cosine" | "rrf" | "unknown";

export interface SourceRef {
  doc_id: number;
  source: string;
  source_uid: string;
  title: string | null;
  date: string;
}

export interface Score {
  value: number;
  scale: Scale;
  /** value / results[0].value. A list that is flat near 1.0 is a bad list. */
  of_best: number | null;
  /** Rank each arm gave this before fusion. Null in single-arm modes. */
  arms: Record<string, number> | null;
}

export interface Hit {
  rank: number;
  doc_id: number;
  title: string | null;
  snippet: string;
  date: string;
  source: string;
  kind: Kind;
  mime: string | null;
  counterparty: string | null;
  sender: { name: string | null; address: string | null } | null;
  category: string | null;
  attachment_count: number;
  content_hash: string | null;
  /** True when /thumb will return an image. Saves a request per row. */
  preview: boolean;
  /** Copies in *this result list*, best first. Renders as (xN). */
  sources: SourceRef[];
  score: Score;
}

export type EmptyCode =
  | "empty_query"
  | "no_matches"
  | "below_confidence_floor"
  | "no_vectors_for_model";

export interface EmptyReason {
  code: EmptyCode;
  message: string;
  matched: number;
  best_score: number | null;
  floor: number | null;
  /** Params to resubmit verbatim to see the weak matches anyway. */
  retry: Record<string, string | number> | null;
}

export interface SearchResponse {
  query: {
    raw: string;
    searched: string;
    mode: Mode;
    limit: number;
    temporal: { matched: string; since: string; until: string } | null;
    filters: {
      since: string | null;
      until: string | null;
      excluded_categories: string[];
    };
  };
  results: Hit[];
  empty_reason: EmptyReason | null;
  took_ms: number;
}

export interface Attachment {
  filename: string | null;
  mime: string | null;
  content_hash: string;
  bytes: number | null;
  url: string;
  doc_id: number | null;
}

export interface ThreadMessage {
  doc_id: number;
  title: string | null;
  date: string;
  sender: { name: string | null; address: string | null } | null;
  /** The message being viewed. Exactly one is true. */
  current: boolean;
}

/** Where a file on disk came from. Null for mail, which has no path. */
export interface Provenance {
  path: string;
  /** kMDItemWhereFroms — the URL, and often the page that linked to it. */
  where_from: string[];
  downloaded_date: string | null;
  size_bytes: number | null;
  modified: string | null;
  created: string | null;
}

export interface DocResponse {
  id: number;
  source: string;
  source_uid: string;
  kind: Kind;
  title: string | null;
  date: string;
  date_source: string;
  mime: string | null;
  category: string | null;
  sender: { name: string | null; address: string | null } | null;
  recipients: { to: string[]; cc: string[] };
  counterparty: string | null;
  thread_uid: string | null;
  thread: { uid: string; count: number; messages: ThreadMessage[] } | null;
  provenance: Provenance | null;
  text: string;
  chunk_count: number;
  redacted_by: string | null;
  excluded_sender: string | null;
  blob: { hash: string; bytes: number | null; mime: string | null; url: string; filename: string } | null;
  attachments: Attachment[];
  copies: SourceRef[];
}

export interface StatusResponse {
  database: string;
  /** Root of the content-addressed store: {blob_dir}/{hash[:2]}/{hash}. */
  blob_dir: string;
  schema: { applied: string[]; pending: string[] };
  counts: { docs: number; chunks: number; vectors: number; attachments: number };
  health: { unfindable: number; redacted: number; chunks_awaiting_vectors: number };
  by_kind: Record<string, number>;
  by_category: Record<string, number>;
  date_range: { earliest: string | null; latest: string | null };
  embedding: { model_id: string; dimensions: number | null; ready?: boolean };
  search_defaults: { modes: Mode[]; default_mode: Mode; excluded_categories: string[] };
  corpus_fingerprint: string;
}

export interface SyncJob {
  job: string;
  state: "running" | "done" | "skipped" | "failed";
  /** Present once finished: the same one-line summary the CLI prints. */
  result?: string | null;
  error?: string | null;
  started?: boolean;
}

/** A message as it was sent, sanitised for display. */
export interface MessageBody {
  is_html: boolean;
  html: string;
  /** Remote images dropped — a pixel that fails to load has still been asked for. */
  images_removed: number;
}

/** A directory a query might have meant. Never mixed into results. */
export interface FolderHit {
  path: string;
  /** Written the way a person would say it: ~/GitHub/muster. */
  display: string;
  name: string;
  depth: number;
}

// --- processes -------------------------------------------------------------
//
// The gather artifact is versioned because other features will consume it; open
// items are one renderer over it, not the only one.

export interface ProcessSummary {
  slug: string;
  name: string;
  created_at: string;
  updated_at: string;
  anchors: { kind: string; value: string }[];
}

export type OpenItemKind = "deadline_near" | "unsatisfied" | "unanswered";

export interface OpenItem {
  kind: OpenItemKind;
  what: string;
  when: string | null;
  counterparty: string | null;
  detail: string | null;
  /** Every item cites its sources; an item without them is an assertion. */
  doc_ids: number[];
  /** Stable identity of the obligation, independent of wording. */
  key: string;
  /** Set when settled by hand: "done" or "not_applicable". */
  settled: string | null;
  settled_at: string | null;
  /** Documents arrived after it was settled; worth a second look. */
  contradicted: boolean;
  /** Deadline passed with nothing since. Asked once, not repeated. */
  needs_confirmation: boolean;
  last_activity: string | null;
}

export interface AnchorProposal {
  id: string;
  kind: string;
  value: string;
  count: number;
  example: string;
}

export interface TimelineEntry {
  doc_id: number;
  date: string | null;
  title: string | null;
  source: string;
  kind: Kind;
  counterparty: string | null;
  /** anchor:<kind> | edge:<name> | seed_only. Never omitted. */
  reason: string;
  via: string | null;
}

export interface ProcessView {
  process: { slug: string; name: string; anchors: { kind: string; value: string }[] };
  gather: {
    version: number;
    size: number;
    capped: boolean;
    dropped: number;
    depth_reached: number;
    /** Documents the seed found that no anchor explains — a missing anchor. */
    seed_only: number;
  };
  open_items: {
    version: number;
    counts: {
      requests_total: number;
      requests_satisfied: number;
      requests_open: number;
      settled_by_me: number;
      settled_contradicted: number;
      awaiting_confirmation: number;
      documents_considered: number;
      documents_unaccounted: number;
      /** Read and not understood, versus not looked at yet. Different problems. */
      documents_unread: number;
      documents_unexamined: number;
      /** Deadlines already past: not "next", but not nothing either. */
      overdue: number;
      /** Obligations found in attachments rather than correspondence. */
      asks_skipped: number;
      open_without_candidates: number;
      duplicates_collapsed: number;
    };
    next_deadline: string | null;
    items: OpenItem[];
    settled: OpenItem[];
    unaccounted_doc_ids: number[];
  };
  extraction: { model_id: string; version: number; pending: number };
  timeline: TimelineEntry[];
}

export interface HomeProcess {
  slug: string;
  name: string;
  documents: number;
  anchors: number;
  open: number;
  settled_by_me: number;
  awaiting_confirmation: number;
  next_deadline: string | null;
  /** True when some judgement was uncached, so `open` is an upper bound. */
  estimated: boolean;
  extraction_pending: number;
  last_activity: string | null;
}

export interface HomeView {
  processes: HomeProcess[];
  index: {
    documents: number;
    missing: number;
    last_sync: string | null;
    last_result: string | null;
    embedding_ready: boolean;
  };
}

export interface ExtractJob {
  job: string | null;
  process: string;
  state: "running" | "done" | "stopped" | "failed";
  done: number;
  total: number;
  extracted: number;
  failed: number;
  error: string | null;
  started?: boolean;
}

export class ApiError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

const BASE = import.meta.env.VITE_MUSTER_API ?? "http://127.0.0.1:8765";

async function post<T>(path: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(new URL(path, BASE), { method: "POST" });
  } catch {
    throw new ApiError("unreachable", `No server at ${BASE}. Run \`muster serve\`.`, 0);
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiError(
      body?.error?.code ?? "http_error",
      body?.error?.message ?? `${response.status} ${response.statusText}`,
      response.status,
    );
  }
  return (await response.json()) as T;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(new URL(path, BASE), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    throw new ApiError("unreachable", `No server at ${BASE}. Run \`muster serve\`.`, 0);
  }
  if (!response.ok) {
    const failed = await response.json().catch(() => null);
    throw new ApiError(
      failed?.error?.code ?? "http_error",
      failed?.error?.message ?? `${response.status} ${response.statusText}`,
      response.status,
    );
  }
  return (await response.json()) as T;
}

async function del<T>(path: string): Promise<T> {
  const response = await fetch(new URL(path, BASE), { method: "DELETE" });
  if (!response.ok) throw new ApiError("http_error", `${response.status}`, response.status);
  return (await response.json()) as T;
}

async function get<T>(path: string, params?: Record<string, unknown>): Promise<T> {
  const url = new URL(path, BASE);
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value !== undefined && value !== null && value !== "") {
      url.searchParams.set(key, String(value));
    }
  }

  let response: Response;
  try {
    response = await fetch(url);
  } catch (cause) {
    // The server is a separate process the user starts themselves, so "not
    // running" is an ordinary state rather than an exceptional one.
    throw new ApiError("unreachable", `No server at ${BASE}. Run \`muster serve\`.`, 0);
  }

  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const error = body?.error;
    throw new ApiError(
      error?.code ?? "http_error",
      error?.message ?? `${response.status} ${response.statusText}`,
      response.status,
    );
  }
  return (await response.json()) as T;
}

export const api = {
  search: (params: {
    q: string;
    mode?: Mode;
    limit?: number;
    since?: string;
    until?: string;
    min_score?: number;
    all_categories?: boolean;
  }) => get<SearchResponse>("/search", params),

  doc: (id: number) => get<DocResponse>(`/doc/${id}`),
  folders: (q: string) => get<{ folders: FolderHit[] }>("/folders", { q }),

  home: () => get<HomeView>("/home"),
  extract: (slug: string) => post<ExtractJob>(`/process/${encodeURIComponent(slug)}/extract`),
  extractJob: (job: string) => get<ExtractJob>(`/process/extract/${encodeURIComponent(job)}`),
  stopExtract: (job: string) =>
    post<ExtractJob>(`/process/extract/${encodeURIComponent(job)}/stop`),
  processes: () => get<{ processes: ProcessSummary[] }>("/process"),
  process: (slug: string) => get<ProcessView>(`/process/${slug}`),
  /** Candidates for a name. Creates nothing — the accept step is the point. */
  proposeProcess: (name: string) =>
    post<{ name: string; slug: string; proposals: AnchorProposal[] }>(
      `/process/propose?name=${encodeURIComponent(name)}`,
    ),
  confirmProcess: (name: string, anchors: string[]) =>
    postJson<{ slug: string; name: string }>("/process/confirm", { name, anchors }),

  addAnchor: (slug: string, kind: string, value: string) =>
    postJson<{ added: boolean }>(`/process/${slug}/anchor`, { kind, value }),
  removeAnchor: (slug: string, kind: string, value: string) =>
    del<{ removed: boolean }>(
      `/process/${slug}/anchor?kind=${encodeURIComponent(kind)}&value=${encodeURIComponent(value)}`,
    ),

  dismissItem: (slug: string, what: string, opts: { undo?: boolean; verdict?: string } = {}) =>
    postJson<{ settled: boolean }>(`/process/${slug}/dismiss`, { what, ...opts }),
  body: (id: number) => get<MessageBody>(`/doc/${id}/body`),

  /** Start an incremental sync. Returns at once; poll syncJob for the outcome. */
  refresh: () => post<SyncJob>("/sync/refresh"),
  syncJob: (id: string) => get<SyncJob>(`/sync/jobs/${id}`),
  status: () => get<StatusResponse>("/status"),
  blobUrl: (hash: string, download = false) =>
    `${BASE}/blob/${hash}${download ? "?download=1" : ""}`,

  /** Where those bytes actually live, for Finder. */
  blobPath: (blobDir: string, hash: string) =>
    `${blobDir}/${hash.slice(0, 2)}/${hash}`,

  /** A rendered preview: first page of a PDF, or a downscaled image. */
  thumbUrl: (docId: number, size = 96) => `${BASE}/thumb/${docId}?size=${size}`,

  /** Absolute URL for a doc's own bytes, blob-backed or on disk. */
  bytesUrl: (path: string) => `${BASE}${path}`,
};
