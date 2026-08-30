import { useEffect, useRef, useState } from "react";
import type { Hit } from "../api";

export interface Action {
  id: string;
  label: string;
  hint?: string;
  run: () => void | Promise<void>;
}

/**
 * The ⌘K sheet.
 *
 * Deliberately a plain filtered list. A command palette is a text input and a
 * list of strings; reaching for a component library to draw one would add a
 * dependency heavier than the feature.
 */
export function Actions({
  hit,
  actions,
  onClose,
}: {
  hit: Hit | null;
  actions: Action[];
  onClose: () => void;
}) {
  const [filter, setFilter] = useState("");
  const [index, setIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => inputRef.current?.focus(), []);

  const visible = actions.filter((a) =>
    a.label.toLowerCase().includes(filter.trim().toLowerCase()),
  );
  const active = visible[Math.min(index, visible.length - 1)];

  return (
    <div className="sheet-backdrop" onClick={onClose}>
      <div className="sheet" onClick={(event) => event.stopPropagation()}>
        <div className="sheet-head">
          {hit?.title ?? "Actions"}
        </div>
        <input
          ref={inputRef}
          className="sheet-input"
          placeholder="Filter actions…"
          value={filter}
          onChange={(event) => {
            setFilter(event.target.value);
            setIndex(0);
          }}
          onKeyDown={(event) => {
            if (event.key === "ArrowDown") {
              event.preventDefault();
              setIndex((i) => Math.min(i + 1, visible.length - 1));
            } else if (event.key === "ArrowUp") {
              event.preventDefault();
              setIndex((i) => Math.max(i - 1, 0));
            } else if (event.key === "Enter" && active) {
              event.preventDefault();
              void active.run();
              onClose();
            } else if (event.key === "Escape") {
              event.preventDefault();
              onClose();
            }
          }}
        />
        <ul className="sheet-list" role="listbox">
          {visible.map((action, i) => (
            <li
              key={action.id}
              role="option"
              aria-selected={i === index}
              className={`sheet-item${i === index ? " selected" : ""}`}
              onMouseMove={() => setIndex(i)}
              onClick={() => {
                void action.run();
                onClose();
              }}
            >
              <span>{action.label}</span>
              {action.hint && <kbd>{action.hint}</kbd>}
            </li>
          ))}
          {visible.length === 0 && <li className="sheet-item dim">No actions</li>}
        </ul>
      </div>
    </div>
  );
}
