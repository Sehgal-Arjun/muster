import { type ReactNode } from "react";

/**
 * FTS5 marks matches with [ and ] — delimiters, not markup.
 *
 * The text either side is message body from an untrusted sender, so it is
 * never interpolated as HTML. React escapes each fragment because it is
 * rendered as a string child; dangerouslySetInnerHTML here would turn a
 * message containing a script tag into one.
 */
export function Snippet({ text }: { text: string }) {
  const out: ReactNode[] = [];
  let buffer = "";
  let depth = 0;
  let key = 0;

  for (const char of text) {
    if (char === "[" && depth === 0) {
      if (buffer) out.push(buffer);
      buffer = "";
      depth = 1;
    } else if (char === "]" && depth === 1) {
      if (buffer) out.push(<mark key={key++}>{buffer}</mark>);
      buffer = "";
      depth = 0;
    } else {
      buffer += char;
    }
  }
  // An unbalanced delimiter is body text, not a broken tag: keep it visible.
  if (buffer) out.push(depth === 1 ? `[${buffer}` : buffer);

  return <span className="snippet">{out}</span>;
}
