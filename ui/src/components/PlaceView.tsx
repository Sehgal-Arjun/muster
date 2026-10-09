import { useCallback, useEffect, useState } from "react";
import { ApiError, api, type FolderHit, type PlaceProposal } from "../api";

/**
 * A file was dropped. Where should it live?
 *
 * Two halves, in the order they are useful: what the index thinks, and a way to
 * overrule it. The suggestions are ranked by evidence and each carries its
 * reasons, because a suggestion about where to put your file is worthless unless
 * you can see why it was made and disagree with it.
 *
 * The number of suggestions is not fixed. One when one is clearly ahead, several
 * when several are close, and none at all when nothing in the archive has
 * anything to say — at which point the filter is the whole answer rather than a
 * fallback.
 *
 * Nothing moves until you confirm, and what moved can be put back.
 */
export function PlaceView({
  path,
  onDone,
  onCancel,
}: {
  path: string;
  onDone: (message: string, undo: (() => Promise<void>) | null) => void;
  onCancel: () => void;
}) {
  const [proposal, setProposal] = useState<PlaceProposal | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [browsed, setBrowsed] = useState<FolderHit[]>([]);
  const [selected, setSelected] = useState(0);
  const [moving, setMoving] = useState(false);

  useEffect(() => {
    let live = true;
    setProposal(null);
    setError(null);
    api
      .placePropose(path)
      .then((body) => live && setProposal(body))
      .catch((cause) =>
        live && setError(cause instanceof ApiError ? cause.message : String(cause)),
      );
    return () => {
      live = false;
    };
  }, [path]);

  // Typing searches every folder Muster knows, not just the suggested ones —
  // the picker is the override, so it must be able to reach anywhere.
  useEffect(() => {
    const q = filter.trim();
    if (!q) {
      setBrowsed([]);
      return;
    }
    const timer = setTimeout(() => {
      api
        .folders(q)
        .then((body) => setBrowsed(body.folders))
        .catch(() => setBrowsed([]));
    }, 120);
    return () => clearTimeout(timer);
  }, [filter]);

  const options: Array<{ path: string; display: string; reasons: string[] }> =
    filter.trim()
      ? browsed.map((f) => ({ path: f.path, display: f.display, reasons: [] }))
      : (proposal?.candidates ?? []).map((c) => ({
          path: c.path,
          display: c.display,
          reasons: c.reasons,
        }));

  const confirm = useCallback(
    async (destination: string) => {
      if (moving) return;
      setMoving(true);
      try {
        const result = await api.placeConfirm(path, destination);
        const name = result.to.split("/").pop() ?? result.to;
        onDone(
          `Moved ${name} to ${destination.replace(/^.*\/([^/]+)$/, "$1")} · ⌘Z to undo`,
          result.move == null
            ? null
            : async () => {
                await api.placeUndo(result.move as number);
              },
        );
      } catch (cause) {
        setError(cause instanceof ApiError ? cause.message : String(cause));
        setMoving(false);
      }
    },
    [moving, onDone, path],
  );

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        event.stopPropagation();
        setSelected((i) => Math.min(i + 1, Math.max(options.length - 1, 0)));
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        event.stopPropagation();
        setSelected((i) => Math.max(i - 1, 0));
      } else if (event.key === "Enter") {
        const option = options[selected];
        if (!option) return;
        event.preventDefault();
        event.stopPropagation();
        void confirm(option.path);
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [options, selected, confirm]);

  const file = proposal?.file;

  return (
    <div className="doc place">
      <header className="doc-head">
        <h1 className="doc-title">{file?.name ?? path.split("/").pop()}</h1>
        <div className="dim place-where">
          in {file?.folder ?? "…"}
          {file && file.doc_id == null && " · not indexed yet"}
        </div>
      </header>

      {error && <div className="caveat warn">{error}</div>}

      {!proposal && !error && <div className="doc-note dim">Working out where it fits…</div>}

      {proposal && (
        <>
          <div className="doc-sub dim">
            {filter.trim()
              ? "Every folder"
              : proposal.candidates.length === 0
                ? "Nothing in the archive suggests a home — type to choose one"
                : proposal.candidates.length === 1
                  ? "One obvious place"
                  : `${proposal.candidates.length} plausible places`}
          </div>

          <ul className="gather-items place-list">
            {options.map((option, index) => (
              <li
                key={option.path}
                className={`gather-row${index === selected ? " selected" : ""}`}
                onClick={() => void confirm(option.path)}
              >
                <span className="gather-what">{option.display}</span>
                {option.reasons.length > 0 && (
                  <span className="dim place-why">{option.reasons.join(" · ")}</span>
                )}
              </li>
            ))}
          </ul>

          <input
            className="sheet-input place-filter"
            placeholder="or type to find any folder…"
            value={filter}
            onChange={(event) => {
              setFilter(event.target.value);
              setSelected(0);
            }}
            spellCheck={false}
            autoFocus={proposal.candidates.length === 0}
          />

          <div className="home-hint dim">
            ↵ moves it there · esc leaves it where it is
            <button className="caveat-action place-cancel" onClick={onCancel}>
              cancel
            </button>
          </div>
        </>
      )}
    </div>
  );
}
