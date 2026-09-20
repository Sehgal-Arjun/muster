import { useEffect, useMemo, useRef, useState } from "react";
import { api, type MessageBody as Body } from "../api";

/**
 * An email rendered as it was sent.
 *
 * The indexed text is deliberately mangled for retrieval — links reduced to
 * bare domains, navigation flattened into prose — so reading a message that way
 * gives a wall of repeated hostnames. The original is in the blob store, so it
 * can be shown properly.
 *
 * In a sandboxed frame with no permissions at all: the body is arbitrary HTML
 * from a stranger. The server strips everything executable before it gets here;
 * the sandbox is the second lock, so a mistake in the first is not the end of it.
 */
export function MessageBody({ docId, onFallback }: { docId: number; onFallback: () => void }) {
  const [body, setBody] = useState<Body | null>(null);
  const frame = useRef<HTMLIFrameElement>(null);
  const [height, setHeight] = useState(320);

  useEffect(() => {
    let live = true;
    setBody(null);
    api
      .body(docId)
      .then((got) => {
        if (!live) return;
        setBody(got);
        // No HTML part: the caller shows the extracted text instead.
        if (!got.is_html) onFallback();
      })
      .catch(() => live && onFallback());
    return () => {
      live = false;
    };
  }, [docId, onFallback]);

  /**
   * The message, on a light page.
   *
   * Mail is written for a white background — dark-mode-aware email is rare, and
   * forcing one turns half of it into black text on black. A white sheet is
   * what every mail client shows and what the sender designed against.
   */
  const document = useMemo(() => {
    if (!body?.is_html) return "";
    return `<!doctype html><html><head><meta charset="utf-8">
      <style>
        html, body { margin: 0; padding: 14px 16px; background: #fff; color: #1c1c1e; }
        body { font: 14px/1.6 -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif; }
        img, table { max-width: 100% !important; height: auto; }
        table { width: auto !important; }
        a { color: #0645ad; }
        * { max-width: 100% !important; }
      </style></head><body>${body.html}</body></html>`;
  }, [body]);

  // Grow to the content: a message in a fixed box that scrolls inside another
  // scrolling view is the worst of both.
  useEffect(() => {
    if (!document) return;
    const timer = setInterval(() => {
      const inner = frame.current?.contentDocument?.body;
      if (inner) setHeight(Math.min(inner.scrollHeight + 32, 4000));
    }, 200);
    const stop = setTimeout(() => clearInterval(timer), 3000);
    return () => {
      clearInterval(timer);
      clearTimeout(stop);
    };
  }, [document]);

  if (!body) return <div className="doc-note dim">Loading the message…</div>;
  if (!body.is_html) return null;

  return (
    <>
      <iframe
        ref={frame}
        className="mail-frame"
        style={{ height }}
        srcDoc={document}
        title="message"
        // Nothing enabled. Not scripts, not forms, not same-origin.
        sandbox=""
      />
      {body.images_removed > 0 && (
        <div className="doc-note dim">
          {body.images_removed} remote image{body.images_removed > 1 ? "s" : ""} not
          loaded — they would have told the sender you opened this.
        </div>
      )}
    </>
  );
}
