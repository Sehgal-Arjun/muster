import type { Mode, SearchResponse } from "../api";

const HINTS: Array<[string, string]> = [
  ["↑↓", "navigate"],
  ["↵", "open"],
  ["⌘↵", "reveal in Finder"],
  ["⌘K", "actions"],
  ["⌘1/2/3", "mode"],
  ["esc", "close"],
];

export function Footer({
  response,
  mode,
  error,
}: {
  response: SearchResponse | null;
  mode: Mode;
  error: string | null;
}) {
  return (
    <footer className="footer">
      <div className="hints">
        {HINTS.map(([key, label]) => (
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
        <span className="mode">{mode}</span>
      </div>
    </footer>
  );
}
