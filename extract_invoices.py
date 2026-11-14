"""Extract key fields from a folder of supplier invoice PDFs into one CSV.

Usage:
    python extract_invoices.py INPUT_FOLDER [--output results.csv] [options]

Run ``python extract_invoices.py --help`` for all options.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from invoice_extractor.llm_anthropic import DEFAULT_MODEL as ANTHROPIC_DEFAULT_MODEL
from invoice_extractor.pdf_loader import load_pdf
from invoice_extractor.pipeline import ExtractFn, find_pdfs, run

PROJECT_DIR = Path(__file__).resolve().parent
log = logging.getLogger("extract_invoices")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract supplier, invoice number, dates, amounts and currency from PDF invoices into a CSV.",
    )
    parser.add_argument("input", type=Path, help="Folder containing the PDF invoices.")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="CSV file to write (default: output/invoices_<date>_<time>.csv).",
    )
    parser.add_argument(
        "--provider",
        choices=["openai", "anthropic"],
        default=os.getenv("LLM_PROVIDER", "openai"),
        help="AI provider (default: LLM_PROVIDER from .env, else openai).",
    )
    parser.add_argument(
        "--model",
        help="Model name (default: OPENAI_MODEL / ANTHROPIC_MODEL from .env, "
        f"else gpt-4o / {ANTHROPIC_DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--workers", type=int, default=4, help="Invoices processed in parallel (default: 4)."
    )
    parser.add_argument(
        "--max-pages", type=int, default=5, help="Pages read per invoice (default: 5)."
    )
    parser.add_argument("--recursive", action="store_true", help="Also read PDFs in subfolders.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only check the PDFs (text vs scanned, readable or not). No API calls, no cost.",
    )
    return parser.parse_args(argv)


def setup_logging() -> Path:
    log_dir = PROJECT_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"run_{datetime.now():%Y%m%d_%H%M%S}.log"
    fmt = "%(asctime)s %(levelname)-7s %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )
    # Keep the OpenAI/HTTP libraries quiet unless something goes wrong.
    for noisy in ("httpx", "httpx2", "httpcore", "openai", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log_file


def dry_run(files: list[Path], max_pages: int) -> int:
    for path in files:
        try:
            content = load_pdf(path, max_pages=max_pages)
            log.info(
                "%-40s pages=%-3d method=%-10s text_chars=%-6d image_pages=%d",
                path.name,
                content.page_count,
                content.method,
                len(content.text),
                len(content.images),
            )
        except Exception as exc:  # noqa: BLE001
            log.error("%-40s UNREADABLE: %s", path.name, exc)
    return 0


def make_extractor(
    provider: str, model: str | None, *, timeout: float | None = None, max_retries: int = 5,
) -> tuple[ExtractFn, str] | None:
    """Create the API client for the chosen provider. Returns None if the key is missing.

    SDKs are imported here so --dry-run works without any key or the unused SDK installed.
    Both SDKs retry rate limits, timeouts and 5xx errors with exponential backoff.
    """
    if provider == "anthropic":
        if not os.getenv("ANTHROPIC_API_KEY"):
            log.error("ANTHROPIC_API_KEY is not set. Put it in a .env file (see .env.example).")
            return None
        from anthropic import Anthropic

        from invoice_extractor import llm_anthropic

        client = Anthropic(max_retries=max_retries, timeout=timeout or 300)
        model = model or os.getenv("ANTHROPIC_MODEL") or ANTHROPIC_DEFAULT_MODEL
        return (lambda content: llm_anthropic.extract_fields(client, content, model)), model

    if not os.getenv("OPENAI_API_KEY"):
        log.error("OPENAI_API_KEY is not set. Put it in a .env file (see .env.example).")
        return None
    from openai import OpenAI

    from invoice_extractor import llm

    client = OpenAI(max_retries=max_retries, timeout=timeout or 120)
    model = model or os.getenv("OPENAI_MODEL") or "gpt-4o"
    return (lambda content: llm.extract_fields(client, content, model)), model


def main(argv: list[str] | None = None) -> int:
    load_dotenv(PROJECT_DIR / ".env")
    load_dotenv()  # also a .env in the current folder, if any
    args = parse_args(argv)
    log_file = setup_logging()

    if not args.input.is_dir():
        log.error("Input folder not found: %s", args.input)
        return 1
    files = find_pdfs(args.input, args.recursive)
    if not files:
        log.error("No PDF files found in %s", args.input)
        return 1
    log.info("Found %d PDF file(s) in %s", len(files), args.input)

    if args.dry_run:
        return dry_run(files, args.max_pages)

    built = make_extractor(args.provider, args.model)
    if built is None:
        return 1
    extract, model = built
    output = args.output or PROJECT_DIR / "output" / f"invoices_{datetime.now():%Y%m%d_%H%M%S}.csv"

    log.info("Provider: %s, model: %s, workers: %d", args.provider, model, args.workers)
    results = run(files, extract, output, args.workers, args.max_pages)

    errors = sum(any(r.startswith("ERROR") for r in res.reasons) for res in results)
    review = sum(res.needs_review for res in results)
    log.info("-" * 60)
    log.info("Processed: %d | OK: %d | Needs review: %d (of which errors: %d)",
             len(results), len(results) - review, review, errors)
    log.info("CSV written to: %s", output)
    log.info("Log written to: %s", log_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
