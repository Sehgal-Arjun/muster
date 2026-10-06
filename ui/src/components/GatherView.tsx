import { useCallback, useEffect, useState } from "react";
import { ApiError, api, type ExtractJob, type OpenItem, type ProcessView } from "../api";
import { shortDate } from "../dates";

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

export function GatherView({
  slug,
  query,
  activeDocId,
  onOpenDoc,
}: {
  slug: string | null;
  /** Used only to offer creating a process when none exists yet. */
  query: string;
  /**
   * The document showing beside this one, in split mode. Marked here so the
   * process still reads as a whole while you work through it — the point of
   * keeping the list on screen is knowing where in it you are.
   */
  activeDocId?: number | null;
  /**
   * The item is passed along with the document so that reading it does not lose
   * the ability to settle it: in the one-column layout this view is unmounted
   * behind the reader, and its ⌘D goes with it.
   */
  onOpenDoc: (docId: number, item?: OpenItem) => void;
}) {
  const [view, setView] = useState<ProcessView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState(0);
  const [flash, setFlash] = useState<string | null>(null);
  const [job, setJob] = useState<ExtractJob | null>(null);
  /**
   * Items settled in the last few seconds, kept in place and struck through.
   *
   * Settling used to move the row into a section further down the page while the
   * eye was still on where it had been — from which the honest reading is
   * "pressing done did nothing". It stays where it is, visibly settled, and the
   * refetch tidies up behind it.
   */
  const [justSettled, setJustSettled] = useState<Record<string, boolean>>({});
  /** The last thing settled, for ⌘Z. */
  const [lastSettled, setLastSettled] = useState<OpenItem | null>(null);

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
      // Struck through before the round trip: the click has to land visibly, and
      // the server has never yet disagreed about a dismissal it accepted.
      setJustSettled((current) => ({ ...current, [item.key || item.what]: true }));
      try {
        await api.dismissItem(slug, item.what, { verdict });
        setLastSettled(item);
        setFlash(`Settled — ${item.what.slice(0, 38)} · ⌘Z to undo`);
        setView(await api.process(slug));
      } catch (cause) {
        setJustSettled((current) => {
          const { [item.key || item.what]: _gone, ...rest } = current;
          return rest;   // it did not settle, so it must not look settled
        });
        setFlash(cause instanceof ApiError ? cause.message : String(cause));
      }
    },
    [slug],
  );

  /** Put back the last thing settled. A wrong keystroke should cost one more. */
  const undoSettle = useCallback(async () => {
    if (!slug || !lastSettled) return;
    try {
      await api.dismissItem(slug, lastSettled.what, { verdict: "done", undo: true });
      setJustSettled({});
      setFlash(`Back on the list — ${lastSettled.what.slice(0, 38)}`);
      setLastSettled(null);
      setView(await api.process(slug));
    } catch (cause) {
      setFlash(cause instanceof ApiError ? cause.message : String(cause));
    }
  }, [slug, lastSettled]);

  /**
   * Read the documents nobody has read yet.
   *
   * The header is only true about documents that have been examined, and until
   * this runs the honest answer to "how many are open" is "unknown". Telling
   * someone to open a terminal to make the screen in front of them true is worse
   * than letting the screen start the work.
   */
  const examine = useCallback(async () => {
    if (!slug || job?.state === "running") return;
    try {
      setJob(await api.extract(slug));
    } catch (cause) {
      setFlash(cause instanceof ApiError ? cause.message : String(cause));
    }
  }, [slug, job]);

  // Polled rather than streamed: a progress number that is two seconds stale
  // costs nothing, and a websocket for it would be a second transport to keep
  // working.
  useEffect(() => {
    if (!job?.job || job.state !== "running") return;
    let live = true;
    const timer = setInterval(async () => {
      try {
        const next = await api.extractJob(job.job as string);
        if (!live) return;
        setJob(next);
        if (next.state !== "running") {
          clearInterval(timer);
          // The numbers on this screen were computed from an unread set. They
          // are wrong now in the good direction, so fetch them again.
          if (slug) setView(await api.process(slug));
          setFlash(
            next.state === "failed"
              ? `Extraction failed: ${next.error ?? "unknown"}`
              : `Examined ${next.extracted} document${next.extracted === 1 ? "" : "s"}` +
                (next.failed ? `, ${next.failed} unreadable` : ""),
          );
        }
      } catch {
        clearInterval(timer);  // server gone; the flash on the next action says so
      }
    }, 2000);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [job, slug]);

  useEffect(() => {
    if (!flash) return;
    const timer = setTimeout(() => setFlash(null), 2200);
    return () => clearTimeout(timer);
  }, [flash]);


  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!view) return;
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "e") {
        // Bound whether or not there are open items: a process with nothing
        // examined has no open items *and* nothing to show, which is exactly
        // when this is needed.
        event.preventDefault();
        void examine();
        return;
      }
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
      } else if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "z") {
        event.preventDefault();
        void undoSettle();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [view, selected, settle, examine, undoSettle]);

  /**
   * Adopt an extraction already in flight.
   *
   * A job runs for half an hour and outlives the panel that started it, so a
   * process being read right now would otherwise look merely empty to a panel
   * that was reopened — or to the other layout.
   */
  useEffect(() => {
    if (!slug || job) return;
    let live = true;
    api
      .runningExtract()
      .then((body) => {
        if (live && body.job && body.job.process === slug) setJob(body.job);
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [slug, job]);

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
  // Nothing examined means nothing computed. A bold "0 open" over a set the
  // extractor has never touched reads as "you are clear", which is the one thing
  // this screen must never say by accident.
  const unexamined = counts.documents_unexamined ?? view.extraction.pending;
  const blind = unexamined >= view.gather.size && view.gather.size > 0;
  const partial = unexamined > 0 && !blind;

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
            value={blind ? "—" : `${counts.requests_satisfied}/${counts.requests_total}`}
            label="requests met"
          />
          <Stat
            // A "+" where documents remain unread: what is known to be open,
            // which can only grow once the rest are examined.
            value={blind ? "—" : `${counts.requests_open}${partial ? "+" : ""}`}
            label={blind ? "not examined" : "open"}
          />
          <Stat
            // Upcoming only. A date that has passed is either missed or moot,
            // and either way it is not next — it is counted beside it instead.
            value={view.open_items.next_deadline ? shortDate(view.open_items.next_deadline) : "—"}
            label={counts.overdue ? `next deadline · ${counts.overdue} overdue` : "next deadline"}
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
          {counts.documents_unread > 0 && (
            <div className="caveat warn">
              {counts.documents_unread} document
              {counts.documents_unread > 1 ? "s" : ""} could not be read, and are not
              represented above.
            </div>
          )}
          {unexamined > 0 && (
            <div className="caveat warn">
              {blind
                ? `Nothing here has been examined yet — the figures above are unknown, not zero.`
                : `${unexamined} of ${view.gather.size} documents not examined yet, so the figures above are a floor.`}{" "}
              {job?.state === "running" ? (
                <span className="caveat-run">
                  Examining {job.done}/{job.total}…{" "}
                  <button
                    className="caveat-action"
                    onClick={() => job.job && void api.stopExtract(job.job)}
                  >
                    stop
                  </button>
                </span>
              ) : (
                <button className="caveat-action" onClick={() => void examine()}>
                  examine now (⌘E)
                </button>
              )}
            </div>
          )}
          {counts.not_obliged > 0 && (
            <div className="caveat">
              {counts.not_obliged} request{counts.not_obliged > 1 ? "s" : ""} left out as
              not owed — invitations, surveys and advice phrased as instructions. Being
              asked is not being obliged, and this is a judgement, so it can be wrong.
            </div>
          )}
          {counts.asks_skipped > 0 && (
            <div className="caveat">
              {counts.asks_skipped} request{counts.asks_skipped > 1 ? "s" : ""} found in
              attachments rather than correspondence, and not counted — a checklist
              in a form lists what anyone might need, not what was asked of you.
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
                settled={Boolean(justSettled[item.key || item.what])}
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
              className={`gather-row${entry.doc_id === activeDocId ? " active" : ""}`}
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
  settled,
  onOpenDoc,
  onSettle,
}: {
  item: OpenItem;
  selected: boolean;
  /** Settled a moment ago and still in place, so the click can be seen landing. */
  settled?: boolean;
  onOpenDoc: (docId: number, item?: OpenItem) => void;
  onSettle: (verdict: "done" | "not_applicable") => void;
}) {
  const overdue = item.kind === "deadline_near" && item.detail === "overdue";
  return (
    <li
      className={`gather-row${overdue ? " overdue" : ""}${selected ? " selected" : ""}${
        settled ? " settled" : ""
      }`}
      onClick={() => item.doc_ids[0] != null && onOpenDoc(item.doc_ids[0], item)}
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
        <button
          className="confirm-ask"
          onClick={(event) => {
            event.stopPropagation();
            onSettle("done");
          }}
        >
          due {item.when ? shortDate(item.when) : "then"}, nothing since{" "}
          {item.last_activity ? shortDate(item.last_activity) : "then"} — done?
        </button>
      )}
    </li>
  );
}
