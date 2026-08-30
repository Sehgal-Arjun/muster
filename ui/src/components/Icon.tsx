import type { Kind } from "../api";

/**
 * Inline SVG rather than an icon package: three glyphs do not justify a
 * dependency, and these inherit currentColor so they follow the theme for free.
 */
export function Icon({ kind, mime }: { kind: Kind; mime: string | null }) {
  const path =
    kind === "attachment"
      ? mime?.includes("pdf")
        ? "M4 2h9l5 5v13H4z M13 2v5h5"
        : "M15 7l-6.5 6.5a3 3 0 104.2 4.2L20 11"
      : kind === "message"
        ? "M3 5h18v14H3z M3 6l9 7 9-7"
        : "M5 3h9l5 5v13H5z";

  return (
    <svg className="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d={path} fill="none" stroke="currentColor" strokeWidth="1.6"
            strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
