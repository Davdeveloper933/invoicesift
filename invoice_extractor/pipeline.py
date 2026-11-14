"""Process a folder of invoices and write the CSV. One bad file never stops the run."""

from __future__ import annotations

import csv
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from .normalize import FIELDS, normalize_fields
from .pdf_loader import PdfContent, load_pdf

# Provider-specific function: PDF content -> raw field dict from the model.
ExtractFn = Callable[[PdfContent], dict[str, Any]]

log = logging.getLogger(__name__)

CSV_COLUMNS = [
    "file_name",
    *FIELDS,
    "needs_review",
    "review_reasons",
    "extraction_method",
]


@dataclass
class InvoiceResult:
    file_name: str
    values: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    method: str = ""
    fatal: bool = False  # error that would fail every other invoice too

    @property
    def needs_review(self) -> bool:
        return bool(self.reasons)

    def to_row(self) -> dict[str, str]:
        row = {"file_name": self.file_name}
        for name in FIELDS:
            value = self.values.get(name)
            if value is None:
                row[name] = ""
            elif isinstance(value, Decimal):
                row[name] = f"{value:.2f}"
            else:
                row[name] = str(value)
        row["needs_review"] = "YES" if self.needs_review else "NO"
        row["review_reasons"] = "; ".join(self.reasons)
        row["extraction_method"] = self.method
        return row


def find_pdfs(folder: Path, recursive: bool = False) -> list[Path]:
    pattern = "**/*" if recursive else "*"
    return sorted(
        (p for p in folder.glob(pattern) if p.is_file() and p.suffix.lower() == ".pdf"),
        key=lambda p: str(p).lower(),
    )


def is_fatal(exc: Exception) -> bool:
    """Errors that will fail for every remaining invoice too: bad key, no credits.

    Retrying the other files would only waste time, so the run stops early.
    """
    if getattr(exc, "status_code", None) in (401, 403):
        return True
    if getattr(exc, "code", None) == "insufficient_quota":  # OpenAI: no credits
        return True
    message = str(exc).lower()
    return "credit balance is too low" in message or "no credits remaining" in message


def process_file(path: Path, extract: ExtractFn, max_pages: int = 5) -> InvoiceResult:
    """Extract one invoice. Never raises: problems end up in ``reasons``."""
    result = InvoiceResult(file_name=path.name)
    try:
        content = load_pdf(path, max_pages=max_pages)
        result.method = content.method
        raw = extract(content)
        result.values, result.reasons = normalize_fields(raw)
    except Exception as exc:  # noqa: BLE001 - isolate every failure to its own row
        log.error("%s: %s: %s", path.name, type(exc).__name__, exc)
        result.reasons = [f"ERROR: {type(exc).__name__}: {exc}"]
        result.fatal = is_fatal(exc)
    return result


def flag_duplicates(results: list[InvoiceResult]) -> None:
    """Flag invoices whose supplier + invoice number appear more than once."""
    seen: dict[tuple[str, str], list[InvoiceResult]] = {}
    for result in results:
        supplier = (result.values.get("supplier_name") or "").casefold()
        number = (result.values.get("invoice_number") or "").casefold()
        if supplier and number:
            seen.setdefault((supplier, number), []).append(result)
    for group in seen.values():
        if len(group) > 1:
            names = ", ".join(r.file_name for r in group)
            for result in group:
                result.reasons.append(f"possible duplicate ({names})")


def write_csv(results: list[InvoiceResult], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig so Excel shows accented supplier names correctly.
    with output.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(r.to_row() for r in results)


def run(
    files: list[Path],
    extract: ExtractFn,
    output: Path,
    workers: int = 4,
    max_pages: int = 5,
) -> list[InvoiceResult]:
    """Process all files (in parallel), write the CSV, return the results in file order.

    A fatal API error (invalid key, no credits) stops the run: the remaining
    files are written as SKIPPED rows, so the CSV still lists every invoice.
    """
    stop = threading.Event()

    def task(indexed: tuple[int, Path]) -> InvoiceResult:
        index, path = indexed
        if stop.is_set():
            result = InvoiceResult(path.name, reasons=["SKIPPED: run stopped after a fatal API error"])
            log.info("[%d/%d] %-6s %s", index, len(files), "SKIP", path.name)
            return result
        result = process_file(path, extract, max_pages)
        if result.fatal and not stop.is_set():
            stop.set()
            log.error("Fatal API error (invalid key or no credits) - skipping the remaining invoices.")
        status = "REVIEW" if result.needs_review else "OK"
        log.info("[%d/%d] %-6s %s", index, len(files), status, path.name)
        return result

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(task, enumerate(files, start=1)))

    flag_duplicates(results)
    write_csv(results, output)
    return results
