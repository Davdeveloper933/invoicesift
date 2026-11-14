"""OpenAI call that turns invoice text / page images into a fixed JSON structure."""

from __future__ import annotations

import json
from typing import Any, Protocol

from .pdf_loader import PdfContent

# Strict JSON schema (OpenAI Structured Outputs). Every field must be present in
# the reply; the model returns null when a value is not on the invoice.
INVOICE_SCHEMA: dict[str, Any] = {
    "name": "invoice_fields",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "supplier_name": {
                "type": ["string", "null"],
                "description": "Legal or trading name of the company that issued the invoice (the seller), not the customer.",
            },
            "invoice_number": {
                "type": ["string", "null"],
                "description": "Invoice number / invoice ID exactly as printed. Not a PO, order, customer or account number.",
            },
            "invoice_date": {
                "type": ["string", "null"],
                "description": "Invoice / issue date in YYYY-MM-DD.",
            },
            "due_date": {
                "type": ["string", "null"],
                "description": "Payment due date in YYYY-MM-DD. Only if a date is printed or follows unambiguously from printed payment terms and the invoice date.",
            },
            "subtotal": {
                "type": ["number", "null"],
                "description": "Net amount before tax.",
            },
            "tax": {
                "type": ["number", "null"],
                "description": "Total tax amount (VAT / GST / sales tax). 0 only if the invoice explicitly shows zero tax.",
            },
            "total": {
                "type": ["number", "null"],
                "description": "Gross invoice total including tax (amount of this invoice, not a running account balance).",
            },
            "currency": {
                "type": ["string", "null"],
                "description": "ISO 4217 currency code, e.g. USD, EUR, GBP.",
            },
        },
        "required": [
            "supplier_name",
            "invoice_number",
            "invoice_date",
            "due_date",
            "subtotal",
            "tax",
            "total",
            "currency",
        ],
    },
}

SYSTEM_PROMPT = """You extract data from supplier invoices for an accounts-payable team.

Rules:
- Return only values that are actually on the invoice. If a value is missing or unreadable, return null. Never guess.
- The supplier is the party issuing the invoice and receiving the payment, not the bill-to customer.
- Dates: convert to YYYY-MM-DD. For numeric dates, use the supplier's country/currency to decide day-first vs month-first (e.g. UK/EU invoices are day-first, US invoices month-first).
- Due date: use the printed due date. If no due date is printed but payment terms with a number of days are printed (e.g. "Net 30", "payable within 30 days"), compute it from the invoice date. Only if the words "due on receipt" (or equivalent) are printed is the due date equal to the invoice date. If neither a due date nor payment terms are printed, return null - do NOT default to the invoice date.
- Supplier name: write it as the company writes it in normal text (e.g. "Acme Supplies Ltd"), not in the all-caps style of a logo or banner, if the normal form appears anywhere on the invoice.
- Amounts: plain numbers with a dot as decimal separator, no currency symbols or thousands separators (e.g. "1.234,50 EUR" -> 1234.50).
- If several tax lines exist, sum them into tax.
- Currency: ISO 4217 code. Infer from symbols only when unambiguous (EUR for the euro sign, GBP for the pound sign); for "$" use the supplier's country if it is clear, otherwise null.
"""


class ChatClient(Protocol):
    """The part of ``openai.OpenAI`` we use (lets tests pass in a fake client)."""

    chat: Any


class ExtractionError(Exception):
    """The model did not return a usable answer."""


def build_messages(content: PdfContent) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": f"Invoice file: {content.path.name} ({content.page_count} page(s)). Extract the fields.",
        }
    ]
    if content.text:
        parts.append({"type": "text", "text": "Invoice text:\n\n" + content.text})
    for image in content.images:
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{image}", "detail": "high"},
            }
        )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": parts},
    ]


def extract_fields(client: ChatClient, content: PdfContent, model: str) -> dict[str, Any]:
    """Send the invoice to the model and return the raw field dict."""
    response = client.chat.completions.create(
        model=model,
        messages=build_messages(content),
        response_format={"type": "json_schema", "json_schema": INVOICE_SCHEMA},
        temperature=0,
    )
    choice = response.choices[0]
    message = choice.message
    if getattr(message, "refusal", None):
        raise ExtractionError(f"model refused: {message.refusal}")
    if choice.finish_reason == "length":
        raise ExtractionError("model reply was cut off (length limit)")
    try:
        data = json.loads(message.content or "")
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"model returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ExtractionError("model returned JSON that is not an object")
    return data
