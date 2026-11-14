"""Compare an output CSV with a hand-checked expected CSV and report accuracy.

    python scripts/compare_results.py test_run/expected_results.csv output/invoices_....csv

Amounts are compared as numbers (to the cent), text case-insensitively.
"""

from __future__ import annotations

import argparse
import csv
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

FIELDS = ["supplier_name", "invoice_number", "invoice_date", "due_date", "subtotal", "tax", "total", "currency"]
AMOUNTS = {"subtotal", "tax", "total"}


def read(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return {row["file_name"]: row for row in csv.DictReader(fh)}


def same(field: str, expected: str, actual: str) -> bool:
    expected, actual = (expected or "").strip(), (actual or "").strip()
    if field in AMOUNTS and expected and actual:
        try:
            return Decimal(expected) == Decimal(actual)
        except InvalidOperation:
            return False
    return expected.casefold() == actual.casefold()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("expected", type=Path)
    parser.add_argument("actual", type=Path)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to a legacy codepage

    expected, actual = read(args.expected), read(args.actual)
    correct = total = 0
    per_field = {f: 0 for f in FIELDS}
    mismatches = []
    for name, exp in expected.items():
        act = actual.get(name)
        if act is None:
            mismatches.append(f"{name}: missing from output")
            total += len(FIELDS)
            continue
        for field in FIELDS:
            total += 1
            if same(field, exp[field], act.get(field, "")):
                correct += 1
                per_field[field] += 1
            else:
                mismatches.append(f"{name}: {field} expected {exp[field]!r}, got {act.get(field, '')!r}")

    n = len(expected)
    print(f"Invoices: {n}   Fields correct: {correct}/{total} ({correct / total:.1%})\n")
    for field in FIELDS:
        print(f"  {field:<15} {per_field[field]}/{n}")
    print("\nMismatches:" if mismatches else "\nNo mismatches.")
    for line in mismatches:
        print("  -", line)
    return 0 if not mismatches else 2


if __name__ == "__main__":
    raise SystemExit(main())
