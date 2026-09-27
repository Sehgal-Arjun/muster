import { useCallback, useEffect, useState } from "react";
import { ApiError, api, type OpenItem, type ProcessView } from "../api";

/**
 * Gather mode: the state of a process rather than a list of matches.
 *
 * Everything numeric on this screen is computed by the server — counts, dates,
 * "6 of 9". Nothing here generates a number, which is why the header can be
 * trusted at a glance.
 *
 * The uncomfortable numbers are shown as prominently as the flattering ones. A
 * process with 53 open items that the archive holds no evidence about says so, in
 * the header, because the alternative is a confident answer that is wrong.
 */

const KIND_LABEL: Record<string, string> = {
  deadline_near: "Deadlines",
  unsatisfied: "Waiting on me",
  unanswered: "I said I would",
};

const KIND_ORDER = ["deadline_near", "unsatisfied", "unanswered"];

function shortDate(iso: string | null): string {
  if (!iso) return "";
  const when = new Date(iso);
  return Number.isNaN(when.getTime())
    ? iso.slice(0, 10)
    : when.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

export function GatherView({
  slug,
  query,
  onOpenDoc,
}: {
  slug: string | null;
  /** Used only to offer creating a process when none exists yet. */
  query: string;
  onOpenDoc: (docId: number) => void;
}) {
  const [view, setView] = useState<ProcessView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    let live = true;
    setView(null);
    setError(null);
    if (!slug) return;
    api
      .process(slug)
      .then((body) => live && setView(body))
      .catch((cause) =>
        live && setError(cause instanceof ApiError ? cause.message : String(cause)),
      );
    return () => {
      live = false;
    };
  }, [slug]);

  const create = useCallback(async () => {
    setCreating(true);
    try {
      const made = await api.createProcess(query);
      setView(await api.process(made.slug));
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : String(cause));
    } finally {
      setCreating(false);
    }
  }, [query]);

  if (!slug) {
    return (
      <div className="empty">
        <div className="empty-headline">No process for “{query}”</div>
        <div className="empty-detail dim">
          Gather reconstructs the state of a process — what was asked, what was
          sent, what is still outstanding.
        </div>
        <button className="link" onClick={() => void create()} disabled={creating}>
          {creating ? "Gathering…" : `Create a process from “${query}”`}
        </button>
      </div>
    );
  }

  if (error) {
    return (
      <div className="empty">
        <div className="empty-headline">Cannot gather this process</div>
        <div className="empty-detail">{error}</div>
        <div className="empty-detail dim">Escape returns to the results.</div>
      </div>
    );
  }

  if (!view) return <div className="doc doc-loading dim">Reconstructing…</div>;

  const counts = view.open_items.counts;
  const items = view.open_items.items;
  const grouped = KIND_ORDER.map((kind) => ({
    kind,
    items: items.filter((i) => i.kind === kind),
  })).filter((group) => group.items.length > 0);

  return (
    <div className="doc gather">
      <header className="doc-head">
        <h1 className="doc-title">{view.process.name}</h1>

        {/* Every figure here is a query result. */}
        <div className="gather-counts">
          <Stat
            value={`${counts.requests_satisfied}/${counts.requests_total}`}
            label="requests met"
          />
          <Stat value={String(counts.requests_open)} label="open" />
          <Stat
            value={view.open_items.next_deadline ? shortDate(view.open_items.next_deadline) : "—"}
            label="next deadline"
          />
          <Stat value={String(view.gather.size)} label="documents" />
        </div>

        {/* Loud uncertainty. A silent omission is the dangerous failure. */}
        <div className="gather-caveats">
          {counts.open_without_candidates > 0 && (
            <div className="caveat">
              {counts.open_without_candidates} of {counts.requests_open} open items have no
              record of fulfilment anywhere in the archive — those were likely done
              somewhere this index cannot see.
            </div>
          )}
          {counts.documents_unaccounted > 0 && (
            <div className="caveat warn">
              {counts.documents_unaccounted} document
              {counts.documents_unaccounted > 1 ? "s" : ""} could not be read, and are
              not represented above.
            </div>
          )}
          {view.extraction.pending > 0 && (
            <div className="caveat warn">
              {view.extraction.pending} document{view.extraction.pending > 1 ? "s" : ""} not
              yet examined — run <code>muster process extract {view.process.slug}</code>.
            </div>
          )}
          {view.gather.seed_only > 0 && (
            <div className="caveat">
              {view.gather.seed_only} document{view.gather.seed_only > 1 ? "s" : ""} matched
              the search but no anchor, which usually means an anchor is missing.
            </div>
          )}
          {view.gather.capped && (
            <div className="caveat warn">
              Capped at {view.gather.size}; {view.gather.dropped} older documents were
              excluded.
            </div>
          )}
        </div>
      </header>

      {grouped.map((group) => (
        <section key={group.kind} className="gather-group">
          <div className="doc-sub dim">
            {KIND_LABEL[group.kind] ?? group.kind} · {group.items.length}
          </div>
          <ul className="gather-items">
            {group.items.map((item, index) => (
              <ItemRow key={`${group.kind}-${index}`} item={item} onOpenDoc={onOpenDoc} />
            ))}
          </ul>
        </section>
      ))}

      <section className="gather-group">
        <div className="doc-sub dim">Timeline · {view.timeline.length}</div>
        <ul className="gather-items">
          {view.timeline.map((entry) => (
            <li
              key={entry.doc_id}
              className="gather-row"
              onClick={() => onOpenDoc(entry.doc_id)}
              title={`included because: ${entry.reason}${entry.via ? ` (${entry.via})` : ""}`}
            >
              <span className="gather-when">{shortDate(entry.date)}</span>
              <span className="gather-what">{entry.title || "(no subject)"}</span>
              <span className="dim gather-who">{entry.counterparty ?? entry.source}</span>
              {/* Why it is here, on every line. */}
              <span className="chip gather-reason">{entry.reason.replace(/^(anchor|edge):/, "")}</span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}

function Stat({ value, label }: { value: string; label: string }) {
  return (
    <div className="stat">
      <span className="stat-value">{value}</span>
      <span className="stat-label dim">{label}</span>
    </div>
  );
}

function ItemRow({
  item,
  onOpenDoc,
}: {
  item: OpenItem;
  onOpenDoc: (docId: number) => void;
}) {
  const overdue = item.kind === "deadline_near" && item.detail === "overdue";
  return (
    <li
      className={`gather-row${overdue ? " overdue" : ""}`}
      onClick={() => item.doc_ids[0] != null && onOpenDoc(item.doc_ids[0])}
      title={item.detail ?? undefined}
    >
      <span className="gather-when">{item.when ? shortDate(item.when) : "—"}</span>
      <span className="gather-what">{item.what}</span>
      {item.counterparty && <span className="dim gather-who">{item.counterparty}</span>}
      <span className="chip gather-reason">
        {item.doc_ids.length} doc{item.doc_ids.length > 1 ? "s" : ""}
      </span>
    </li>
  );
}
