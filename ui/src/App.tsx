import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api, type Hit, type Mode, type SearchResponse } from "./api";
import { mockSearchFor } from "./mock";
import { Actions, type Action } from "./components/Actions";
import { EmptyState } from "./components/EmptyState";
import { Footer } from "./components/Footer";
import { ResultRow } from "./components/ResultRow";

/**
 * Set VITE_USE_MOCK=1 to run the whole screen against the fixtures in mock.ts
 * with no server. Every state the UI has to draw — including the ones that are
 * awkward to provoke on demand, like the floor rejecting everything — is
 * reachable that way.
 */
const USE_MOCK = import.meta.env.VITE_USE_MOCK === "1";

const MODES: Mode[] = ["hybrid", "keyword", "vector"];
const DEBOUNCE_MS = 120;

export default function App() {
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<Mode>("hybrid");
  const [response, setResponse] = useState<SearchResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState(0);
  const [showActions, setShowActions] = useState(false);
  const [override, setOverride] = useState<Record<string, string | number> | null>(null);

  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  // Guards against a slow response for an old query overwriting a fast one for
  // the query the user is actually looking at.
  const latest = useRef(0);

  const hits = response?.results ?? [];
  const current: Hit | null = hits[selected] ?? null;

  useEffect(() => inputRef.current?.focus(), []);

  useEffect(() => {
    const q = query.trim();
    if (!q) {
      setResponse(null);
      setError(null);
      return;
    }
    const ticket = ++latest.current;
    const timer = setTimeout(async () => {
      try {
        const body = USE_MOCK
          ? mockSearchFor(q)
          : await api.search({ q, mode, ...(override ?? {}) });
        if (ticket !== latest.current) return;
        setResponse(body);
        setError(null);
        setSelected(0);
      } catch (cause) {
        if (ticket !== latest.current) return;
        setResponse(null);
        setError(cause instanceof ApiError ? cause.message : String(cause));
      }
    }, DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [query, mode, override]);

  // A new query invalidates a "show weak matches" override.
  useEffect(() => setOverride(null), [query, mode]);

  useEffect(() => {
    listRef.current
      ?.querySelector<HTMLElement>(".row.selected")
      ?.scrollIntoView({ block: "nearest" });
  }, [selected]);

  const hide = useCallback(async () => {
    if (USE_MOCK) return;
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("hide_panel").catch(() => {});
  }, []);

  const open = useCallback(
    async (hit: Hit | null, reveal = false) => {
      if (!hit) return;
      const url = hit.content_hash ? api.blobUrl(hit.content_hash, reveal) : null;
      if (!url) return;
      if (USE_MOCK) {
        console.info(reveal ? "reveal" : "open", url);
        return;
      }
      const { openUrl } = await import("@tauri-apps/plugin-opener");
      await openUrl(url);
      if (!reveal) await hide();
    },
    [hide],
  );

  const actions: Action[] = useMemo(() => {
    if (!current) return [];
    const list: Action[] = [
      { id: "open", label: "Open original", hint: "↵", run: () => open(current) },
      { id: "download", label: "Download a copy", hint: "⌘↵", run: () => open(current, true) },
      {
        id: "copy-title",
        label: "Copy subject",
        run: () => navigator.clipboard.writeText(current.title ?? ""),
      },
      {
        id: "copy-uid",
        label: "Copy source id",
        run: () =>
          navigator.clipboard.writeText(
            `${current.sources[0]!.source}:${current.sources[0]!.source_uid}`,
          ),
      },
    ];
    // Only offered when there is more than one copy — otherwise it is a lie.
    if (current.sources.length > 1) {
      list.push({
        id: "copies",
        label: `Copy all ${current.sources.length} source ids`,
        run: () =>
          navigator.clipboard.writeText(
            current.sources.map((s) => `${s.source}:${s.source_uid}`).join("\n"),
          ),
      });
    }
    return list;
  }, [current, open]);

  function onKeyDown(event: React.KeyboardEvent) {
    if (showActions) return;
    const meta = event.metaKey || event.ctrlKey;

    if (event.key === "Escape") {
      event.preventDefault();
      if (query) setQuery("");
      else void hide();
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      setSelected((i) => Math.min(i + 1, hits.length - 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setSelected((i) => Math.max(i - 1, 0));
    } else if (event.key === "Enter") {
      event.preventDefault();
      void open(current, meta);
    } else if (meta && event.key.toLowerCase() === "k") {
      event.preventDefault();
      if (current) setShowActions(true);
    } else if (meta && ["1", "2", "3"].includes(event.key)) {
      event.preventDefault();
      setMode(MODES[Number(event.key) - 1]!);
    }
  }

  return (
    <div className="app" onKeyDown={onKeyDown}>
      <div className="search">
        <input
          ref={inputRef}
          className="query"
          placeholder="Search everything you have ever received…"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          spellCheck={false}
          autoComplete="off"
        />
        {response?.query.searched !== response?.query.raw && response && (
          <span className="rewritten" title="the date became a filter">
            {response.query.searched}
          </span>
        )}
      </div>

      <div className="results">
        {hits.length > 0 && (
          <ul className="rows" ref={listRef} role="listbox">
            {hits.map((hit, i) => (
              <ResultRow
                key={`${hit.doc_id}-${hit.rank}`}
                hit={hit}
                selected={i === selected}
                onSelect={() => setSelected(i)}
                onOpen={() => void open(hit)}
              />
            ))}
          </ul>
        )}

        {hits.length === 0 && response?.empty_reason && (
          <EmptyState reason={response.empty_reason} onRetry={setOverride} />
        )}

        {hits.length === 0 && !response && !error && (
          <div className="empty">
            <div className="empty-headline">Muster</div>
            <div className="empty-detail dim">Start typing.</div>
          </div>
        )}

        {error && (
          <div className="empty">
            <div className="empty-headline">Cannot reach the index</div>
            <div className="empty-detail">{error}</div>
            <div className="empty-detail dim">
              Start it with <code>muster serve</code>.
            </div>
          </div>
        )}
      </div>

      <Footer response={response} mode={mode} error={error} />

      {showActions && (
        <Actions hit={current} actions={actions} onClose={() => setShowActions(false)} />
      )}
    </div>
  );
}
