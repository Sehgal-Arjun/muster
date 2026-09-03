import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api, type Attachment, type DocResponse } from "../api";
import { mockDoc, mockFileDoc } from "../mock";
import { Highlight, queryTerms } from "./Highlight";
import { Icon } from "./Icon";

const USE_MOCK = import.meta.env.VITE_USE_MOCK === "1";

/**
 * What the panel is currently showing instead of the result list.
 *
 * A stack of these is what makes "message → its PDF attachment → back" work:
 * escape pops one level rather than dropping the reader all the way out.
 */
export type View =
  | { kind: "doc"; id: number }
  | { kind: "blob"; hash: string; mime: string | null; filename: string | null };

const isPdf = (mime: string | null) => !!mime?.includes("pdf");
const isImage = (mime: string | null) => !!mime?.startsWith("image/");

function bytes(n: number | null | undefined): string {
  if (n == null) return "";
  const units = ["B", "KB", "MB", "GB"];
  let value = n;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit++;
  }
  return `${value < 10 && unit > 0 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

function longDate(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleString(undefined, {
        weekday: "short", day: "numeric", month: "short", year: "numeric",
        hour: "numeric", minute: "2-digit",
      });
}

/** The last path segment, which is the part a reader is actually looking for. */
const basename = (path: string) => path.split("/").filter(Boolean).pop() ?? path;

export function DocView({
  view,
  query,
  blobDir,
  onOpen,
  onExit,
}: {
  view: View;
  /** The query that produced this document, for highlighting. */
  query: string;
  blobDir: string | null;
  onOpen: (next: View) => void;
  onExit: () => void;
}) {
  const [doc, setDoc] = useState<DocResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  // null means "reading the body"; a number selects an attachment.
  const [attachSel, setAttachSel] = useState<number | null>(null);

  const scrollRef = useRef<HTMLDivElement>(null);
  const terms = useMemo(() => queryTerms(query), [query]);
  const attachments = doc?.attachments ?? [];

  useEffect(() => {
    let live = true;
    setDoc(null);
    setError(null);
    setAttachSel(null);
    if (view.kind !== "doc") return;
    if (USE_MOCK) {
      setDoc(view.id === mockFileDoc.id ? mockFileDoc : mockDoc);
      return;
    }
    api
      .doc(view.id)
      .then((body) => live && setDoc(body))
      .catch((cause) =>
        live && setError(cause instanceof ApiError ? cause.message : String(cause)),
      );
    return () => {
      live = false;
    };
  }, [view]);

  // Jump to the first match, so a hit buried on page four is not something the
  // reader has to go hunting for. Falls back to the top when nothing matched.
  useEffect(() => {
    if (!doc) return;
    const first = scrollRef.current?.querySelector("mark");
    if (first) first.scrollIntoView({ block: "center" });
    else scrollRef.current?.scrollTo({ top: 0 });
  }, [doc]);

  useEffect(() => {
    if (!flash) return;
    const timer = setTimeout(() => setFlash(null), 1400);
    return () => clearTimeout(timer);
  }, [flash]);

  useEffect(() => {
    scrollRef.current
      ?.querySelector<HTMLElement>(".attachment.selected")
      ?.scrollIntoView({ block: "nearest" });
  }, [attachSel]);

  /** Reveal the bytes in Finder — the real file if there is one, else the blob. */
  const reveal = useCallback(
    async (hash: string | null, path?: string | null) => {
      const target = path ?? (hash && blobDir ? api.blobPath(blobDir, hash) : null);
      if (!target) return;
      try {
        const { revealItemInDir } = await import("@tauri-apps/plugin-opener");
        await revealItemInDir(target);
      } catch (cause) {
        setFlash(`Could not reveal: ${cause}`);
      }
    },
    [blobDir],
  );

  const preview = useCallback(
    (attachment: Attachment) => {
      // Its own document when it has one — that is the searchable version, with
      // extracted text and highlights. Otherwise the bytes are all there is.
      if (attachment.doc_id != null) onOpen({ kind: "doc", id: attachment.doc_id });
      else
        onOpen({
          kind: "blob",
          hash: attachment.content_hash,
          mime: attachment.mime,
          filename: attachment.filename,
        });
    },
    [onOpen],
  );

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const meta = event.metaKey || event.ctrlKey;
      const scroller = scrollRef.current;

      if (meta && event.key.toLowerCase() === "c") {
        // Never override a real selection: the user asked to copy that, not the
        // whole body.
        if (window.getSelection()?.toString()) return;
        event.preventDefault();
        void navigator.clipboard.writeText(doc?.text ?? "");
        setFlash("Body copied");
        return;
      }

      if (event.key === "Enter") {
        event.preventDefault();
        const selected = attachSel != null ? attachments[attachSel] : null;
        if (meta) {
          void (selected
            ? reveal(selected.content_hash)
            : reveal(doc?.blob?.hash ?? null, doc?.provenance?.path));
        } else if (selected) {
          preview(selected);
        }
        return;
      }

      const down = event.key === "ArrowDown" || event.key === "j";
      const up = event.key === "ArrowUp" || event.key === "k";
      if (!down && !up) return;
      event.preventDefault();

      // One column, walked in document order: the attachments sit above the
      // body, so going up off the top of the body lands on the last of them and
      // going down off the end of the list returns to the body.
      if (down) {
        if (attachSel == null) scroller?.scrollBy({ top: 90 });
        else if (attachSel < attachments.length - 1) setAttachSel(attachSel + 1);
        else setAttachSel(null);
      } else if (attachSel != null) {
        if (attachSel > 0) setAttachSel(attachSel - 1);
      } else if (attachments.length && (scroller?.scrollTop ?? 0) <= 0) {
        setAttachSel(attachments.length - 1);
      } else {
        scroller?.scrollBy({ top: -90 });
      }
    };

    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [doc, attachments, attachSel, preview, reveal]);

  if (view.kind === "blob") {
    return (
      <div className="doc" ref={scrollRef}>
        <BlobBody
          hash={view.hash}
          mime={view.mime}
          filename={view.filename}
          onBack={onExit}
        />
      </div>
    );
  }

  if (error) {
    return (
      <div className="doc">
        <div className="empty">
          <div className="empty-headline">Cannot open this document</div>
          <div className="empty-detail">{error}</div>
          <div className="empty-detail dim">Escape returns to the results.</div>
        </div>
      </div>
    );
  }

  if (!doc) return <div className="doc doc-loading dim">Loading…</div>;

  const provenance = doc.provenance;
  const showPdf = isPdf(doc.mime) && doc.blob;
  const showImage = isImage(doc.mime) && doc.blob;

  return (
    <div className="doc" ref={scrollRef}>
      <header className="doc-head">
        <h1 className="doc-title">{doc.title || "(no subject)"}</h1>

        <div className="doc-fields">
          {doc.sender && (
            <Field label="From" value={
              doc.sender.name
                ? `${doc.sender.name}${doc.sender.address ? ` <${doc.sender.address}>` : ""}`
                : doc.sender.address
            } />
          )}
          {!!doc.recipients?.to?.length && (
            <Field label="To" value={doc.recipients.to.join(", ")} />
          )}
          {!!doc.recipients?.cc?.length && (
            <Field label="Cc" value={doc.recipients.cc.join(", ")} />
          )}
          <Field
            label="Date"
            value={longDate(doc.date)}
            // mtime is a lie often enough to be worth labelling as one.
            hint={doc.date_source !== "header" ? doc.date_source : undefined}
          />
          {provenance && <Field label="Path" value={provenance.path} mono />}
          {provenance?.created && <Field label="Created" value={longDate(provenance.created)} />}
          {provenance?.modified && <Field label="Modified" value={longDate(provenance.modified)} />}
          {provenance?.size_bytes != null && (
            <Field label="Size" value={bytes(provenance.size_bytes)} />
          )}
          {!!provenance?.where_from?.length && (
            <Field label="Downloaded from" value={provenance!.where_from.join("\n")} mono />
          )}
          {provenance?.downloaded_date && (
            <Field label="Downloaded" value={longDate(provenance.downloaded_date)} />
          )}
        </div>

        {doc.thread && doc.thread.count > 1 && (
          <div className="thread">
            <span className="dim">Thread · {doc.thread.count} messages</span>
            <ol className="thread-list">
              {doc.thread.messages.map((message) => (
                <li
                  key={message.doc_id}
                  className={`thread-item${message.current ? " current" : ""}`}
                  onClick={() =>
                    !message.current && onOpen({ kind: "doc", id: message.doc_id })
                  }
                >
                  <span className="thread-who">
                    {message.sender?.name || message.sender?.address || "—"}
                  </span>
                  <span className="dim">{longDate(message.date)}</span>
                </li>
              ))}
            </ol>
          </div>
        )}
      </header>

      {attachments.length > 0 && (
        <section className="attachments">
          <div className="doc-sub dim">
            {attachments.length} attachment{attachments.length > 1 ? "s" : ""}
          </div>
          <ul className="attachment-list">
            {attachments.map((attachment, index) => (
              <li
                key={attachment.content_hash + index}
                className={`attachment${index === attachSel ? " selected" : ""}`}
                onMouseMove={() => setAttachSel(index)}
                onClick={() => preview(attachment)}
              >
                <Icon kind="attachment" mime={attachment.mime} />
                <span className="attachment-name">
                  {attachment.filename || "(unnamed)"}
                </span>
                <span className="dim">{attachment.mime ?? "unknown type"}</span>
                <span className="dim">{bytes(attachment.bytes)}</span>
                {attachment.doc_id == null && (
                  <span className="chip" title="No text was extracted from this file">
                    not indexed
                  </span>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      {doc.redacted_by ? (
        <div className="empty-detail dim doc-note">
          The body was deliberately not indexed ({doc.redacted_by}). There is
          nothing to read here — that is the intended behaviour, not a failure.
        </div>
      ) : (
        <>
          {showPdf && (
            <iframe
              className="doc-embed"
              src={api.blobUrl(doc.blob!.hash)}
              title={doc.title ?? "PDF"}
            />
          )}
          {showImage && (
            <img className="doc-embed" src={api.blobUrl(doc.blob!.hash)} alt={doc.title ?? ""} />
          )}
          {doc.text.trim() ? (
            <article className="doc-body">
              {(showPdf || showImage) && <div className="doc-sub dim">Extracted text</div>}
              <Highlight text={doc.text} terms={terms} />
            </article>
          ) : (
            !showPdf && !showImage && (
              <div className="empty-detail dim doc-note">
                No text was extracted from this document.
              </div>
            )
          )}
        </>
      )}

      {flash && <div className="doc-flash">{flash}</div>}
    </div>
  );
}

function Field({
  label,
  value,
  hint,
  mono,
}: {
  label: string;
  value: string | null;
  hint?: string;
  mono?: boolean;
}) {
  if (!value) return null;
  return (
    <div className="field">
      <span className="field-label dim">{label}</span>
      <span className={`field-value${mono ? " mono" : ""}`}>
        {value}
        {hint && <span className="chip field-hint">{hint}</span>}
      </span>
    </div>
  );
}

/** An attachment with no document of its own: the bytes are all there is. */
function BlobBody({
  hash,
  mime,
  filename,
  onBack,
}: {
  hash: string;
  mime: string | null;
  filename: string | null;
  onBack: () => void;
}) {
  return (
    <>
      <header className="doc-head">
        <h1 className="doc-title">{filename || basename(hash)}</h1>
        <div className="doc-fields">
          <Field label="Type" value={mime ?? "unknown"} />
        </div>
      </header>
      {isPdf(mime) ? (
        <iframe className="doc-embed" src={api.blobUrl(hash)} title={filename ?? "PDF"} />
      ) : isImage(mime) ? (
        <img className="doc-embed" src={api.blobUrl(hash)} alt={filename ?? ""} />
      ) : (
        <div className="empty-detail dim doc-note">
          No inline preview for {mime ?? "this type"}. ⌘↵ reveals it in Finder.
          <button className="link" onClick={onBack}>Back</button>
        </div>
      )}
    </>
  );
}
