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


def is_text_bearing(mime: str | None, filename: str | None) -> bool:
    """Whether extraction is worth attempting at all."""
    mime = (mime or "").lower()
    if mime in TEXT_MIMES or mime in PDF_MIMES or mime in IMAGE_MIMES:
        return True
    if mime.startswith("text/"):
        return True
    name = (filename or "").lower()
    return name.endswith((".txt", ".md", ".csv", ".json", ".pdf", ".log"))


def extract_text(data: bytes, mime: str | None, filename: str | None) -> str:
    mime = (mime or "").lower()
    name = (filename or "").lower()
    try:
        if mime in PDF_MIMES or name.endswith(".pdf"):
            return extract_pdf(data)
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
