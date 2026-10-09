import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  api,
  type DocResponse,
  type FolderHit,
  type Hit,
  type Mode,
  type ProcessSummary,
  type SearchResponse,
} from "./api";
import { mockSearchFor, mockStatus } from "./mock";
import { Actions, type Action } from "./components/Actions";
import { DocView, type View } from "./components/DocView";
import { EmptyState } from "./components/EmptyState";
import { FolderJump } from "./components/FolderJump";
import { GatherView } from "./components/GatherView";
import { HomeView } from "./components/HomeView";
import { Keys } from "./components/Keys";
import { Placed, PlaceView } from "./components/PlaceView";
import {
  applyLayout, applyPinned, storeLayout, storedLayout, storedPinned, type Layout,
} from "./layout";
import { ProcessPicker } from "./components/ProcessPicker";
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
/**
 * How many matches to ask for.
 *
 * Ten is the server's default and the number the eval harness scores at. The
 * list is a different thing from a benchmark: newest-first over ten rows is
 * "the most recent of the ten best matches", which is a thinner promise than it
 * looks. Twenty-five gives the order something to work with, and the ranker is
 * untouched either way — this is how many of its answers to show, not how it
 * decided them.
 */
const LIST_LIMIT = 25;
const DEBOUNCE_MS = 120;
/**
 * The panel opens far more often than mail arrives, so asking on every summon
 * would spend a network round trip to learn nothing. Fifteen minutes is the
 * scheduled interval; two is short enough that a summon after a quiet spell
 * still gets fresh mail, and long enough that reopening the panel four times in
 * a minute costs one request.
 */
const REFRESH_EVERY_MS = 2 * 60 * 1000;
/** How long to give launchd to bring the server up before trying again. */
const SERVER_START_MS = 1200;

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

/**
 * How the list is ordered. Presentation, not retrieval.
 *
 * "rank" is what the ranker decided — the order the eval harness measures, and
 * the only order in which the first result is a claim about relevance. "newest"
 * re-orders those same matches by date, which is what reading a queue of mail
 * wants: what arrived most recently, first.
 *
 * Worth being exact about, because the two are easy to confuse: this reorders
 * what matched. It does not change what matched. The tenth-best match by
 * relevance can be top of the list if it is the most recent, but an eleventh
 * that never came back is still not there.
 */
const SORT_KEY = "muster.sort";

type Sort = "rank" | "newest";

function storedSort(): Sort {
  try {
    return localStorage.getItem(SORT_KEY) === "rank" ? "rank" : "newest";
  } catch {
    return "newest";
  }
}

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
  /** The full key list, which the footer is deliberately too small to be. */
  const [showKeys, setShowKeys] = useState(false);
  /**
   * Files dropped on the panel, waiting to be placed. A queue, because dropping
   * four things at once is one gesture and four decisions.
   */
  const [dropped, setDropped] = useState<string[]>([]);
  const [placeFlash, setPlaceFlash] = useState<string | null>(null);
  /** Something is being dragged over the panel right now. */
  const [dragging, setDragging] = useState(false);
  /** The move just made, shown as a result rather than a disappearing toast. */
  const [placed, setPlaced] = useState<
    { name: string; to: string; move: number | null } | null
  >(null);
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
  // Guards against asking launchd to start the server on every keystroke while
  // it is already starting.
  const startingServer = useRef(false);
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
  /** A directory the query might have meant. Never mixed into the results. */
  const [folder, setFolder] = useState<FolderHit | null>(null);
  /**
   * Gather mode. Null means the results list; a slug or the empty string means
   * the state of a process rather than a list of matches.
   */
  const [gatherSlug, setGatherSlug] = useState<string | null>(null);
  const [processes, setProcesses] = useState<ProcessSummary[]>([]);
  /**
   * The open item whose document is being read, so ⌘D still settles it.
   *
   * In the one-column layout the process view is unmounted behind the reader and
   * its key bindings go with it — so opening an item to check what it was, then
   * pressing done, did nothing at all.
   */
  const [readingItem, setReadingItem] = useState<{ slug: string; what: string } | null>(null);
  const [picking, setPicking] = useState(false);
  const [syncing, setSyncing] = useState(false);
  /**
   * "stack" is the launcher: one column, a document replaces the list. "split"
   * keeps the list on the left and renders the document beside it, which is what
   * reading mail rather than finding it actually wants.
   */
  const [layout, setLayout] = useState<Layout>(storedLayout);
  const [sort, setSort] = useState<Sort>(storedSort);
  /** Stay up when focus goes elsewhere — needed to drag a file in from Finder. */
  const [pinned, setPinned] = useState(storedPinned);
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

  /**
   * Bring the server back rather than telling the reader to.
   *
   * "No server at 127.0.0.1:8765. Run `muster serve`." is a poor answer to
   * someone who has just pressed a hotkey. launchd owns the process, so this
   * only asks it to get on with it — the server stays a separate thing the
   * panel talks to, never something bundled into the app.
   *
   * Returns true if it is worth retrying the request.
   */
  const reviveServer = useCallback(async (): Promise<boolean> => {
    if (USE_MOCK || startingServer.current) return false;
    startingServer.current = true;
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("start_server");
      await new Promise((done) => setTimeout(done, SERVER_START_MS));
      return true;
    } catch {
      // launchd has no such job: the installer has never run. The ordinary
      // error message is the right one, so let it stand.
      return false;
    } finally {
      startingServer.current = false;
    }
  }, []);

  /**
   * Pick the panel up and move it.
   *
   * There is no titlebar — that is the point of a panel — so the query bar and
   * the footer are the handles. Only their own background: a mousedown that
   * landed on the text field or a button belongs to that control, and a window
   * that moves when you try to place the cursor is worse than one that cannot
   * move at all.
   */
  const beginDrag = useCallback(async (event: React.MouseEvent) => {
    if (event.target !== event.currentTarget) return;
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("start_drag");
    } catch {
      // A browser, running against the fixtures. Nothing to drag.
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

  const hits = useMemo(() => {
    const results = response?.results ?? [];
    if (sort === "rank") return results;
    // Stable: equal dates keep the ranker's order, so the better match of two
    // things sent the same day is still the higher one. Undated last — a
    // document with no date is not new, it is unknown.
    return [...results].sort((a, b) => {
      if (!a.date && !b.date) return a.rank - b.rank;
      if (!a.date) return 1;
      if (!b.date) return -1;
      return a.date === b.date ? a.rank - b.rank : a.date < b.date ? 1 : -1;
    });
  }, [response, sort]);
  const reading = stack.length > 0;
  /**
   * The document the list is pointing at, for the row highlight in split mode:
   * the outermost one, so following a message to its attachment still marks the
   * message you came from rather than nothing at all.
   */
  const activeDocId = (() => {
    for (const view of stack) if (view.kind === "doc") return view.id;
    return null;
  })();
  const gathering = gatherSlug !== null || picking;
  const current: Hit | null = hits[selected] ?? null;

  useEffect(() => inputRef.current?.focus(), []);

  // Dropped files arrive from the window rather than the DOM: Tauri's own
  // drag-drop handling swallows the HTML events.
  useEffect(() => {
    if (USE_MOCK) return;
    let stop: (() => void) | undefined;
    void (async () => {
      const { listen } = await import("@tauri-apps/api/event");
      stop = await listen<string[]>("muster://dropped", (event) => {
        setDragging(false);
        setDropped(event.payload.filter(Boolean));
      });
    })();
    return () => stop?.();
  }, []);

  // Drag over and away, so the panel can show it is a target instead of leaving
  // you to find out by letting go.
  useEffect(() => {
    if (USE_MOCK) return;
    let stop: (() => void) | undefined;
    void (async () => {
      const { listen } = await import("@tauri-apps/api/event");
      stop = await listen<string>("muster://dragging", (event) => {
        setDragging(event.payload === "over");
      });
    })();
    return () => stop?.();
  }, []);

  useEffect(() => {
    if (!placeFlash) return;
    const timer = setTimeout(() => setPlaceFlash(null), 4000);
    return () => clearTimeout(timer);
  }, [placeFlash]);

  // The window has to change shape, not just the stylesheet: a message at 720
  // points is a column of six words.
  useEffect(() => {
    storeLayout(layout);
    void applyLayout(layout);
  }, [layout]);

  // Also pinned while a dropped file is waiting to be placed: the queue exists
  // because you went to another window to fetch something, and the next thing you
  // do may be fetching another.
  useEffect(() => {
    void applyPinned(pinned || dropped.length > 0);
  }, [pinned, dropped.length]);

  useEffect(() => {
    try {
      localStorage.setItem(SORT_KEY, sort);
    } catch {
      // Applies for this session regardless.
    }
    setSelected(0);
  }, [sort]);

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
      // Checked on the way in, so the first keystroke does not have to pay for
      // discovering the server is absent.
      void api.status().catch(() => reviveServer());
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
  }, [refresh, reviveServer]);

  const refreshProcesses = useCallback(async () => {
    if (USE_MOCK) return;
    try {
      setProcesses((await api.processes()).processes);
    } catch {
      // The list is a convenience for matching Tab; failing to load it should
      // not break searching.
    }
  }, []);

  useEffect(() => {
    void refreshProcesses();
  }, [refreshProcesses]);

  /**
   * The process a query is asking about, if any.
   *
   * Matched on the name rather than searched for: a process is a thing the user
   * named, and guessing which one they meant by similarity would make the same
   * keystrokes open different processes on different days.
   */
  const matchProcess = useCallback(
    (text: string): string | null => {
      const wanted = text.trim().toLowerCase();
      if (!wanted) return null;
      const exact = processes.find((p) => p.name.toLowerCase() === wanted);
      if (exact) return exact.slug;
      const partial = processes.find(
        (p) => p.name.toLowerCase().includes(wanted) || wanted.includes(p.name.toLowerCase()),
      );
      return partial?.slug ?? null;
    },
    [processes],
  );

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
      // Emptying the box is a cancellation, not a pause. Without bumping the
      // ticket, a search already in flight came back and repopulated results for
      // a query that no longer existed — and `searching` stayed true meanwhile,
      // so the panel said "Searching…" over an empty box instead of going home.
      latest.current += 1;
      setResponse(null);
      setError(null);
      setSearching(false);
      setSelected(0);
      return;
    }
    const ticket = ++latest.current;
    const timer = setTimeout(async () => {
      setSearching(true);
      try {
        const body = USE_MOCK
          ? mockSearchFor(q)
          : await api.search({ q, mode, limit: LIST_LIMIT, ...(override ?? {}) });
        if (ticket !== latest.current) return;
        setResponse(body);
        setError(null);
        setSelected(0);
      } catch (cause) {
        if (ticket !== latest.current) return;
        // The server being down is recoverable: start it and ask once more
        // before reporting anything.
        if (cause instanceof ApiError && cause.code === "unreachable") {
          if (await reviveServer()) {
            try {
              const body = await api.search({
                q, mode, limit: LIST_LIMIT, ...(override ?? {}),
              });
              if (ticket !== latest.current) return;
              setResponse(body);
              setError(null);
              setSelected(0);
              return;
            } catch (second) {
              setError(second instanceof ApiError ? second.message : String(second));
              return;
            }
          }
        }
        setResponse(null);
        setError(cause instanceof ApiError ? cause.message : String(cause));
      } finally {
        if (ticket === latest.current) setSearching(false);
      }
    }, DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [query, mode, override, reviveServer]);

  useEffect(() => {
    const q = query.trim();
    if (!q || USE_MOCK) {
      setFolder(null);
      return;
    }
    const timer = setTimeout(() => {
      api
        .folders(q)
        .then((body) => setFolder(body.folders[0] ?? null))
        // A missing suggestion is not worth an error: the results are the point.
        .catch(() => setFolder(null));
    }, DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [query]);

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

  const openFolder = useCallback(async () => {
    if (!folder) return;
    if (USE_MOCK) {
      console.info("open folder", folder.path);
      return;
    }
    try {
      const { openPath } = await import("@tauri-apps/plugin-opener");
      await openPath(folder.path);
      await hide();
    } catch (cause) {
      setError(`Could not open ${folder.display}: ${cause}`);
    }
  }, [folder, hide]);

  /** Read a document inside the panel. */
  /**
   * In split mode the selection *is* the preview.
   *
   * The list is a reading queue there, and needing to press enter on each one
   * turns walking a queue into a series of decisions. Debounced, so holding the
   * down arrow does not fetch every row it passes through.
   */
  useEffect(() => {
    if (layout !== "split") return;
    const hit = hits[selected];
    if (!hit || stack.length > 1 || activeDocId === hit.doc_id) return;
    const timer = setTimeout(() => setStack([{ kind: "doc", id: hit.doc_id }]), 140);
    return () => clearTimeout(timer);
  }, [layout, hits, selected, stack.length, activeDocId]);

  const read = useCallback((hit: Hit | null) => {
    if (!hit) return;
    listScroll.current = resultsRef.current?.scrollTop ?? 0;
    setStack([{ kind: "doc", id: hit.doc_id }]);
  }, []);

  /*
   * ⌘\ and ⌘H are bound at the document only, never here as well.
   *
   * Both handlers see the same keystroke — React's runs as it bubbles to the app
   * container, the listener runs when it reaches the document — so a key bound in
   * both places fired twice. ⌘H survived that, being idempotent. ⌘\ toggled the
   * layout and toggled it straight back, which looks exactly like a key that does
   * nothing.
   */

  /**
   * Back to the resting state: no query, no process, nothing open.
   *
   * Reachable from every screen, because the alternative is pressing Escape an
   * uncertain number of times and hoping.
   */
  const goHome = useCallback(() => {
    setQuery("");
    setResponse(null);
    setStack([]);
    setGatherSlug(null);
    setPicking(false);
    setMode("hybrid");
    requestAnimationFrame(() => inputRef.current?.focus());
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
    // Not in split mode: there the document sits beside the list rather than in
    // front of it, and taking focus away would stop the reader typing a new
    // query without first dismissing what they are reading.
    if (reading && layout !== "split") inputRef.current?.blur();
    else inputRef.current?.focus();
  }, [reading, layout]);

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
      if (
        (event.metaKey || event.ctrlKey)
        && event.key.toLowerCase() === "d"
        && readingItem
        && stack.length > 0
      ) {
        // Reading the document an item came from, in the layout where the list
        // is not on screen. Settling it here means the same thing it means there.
        event.preventDefault();
        const settling = readingItem;
        void api
          .dismissItem(settling.slug, settling.what, { verdict: "done" })
          .then(() => {
            setReadingItem(null);
            setStack([]);      // back to the process, where the change shows
          })
          .catch(() => undefined);
        return;
      }
      if (
        (event.metaKey || event.ctrlKey) && event.shiftKey
        && event.key.toLowerCase() === "p"
      ) {
        event.preventDefault();
        setPinned((on) => !on);
        return;
      }
      if ((event.metaKey || event.ctrlKey) && event.key === "/") {
        event.preventDefault();
        setShowKeys((open) => !open);
        return;
      }
      if ((event.metaKey || event.ctrlKey) && event.key === "\\") {
        event.preventDefault();
        setLayout((current) => (current === "split" ? "stack" : "split"));
        return;
      }
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "h") {
        // Bound at the document too: the reader and the picker both stop the
        // container handler, and "go back to the start" should work anywhere.
        event.preventDefault();
        goHome();
        return;
      }
      if (
        (event.metaKey || event.ctrlKey)
        && event.key.toLowerCase() === "z"
        && placed?.move != null
      ) {
        // Muster performed the move, so Muster can take it back.
        event.preventDefault();
        const move = placed.move;
        setPlaced(null);
        void api
          .placeUndo(move)
          .then(() => setPlaceFlash("Put back where it was"))
          .catch(() => setPlaceFlash("Could not put it back"));
        return;
      }
      if (event.key !== "Escape") return;
      event.preventDefault();
      // The key list is reference material laid over whatever you were doing;
      // escape should put it away and leave that untouched.
      if (showKeys) {
        setShowKeys(false);
        return;
      }
      if (placed) {
        setPlaced(null);
        return;
      }
      if (dropped.length) {
        // Leaves the file exactly where it was: nothing has been moved yet.
        setDropped((queue) => queue.slice(1));
        return;
      }
      // The actions palette owns Escape while it is open: it should close on
      // its own without taking the panel with it.
      if (showActions) return;
      // Reading something? Escape is "back", not "close". Only a reader who is
      // already looking at the results means dismiss.
      if (picking) {
        setPicking(false);
        return;
      }
      // Split mode: the preview follows the selection, so backing out of it
      // would only re-open it a moment later and Escape would look broken.
      // Escape there means what it means in the list — the level above, or out.
      if (stack.length > (layout === "split" ? 1 : 0)) {
        back();
        return;
      }
      if (gatherSlug !== null) {
        setGatherSlug(null);
        return;
      }
      void hide();
    };
    document.addEventListener("keydown", onEscape);
    return () => document.removeEventListener("keydown", onEscape);
  }, [showActions, hide, stack.length, back, gatherSlug, picking, goHome, layout, readingItem, showKeys, dropped.length, placed]);

  function onKeyDown(event: React.KeyboardEvent) {
    if (showActions) return;
    // The document view binds its own keys — j/k, enter into an attachment,
    // copy — and they mean different things there than they do over a list.
    if (stack.length) return;
    // Gather mode is not a list of hits. Leaving list navigation live behind it
    // meant Enter opened whichever search result happened to be selected,
    // which is not the thing on screen.
    if (picking) return;
    if (gatherSlug !== null && event.key !== "Tab") return;
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
    } else if (meta && event.key.toLowerCase() === "s") {
      event.preventDefault();
      setSort((current) => (current === "newest" ? "rank" : "newest"));
    } else if (meta && event.key.toLowerCase() === "p") {
      event.preventDefault();
      setPicking(true);
    } else if (event.key === "Tab") {
      // Promote the query from "find me the thing" to "what is the state of the
      // thing". Tab because it reads as going deeper into what is already there.
      event.preventDefault();
      if (query.trim()) setGatherSlug(matchProcess(query) ?? "");
    } else if (meta && event.shiftKey && event.key.toLowerCase() === "f") {
      event.preventDefault();
      void openFolder();
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

  const split = layout === "split";
  // In split mode the list stays put and the document appears beside it, so the
  // reader is not "in" a document — they are looking at one from the list.
  const showDocInPane = split && reading;

  const listContent = (
    <>
      {hits.length > 0 && (
        <ul className="rows" ref={listRef} role="listbox">
          {hits.map((hit, i) => (
            <ResultRow
              key={`${hit.doc_id}-${hit.rank}`}
              hit={hit}
              selected={i === selected}
              onSelect={() => setSelected(i)}
              // Clicking a result reads it, exactly as Enter does. Sending the
              // reader straight out to Preview or Gmail skips the view built for
              // the purpose, and is not what a click means anywhere else in a
              // list.
              onOpen={() => read(hit)}
            />
          ))}
        </ul>
      )}

      {hits.length === 0 && response?.empty_reason && (
        <EmptyState reason={response.empty_reason} onRetry={setOverride} />
      )}

      {/* A search in flight is not an empty result list. Drawing one is how a
          slow first query — the model loading after a restart — became
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
        <HomeView onOpenProcess={(slug) => setGatherSlug(slug)} />
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
  );

  const picker = (
    <ProcessPicker
      processes={processes}
      query={query}
      onOpen={(slug) => {
        setPicking(false);
        setGatherSlug(slug);
      }}
      onCreated={() => void refreshProcesses()}
      onClose={() => setPicking(false)}
    />
  );

  const gatherPane = (
    <GatherView
      slug={gatherSlug || null}
      query={query}
      // So the row you are reading stays marked in the list beside it.
      activeDocId={split ? activeDocId : null}
      onOpenDoc={(docId, item) => {
        listScroll.current = resultsRef.current?.scrollTop ?? 0;
        setReadingItem(
          item && gatherSlug ? { slug: gatherSlug, what: item.what } : null,
        );
        setStack([{ kind: "doc", id: docId }]);
      }}
    />
  );

  const docPane = reading ? (
    <DocView
      view={stack[stack.length - 1]!}
      query={query}
      blobDir={blobDir}
      // The list keeps the arrows in split mode, and the query field keeps the
      // letters. A document that also claimed j and k would scroll while the
      // reader was typing.
      keys={!split}
      onOpen={(next) => setStack((current) => [...current, next])}
      onExit={back}
      onDoc={setOpenDoc}
    />
  ) : null;

  const searchBox = (
    <div className="search drag-handle" onMouseDown={beginDrag}>
        <input
          ref={inputRef}
          className="query"
          placeholder="Search everything you have ever received…"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          readOnly={reading || gathering}
          spellCheck={false}
          autoComplete="off"
        />
      {response?.query.searched !== response?.query.raw && response && (
        <span className="rewritten" title="the date became a filter">
          {response.query.searched}
        </span>
      )}
    </div>
  );

  const folderHint =
    folder && !reading && !gathering ? (
      <FolderJump folder={folder} onOpen={() => void openFolder()} />
    ) : null;

  const footer = (
    <Footer
      response={response}
      mode={mode}
      error={error}
      reading={reading && !split}
      readingMail={openDoc?.mime === "message/rfc822"}
      gathering={gatherSlug !== null}
      split={split}
      sort={sort}
      pinned={pinned || dropped.length > 0}
      syncing={syncing}
      onDrag={beginDrag}
    />
  );

  if (split) {
    return (
      <div className="app app-split" onKeyDown={onKeyDown}>
        <div className="split-body">
          <aside className="pane-side">
            {searchBox}
            {folder && !gathering && (
              <FolderJump folder={folder} onOpen={() => void openFolder()} />
            )}
            <div className="results" ref={resultsRef}>
              {picking ? picker : gatherSlug !== null ? gatherPane : listContent}
            </div>
          </aside>
          <main className="pane-main">
            {dropped.length > 0 ? (
              <PlaceView
                path={dropped[0]!}
                onDone={(result) => {
                  setPlaced(result);
                  setDropped((queue) => queue.slice(1));
                }}
                onCancel={() => setDropped((queue) => queue.slice(1))}
              />
            ) : showDocInPane ? (
              docPane
            ) : (
              <div className="empty pane-empty">
                <div className="empty-headline">Nothing open</div>
                <div className="empty-detail dim">
                  Select something on the left and it renders here, as it was
                  sent — attachments and all.
                </div>
                <div className="empty-detail dim">⌘\\ returns to one column.</div>
              </div>
            )}
          </main>
        </div>
        {footer}
        {showActions && (
          <Actions hit={current} actions={actions} onClose={() => setShowActions(false)} />
        )}
        {showKeys && <Keys onClose={() => setShowKeys(false)} />}
        {/* A drop target you can see. Dashed rather than solid, because the panel
            is not a well and the file is not falling in yet. */}
        {dragging && dropped.length === 0 && (
          <div className="drop-veil">
            <div className="drop-veil-inner">
                <div className="drop-title">Drop it here</div>
                <div className="dim">Muster will say where it thinks it belongs</div>
            </div>
          </div>
        )}

        {placed && (
          <Placed
            name={placed.name}
            to={placed.to}
            onUndo={
                placed.move == null
                  ? null
                  : async () => {
                        const move = placed.move as number;
                        setPlaced(null);
                        try {
                          await api.placeUndo(move);
                          setPlaceFlash("Put back where it was");
                        } catch {
                          setPlaceFlash("Could not put it back");
                        }
                    }
            }
            onClose={() => setPlaced(null)}
          />
        )}

        {placeFlash && <div className="doc-flash place-flash">{placeFlash}</div>}
      </div>
    );
  }

  return (
    <div className="app" onKeyDown={onKeyDown}>
      {searchBox}

      {folderHint}

      <div className={`results${reading || gathering || dropped.length ? " reading" : ""}`} ref={resultsRef}>
        {dropped.length > 0 ? (
          <PlaceView
            path={dropped[0]!}
            onDone={(result) => {
              setPlaced(result);
              setDropped((queue) => queue.slice(1));
            }}
            onCancel={() => setDropped((queue) => queue.slice(1))}
          />
        ) : picking
          ? picker
          : gatherSlug !== null && !reading
            ? gatherPane
            : reading
              ? docPane
              : listContent}
      </div>

      {footer}

      {showActions && (
        <Actions hit={current} actions={actions} onClose={() => setShowActions(false)} />
      )}

      {showKeys && <Keys onClose={() => setShowKeys(false)} />}

      {/* A drop target you can see. Dashed rather than solid, because the panel
          is not a well and the file is not falling in yet. */}
      {dragging && dropped.length === 0 && (
        <div className="drop-veil">
          <div className="drop-veil-inner">
            <div className="drop-title">Drop it here</div>
            <div className="dim">Muster will say where it thinks it belongs</div>
          </div>
        </div>
      )}

      {placed && (
        <Placed
          name={placed.name}
          to={placed.to}
          onUndo={
            placed.move == null
              ? null
              : async () => {
                  const move = placed.move as number;
                  setPlaced(null);
                  try {
                    await api.placeUndo(move);
                    setPlaceFlash("Put back where it was");
                  } catch {
                    setPlaceFlash("Could not put it back");
                  }
                }
          }
          onClose={() => setPlaced(null)}
        />
      )}

      {placeFlash && <div className="doc-flash place-flash">{placeFlash}</div>}
    </div>
  );
}
