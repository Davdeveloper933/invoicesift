"""End-to-end tests with a fake OpenAI client (no network, no API key)."""

import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pymupdf

from invoice_extractor import llm
from invoice_extractor.pipeline import run as _run

REPLY = {
    "supplier_name": "Acme Ltd",
    "invoice_number": "INV-1",
    "invoice_date": "2026-03-01",
    "due_date": None,
    "subtotal": 100,
    "tax": 20,
    "total": 120,
    "currency": "GBP",
}


def run(files, client, model, output, **kwargs):
    """Run the pipeline with the OpenAI backend wired to a fake client."""
    return _run(files, lambda content: llm.extract_fields(client, content, model), output, **kwargs)


class FakeClient:
    """Mimics ``client.chat.completions.create`` and records what was sent."""

    def __init__(self, reply=REPLY, fail_for=()):
        self.reply, self.fail_for, self.calls = reply, set(fail_for), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        file_line = kwargs["messages"][1]["content"][0]["text"]
        if any(name in file_line for name in self.fail_for):
            raise RuntimeError("simulated API failure")
        message = SimpleNamespace(content=json.dumps(self.reply), refusal=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])


def make_text_pdf(path: Path, text: str = "INVOICE INV-1 from Acme Ltd, total 120.00 GBP, date 2026-03-01"):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text)
    doc.save(path)


def make_blank_pdf(path: Path):
    """A page with no text layer stands in for a scanned invoice."""
    doc = pymupdf.open()
    doc.new_page()
    doc.save(path)


def read_rows(path: Path):
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def test_bad_file_does_not_stop_the_run(tmp_path):
    make_text_pdf(tmp_path / "a.pdf")
    (tmp_path / "b_corrupt.pdf").write_bytes(b"this is not a pdf")
    make_text_pdf(tmp_path / "c.pdf")
    out = tmp_path / "out.csv"

    run(sorted(tmp_path.glob("*.pdf")), FakeClient(), "gpt-4o", out, workers=2)

    rows = read_rows(out)
    assert [r["file_name"] for r in rows] == ["a.pdf", "b_corrupt.pdf", "c.pdf"]
    assert rows[1]["needs_review"] == "YES"
    assert rows[1]["review_reasons"].startswith("ERROR")
    assert rows[0]["total"] == "120.00"


def test_api_error_on_one_file_is_isolated(tmp_path):
    make_text_pdf(tmp_path / "a.pdf")
    make_text_pdf(tmp_path / "b.pdf")
    out = tmp_path / "out.csv"

    run(sorted(tmp_path.glob("*.pdf")), FakeClient(fail_for={"b.pdf"}), "gpt-4o", out)

    rows = read_rows(out)
    assert rows[0]["supplier_name"] == "Acme Ltd"
    assert "simulated API failure" in rows[1]["review_reasons"]


def test_no_credits_stops_the_run_but_csv_lists_every_file(tmp_path):
    class QuotaError(Exception):
        code = "insufficient_quota"

    def extract(content):
        raise QuotaError("You have no credits remaining")

    for name in ("a.pdf", "b.pdf", "c.pdf"):
        make_text_pdf(tmp_path / name)
    out = tmp_path / "out.csv"
    _run(sorted(tmp_path.glob("*.pdf")), extract, out, workers=1)

    rows = read_rows(out)
    assert [r["file_name"] for r in rows] == ["a.pdf", "b.pdf", "c.pdf"]
    assert rows[0]["review_reasons"].startswith("ERROR")
    assert all(r["review_reasons"].startswith("SKIPPED") for r in rows[1:])


def test_csv_format_and_missing_field_flag(tmp_path):
    make_text_pdf(tmp_path / "a.pdf")
    out = tmp_path / "out.csv"
    run([tmp_path / "a.pdf"], FakeClient(), "gpt-4o", out)

    row = read_rows(out)[0]
    assert row["invoice_date"] == "2026-03-01"
    assert row["subtotal"] == "100.00" and row["tax"] == "20.00"
    assert row["due_date"] == ""
    assert row["needs_review"] == "YES"
    assert "missing: due_date" in row["review_reasons"]
    assert row["extraction_method"] == "text"


def test_scanned_page_is_sent_as_image(tmp_path):
    make_blank_pdf(tmp_path / "scan.pdf")
    client = FakeClient()
    run([tmp_path / "scan.pdf"], client, "gpt-4o", tmp_path / "out.csv")

    parts = client.calls[0]["messages"][1]["content"]
    assert any(p["type"] == "image_url" for p in parts)
    assert read_rows(tmp_path / "out.csv")[0]["extraction_method"] == "image"


def test_structured_output_schema_is_requested(tmp_path):
    make_text_pdf(tmp_path / "a.pdf")
    client = FakeClient()
    run([tmp_path / "a.pdf"], client, "gpt-4o", tmp_path / "out.csv")

    fmt = client.calls[0]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True


def test_duplicates_are_flagged(tmp_path):
    full = {**REPLY, "due_date": "2026-03-31"}
    make_text_pdf(tmp_path / "a.pdf")
    make_text_pdf(tmp_path / "a_copy.pdf")
    out = tmp_path / "out.csv"
    run(sorted(tmp_path.glob("*.pdf")), FakeClient(reply=full), "gpt-4o", out)

    for row in read_rows(out):
        assert "possible duplicate" in row["review_reasons"]


def test_long_pdf_text_is_capped(tmp_path):
    from invoice_extractor.pdf_loader import TRUNCATED_NOTE, load_pdf

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "INVOICE " + "x" * 300)
    doc.save(tmp_path / "long.pdf")
    content = load_pdf(tmp_path / "long.pdf", max_chars=100)
    assert content.text.endswith(TRUNCATED_NOTE)
    assert len(content.text) == 100 + len(TRUNCATED_NOTE)
