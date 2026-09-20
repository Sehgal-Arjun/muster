import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api, type DocResponse, type Hit, type Mode, type SearchResponse } from "./api";
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
/**
 * The panel opens far more often than mail arrives, so asking on every summon
 * would spend a network round trip to learn nothing. Fifteen minutes is the
 * scheduled interval; two is short enough that a summon after a quiet spell
 * still gets fresh mail, and long enough that reopening the panel four times in
 * a minute costs one request.
 */
const REFRESH_EVERY_MS = 2 * 60 * 1000;

/**
 * The last search, kept across restarts of the app itself.
 *
 * In-memory state already survives the panel being hidden, because the webview
 * is not remounted. This is for the other case — a reboot, a rebuild, a crash —
 * where the alternative is an empty box and no idea what you last asked.
 *
 * Only the question is stored — never the answers, which go stale and cost
 * about half a second to ask for again, and never the mode, which resets.
 */
const LAST_SEARCH = "muster.lastSearch";

function restoreQuery(): string {
  try {
    const saved = JSON.parse(localStorage.getItem(LAST_SEARCH) ?? "");
    return typeof saved?.query === "string" ? saved.query : "";
  } catch {
    // Nothing saved, or saved by an older version. Not worth reporting.
    return "";
  }
}

export default function App() {
  const [query, setQuery] = useState(restoreQuery);
  const [mode, setMode] = useState<Mode>("hybrid");
  const [response, setResponse] = useState<SearchResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState(0);
  const [showActions, setShowActions] = useState(false);
  const [override, setOverride] = useState<Record<string, string | number> | null>(null);
  // Needed to turn a content hash into a path Finder can reveal. Fetched once;
  // a search should never wait on it.
  const [blobDir, setBlobDir] = useState<string | null>(null);
  const [searching, setSearching] = useState(false);
  /**
   * False for the few seconds after the server restarts, while bge-m3 loads.
   * The first search then takes ~25s, and without saying so the panel draws an
   * empty list — which reads as "nothing found" rather than "not ready yet".
   */
  const [modelReady, setModelReady] = useState(true);
  /**
   * What is showing instead of the results, innermost last.
   *
   * A stack rather than a single view, so following a message to its PDF
   * attachment and pressing escape goes back to the message rather than all the
   * way out to the list.
   */
  const [stack, setStack] = useState<View[]>([]);
  // What the document view is showing, so the footer can name the keys for it.
  const [openDoc, setOpenDoc] = useState<DocResponse | null>(null);
  const [syncing, setSyncing] = useState(false);
  // When the last refresh was asked for, so summoning the panel repeatedly does
  // not mean a request each time.
  const lastRefresh = useRef(0);

  /**
   * Ask the server to pull new mail. Never awaited by anything the user is
   * waiting on — a search box that blocks on the network to become usable is
   * worse than an index that is a few minutes stale.
   */
  const refresh = useCallback(async () => {
    if (USE_MOCK) return;
    const now = Date.now();
    if (now - lastRefresh.current < REFRESH_EVERY_MS) return;
    lastRefresh.current = now;
    try {
      const job = await api.refresh();
      if (job.state !== "running") return;
      setSyncing(true);
      // Polled rather than pushed: the indicator is decoration, and a websocket
      // for it would be more machinery than the thing it reports on.
      const poll = setInterval(async () => {
        try {
          const status = await api.syncJob(job.job);
          if (status.state !== "running") {
            clearInterval(poll);
            setSyncing(false);
          }
        } catch {
          clearInterval(poll);
          setSyncing(false);
        }
      }, 1500);
    } catch {
      // The server being down is already visible on the next search; a failed
      // background refresh should not put an error over the results.
      setSyncing(false);
    }
  }, []);

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
      // The last search is still there. Closing the panel to go and read
      // something is not the same as being finished with what you looked up,
      // and retyping it is a small tax on the most common thing anyone does.
      //
      // Selected rather than cleared, so typing still replaces it in one
      // keystroke — you get the result back if you want it, and pay nothing if
      // you don't.
      requestAnimationFrame(() => {
        inputRef.current?.focus();
        inputRef.current?.select();
      });
      // The question is kept; the mode is not. Keyword and vector are
      // experiments you run on one query, and one left switched on made every
      // later search quietly worse with a word in the footer as the only clue.
      // Carrying that across summons is the bug, not the feature.
      setMode("hybrid");
      void refresh();
    };
    // Only focus, not clear: regaining focus by clicking the panel back to the
    // front should not throw away what is already typed in it.
    window.addEventListener("focus", focus);
    const unlisten = import("@tauri-apps/api/event").then(({ listen }) =>
      listen("muster://shown", summoned),
    );
    // The first summon is the mount, which fires no event.
    void refresh();
    return () => {
      window.removeEventListener("focus", focus);
      void unlisten.then((stop) => stop());
    };
  }, [refresh]);

  useEffect(() => {
    if (USE_MOCK) {
      setBlobDir(mockStatus.blob_dir);
      return;
    }
    api
      .status()
      .then((s) => {
        setBlobDir(s.blob_dir);
        setModelReady(s.embedding.ready !== false);
      })
      .catch(() => {});
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
      setSearching(true);
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
      } finally {
        if (ticket === latest.current) setSearching(false);
      }
    }, DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [query, mode, override]);

  // A new query invalidates a "show weak matches" override.
  useEffect(() => setOverride(null), [query, mode]);

  useEffect(() => {
    try {
      localStorage.setItem(LAST_SEARCH, JSON.stringify({ query }));
    } catch {
      // A full or disabled store is not a reason to break searching.
    }
  }, [query]);

  useEffect(() => {
    listRef.current
      ?.querySelector<HTMLElement>(".row.selected")
      ?.scrollIntoView({ block: "nearest" });
  }, [selected]);

  const hide = useCallback(async () => {
    // Deliberately keeps the query, the results and any open document. What was
    // on screen is what should be there on the way back in.
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

  /**
   * Where a hit's bytes actually are.
   *
   * A result carries a content hash, which is enough for mail — but a document
   * from the filesystem keeps no blob, so the hash addresses nothing. One
   * request to /doc answers it for either kind rather than guessing from the
   * hash and 404ing on half the index.
   */
  const locate = useCallback(async (hit: Hit) => {
    const doc = await api.doc(hit.doc_id);
    return { path: doc.provenance?.path ?? null, url: doc.blob?.url ?? null };
  }, []);

  /** Open the original in whatever handles its type. */
  const open = useCallback(
    async (hit: Hit | null) => {
      if (!hit) return;
      if (USE_MOCK) {
        console.info("open", hit.doc_id);
        return;
      }
      try {
        const opener = await import("@tauri-apps/plugin-opener");
        if (hit.kind === "message" && hit.source === "gmail") {
          const id = hit.sources[0]?.source_uid.split("#")[0];
          if (id) {
            await opener.openUrl(`https://mail.google.com/mail/u/0/#all/${id}`);
            await hide();
            return;
          }
        }
        const { path, url } = await locate(hit);
        if (path) await opener.openPath(path);
        else if (url) await opener.openUrl(api.bytesUrl(url));
        else return;
        await hide();
      } catch (cause) {
        setError(`Could not open: ${cause}`);
      }
    },
    [hide, locate],
  );

  /**
   * Reveal the stored original in Finder.
   *
   * Finder takes a path, not a URL, which is why /status exposes blob_dir: the
   * store is content-addressed, so the hash plus that root is the whole path.
   */
  const reveal = useCallback(
    async (hit: Hit | null) => {
      if (!hit) return;
      if (USE_MOCK) {
        console.info("reveal", hit.doc_id);
        return;
      }
      try {
        // Mail has no file to reveal. Its blob is a content-addressed .eml in a
        // directory that means nothing to a reader, so it opens where it
        // actually lives instead.
        if (hit.kind === "message" && hit.source === "gmail") {
          await open(hit);
          return;
        }
        const { path } = await locate(hit);
        const target =
          path ?? (hit.content_hash && blobDir ? api.blobPath(blobDir, hit.content_hash) : null);
        if (!target) return;
        const { revealItemInDir } = await import("@tauri-apps/plugin-opener");
        await revealItemInDir(target);
        await hide();
      } catch (cause) {
        setError(`Could not reveal: ${cause}`);
      }
    },
    [blobDir, hide, locate, open],
  );

  const actions: Action[] = useMemo(() => {
    if (!current) return [];
    const list: Action[] = [
      { id: "read", label: "Read here", hint: "↵", run: () => read(current) },
      { id: "reveal", label: "Reveal in Finder", hint: "⌘↵", run: () => reveal(current) },
      // Still offered: reading it here is the default, not a replacement for
      // whatever the reader actually wants to do with the file.
      { id: "open", label: "Open in default app", hint: "⌘O", run: () => open(current) },
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
    } else if (meta && event.key.toLowerCase() === "o") {
      event.preventDefault();
      void open(current);
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
            onDoc={setOpenDoc}
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
                // Clicking a result reads it, exactly as Enter does. Sending
                // the reader straight out to Preview or Gmail skips the view
                // built for the purpose, and is not what a click means anywhere
                // else in a list.
                onOpen={() => read(hit)}
              />
            ))}
          </ul>
        )}

        {hits.length === 0 && response?.empty_reason && (
          <EmptyState reason={response.empty_reason} onRetry={setOverride} />
        )}

        {/* A search in flight is not an empty result list. Drawing one is how
            a slow first query — the model loading after a restart — became
            indistinguishable from a document that was not there. */}
        {hits.length === 0 && !response && !error && searching && (
          <div className="empty">
            <div className="empty-headline">Searching…</div>
            {!modelReady && (
              <div className="empty-detail dim">
                First search since the server started — loading the embedding
                model. This one is slow; the rest are not.
              </div>
            )}
          </div>
        )}

        {hits.length === 0 && !response && !error && !searching && (
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

      <Footer
          response={response}
          mode={mode}
          error={error}
          reading={reading}
          readingMail={openDoc?.mime === "message/rfc822"}
          syncing={syncing}
        />

      {showActions && (
        <Actions hit={current} actions={actions} onClose={() => setShowActions(false)} />
      )}
    </div>
  );
}
