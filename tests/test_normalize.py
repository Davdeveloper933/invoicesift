from decimal import Decimal

import pytest

from invoice_extractor.normalize import normalize_fields, parse_amount, parse_currency, parse_date

GOOD = {
    "supplier_name": "Acme Ltd",
    "invoice_number": "INV-1",
    "invoice_date": "2026-03-01",
    "due_date": "2026-03-31",
    "subtotal": 100,
    "tax": 20,
    "total": 120,
    "currency": "GBP",
}


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-03-05", "2026-03-05"),
        ("March 5, 2026", "2026-03-05"),
        ("5 March 2026", "2026-03-05"),
        ("5th March 2026", "2026-03-05"),
        ("Mar 5 2026", "2026-03-05"),
        ("2026/03/05", "2026-03-05"),
        ("03/05/2026", None),  # ambiguous day/month -> not guessed here
        ("not a date", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_date(value, expected):
    assert parse_date(value) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        (1234.5, "1234.50"),
        (0, "0.00"),
        ("1,234.50", "1234.50"),
        ("1.234,50", "1234.50"),
        ("1 234,50 €", "1234.50"),
        ("$ 99", "99.00"),
        ("12,50", "12.50"),
        ("1,234", "1234.00"),
        ("1.234.567", "1234567.00"),
        ("(12.00)", "-12.00"),
        ("n/a", None),
        (None, None),
    ],
)
def test_parse_amount(value, expected):
    result = parse_amount(value)
    assert (None if result is None else str(result)) == expected


def test_parse_currency():
    assert parse_currency("usd") == "USD"
    assert parse_currency("€") == "EUR"
    assert parse_currency("dollars") is None
    assert parse_currency(None) is None


def test_clean_invoice_has_no_review_reasons():
    clean, reasons = normalize_fields(GOOD)
    assert reasons == []
    assert clean["total"] == Decimal("120.00")


def test_missing_field_is_blank_and_flagged():
    clean, reasons = normalize_fields({**GOOD, "due_date": None})
    assert clean["due_date"] is None
    assert any("missing: due_date" in r for r in reasons)


def test_totals_that_do_not_add_up_are_flagged():
    _, reasons = normalize_fields({**GOOD, "total": 150})
    assert any("!= total" in r for r in reasons)


def test_rounding_difference_is_tolerated():
    _, reasons = normalize_fields({**GOOD, "total": 120.01})
    assert reasons == []


def test_due_before_invoice_date_is_flagged():
    _, reasons = normalize_fields({**GOOD, "due_date": "2026-02-01"})
    assert any("before invoice_date" in r for r in reasons)


def test_unparseable_value_is_blank_and_flagged():
    clean, reasons = normalize_fields({**GOOD, "invoice_date": "sometime in March"})
    assert clean["invoice_date"] is None
    assert any("invoice_date not a valid date" in r for r in reasons)
