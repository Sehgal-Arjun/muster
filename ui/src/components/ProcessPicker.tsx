import { useEffect, useMemo, useRef, useState } from "react";
import { api, type AnchorProposal, type ProcessSummary } from "../api";

/**
 * Choosing a process, and creating one.
 *
 * The accept/reject step for anchors lives here and is the reason anchors can be
 * trusted at all: an anchor is permanent and pulls in documents forever, so it
 * has to be a decision somebody made rather than a threshold somebody set. It is
 * a screen with space to read the evidence, not a flag.
 */
export function ProcessPicker({
  processes,
  query,
  onOpen,
  onCreated,
  onClose,
}: {
  processes: ProcessSummary[];
  query: string;
  onOpen: (slug: string) => void;
  onCreated: () => void;
  onClose: () => void;
}) {
  const [selected, setSelected] = useState(0);
  const [naming, setNaming] = useState<string | null>(null);
  const [proposals, setProposals] = useState<AnchorProposal[] | null>(null);
  const [accepted, setAccepted] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const listRef = useRef<HTMLUListElement>(null);

  // The list, plus "create from this query" as the last row when there is one.
  const rows = useMemo(() => {
    const out: { key: string; label: string; detail: string; create?: boolean }[] =
      processes.map((p) => ({
        key: p.slug,
        label: p.name,
        detail: `${p.anchors.length} anchor${p.anchors.length === 1 ? "" : "s"}`,
      }));
    if (query.trim()) {
      out.push({
        key: "__create__",
        label: `Create a process from “${query.trim()}”`,
        detail: "propose anchors",
        create: true,
      });
    }
    return out;
  }, [processes, query]);

  const propose = async (name: string) => {
    setBusy(true);
    setError(null);
    try {
      const body = await api.proposeProcess(name);
      setNaming(name);
      setProposals(body.proposals);
      // Nothing pre-selected. Accepting by default is the same as a threshold,
      // which is what this screen exists to replace.
      setAccepted(new Set());
    } catch (cause) {
      setError(String(cause instanceof Error ? cause.message : cause));
    } finally {
      setBusy(false);
    }
  };

  const confirm = async () => {
    if (!naming) return;
    setBusy(true);
    try {
      const made = await api.confirmProcess(naming, [...accepted]);
      onCreated();
      onOpen(made.slug);
    } catch (cause) {
      setError(String(cause instanceof Error ? cause.message : cause));
    } finally {
      setBusy(false);
    }
  };

  // Keys are bound here rather than in App so the two screens cannot both
  // interpret the same press.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const list = proposals ?? rows;
      if (event.key === "ArrowDown" || event.key === "j") {
        event.preventDefault();
        setSelected((i) => Math.min(i + 1, list.length - 1));
      } else if (event.key === "ArrowUp" || event.key === "k") {
        event.preventDefault();
        setSelected((i) => Math.max(i - 1, 0));
      } else if (event.key === " " && proposals) {
        event.preventDefault();
        const proposal = proposals[selected];
        if (!proposal) return;
        setAccepted((current) => {
          const next = new Set(current);
          next.has(proposal.id) ? next.delete(proposal.id) : next.add(proposal.id);
          return next;
        });
      } else if (event.key === "Enter") {
        event.preventDefault();
        if (proposals) {
          void confirm();
          return;
        }
        const row = rows[selected];
        if (!row) return;
        if (row.create) void propose(query.trim());
        else onOpen(row.key);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  });

  useEffect(() => {
    listRef.current
      ?.querySelector<HTMLElement>(".picker-row.selected")
      ?.scrollIntoView({ block: "nearest" });
  }, [selected]);

  if (error) {
    return (
      <div className="empty">
        <div className="empty-headline">Could not do that</div>
        <div className="empty-detail">{error}</div>
        <button className="link" onClick={onClose}>Back</button>
      </div>
    );
  }

  if (proposals) {
    return (
      <div className="doc gather">
        <header className="doc-head">
          <h1 className="doc-title">Anchors for “{naming}”</h1>
          <div className="caveat">
            An anchor includes everything matching it, permanently. Take the ones
            that name this process and nothing else — the search finds the rest
            on its own, so an anchor you skip costs far less than one you regret.
          </div>
        </header>
        <section className="gather-group">
          <div className="doc-sub dim">
            {proposals.length} proposed · {accepted.size} accepted ·
            space to toggle, enter to create
          </div>
          <ul className="gather-items" ref={listRef}>
            {proposals.map((proposal, index) => (
              <li
                key={proposal.id}
                className={`picker-row gather-row${index === selected ? " selected" : ""}`}
                onMouseMove={() => setSelected(index)}
                onClick={() =>
                  setAccepted((current) => {
                    const next = new Set(current);
                    next.has(proposal.id) ? next.delete(proposal.id) : next.add(proposal.id);
                    return next;
                  })
                }
              >
                <span className="picker-tick">{accepted.has(proposal.id) ? "✓" : "○"}</span>
                <span className="chip">{proposal.kind}</span>
                <span className="gather-what">{proposal.value}</span>
                <span className="dim gather-who">{proposal.count} docs</span>
                <span className="dim picker-example">{proposal.example}</span>
              </li>
            ))}
          </ul>
        </section>
        {busy && <div className="doc-note dim">Working…</div>}
      </div>
    );
  }

  return (
    <div className="doc gather">
      <header className="doc-head">
        <h1 className="doc-title">Processes</h1>
        <div className="caveat">
          A process is the thing a pile of documents is about — what was asked,
          what was sent, what is still outstanding.
        </div>
      </header>
      <section className="gather-group">
        <ul className="gather-items" ref={listRef}>
          {rows.map((row, index) => (
            <li
              key={row.key}
              className={`picker-row gather-row${index === selected ? " selected" : ""}${row.create ? " picker-create" : ""}`}
              onMouseMove={() => setSelected(index)}
              onClick={() => (row.create ? void propose(query.trim()) : onOpen(row.key))}
            >
              <span className="gather-what">{row.label}</span>
              <span className="dim gather-who">{row.detail}</span>
            </li>
          ))}
          {rows.length === 0 && (
            <li className="dim doc-note">No processes yet. Search for something first.</li>
          )}
        </ul>
      </section>
      {busy && <div className="doc-note dim">Looking for anchors…</div>}
    </div>
  );
}
