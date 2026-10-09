/**
 * Every keystroke, on demand.
 *
 * The footer used to try to be this, and could not: thirteen hints in a panel
 * 720 points wide wrapped into a second row and pushed "reveal in Finder" into a
 * column of its own. A hint bar is a reminder of the three or four keys you
 * reach for constantly; the full set is reference material, and reference
 * material belongs behind a key rather than under the results.
 */

const GROUPS: Array<[string, Array<[string, string]>]> = [
  ["Everywhere", [
    ["⌘⇧Space", "summon or dismiss the panel"],
    ["⌘H", "home"],
    ["⌘P", "processes"],
    ["⌘\\", "one column or two"],
    ["⌘⇧P", "keep the panel open — to drag a file in from Finder"],
    ["⌘/", "this list"],
    ["esc", "back, then close"],
  ]],
  ["Results", [
    ["↑ ↓", "move"],
    ["↵", "read"],
    ["⇥", "gather the query into a process"],
    ["⌘↵", "reveal the original in Finder"],
    ["⌘O", "open in Gmail, Preview or the browser"],
    ["⌘⇧F", "open the folder a query suggests"],
    ["⌘K", "actions for the selected result"],
    ["⌘S", "newest first, or best match first"],
    ["⌘1 ⌘2 ⌘3", "hybrid, keyword or vector"],
  ]],
  ["Reading", [
    ["↑ ↓ j k", "scroll, then into the attachments"],
    ["↵", "open the selected attachment"],
    ["⌘C", "copy the body"],
    ["⌘D", "settle the item this came from"],
  ]],
  ["A dropped file", [
    ["↑ ↓", "move through the suggestions"],
    ["↵", "move it there"],
    ["⌘Z", "put it back where it was"],
    ["esc", "leave it where it is"],
  ]],
  ["A process", [
    ["↑ ↓", "move through what is open"],
    ["⌘D", "settle an item"],
    ["⌘Z", "put the last one back"],
    ["⌘E", "read the documents nobody has read yet"],
  ]],
];

export function Keys({ onClose }: { onClose: () => void }) {
  return (
    <div className="sheet-backdrop" onClick={onClose}>
      <div className="sheet keys-sheet" onClick={(event) => event.stopPropagation()}>
        <div className="sheet-head">Keys · esc to close</div>
        <div className="keys-body">
          {GROUPS.map(([title, keys]) => (
            <section key={title} className="keys-group">
              <div className="keys-title dim">{title}</div>
              {keys.map(([key, what]) => (
                <div key={`${title}-${key}`} className="keys-row">
                  <kbd>{key}</kbd>
                  <span className="dim">{what}</span>
                </div>
              ))}
            </section>
          ))}
        </div>
      </div>
    </div>
  );
}
