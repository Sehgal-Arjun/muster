import type { FolderHit } from "../api";

/**
 * One directory a search might have meant, offered beside the results.
 *
 * Deliberately not in the list. A folder has no content, so ranking it against
 * documents would mean inventing a score, and a suggestion that pushes a real
 * result off the page is worse than no suggestion. It sits in its own strip,
 * takes one line, and costs nothing to ignore.
 */
export function FolderJump({
  folder,
  onOpen,
}: {
  folder: FolderHit;
  onOpen: () => void;
}) {
  return (
    <button className="folder-jump" onClick={onOpen} title={folder.path}>
      <svg className="icon" viewBox="0 0 24 24" aria-hidden="true">
        <path
          d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z"
          fill="none" stroke="currentColor" strokeWidth="1.6"
          strokeLinecap="round" strokeLinejoin="round"
        />
      </svg>
      <span className="folder-path">{folder.display}</span>
      <span className="dim folder-label">open in Finder</span>
      <kbd>⌘⇧F</kbd>
    </button>
  );
}
