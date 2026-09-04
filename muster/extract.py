"""Pulling searchable text out of attachment bytes.

Every function here returns a string and never raises: an attachment that
cannot be read is still worth keeping as a blob, and one unreadable PDF must
not cost us the message it arrived with.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Below this much text per page, a PDF is almost certainly scanned images with
# no text layer, and OCR is worth the seconds it costs.
MIN_CHARS_PER_PAGE = 40
MAX_OCR_PAGES = 20

TEXT_MIMES = {
    "text/plain",
    "text/markdown",
    "text/csv",
    "text/html",
    "application/json",
    "application/xml",
    "text/xml",
}
PDF_MIMES = {"application/pdf", "application/x-pdf"}
IMAGE_MIMES = {"image/png", "image/jpeg", "image/jpg", "image/tiff", "image/heic", "image/webp"}
DOCX_MIMES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
RTF_MIMES = {"application/rtf", "text/rtf"}


def is_text_bearing(mime: str | None, filename: str | None) -> bool:
    """Whether extraction is worth attempting at all."""
    mime = (mime or "").lower()
    if mime in TEXT_MIMES or mime in PDF_MIMES or mime in IMAGE_MIMES:
        return True
    if mime.startswith("text/"):
        return True
    if mime in DOCX_MIMES or mime in RTF_MIMES:
        return True
    name = (filename or "").lower()
    return name.endswith(
        (".txt", ".md", ".csv", ".json", ".pdf", ".log", ".docx", ".rtf", ".pages")
    )


def extract_text(data: bytes, mime: str | None, filename: str | None) -> str:
    mime = (mime or "").lower()
    name = (filename or "").lower()
    try:
        if mime in PDF_MIMES or name.endswith(".pdf"):
            return extract_pdf(data)
        if mime in DOCX_MIMES or name.endswith(".docx"):
            return extract_docx(data)
        if name.endswith(".pages"):
            return extract_pages(data)
        if mime in RTF_MIMES or name.endswith(".rtf"):
            return extract_rtf(data)
        if mime in IMAGE_MIMES or name.endswith((".png", ".jpg", ".jpeg", ".tiff", ".heic")):
            return ocr_image_bytes(data)
        if mime in TEXT_MIMES or mime.startswith("text/") or name.endswith((".txt", ".md", ".csv", ".json", ".log")):
            return decode_text(data)
    except Exception as exc:
        log.warning("extraction failed for %s (%s): %s", filename, mime, exc)
    return ""


def decode_text(data: bytes, charset: str | None = None) -> str:
    """Decode bytes to text, trying the declared charset then common fallbacks."""
    for candidate in (charset, "utf-8", "cp1252", "latin-1"):
        if not candidate:
            continue
        try:
            return data.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def extract_pdf(data: bytes) -> str:
    """Text layer via PyMuPDF, falling back to OCR when there isn't one."""
    try:
        import pymupdf
    except ImportError:
        log.warning("pymupdf not installed; skipping PDF text extraction")
        return ""

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        log.warning("unreadable PDF: %s", exc)
        return ""

    with doc:
        pages = [_safe_page_text(page) for page in doc]
        text = "\n\n".join(p for p in pages if p.strip())
        if not pages:
            return ""
        if len(text) >= MIN_CHARS_PER_PAGE * len(pages):
            return text
        ocred = _ocr_pdf(doc)

    return ocred or text


def _safe_page_text(page) -> str:
    try:
        return page.get_text("text")
    except Exception as exc:
        log.debug("page text extraction failed: %s", exc)
        return ""


def _ocr_pdf(doc) -> str:
    """Render pages and OCR them. macOS Vision via ocrmac; no-op elsewhere."""
    try:
        import pymupdf
    except ImportError:
        return ""

    out: list[str] = []
    for index, page in enumerate(doc):
        if index >= MAX_OCR_PAGES:
            log.warning("stopping OCR after %d pages", MAX_OCR_PAGES)
            break
        try:
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2))
            text = ocr_image_bytes(pixmap.tobytes("png"))
        except Exception as exc:
            log.debug("OCR failed on page %d: %s", index, exc)
            continue
        if text.strip():
            out.append(text)
    return "\n\n".join(out)


def ocr_image_bytes(data: bytes) -> str:
    try:
        import io

        from ocrmac import ocrmac
        from PIL import Image
    except ImportError:
        log.debug("ocrmac/Pillow not installed; skipping OCR")
        return ""

    try:
        image = Image.open(io.BytesIO(data))
        image.load()
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        results = ocrmac.OCR(image, language_preference=["en-US", "de-DE"]).recognize()
    except Exception as exc:
        log.debug("OCR failed: %s", exc)
        return ""

    return "\n".join(text for text, _confidence, _box in results if text.strip())


def extract_docx(data: bytes) -> str:
    """Text from a .docx.

    A .docx is a zip holding XML, so this needs no dependency: pull the document
    part and take the text nodes. Paragraph and tab boundaries are turned into
    whitespace first, because stripping tags without them welds the last word of
    one paragraph to the first of the next and produces tokens that match nothing.
    """
    import io
    import re
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", "replace")
    except (KeyError, zipfile.BadZipFile, OSError) as exc:
        log.warning("not a readable docx: %s", exc)
        return ""

    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    xml = re.sub(r"<w:br[^>]*/>", "\n", xml)
    return _collapse_blank_lines(re.sub(r"<[^>]+>", "", xml))


def extract_pages(data: bytes) -> str:
    """Text from an Apple Pages document.

    The document body is Apple's own binary IWA format, which is not worth
    reimplementing. Pages bundles a PDF preview for Quick Look, and reading that
    gets the text through a path already relied on for every other PDF.
    """
    import io
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            preview = next(
                (
                    name
                    for name in archive.namelist()
                    if name.lower().endswith(".pdf") and "preview" in name.lower()
                ),
                None,
            )
            if preview is None:
                log.info("pages file has no preview pdf; nothing to extract")
                return ""
            return extract_pdf(archive.read(preview))
    except (zipfile.BadZipFile, OSError) as exc:
        log.warning("not a readable pages file: %s", exc)
        return ""


def extract_rtf(data: bytes) -> str:
    """Text from RTF.

    Hand-rolled rather than adding a dependency for a format that is mostly
    braces and backslashes. A scanner rather than a set of regexes, because the
    part that matters is nesting: font and colour tables are groups whose
    *contents* must be skipped whole, and a pattern that cannot count braces
    will leak "Helvetica" into the index as though it were body text.

    Not a full RTF reader, and does not claim to be.
    """
    import re

    text = data.decode("cp1252", "replace") if isinstance(data, bytes) else data

    # Groups that describe the document rather than say anything.
    SKIPPED = {
        "fonttbl", "colortbl", "stylesheet", "info", "pict", "object",
        "listtable", "listoverridetable", "rsidtbl", "generator", "themedata",
        "datastore", "xmlnstbl",
    }
    BREAKS = {"par": "\n", "line": "\n", "sect": "\n\n", "tab": "\t", "cell": "\t"}

    out: list[str] = []
    depth = 0
    skip_to_depth: int | None = None
    i, n = 0, len(text)

    while i < n:
        char = text[i]

        if char == "{":
            depth += 1
            i += 1
            continue
        if char == "}":
            if skip_to_depth is not None and depth <= skip_to_depth:
                skip_to_depth = None
            depth -= 1
            i += 1
            continue

        if char == "\\":
            match = re.match(r"\\([a-zA-Z]+)(-?\d+)?[ ]?", text[i:])
            if match:
                word, param = match.group(1), match.group(2)
                i += match.end()
                if skip_to_depth is not None:
                    continue
                if word in SKIPPED:
                    # Everything up to this group's closing brace is metadata.
                    skip_to_depth = depth
                elif word == "u" and param is not None:
                    out.append(chr(int(param) % 0x10000))
                    # The following character is the substitute for readers that
                    # cannot do unicode; it would be a duplicate here.
                    if i < n and text[i] not in "\\{}":
                        i += 1
                elif word in BREAKS:
                    out.append(BREAKS[word])
                continue

            # An escape: \' hex, or a literal brace or backslash.
            if text.startswith("\\'", i) and i + 3 < n:
                try:
                    byte = bytes([int(text[i + 2 : i + 4], 16)])
                    if skip_to_depth is None:
                        out.append(byte.decode("cp1252", "replace"))
                except ValueError:
                    pass
                i += 4
                continue
            if i + 1 < n:
                if skip_to_depth is None:
                    out.append(text[i + 1])
                i += 2
                continue
            i += 1
            continue

        if skip_to_depth is None:
            out.append(char)
        i += 1

    return _collapse_blank_lines("".join(out))


def _collapse_blank_lines(text: str) -> str:
    import re

    return re.sub(r"\n{3,}", "\n\n", text).strip()
