"""Clean model output into consistent values and decide which rows need review."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

FIELDS = [
    "supplier_name",
    "invoice_number",
    "invoice_date",
    "due_date",
    "subtotal",
    "tax",
    "total",
    "currency",
]
AMOUNT_FIELDS = ("subtotal", "tax", "total")
DATE_FIELDS = ("invoice_date", "due_date")

# Allowed difference between subtotal + tax and total (rounding on line items).
TOTAL_TOLERANCE = Decimal("0.02")

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%d %B %Y",
    "%d %b %Y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%B %d %Y",
    "%b %d %Y",
)

_CURRENCY_ALIASES = {
    "€": "EUR",
    "EURO": "EUR",
    "EUROS": "EUR",
    "£": "GBP",
    "POUND": "GBP",
    "US$": "USD",
    "USD$": "USD",
    "CA$": "CAD",
    "C$": "CAD",
    "A$": "AUD",
    "AU$": "AUD",
    "¥": "JPY",
    "CHF.": "CHF",
}


def parse_date(value: Any) -> str | None:
    """Return the date as YYYY-MM-DD, or None if it isn't an unambiguous date.

    The model is asked for ISO dates already; this is a safety net. Numeric
    day/month formats like 03/04/2026 are deliberately NOT parsed here because
    they are ambiguous without context -- the model resolves those.
    """
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    text = re.sub(r"\s+", " ", str(value)).strip().rstrip(".")
    if not text:
        return None
    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text)  # "5th March" -> "5 March"
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        if 1990 <= parsed.year <= 2100:
            return parsed.isoformat()
    return None


def parse_amount(value: Any) -> Decimal | None:
    """Turn 1234.5, "1,234.50", "1.234,50", "$ 99" or "(12.00)" into a Decimal."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value)).quantize(Decimal("0.01"))
        except InvalidOperation:
            return None

    text = str(value).strip()
    negative = text.startswith("(") and text.endswith(")") or "-" in text
    digits = re.sub(r"[^\d.,]", "", text)
    if not digits or not any(ch.isdigit() for ch in digits):
        return None

    last_dot, last_comma = digits.rfind("."), digits.rfind(",")
    if last_dot != -1 and last_comma != -1:
        decimal_sep = "." if last_dot > last_comma else ","
    elif last_comma != -1:
        # "1,234" (thousands) vs "12,50" (decimal): two decimals after the comma.
        decimal_sep = "," if len(digits) - last_comma - 1 == 2 else None
    elif last_dot != -1:
        # One dot is a decimal point; several ("1.234.567") are thousands separators.
        decimal_sep = "." if digits.count(".") == 1 else None
    else:
        decimal_sep = None

    if decimal_sep:
        whole, _, frac = digits.rpartition(decimal_sep)
        whole = re.sub(r"[.,]", "", whole)
        normalized = f"{whole or '0'}.{frac}"
    else:
        normalized = re.sub(r"[.,]", "", digits)
    try:
        amount = Decimal(normalized).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    return -amount if negative else amount


def parse_currency(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    if not text:
        return None
    text = _CURRENCY_ALIASES.get(text, text)
    return text if re.fullmatch(r"[A-Z]{3}", text) else None


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def normalize_fields(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return (clean values, review reasons) for one invoice."""
    reasons: list[str] = []
    clean: dict[str, Any] = {
        "supplier_name": clean_text(raw.get("supplier_name")),
        "invoice_number": clean_text(raw.get("invoice_number")),
        "currency": parse_currency(raw.get("currency")),
    }

    for name in DATE_FIELDS:
        raw_value = raw.get(name)
        clean[name] = parse_date(raw_value)
        if clean[name] is None and clean_text(raw_value):
            reasons.append(f"{name} not a valid date ({raw_value!r})")

    for name in AMOUNT_FIELDS:
        raw_value = raw.get(name)
        clean[name] = parse_amount(raw_value)
        if clean[name] is None and clean_text(raw_value):
            reasons.append(f"{name} not a valid number ({raw_value!r})")

    if raw.get("currency") and clean["currency"] is None:
        reasons.append(f"currency not an ISO code ({raw.get('currency')!r})")

    missing = [name for name in FIELDS if clean[name] is None]
    if missing:
        reasons.append("missing: " + ", ".join(missing))

    subtotal, tax, total = clean["subtotal"], clean["tax"], clean["total"]
    if None not in (subtotal, tax, total) and abs(subtotal + tax - total) > TOTAL_TOLERANCE:
        reasons.append(f"subtotal + tax ({subtotal + tax}) != total ({total})")
    if total is not None and total <= 0:
        reasons.append("total is zero or negative (credit note?)")

    if clean["invoice_date"] and clean["due_date"] and clean["due_date"] < clean["invoice_date"]:
        reasons.append("due_date is before invoice_date")
    if clean["invoice_date"] and clean["invoice_date"] > date.today().isoformat():
        reasons.append("invoice_date is in the future")

    return clean, reasons
