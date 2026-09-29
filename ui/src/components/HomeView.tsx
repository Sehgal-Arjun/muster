import { useEffect, useState } from "react";
import { ApiError, api, type HomeView as Home } from "../api";

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
function shortDate(iso: string | null): string {
  if (!iso) return "";
  const when = new Date(iso);
  return Number.isNaN(when.getTime())
    ? iso.slice(0, 10)
    : when.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

function overdue(iso: string | null): boolean {
  if (!iso) return false;
  return new Date(iso) < new Date();
}

export function HomeView({ onOpenProcess }: { onOpenProcess: (slug: string) => void }) {
  const [home, setHome] = useState<Home | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Only after a beat: a warm answer arrives in ~2s and a flash of "working"
  // before it would be worse than nothing. A cold server has to load the
  // embedding model first, and that is the wait worth naming.
  const [slow, setSlow] = useState(false);

  useEffect(() => {
    let live = true;
    api
      .home()
      .then((body) => live && setHome(body))
      .catch((cause) =>
        live && setError(cause instanceof ApiError ? cause.message : String(cause)),
      );
    const beat = setTimeout(() => live && setSlow(true), 1500);
    return () => {
      live = false;
      clearTimeout(beat);
    };
  }, []);

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
    (p) => p.open > 0 || p.awaiting_confirmation > 0,
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
            {home.processes.map((process) => (
              <li
                key={process.slug}
                className="gather-row home-row"
                onClick={() => onOpenProcess(process.slug)}
              >
                <span className="gather-what">{process.name}</span>

                {process.open > 0 && (
                  <span className="chip home-open">
                    {process.open}
                    {/* An upper bound, not a count, until its judgements are made. */}
                    {process.estimated ? "+" : ""} open
                  </span>
                )}
                {process.awaiting_confirmation > 0 && (
                  <span className="chip contradicted">
                    {process.awaiting_confirmation} to confirm
                  </span>
                )}
                {process.extraction_pending > 0 && (
                  <span className="chip contradicted">
                    {process.extraction_pending} unread
                  </span>
                )}

                <span
                  className={`gather-who dim${overdue(process.next_deadline) ? " home-overdue" : ""}`}
                >
                  {process.next_deadline ? `due ${shortDate(process.next_deadline)}` : ""}
                </span>
                <span className="dim home-docs">{process.documents} docs</span>
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
