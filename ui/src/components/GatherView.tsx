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
  const [selected, setSelected] = useState(0);
  const [flash, setFlash] = useState<string | null>(null);

  /**
   * Settle an item by hand.
   *
   * Recorded rather than removed: most obligations in a real process are
   * discharged somewhere this index cannot see, and an item that no evidence can
   * ever satisfy would otherwise sit in the list until the list stops being read.
   */
  const settle = useCallback(
    async (item: OpenItem, verdict: "done" | "not_applicable") => {
      if (!slug) return;
      try {
        await api.dismissItem(slug, item.what, { verdict });
        setFlash(`Marked done — ${item.what.slice(0, 40)}`);
        setView(await api.process(slug));
      } catch (cause) {
        setFlash(cause instanceof ApiError ? cause.message : String(cause));
      }
    },
    [slug],
  );

  useEffect(() => {
    if (!flash) return;
    const timer = setTimeout(() => setFlash(null), 2200);
    return () => clearTimeout(timer);
  }, [flash]);


  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!view) return;
      const open = view.open_items.items;
      if (!open.length) return;
      if (event.key === "ArrowDown" || event.key === "j") {
        event.preventDefault();
        setSelected((i) => Math.min(i + 1, open.length - 1));
      } else if (event.key === "ArrowUp" || event.key === "k") {
        event.preventDefault();
        setSelected((i) => Math.max(i - 1, 0));
      } else if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "d") {
        event.preventDefault();
        const item = open[selected];
        if (item) void settle(item, "done");
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [view, selected, settle]);

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

  if (!slug) {
    return (
      <div className="empty">
        <div className="empty-headline">No process for “{query}”</div>
        <div className="empty-detail dim">
          Gather reconstructs the state of a process — what was asked, what was
          sent, what is still outstanding.
        </div>
        <div className="empty-detail dim">
          ⌘P opens the process list, where you can create one from this query and
          choose its anchors.
        </div>
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
  const settledItems = view.open_items.settled ?? [];
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

      {settledItems.length > 0 && (
        <section className="gather-group">
          <div className="doc-sub dim">
            Settled by you · {settledItems.length}
            {counts.settled_contradicted > 0 &&
              ` · ${counts.settled_contradicted} with new documents since`}
          </div>
          <ul className="gather-items">
            {settledItems.map((item) => (
              <li
                key={item.key}
                className="gather-row settled"
                onClick={() => item.doc_ids[0] != null && onOpenDoc(item.doc_ids[0])}
                title={`settled ${item.settled_at ?? ""}`}
              >
                <span className="gather-when">{shortDate(item.settled_at)}</span>
                <span className="gather-what">{item.what}</span>
                {item.contradicted && (
                  <span className="chip contradicted">new documents since</span>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      {flash && <div className="doc-flash">{flash}</div>}

      {grouped.map((group) => (
        <section key={group.kind} className="gather-group">
          <div className="doc-sub dim">
            {KIND_LABEL[group.kind] ?? group.kind} · {group.items.length}
          </div>
          <ul className="gather-items">
            {group.items.map((item, index) => (
              <ItemRow
                key={item.key || `${group.kind}-${index}`}
                item={item}
                selected={items.indexOf(item) === selected}
                onOpenDoc={onOpenDoc}
                onSettle={(verdict) => void settle(item, verdict)}
              />
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
  selected,
  onOpenDoc,
  onSettle,
}: {
  item: OpenItem;
  selected: boolean;
  onOpenDoc: (docId: number) => void;
  onSettle: (verdict: "done" | "not_applicable") => void;
}) {
  const overdue = item.kind === "deadline_near" && item.detail === "overdue";
  return (
    <li
      className={`gather-row${overdue ? " overdue" : ""}${selected ? " selected" : ""}`}
      onClick={() => item.doc_ids[0] != null && onOpenDoc(item.doc_ids[0])}
      title={item.detail ?? undefined}
    >
      <span className="gather-when">{item.when ? shortDate(item.when) : "—"}</span>
      <span className="gather-what">{item.what}</span>
      {item.counterparty && <span className="dim gather-who">{item.counterparty}</span>}
      <span className="chip gather-reason">
        {item.doc_ids.length} doc{item.doc_ids.length > 1 ? "s" : ""}
      </span>
      <button
        className="settle"
        title="I did this (⌘D)"
        onClick={(event) => {
          event.stopPropagation();
          onSettle("done");
        }}
      >
        done
      </button>
      {/* Asked once, when a deadline has passed with nothing since. Not
          repeated: nagging is how a list teaches somebody to stop reading it. */}
      {item.needs_confirmation && (
        <span className="confirm-ask">
          due {item.when ? shortDate(item.when) : "then"}, nothing since{" "}
          {item.last_activity ? shortDate(item.last_activity) : "then"} — done?
        </span>
      )}
    </li>
  );
}
