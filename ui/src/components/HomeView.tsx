import { useEffect, useState } from "react";
import { ApiError, api, type HomeView as Home } from "../api";
import { isPast, shortDate } from "../dates";

/**
 * The panel's resting state: what is going on, across everything.
 *
 * Gather answers "what is the state of this thing"; this answers it for all of
 * them at once, and is what an empty query should show instead of an instruction
 * to start typing.
 *
 * Built from cached judgements only, so opening the panel never waits on a
 * model. A process nobody has opened yet reports its open count as an upper
 * bound and marks it, rather than blocking or quietly guessing.
 */


export function HomeView({ onOpenProcess }: { onOpenProcess: (slug: string) => void }) {
  const [home, setHome] = useState<Home | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Only after a beat: a warm answer arrives in ~2s and a flash of "working"
  // before it would be worse than nothing. A cold server has to load the
  // embedding model first, and that is the wait worth naming.
  const [slow, setSlow] = useState(false);
  /**
   * Keyboard selection. The panel is summoned by a hotkey and driven by keys;
   * a screen whose only way in is the mouse is the one place that stops being
   * true, and it was the first screen you see.
   */
  const [selected, setSelected] = useState(0);

  /**
   * Ask, and keep asking while anything is still being worked out.
   *
   * A cold server has to load the embedding model and reconstruct each process
   * before it can say anything about them. The overview no longer waits for that
   * — it lists what it knows, marks the rest, and this fills them in as they
   * arrive, so the screen is never blank and never lies.
   */
  useEffect(() => {
    let live = true;
    let timer: number | undefined;

    const ask = async () => {
      try {
        const body = await api.home();
        if (!live) return;
        setHome(body);
        setError(null);
        if (body.warming) timer = window.setTimeout(ask, 1200);
      } catch (cause) {
        if (!live) return;
        setError(cause instanceof ApiError ? cause.message : String(cause));
      }
    };

    void ask();
    const beat = setTimeout(() => live && setSlow(true), 1500);
    return () => {
      live = false;
      clearTimeout(beat);
      if (timer) clearTimeout(timer);
    };
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const rows = home?.processes ?? [];
      if (!rows.length) return;
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setSelected((i) => Math.min(i + 1, rows.length - 1));
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        setSelected((i) => Math.max(i - 1, 0));
      } else if (event.key === "Enter") {
        const row = rows[selected];
        // A process still being reconstructed has nothing to show yet, and
        // opening an empty screen is not an answer to a keystroke.
        if (!row || row.pending) return;
        event.preventDefault();
        onOpenProcess(row.slug);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [home, selected, onOpenProcess]);

  if (error) {
    return (
      <div className="empty">
        <div className="empty-headline">Muster</div>
        <div className="empty-detail">{error}</div>
        <div className="empty-detail dim">Start typing to search anyway.</div>
      </div>
    );
  }

  if (!home) {
    return (
      <div className="empty">
        <div className="empty-headline">Muster</div>
        <div className="empty-detail dim">
          {slow ? "Working out where things stand…" : "Start typing."}
        </div>
      </div>
    );
  }

  const attention = home.processes.filter(
    (p) => (p.open ?? 0) > 0 || (p.awaiting_confirmation ?? 0) > 0,
  );

  return (
    <div className="doc home">
      <header className="doc-head">
        <h1 className="doc-title">Muster</h1>
        <div className="home-index dim">
          {home.index.documents.toLocaleString()} documents
          {home.index.missing > 0 && ` · ${home.index.missing} originals gone`}
          {home.index.last_sync && ` · synced ${shortDate(home.index.last_sync)}`}
          {!home.index.embedding_ready && " · still starting up"}
        </div>
      </header>

      {home.processes.length === 0 ? (
        <div className="doc-note dim">
          No processes yet. Search for something, then press ⇥ to reconstruct it
          as a process — or ⌘P to create one.
        </div>
      ) : (
        <section className="gather-group">
          <div className="doc-sub dim">
            {attention.length > 0
              ? `Needs attention · ${attention.length}`
              : `Processes · ${home.processes.length}`}
          </div>
          <ul className="gather-items">
            {home.processes.map((process, index) => (
              <li
                key={process.slug}
                className={`gather-row home-row${index === selected ? " selected" : ""}`}
                onClick={() => onOpenProcess(process.slug)}
              >
                <span className="gather-what">{process.name}</span>

                {/* Still being reconstructed: say so, rather than showing zeros
                    that would read as "nothing outstanding". */}
                {process.pending && (
                  <span className="chip pending-chip">
                    <span className="pulse" /> working it out…
                  </span>
                )}

                {(process.open ?? 0) > 0 && (
                  <span className="chip home-open">
                    {process.open}
                    {/* An upper bound, not a count, until its judgements are made. */}
                    {process.estimated ? "+" : ""} open
                  </span>
                )}
                {(process.awaiting_confirmation ?? 0) > 0 && (
                  <span className="chip contradicted">
                    {process.awaiting_confirmation} to confirm
                  </span>
                )}
                {(process.extraction_pending ?? 0) > 0 && (
                  <span className="chip contradicted">
                    {process.extraction_pending} unread
                  </span>
                )}

                <span
                  className={`gather-who dim${isPast(process.next_deadline) ? " home-overdue" : ""}`}
                >
                  {process.next_deadline ? `due ${shortDate(process.next_deadline)}` : ""}
                </span>
                <span className="dim home-docs">
                  {process.documents === null ? "" : `${process.documents} docs`}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <div className="home-hint dim">
        Start typing to search · ⇥ gather · ⌘P processes · ⌘H back here
      </div>
    </div>
  );
}
