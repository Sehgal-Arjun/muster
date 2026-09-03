import { type ReactNode, useMemo } from "react";

/**
 * The words worth marking in a document body.
 *
 * Derived from the query on the client rather than asked of the server: /doc is
 * keyed by id alone, which is what makes it cacheable, and adding the query to
 * it would mean a different response per query for identical content.
 *
 * Very short tokens and the handful of words that appear in every document are
 * dropped — marking every "the" in a long email is worse than marking nothing,
 * because it hides the matches that matter.
 */
const NOISE = new Set([
  "the", "and", "for", "from", "with", "that", "this", "was", "were", "are",
  "you", "your", "our", "not", "but", "all", "any", "can", "has", "have", "had",
  "about", "into", "out", "who", "what", "when", "where", "which", "will",
]);

export function queryTerms(query: string): string[] {
  const seen = new Set<string>();
  for (const raw of query.toLowerCase().split(/[^\p{L}\p{N}]+/u)) {
    if (raw.length >= 3 && !NOISE.has(raw)) seen.add(raw);
  }
  return [...seen];
}

function escapeRegExp(term: string): string {
  return term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Mark occurrences of the query's words in a run of plain text.
 *
 * Matching is by word prefix, so "invoice" also marks "invoices". That is
 * looser than the index — FTS5 matches whole terms unless asked otherwise — but
 * erring towards showing the reader why a document was returned is the right
 * side to err on, and a mark that is slightly generous is easy to read past.
 *
 * The text is a document body from an untrusted sender and is only ever
 * rendered as string children, never as HTML.
 */
export function Highlight({ text, terms }: { text: string; terms: string[] }) {
  const pattern = useMemo(() => {
    if (!terms.length) return null;
    // Longest first, so "lease" cannot pre-empt "leaseback".
    const ordered = [...terms].sort((a, b) => b.length - a.length).map(escapeRegExp);
    return new RegExp(`(${ordered.join("|")})\\p{L}*`, "giu");
  }, [terms]);

  const out = useMemo(() => {
    if (!pattern) return [text];
    const parts: ReactNode[] = [];
    let last = 0;
    let key = 0;
    for (const match of text.matchAll(pattern)) {
      const at = match.index ?? 0;
      // Only whole words: a match starting mid-word is a coincidence, not a hit.
      if (at > 0 && /[\p{L}\p{N}]/u.test(text[at - 1]!)) continue;
      if (at > last) parts.push(text.slice(last, at));
      parts.push(<mark key={key++}>{match[0]}</mark>);
      last = at + match[0].length;
    }
    if (last < text.length) parts.push(text.slice(last));
    return parts;
  }, [text, pattern]);

  return <>{out}</>;
}
