import type { Mode, SearchResponse } from "../api";

const SEARCH_HINTS: Array<[string, string]> = [
  ["↑↓", "navigate"],
  ["↵", "read"],
  ["⌘↵", "reveal in Finder"],
  ["⌘O", "open"],
  ["⌘K", "actions"],
  ["⌘1/2/3", "mode"],
  ["esc", "close"],
];

// A message has no file on disk, so revealing one shows a content-addressed
// .eml in a directory that means nothing. Both keys open it where it lives.
const MAIL_HINTS: Array<[string, string]> = [
  ["↑↓ jk", "scroll"],
  ["↵", "open attachment"],
  ["⌘O", "open in Gmail"],
  ["⌘C", "copy body"],
  ["esc", "back"],
];

// Same keys, different meanings. Showing the search hints over a document
// would be telling the reader the wrong thing.
const READING_HINTS: Array<[string, string]> = [
  ["↑↓ jk", "scroll"],
  ["↵", "open attachment"],
  ["⌘O", "open"],
  ["⌘↵", "reveal in Finder"],
  ["⌘C", "copy body"],
  ["esc", "back"],
];

export function Footer({
  response,
  mode,
  error,
  reading,
  readingMail,
  syncing,
}: {
  response: SearchResponse | null;
  mode: Mode;
  error: string | null;
  reading: boolean;
  /** Mail has no file to reveal, so ⌘↵ means something else over a message. */
  readingMail?: boolean;
  syncing: boolean;
}) {
  return (
    <footer className="footer">
      <div className="hints">
        {(reading ? (readingMail ? MAIL_HINTS : READING_HINTS) : SEARCH_HINTS).map(([key, label]) => (
          <span key={key} className="hint">
            <kbd>{key}</kbd>
            {label}
          </span>
        ))}
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
        <span className={`mode${mode === "hybrid" ? "" : " mode-narrowed"}`}
              title={mode === "hybrid" ? undefined : `${mode} only — ⌘1 for hybrid`}>
          {mode}
        </span>
      </div>
    </footer>
  );
}
