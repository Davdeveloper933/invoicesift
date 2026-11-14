"""Tests for the Anthropic (Claude) backend with a fake client (no network, no key)."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from invoice_extractor import llm_anthropic
from invoice_extractor.llm import ExtractionError
from invoice_extractor.pdf_loader import PdfContent

REPLY = {
    "supplier_name": "Acme Ltd",
    "invoice_number": "INV-1",
    "invoice_date": "2026-03-01",
    "due_date": "2026-03-31",
    "subtotal": 100,
    "tax": 20,
    "total": 120,
    "currency": "GBP",
}


class FakeAnthropic:
    def __init__(self, text=json.dumps(REPLY), stop_reason="end_turn"):
        self.text, self.stop_reason, self.calls = text, stop_reason, []
        messages = SimpleNamespace(create=self.create)
        self.beta = SimpleNamespace(messages=messages)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            content=[SimpleNamespace(type="text", text=self.text)],
        )


def content(images=()):
    return PdfContent(path=Path("a.pdf"), page_count=1, text="INVOICE INV-1", images=list(images))


def test_returns_fields_and_requests_structured_output():
    client = FakeAnthropic()
    assert llm_anthropic.extract_fields(client, content()) == REPLY

    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5"
    fmt = call["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False
    assert set(fmt["schema"]["required"]) == set(REPLY)
    # Nullable fields are expressed with anyOf, not type arrays.
    assert fmt["schema"]["properties"]["due_date"]["anyOf"] == [{"type": "string"}, {"type": "null"}]


def test_scanned_pages_are_sent_as_images():
    client = FakeAnthropic()
    llm_anthropic.extract_fields(client, content(images=["aGVsbG8="]))
    blocks = client.calls[0]["messages"][0]["content"]
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"]["media_type"] == "image/png"


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_unusable_stop_reasons_raise(stop_reason):
    with pytest.raises(ExtractionError):
        llm_anthropic.extract_fields(FakeAnthropic(stop_reason=stop_reason), content())


def test_invalid_json_raises():
    with pytest.raises(ExtractionError):
        llm_anthropic.extract_fields(FakeAnthropic(text="not json"), content())
