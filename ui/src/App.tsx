import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api, type Hit, type Mode, type SearchResponse } from "./api";
import { mockSearchFor, mockStatus } from "./mock";
import { Actions, type Action } from "./components/Actions";
import { DocView, type View } from "./components/DocView";
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
  // Needed to turn a content hash into a path Finder can reveal. Fetched once;
  // a search should never wait on it.
  const [blobDir, setBlobDir] = useState<string | null>(null);
  /**
   * What is showing instead of the results, innermost last.
   *
   * A stack rather than a single view, so following a message to its PDF
   * attachment and pressing escape goes back to the message rather than all the
   * way out to the list.
   */
  const [stack, setStack] = useState<View[]>([]);

  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const resultsRef = useRef<HTMLDivElement>(null);
  // Where the list was when the reader left it. Restored on the way back, so
  // returning from a document does not silently scroll them to the top.
  const listScroll = useRef(0);
  // Guards against a slow response for an old query overwriting a fast one for
  // the query the user is actually looking at.
  const latest = useRef(0);

  const hits = response?.results ?? [];
  const reading = stack.length > 0;
  const current: Hit | null = hits[selected] ?? null;

  useEffect(() => inputRef.current?.focus(), []);

  /**
   * Take focus on every summon, not just the first.
   *
   * The panel is hidden and re-shown rather than rebuilt, so the webview is
   * mounted exactly once and the effect above fires exactly once. Without this
   * the second summon draws a panel whose text field is not the first responder
   * — it appears, and then silently eats everything typed into it.
   *
   * A window `focus` listener is the belt to the event's braces: it also covers
   * focus arriving without a summon, such as clicking the panel back to front.
   */
  useEffect(() => {
    if (USE_MOCK) return;
    const focus = () => {
      // A frame late, because the field cannot be made first responder until
      // the panel it lives in has actually become key.
      requestAnimationFrame(() => inputRef.current?.focus());
    };
    const summoned = () => {
      // Every summon is a fresh prompt. Dismissing with the hotkey hides the
      // panel from Rust without running hide() below, so without this it comes
      // back still holding the last query and its results.
      setQuery("");
      setResponse(null);
      setStack([]);
      focus();
    };
    // Only focus, not clear: regaining focus by clicking the panel back to the
    // front should not throw away what is already typed in it.
    window.addEventListener("focus", focus);
    const unlisten = import("@tauri-apps/api/event").then(({ listen }) =>
      listen("muster://shown", summoned),
    );
    return () => {
      window.removeEventListener("focus", focus);
      void unlisten.then((stop) => stop());
    };
  }, []);

  useEffect(() => {
    if (USE_MOCK) {
      setBlobDir(mockStatus.blob_dir);
      return;
    }
    api.status().then((s) => setBlobDir(s.blob_dir)).catch(() => {});
  }, []);

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
    // Clear on the way out so the next summon is a fresh prompt rather than
    // whatever was left behind.
    setQuery("");
    setResponse(null);
    setStack([]);
    if (USE_MOCK) return;
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("hide_panel");
    } catch (cause) {
      // Swallowing this leaves a panel on screen that will not close and no
      // clue why. It is the one error the user cannot work around.
      setError(`Could not close the panel: ${cause}`);
    }
  }, []);

  /** Read a document inside the panel. */
  const read = useCallback((hit: Hit | null) => {
    if (!hit) return;
    listScroll.current = resultsRef.current?.scrollTop ?? 0;
    setStack([{ kind: "doc", id: hit.doc_id }]);
  }, []);

  const back = useCallback(() => {
    setStack((current) => current.slice(0, -1));
  }, []);

  /**
   * The query field gives up focus while a document is open.
   *
   * It stays on screen as context, but if it kept focus then j, k and c would
   * be typed into it as well as acted on — the reader would scroll and edit
   * their query at the same time.
   */
  useEffect(() => {
    if (reading) inputRef.current?.blur();
    else inputRef.current?.focus();
  }, [reading]);

  // Restore the list exactly where it was, once it is on screen again.
  useEffect(() => {
    if (stack.length === 0 && resultsRef.current) {
      resultsRef.current.scrollTop = listScroll.current;
    }
  }, [stack.length]);

  /** Open the original in whatever handles its type. */
  const open = useCallback(
    async (hit: Hit | null) => {
      if (!hit?.content_hash) return;
      const url = api.blobUrl(hit.content_hash);
      if (USE_MOCK) {
        console.info("open", url);
        return;
      }
      const { openUrl } = await import("@tauri-apps/plugin-opener");
      await openUrl(url);
      await hide();
    },
    [hide],
  );

  /**
   * Reveal the stored original in Finder.
   *
   * Finder takes a path, not a URL, which is why /status exposes blob_dir: the
   * store is content-addressed, so the hash plus that root is the whole path.
   */
  const reveal = useCallback(
    async (hit: Hit | null) => {
      if (!hit?.content_hash || !blobDir) return;
      const path = api.blobPath(blobDir, hit.content_hash);
      if (USE_MOCK) {
        console.info("reveal", path);
        return;
      }
      const { revealItemInDir } = await import("@tauri-apps/plugin-opener");
      await revealItemInDir(path);
      await hide();
    },
    [blobDir, hide],
  );

  const actions: Action[] = useMemo(() => {
    if (!current) return [];
    const list: Action[] = [
      { id: "read", label: "Read here", hint: "↵", run: () => read(current) },
      { id: "reveal", label: "Reveal in Finder", hint: "⌘↵", run: () => reveal(current) },
      // Still offered: reading it here is the default, not a replacement for
      // whatever the reader actually wants to do with the file.
      { id: "open", label: "Open in default app", run: () => open(current) },
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
  }, [current, open, reveal, read]);

  /**
   * Escape lives on the document, not on the container below.
   *
   * A React handler on `.app` only sees keys when focus is already somewhere
   * inside it. If focus is anywhere else the press is simply lost, which is
   * exactly the "escape does nothing" the panel shipped with. Escape always
   * closes — clearing the text on the first press and closing on the second
   * reads as a dead key to anyone who meant to dismiss it.
   */
  useEffect(() => {
    const onEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      // The actions palette owns Escape while it is open: it should close on
      // its own without taking the panel with it.
      if (showActions) return;
      // Reading something? Escape is "back", not "close". Only a reader who is
      // already looking at the results means dismiss.
      if (stack.length) {
        back();
        return;
      }
      void hide();
    };
    document.addEventListener("keydown", onEscape);
    return () => document.removeEventListener("keydown", onEscape);
  }, [showActions, hide, stack.length, back]);

  function onKeyDown(event: React.KeyboardEvent) {
    if (showActions) return;
    // The document view binds its own keys — j/k, enter into an attachment,
    // copy — and they mean different things there than they do over a list.
    if (stack.length) return;
    const meta = event.metaKey || event.ctrlKey;

    if (event.key === "ArrowDown") {
      event.preventDefault();
      setSelected((i) => Math.min(i + 1, hits.length - 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setSelected((i) => Math.max(i - 1, 0));
    } else if (event.key === "Enter") {
      event.preventDefault();
      // Enter reads it here. Leaving the panel to open Gmail or Preview is the
      // failure this view exists to remove; ⌘↵ still reveals the original.
      void (meta ? reveal(current) : read(current));
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
          readOnly={reading}
          spellCheck={false}
          autoComplete="off"
        />
        {response?.query.searched !== response?.query.raw && response && (
          <span className="rewritten" title="the date became a filter">
            {response.query.searched}
          </span>
        )}
      </div>

      <div className={`results${reading ? " reading" : ""}`} ref={resultsRef}>
        {reading ? (
          <DocView
            view={stack[stack.length - 1]!}
            query={query}
            blobDir={blobDir}
            onOpen={(next) => setStack((current) => [...current, next])}
            onExit={back}
          />
        ) : (
        <>
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
        </>
        )}
      </div>

      <Footer response={response} mode={mode} error={error} reading={stack.length > 0} />

      {showActions && (
        <Actions hit={current} actions={actions} onClose={() => setShowActions(false)} />
      )}
    </div>
  );
}
