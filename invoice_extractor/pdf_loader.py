"""Reading PDFs: selectable text where available, page images for scanned pages."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

# A page with fewer letters/digits than this is treated as a scanned image.
MIN_TEXT_CHARS_PER_PAGE = 40
# Longest side of a rendered page image. A4 at 200 dpi is ~2340 px.
MAX_IMAGE_SIDE_PX = 3500
# Cap on text sent to the model. A normal invoice is 1-5k characters; a PDF stuffed
# with tiny or invisible text could otherwise cost hundreds of thousands of tokens.
MAX_TEXT_CHARS = 20_000
TRUNCATED_NOTE = "\n\n[... text truncated ...]"


class PdfReadError(Exception):
    """The file could not be opened or read as a PDF."""


@dataclass
class PdfContent:
    path: Path
    page_count: int
    text: str = ""
    # Base64-encoded PNGs of pages that had no usable text layer.
    images: list[str] = field(default_factory=list)

    @property
    def method(self) -> str:
        """How the invoice content is sent to the model: text, image or text+image."""
        if self.text and self.images:
            return "text+image"
        return "image" if self.images else "text"


def _meaningful_chars(text: str) -> int:
    return sum(ch.isalnum() for ch in text)


def load_pdf(path: Path, max_pages: int = 5, dpi: int = 200, max_chars: int = MAX_TEXT_CHARS) -> PdfContent:
    """Extract text from each page; render pages without a text layer to PNG.

    Only the first ``max_pages`` pages are used: invoice header fields and totals
    are almost always on the first pages, and this keeps API cost predictable.
    """
    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # pymupdf raises several different types
        raise PdfReadError(f"cannot open PDF: {exc}") from exc

    with doc:
        if doc.needs_pass and not doc.authenticate(""):
            raise PdfReadError("PDF is password-protected")
        if doc.page_count == 0:
            raise PdfReadError("PDF has no pages")

        content = PdfContent(path=path, page_count=doc.page_count)
        text_parts: list[str] = []
        for index in range(min(doc.page_count, max_pages)):
            page = doc[index]
            page_text = page.get_text("text", sort=True).strip()
            if _meaningful_chars(page_text) >= MIN_TEXT_CHARS_PER_PAGE:
                text_parts.append(f"--- Page {index + 1} ---\n{page_text}")
            else:
                # Cap the image size: a page with absurd dimensions ("PDF bomb")
                # would otherwise need gigabytes of memory to render.
                longest = max(page.rect.width, page.rect.height, 1)
                zoom = min(dpi / 72, MAX_IMAGE_SIDE_PX / longest)
                pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
                content.images.append(base64.b64encode(pixmap.tobytes("png")).decode("ascii"))

        text = "\n\n".join(text_parts)
        if len(text) > max_chars:
            text = text[:max_chars] + TRUNCATED_NOTE
        content.text = text
        return content
