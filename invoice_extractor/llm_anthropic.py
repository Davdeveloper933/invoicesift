"""Optional Anthropic (Claude) backend. Same prompt, same fields as the OpenAI backend.

Used with ``--provider anthropic``. OpenAI remains the default provider.
"""

from __future__ import annotations

import json
from typing import Any

from .llm import INVOICE_SCHEMA, SYSTEM_PROMPT, ExtractionError
from .pdf_loader import PdfContent

DEFAULT_MODEL = "claude-opus-5-5"


def _claude_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Rewrite ``"type": ["string", "null"]`` as ``anyOf`` for Claude's structured outputs."""
    properties = {}
    for name, prop in schema["properties"].items():
        types = prop["type"]
        if isinstance(types, list):
            prop = {
                "anyOf": [{"type": t} for t in types],
                "description": prop.get("description", ""),
            }
        properties[name] = prop
    return {**schema, "properties": properties}


CLAUDE_SCHEMA = _claude_schema(INVOICE_SCHEMA["schema"])


def build_content(content: PdfContent) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for image in content.images:
        blocks.append(
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image}}
        )
    text = f"Invoice file: {content.path.name} ({content.page_count} page(s)). Extract the fields."
    if content.text:
        text += "\n\nInvoice text:\n\n" + content.text
    blocks.append({"type": "text", "text": text})
    return blocks


def extract_fields(client: Any, content: PdfContent, model: str = DEFAULT_MODEL) -> dict[str, Any]:
    """Send the invoice to Claude and return the raw field dict."""
    response = client.beta.messages.create(
        model=model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_content(content)}],
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": CLAUDE_SCHEMA},
        },
        # If a safety classifier declines the request, the API retries it on a
        # fallback model inside the same call instead of returning a refusal.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise ExtractionError("model refused the request")
    if response.stop_reason == "max_tokens":
        raise ExtractionError("model reply was cut off (max_tokens)")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"model returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ExtractionError("model returned JSON that is not an object")
    return data
