import { useState } from "react";
import { api, type Hit } from "../api";
import { shortDate as sharedShortDate } from "../dates";
import { Icon } from "./Icon";
import { Score } from "./Score";
import { Snippet } from "./Snippet";

function shortDate(iso: string): string {
  return sharedShortDate(iso, { year: false });
}

/**
 * The document itself where there is something to show, the generic glyph
 * otherwise. A page of the actual PDF says more about whether this is the right
 * result than any icon can.
 */
function Thumb({ hit }: { hit: Hit }) {
  const [failed, setFailed] = useState(false);
  if (!hit.preview || failed) return <Icon kind={hit.kind} mime={hit.mime} />;
  return (
    <img
      className="thumb"
      src={api.thumbUrl(hit.doc_id, 96)}
      alt=""
      loading="lazy"
      // A preview that 404s must fall back to the icon, not leave a broken
      // image in the row.
      onError={() => setFailed(true)}
    />
  );
}

export function ResultRow({
  hit,
  selected,
  onSelect,
  onOpen,
}: {
  hit: Hit;
  selected: boolean;
  onSelect: () => void;
  /** Read it here — the same thing Enter does. */
  onOpen: () => void;
}) {
  const copies = hit.sources.length;
  const who = hit.sender?.name || hit.sender?.address || hit.counterparty;

  return (
    <li
      className={`row${selected ? " selected" : ""}`}
      onMouseMove={onSelect}
      // A single click, not a double: this is a launcher list, and the row is
      // the button.
      onClick={onOpen}
      // Selection is driven by the keyboard; the list itself is the widget.
      aria-selected={selected}
      role="option"
    >
      <Thumb hit={hit} />
      <div className="row-body">
        <div className="row-title">
          <span className="title">{hit.title || "(no subject)"}</span>
          {copies > 1 && (
            <span className="copies" title={`${copies} identical copies in these results`}>
              ×{copies}
            </span>
          )}
        </div>
        <Snippet text={hit.snippet} />
        <div className="row-meta">
          {who && <span className="who">{who}</span>}
          <span>{shortDate(hit.date)}</span>
          <span className="source">{hit.source}</span>
          {hit.attachment_count > 0 && (
            <span title={`${hit.attachment_count} attachments`}>
              ¶ {hit.attachment_count}
            </span>
          )}
          {hit.category && <span className="chip">{hit.category}</span>}
        </div>
      </div>
      <Score score={hit.score} />
    </li>
  );
}
