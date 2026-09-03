/**
 * Fixture responses shaped exactly like docs/api.md.
 *
 * The frontend is built against these first so that layout, keyboard handling
 * and the empty states can be exercised without a running server — including
 * the states that are hard to provoke on demand, like the confidence floor
 * rejecting everything.
 *
 * Content is synthetic. Nothing here comes from a real mailbox.
 */
import type { DocResponse, SearchResponse, StatusResponse } from "./api";

const hit = (over: Partial<SearchResponse["results"][number]> = {}) => ({
  rank: 1,
  doc_id: 101,
  title: "Warehouse lease renewal",
  snippet: "Confirming the [warehouse] [lease] [renewal] at 4200 EUR per month, starting 1 July.",
  date: "2026-03-16T09:14:00+00:00",
  source: "gmail",
  kind: "message" as const,
  mime: "message/rfc822",
  counterparty: "bob@example.com",
  sender: { name: "Bob Fischer", address: "bob@example.com" },
  category: null,
  attachment_count: 0,
  content_hash: "1111111111111111111111111111111111111111111111111111111111111111",
  sources: [
    {
      doc_id: 101,
      source: "gmail",
      source_uid: "19aa00bb11cc22dd",
      title: "Warehouse lease renewal",
      date: "2026-03-16T09:14:00+00:00",
    },
  ],
  score: { value: 0.0328, scale: "rrf" as const, of_best: 1.0, arms: { keyword: 1, vector: 2 } },
  ...over,
});

export const mockSearch: SearchResponse = {
  query: {
    raw: "warehouse lease renewal",
    searched: "warehouse lease renewal",
    mode: "hybrid",
    limit: 10,
    temporal: null,
    filters: { since: null, until: null, excluded_categories: ["promotions", "social"] },
  },
  results: [
    hit(),
    hit({
      rank: 2,
      doc_id: 102,
      title: "Offer Letter - Application.pdf",
      kind: "attachment",
      mime: "application/pdf",
      snippet: "On behalf of [Northwind], Inc. I am pleased to [offer] you the position of Fullstack Developer.",
      date: "2026-02-28T19:34:00+00:00",
      attachment_count: 0,
      content_hash: "2222222222222222222222222222222222222222222222222222222222222222",
      // Four copies of one file: the case that motivated collapsing at all.
      sources: [
        { doc_id: 102, source: "gmail", source_uid: "19aa00bb11cc9f31#att18", title: "Offer Letter - Application.pdf", date: "2026-02-28T19:34:00+00:00" },
        { doc_id: 103, source: "gmail", source_uid: "19aa00bb11ccff40#att14", title: "Offer Letter - Application.pdf", date: "2026-02-27T11:02:00+00:00" },
        { doc_id: 104, source: "gmail", source_uid: "19bb44cc55dd66ee#att2", title: "InternshipOfferLetter.pdf", date: "2025-07-31T10:02:00+00:00" },
        { doc_id: 105, source: "gmail", source_uid: "19bb44cc55dd7711#att2", title: "Offer.pdf", date: "2025-07-30T09:00:00+00:00" },
      ],
      score: { value: 0.0164, scale: "rrf", of_best: 0.5, arms: { keyword: 3 } },
    }),
    hit({
      rank: 3,
      doc_id: 106,
      title: "Rechnung 2026-0042",
      snippet: "Attachments: [Rechnung]_2026-0042.pdf. Sehr geehrter Herr, anbei die Rechnung fuer Maerz.",
      date: "2026-03-10T07:45:00+00:00",
      attachment_count: 1,
      sender: { name: "Buchhaltung", address: "billing@lieferant.example" },
      counterparty: "billing@lieferant.example",
      category: "updates",
      content_hash: "3333333333333333333333333333333333333333333333333333333333333333",
      sources: [{ doc_id: 106, source: "gmail", source_uid: "19cc00dd22ee33ff", title: "Rechnung 2026-0042", date: "2026-03-10T07:45:00+00:00" }],
      score: { value: 0.0151, scale: "rrf", of_best: 0.46, arms: { vector: 4 } },
    }),
  ],
  empty_reason: null,
  took_ms: 41,
};

export const mockTemporal: SearchResponse = {
  ...mockSearch,
  query: {
    ...mockSearch.query,
    raw: "payslip july 2025",
    searched: "payslip",
    temporal: {
      matched: "july 2025",
      since: "2025-07-01T00:00:00+00:00",
      until: "2025-07-31T23:59:59+00:00",
    },
  },
};

export const mockBelowFloor: SearchResponse = {
  query: { ...mockSearch.query, raw: "lost jacket", searched: "lost jacket" },
  results: [],
  empty_reason: {
    code: "below_confidence_floor",
    message: "3 document(s) matched but none scored above the confidence floor.",
    matched: 3,
    best_score: 7.21,
    floor: 12.64,
    retry: { min_score: 0 },
  },
  took_ms: 22,
};

export const mockNoMatches: SearchResponse = {
  query: { ...mockSearch.query, raw: "zzzqqxx", searched: "zzzqqxx" },
  results: [],
  empty_reason: {
    code: "no_matches",
    message: "Nothing in the index matches this query.",
    matched: 0,
    best_score: null,
    floor: 12.64,
    retry: null,
  },
  took_ms: 9,
};

export const mockDoc: DocResponse = {
  id: 101,
  source: "gmail",
  source_uid: "19aa00bb11cc22dd",
  kind: "message",
  title: "Warehouse lease renewal",
  date: "2026-03-16T09:14:00+00:00",
  date_source: "header",
  mime: "message/rfc822",
  category: null,
  sender: { name: "Bob Fischer", address: "bob@example.com" },
  recipients: { to: ["you@example.com"], cc: ["legal@example.com"] },
  counterparty: "bob@example.com",
  thread_uid: "19aa00bb11cc22dd",
  thread: {
    uid: "19aa00bb11cc22dd",
    count: 3,
    messages: [
      { doc_id: 99, title: "Warehouse lease renewal", date: "2026-03-14T11:00:00+00:00",
        sender: { name: "Sam", address: "you@example.com" }, current: false },
      { doc_id: 101, title: "Warehouse lease renewal", date: "2026-03-16T09:14:00+00:00",
        sender: { name: "Bob Fischer", address: "bob@example.com" }, current: true },
      { doc_id: 104, title: "Re: Warehouse lease renewal", date: "2026-03-18T15:40:00+00:00",
        sender: { name: "Sam", address: "you@example.com" }, current: false },
    ],
  },
  provenance: null,
  text:
    "Hi,\n\nConfirming the warehouse lease renewal at 4200 EUR per month, " +
    "starting 1 July 2026. The signed agreement is attached, along with the " +
    "schedule of works.\n\nThe lease term runs for thirty-six months with a " +
    "break clause at twenty-four. Rent is reviewed annually against CPI.\n\n" +
    "Best,\nBob",
  chunk_count: 1,
  redacted_by: null,
  excluded_sender: null,
  blob: {
    hash: "1111111111111111111111111111111111111111111111111111111111111111",
    bytes: 18432,
    mime: "message/rfc822",
    url: "/blob/1111111111111111111111111111111111111111111111111111111111111111",
    filename: "Warehouse lease renewal.eml",
  },
  attachments: [
    {
      filename: "Lease-Agreement-2026.pdf",
      mime: "application/pdf",
      content_hash: "2222222222222222222222222222222222222222222222222222222222222222",
      bytes: 918234,
      url: "/blob/2222222222222222222222222222222222222222222222222222222222222222",
      doc_id: 102,
    },
    {
      filename: "signature.png",
      mime: "image/png",
      content_hash: "3333333333333333333333333333333333333333333333333333333333333333",
      bytes: 4821,
      url: "/blob/3333333333333333333333333333333333333333333333333333333333333333",
      doc_id: null,
    },
  ],
  copies: [],
};

/** A file on disk, so the provenance block and its date sourcing are reachable. */
export const mockFileDoc: DocResponse = {
  ...mockDoc,
  id: 102,
  source: "file",
  source_uid: "/Users/you/Downloads/Lease-Agreement-2026.pdf",
  kind: "attachment",
  title: "Lease-Agreement-2026.pdf",
  date_source: "pdfmeta",
  mime: "application/pdf",
  sender: null,
  recipients: { to: [], cc: [] },
  thread: null,
  provenance: {
    path: "/Users/you/Downloads/Lease-Agreement-2026.pdf",
    where_from: ["https://mail.google.com/", "https://example.com/lease.pdf"],
    downloaded_date: "2026-03-16T09:20:00+00:00",
    size_bytes: 918234,
    modified: "2026-03-16T09:20:00+00:00",
    created: "2026-03-16T09:20:00+00:00",
  },
  text:
    "LEASE AGREEMENT\n\nThis warehouse lease is made between the parties named " +
    "below for a term of thirty-six months commencing 1 July 2026.",
  attachments: [],
};

export const mockStatus: StatusResponse = {
  database: "/Users/you/.muster/muster.db",
  blob_dir: "/Users/you/.muster/blobs",
  schema: { applied: ["001_initial", "005_fts_party_column"], pending: [] },
  counts: { docs: 8038, chunks: 16078, vectors: 16078, attachments: 681 },
  health: { unfindable: 0, redacted: 94, chunks_awaiting_vectors: 0 },
  by_kind: { message: 5961, attachment: 310 },
  by_category: { updates: 5074, personal: 495 },
  date_range: { earliest: "2023-08-09T04:35:54+00:00", latest: "2026-08-09T05:30:32+00:00" },
  embedding: { model_id: "BAAI/bge-m3", dimensions: 1024 },
  search_defaults: {
    modes: ["keyword", "vector", "hybrid"],
    default_mode: "hybrid",
    excluded_categories: ["promotions", "social"],
  },
  corpus_fingerprint: "81af6e3cf91dc885",
};

/** Routes a query string to whichever fixture demonstrates the state. */
export function mockSearchFor(q: string): SearchResponse {
  const query = q.trim().toLowerCase();
  if (!query) return { ...mockNoMatches, empty_reason: { ...mockNoMatches.empty_reason!, code: "empty_query", message: "The query has no searchable terms." } };
  if (query.includes("zzz")) return mockNoMatches;
  if (query.includes("jacket")) return mockBelowFloor;
  if (query.includes("payslip")) return mockTemporal;
  return { ...mockSearch, query: { ...mockSearch.query, raw: q, searched: q } };
}
