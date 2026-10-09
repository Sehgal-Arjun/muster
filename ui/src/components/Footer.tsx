import type { MouseEvent } from "react";

import type { Mode, SearchResponse } from "../api";

/**
 * The four or five keys you actually reach for, per screen.
 *
 * Thirteen hints did not fit a 720-point panel: the bar wrapped, and "reveal in
 * Finder" ended up stacked in a column of its own. A hint bar is a reminder, not
 * a reference — the full set lives behind ⌘/, where it can be read rather than
 * skimmed past.
 */
const SEARCH_HINTS: Array<[string, string]> = [
  ["↑↓", "move"],
  ["↵", "read"],
  ["⇥", "gather"],
  ["⌘⇧P", "keep open"],
];

// A message has no file on disk, so revealing one shows a content-addressed
// .eml in a directory that means nothing. ⌘O opens it where it lives.
const MAIL_HINTS: Array<[string, string]> = [
  ["↑↓", "scroll"],
  ["↵", "attachment"],
  ["⌘O", "open in Gmail"],
  ["esc", "back"],
];

// Same keys, different meanings. Showing the search hints over a document would
// be telling the reader the wrong thing.
const READING_HINTS: Array<[string, string]> = [
  ["↑↓", "scroll"],
  ["↵", "attachment"],
  ["⌘↵", "reveal"],
  ["esc", "back"],
];

// Gather is not a result list: ⇥ is spent, and the keys that matter change the
// process rather than move through it.
const GATHER_HINTS: Array<[string, string]> = [
  ["↑↓", "move"],
  ["⌘D", "done"],
  ["⌘E", "examine"],
  ["esc", "back"],
];

export function Footer({
  response,
  mode,
  error,
  reading,
  readingMail,
  gathering,
  split,
  sort,
  pinned,
  syncing,
  onDrag,
}: {
  response: SearchResponse | null;
  mode: Mode;
  error: string | null;
  reading: boolean;
  /** Mail has no file to reveal, so ⌘↵ means something else over a message. */
  readingMail?: boolean;
  /** Gather has its own verbs; the search hints would be wrong here. */
  gathering?: boolean;
  /** Two panes: the list keeps the arrows, so the hints stay the list's. */
  split?: boolean;
  /** Named next to the mode, because it changes what "first" means. */
  sort?: "rank" | "newest";
  /** Staying up when focus leaves. Shown, because it changes what a click does. */
  pinned?: boolean;
  syncing: boolean;
  /** The other handle for moving the panel. See beginDrag in App. */
  onDrag?: (event: MouseEvent) => void;
}) {
  return (
    <footer className="footer drag-handle" onMouseDown={onDrag}>
      <div className="hints">
        {/* Split mode reads from the list, so the list's keys are the true ones
            even with a document on screen beside it. */}
        {(reading && !split
          ? readingMail
            ? MAIL_HINTS
            : READING_HINTS
          : gathering
            ? GATHER_HINTS
            : SEARCH_HINTS
        ).map(([key, label]) => (
          <span key={key} className="hint">
            <kbd>{key}</kbd>
            {label}
          </span>
        ))}
        {/* One door to everything else, rather than everything else on the bar. */}
        <span className="hint hint-more">
          <kbd>⌘/</kbd>
          keys
        </span>
      </div>
      <div className="footer-right">
        {error ? (
          <span className="err">{error}</span>
        ) : response ? (
          <>
            {/* Shown because a query whose date was lifted out behaves in a way
                the user cannot otherwise explain. */}
            {response.query.temporal && (
              <span className="chip" title={`${response.query.temporal.since.slice(0, 10)} → ${response.query.temporal.until.slice(0, 10)}`}>
                {response.query.temporal.matched}
              </span>
            )}
            <span className="dim">{response.results.length} results · {response.took_ms}ms</span>
          </>
        ) : null}
        {/* Deliberately small and unlabelled-until-hovered: a refresh happens
            on every summon and must not read as something needing attention. */}
        {syncing && <span className="syncing" title="checking for new mail" />}
        {/* Marked when it is not the default, because a single-arm mode is a
            temporary thing and looks identical to normal otherwise. */}
        {/* A panel that no longer closes when you click away behaves differently
            enough that it has to say so. */}
        {pinned && (
          <span className="mode mode-narrowed" title="staying open — ⌘⇧P to let it close">
            pinned
          </span>
        )}
        {/* Shown always: which of the two orders is in force decides whether
            the top row is a claim about relevance or about time. */}
        {sort && (
          <span
            className={`mode${sort === "newest" ? " mode-narrowed" : ""}`}
            title={
              sort === "newest"
                ? "newest first — ⌘S for best match first"
                : "best match first — ⌘S for newest first"
            }
          >
            {sort === "newest" ? "newest" : "best"}
          </span>
        )}
        <span className={`mode${mode === "hybrid" ? "" : " mode-narrowed"}`}
              title={mode === "hybrid" ? undefined : `${mode} only — ⌘1 for hybrid`}>
          {mode}
        </span>
      </div>
    </footer>
  );
}
