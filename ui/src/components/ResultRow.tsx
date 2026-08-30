import type { Hit } from "../api";
import { Icon } from "./Icon";
import { Score } from "./Score";
import { Snippet } from "./Snippet";

function shortDate(iso: string): string {
  const d = new Date(iso);
  const now = new Date();
  const sameYear = d.getFullYear() === now.getFullYear();
  return d.toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    ...(sameYear ? {} : { year: "numeric" }),
  });
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
  onOpen: () => void;
}) {
  const copies = hit.sources.length;
  const who = hit.sender?.name || hit.sender?.address || hit.counterparty;

  return (
    <li
      className={`row${selected ? " selected" : ""}`}
      onMouseMove={onSelect}
      onClick={onOpen}
      // Selection is driven by the keyboard; the list itself is the widget.
      aria-selected={selected}
      role="option"
    >
      <Icon kind={hit.kind} mime={hit.mime} />
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
