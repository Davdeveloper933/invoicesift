"""Generate 10 synthetic test invoices + the expected (ground-truth) CSV.

These are placeholders for local testing until the client's real samples are
used. They deliberately cover the hard cases: different layouts and languages,
day-first vs month-first dates, European number formats, two tax lines,
payment terms without a printed due date, a missing due date, a running
account balance, a 2-page invoice, and two scanned (image-only) PDFs.

    python scripts/make_sample_invoices.py [--out sample_invoices]
"""

from __future__ import annotations

import argparse
import csv
import io
import random
from pathlib import Path

import pymupdf
from PIL import Image, ImageFilter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, LETTER
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas

ROOT = Path(__file__).resolve().parent.parent


def fmt_us(x: float) -> str:
    return f"{x:,.2f}"


def fmt_de(x: float) -> str:
    return f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_fr(x: float) -> str:
    return f"{x:,.2f}".replace(",", " ").replace(".", ",")


# ---------------------------------------------------------------- layouts

def layout_classic(c: Canvas, inv: dict) -> None:
    """Supplier top-left, invoice meta top-right, item table, totals bottom-right."""
    w, h = inv["pagesize"]
    f = inv["fmt"]
    c.setFont("Helvetica-Bold", 16)
    c.drawString(20 * mm, h - 25 * mm, inv["supplier"])
    c.setFont("Helvetica", 9)
    for i, line in enumerate(inv["supplier_address"]):
        c.drawString(20 * mm, h - 31 * mm - i * 4.5 * mm, line)

    c.setFont("Helvetica-Bold", 22)
    c.drawRightString(w - 20 * mm, h - 25 * mm, inv["labels"]["title"])
    c.setFont("Helvetica", 10)
    y = h - 35 * mm
    for label, value in inv["meta"]:
        c.drawRightString(w - 60 * mm, y, label)
        c.drawRightString(w - 20 * mm, y, value)
        y -= 5.5 * mm

    c.setFont("Helvetica-Bold", 10)
    c.drawString(20 * mm, h - 65 * mm, inv["labels"]["bill_to"])
    c.setFont("Helvetica", 10)
    for i, line in enumerate(CUSTOMER):
        c.drawString(20 * mm, h - 71 * mm - i * 5 * mm, line)

    y = draw_items(c, inv, inv["items"], h - 100 * mm)
    draw_totals(c, inv, y - 8 * mm, f)
    draw_footer(c, inv)


def layout_banner(c: Canvas, inv: dict) -> None:
    """Coloured banner, summary box with the amount due at the top."""
    w, h = inv["pagesize"]
    f = inv["fmt"]
    c.setFillColor(colors.HexColor(inv.get("color", "#1F4E79")))
    c.rect(0, h - 40 * mm, w, 40 * mm, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(15 * mm, h - 20 * mm, inv["supplier"].upper())
    c.setFont("Helvetica", 9)
    c.drawString(15 * mm, h - 28 * mm, "  |  ".join(inv["supplier_address"]))
    c.setFillColor(colors.black)

    c.setStrokeColor(colors.grey)
    c.rect(w - 95 * mm, h - 90 * mm, 80 * mm, 42 * mm)
    c.setFont("Helvetica", 9)
    y = h - 55 * mm
    for label, value in inv["meta"]:
        c.drawString(w - 92 * mm, y, label)
        c.drawRightString(w - 18 * mm, y, value)
        y -= 5 * mm
    c.setFont("Helvetica-Bold", 12)
    c.drawString(w - 92 * mm, h - 86 * mm, inv["labels"].get("due_box", "AMOUNT DUE"))
    c.drawRightString(w - 18 * mm, h - 86 * mm, inv["amount_due_text"])

    c.setFont("Helvetica-Bold", 10)
    c.drawString(15 * mm, h - 55 * mm, inv["labels"]["bill_to"])
    c.setFont("Helvetica", 10)
    for i, line in enumerate(CUSTOMER):
        c.drawString(15 * mm, h - 61 * mm - i * 5 * mm, line)

    y = draw_items(c, inv, inv["items"], h - 105 * mm)
    draw_totals(c, inv, y - 8 * mm, f)
    draw_footer(c, inv)


def layout_european(c: Canvas, inv: dict) -> None:
    """DIN-style letter: small sender line, address window, meta block on the left."""
    w, h = inv["pagesize"]
    f = inv["fmt"]
    c.setFont("Helvetica-Bold", 14)
    c.drawRightString(w - 20 * mm, h - 20 * mm, inv["supplier"])
    c.setFont("Helvetica", 8)
    for i, line in enumerate(inv["supplier_address"]):
        c.drawRightString(w - 20 * mm, h - 25 * mm - i * 4 * mm, line)
    c.setFont("Helvetica", 7)
    c.drawString(25 * mm, h - 45 * mm, inv["supplier"] + " · " + " · ".join(inv["supplier_address"][:2]))
    c.setFont("Helvetica", 10)
    for i, line in enumerate(CUSTOMER):
        c.drawString(25 * mm, h - 52 * mm - i * 5 * mm, line)

    c.setFont("Helvetica-Bold", 16)
    c.drawString(25 * mm, h - 90 * mm, inv["labels"]["title"])
    c.setFont("Helvetica", 10)
    y = h - 98 * mm
    for label, value in inv["meta"]:
        c.drawString(25 * mm, y, label)
        c.drawString(75 * mm, y, value)
        y -= 5 * mm

    y = draw_items(c, inv, inv["items"], y - 8 * mm)
    draw_totals(c, inv, y - 8 * mm, f)
    draw_footer(c, inv)


def draw_items(c: Canvas, inv: dict, items: list, top: float) -> float:
    w, _ = inv["pagesize"]
    f = inv["fmt"]
    L = inv["labels"]
    c.setFillColor(colors.HexColor("#EEEEEE"))
    c.rect(20 * mm, top - 2 * mm, w - 40 * mm, 7 * mm, stroke=0, fill=1)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(22 * mm, top, L["desc"])
    c.drawRightString(w - 75 * mm, top, L["qty"])
    c.drawRightString(w - 50 * mm, top, L["price"])
    c.drawRightString(w - 22 * mm, top, L["amount"])
    c.setFont("Helvetica", 9)
    y = top - 7 * mm
    for desc, qty, price in items:
        c.drawString(22 * mm, y, desc)
        c.drawRightString(w - 75 * mm, y, str(qty))
        c.drawRightString(w - 50 * mm, y, f(price))
        c.drawRightString(w - 22 * mm, y, f(qty * price))
        y -= 5.5 * mm
    c.line(20 * mm, y + 2 * mm, w - 20 * mm, y + 2 * mm)
    return y


def draw_totals(c: Canvas, inv: dict, y: float, f) -> None:
    w, _ = inv["pagesize"]
    for label, value, bold in inv["totals"]:
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 11 if bold else 10)
        c.drawRightString(w - 60 * mm, y, label)
        c.drawRightString(w - 22 * mm, y, value)
        y -= 6 * mm


def draw_footer(c: Canvas, inv: dict) -> None:
    w, _ = inv["pagesize"]
    c.setFont("Helvetica", 8)
    c.setFillColor(colors.grey)
    for i, line in enumerate(inv.get("footer", [])):
        c.drawCentredString(w / 2, 22 * mm - i * 4 * mm, line)
    c.setFillColor(colors.black)


CUSTOMER = ["Riverside Hospitality Group", "Accounts Payable", "250 Commerce Street", "Springfield, IL 62701"]

EN = dict(title="INVOICE", bill_to="BILL TO", desc="Description", qty="Qty", price="Unit Price", amount="Amount")


def sums(items):
    return round(sum(q * p for _, q, p in items), 2)


# ---------------------------------------------------------------- the 10 invoices

def build_invoices() -> list[dict]:
    invs = []

    # 1. US, month-name dates, sales tax.
    items = [("Copy paper A4, 80gsm (box of 5 reams)", 20, 32.50), ("Toner cartridge HP 26X", 6, 89.00),
             ("Desk organizer, black mesh", 12, 14.75), ("Ballpoint pens, blue (pack of 50)", 5, 9.20)]
    sub = sums(items); tax = round(sub * 0.0825, 2)
    invs.append(dict(
        file="01_northwind_office_supplies.pdf", layout=layout_classic, pagesize=LETTER, fmt=fmt_us, labels=EN,
        supplier="Northwind Office Supplies Inc.", supplier_address=["1200 Market Ave, Suite 400", "Dallas, TX 75201", "Tax ID 75-2849311"],
        meta=[("Invoice #:", "NW-2026-04817"), ("Invoice Date:", "March 3, 2026"), ("Due Date:", "April 2, 2026"), ("PO Number:", "PO-88412")],
        items=items, totals=[("Subtotal", f"${fmt_us(sub)}", False), ("Sales Tax (8.25%)", f"${fmt_us(tax)}", False), ("TOTAL DUE", f"${fmt_us(sub + tax)}", True)],
        footer=["Payment terms: Net 30. Please reference the invoice number with your payment."],
        expected=dict(supplier_name="Northwind Office Supplies Inc.", invoice_number="NW-2026-04817", invoice_date="2026-03-03", due_date="2026-04-02", subtotal=sub, tax=tax, currency="USD"),
    ))

    # 2. UK, day-first numeric dates (14/09 is unambiguous, 05/10 is not), VAT.
    items = [("Pallet delivery - Manchester to Leeds", 14, 145.00), ("Tail-lift surcharge", 14, 12.50), ("Fuel surcharge (October)", 1, 175.00)]
    sub = sums(items); tax = round(sub * 0.20, 2)
    invs.append(dict(
        file="02_brightline_logistics.pdf", layout=layout_banner, pagesize=A4, fmt=fmt_us, color="#0B6E4F",
        labels=dict(EN, due_box="BALANCE DUE"), amount_due_text=f"£{fmt_us(sub + tax)}",
        supplier="Brightline Logistics Ltd", supplier_address=["Unit 7 Trafford Park", "Manchester M17 1HH", "VAT Reg GB 284 1937 22"],
        meta=[("Invoice No.", "BL/26/10233"), ("Invoice Date", "05/10/2026"), ("Due Date", "04/11/2026"), ("Account", "RIV001")],
        items=items, totals=[("Net", f"£{fmt_us(sub)}", False), ("VAT @ 20%", f"£{fmt_us(tax)}", False), ("Total", f"£{fmt_us(sub + tax)}", True)],
        footer=["Brightline Logistics Ltd, registered in England & Wales No. 08812345", "Bank: Barclays, Sort code 20-45-77, Account 43218765"],
        expected=dict(supplier_name="Brightline Logistics Ltd", invoice_number="BL/26/10233", invoice_date="2026-10-05", due_date="2026-11-04", subtotal=sub, tax=tax, currency="GBP"),
    ))

    # 3. German, European number format, dotted dates.
    items = [("Hydraulikpumpe HP-220", 2, 1475.00), ("Dichtungssatz DS-14", 10, 38.50), ("Montage vor Ort (Std.)", 6, 92.50)]
    sub = sums(items); tax = round(sub * 0.19, 2)
    invs.append(dict(
        file="03_mueller_industrietechnik.pdf", layout=layout_european, pagesize=A4, fmt=fmt_de,
        labels=dict(title="RECHNUNG", bill_to="", desc="Bezeichnung", qty="Menge", price="Einzelpreis", amount="Gesamt"),
        supplier="Müller Industrietechnik GmbH", supplier_address=["Industriestraße 12", "70565 Stuttgart", "USt-IdNr. DE 811 234 567"],
        meta=[("Rechnungsnummer:", "RE-2026-1187"), ("Rechnungsdatum:", "02.10.2026"), ("Kundennummer:", "K-40211"), ("Fällig am:", "01.11.2026")],
        items=items, totals=[("Nettobetrag", f"{fmt_de(sub)} EUR", False), ("zzgl. 19% MwSt.", f"{fmt_de(tax)} EUR", False), ("Rechnungsbetrag", f"{fmt_de(sub + tax)} EUR", True)],
        footer=["Zahlbar innerhalb 30 Tagen ohne Abzug.", "Commerzbank Stuttgart · IBAN DE12 6004 0071 0123 4567 00"],
        expected=dict(supplier_name="Müller Industrietechnik GmbH", invoice_number="RE-2026-1187", invoice_date="2026-10-02", due_date="2026-11-01", subtotal=sub, tax=tax, currency="EUR"),
    ))

    # 4. US, terms only (Net 15, no printed due date) + previous balance trap.
    items = [("Corrugated boxes 18x12x12 (bundle 25)", 16, 38.00), ("Packing tape 2in clear (case)", 4, 41.00), ("Void fill paper roll", 3, 49.33)]
    sub = sums(items); tax = round(sub * 0.0725, 2); total = round(sub + tax, 2)
    invs.append(dict(
        file="04_pacific_coast_packaging.pdf", layout=layout_banner, pagesize=LETTER, fmt=fmt_us, color="#8A3B12",
        labels=EN, amount_due_text=f"${fmt_us(total + 250.00)}",
        supplier="Pacific Coast Packaging Co.", supplier_address=["88 Harbor Blvd", "Long Beach, CA 90802"],
        meta=[("Invoice", "PCP-55190"), ("Date", "08/20/2026"), ("Terms", "Net 15"), ("Customer ID", "C-1182")],
        items=items, totals=[("Subtotal", fmt_us(sub), False), ("CA Sales Tax 7.25%", fmt_us(tax), False), ("Invoice Total", fmt_us(total), True),
                             ("Previous Balance", fmt_us(250.00), False), ("Amount Due", fmt_us(total + 250.00), True)],
        footer=["Thank you for your business!"],
        expected=dict(supplier_name="Pacific Coast Packaging Co.", invoice_number="PCP-55190", invoice_date="2026-08-20", due_date="2026-09-04", subtotal=sub, tax=tax, currency="USD"),
    ))

    # 5. Canada, two tax lines (GST + PST) that must be summed.
    items = [("Managed IT support - July", 1, 2400.00), ("Firewall licence renewal", 1, 650.00), ("On-site visit (hours)", 2, 75.00)]
    sub = sums(items); gst = round(sub * 0.05, 2); pst = round(sub * 0.07, 2)
    invs.append(dict(
        file="05_maple_leaf_it_services.pdf", layout=layout_classic, pagesize=LETTER, fmt=fmt_us, labels=EN,
        supplier="Maple Leaf IT Services Ltd.", supplier_address=["455 Granville St", "Vancouver, BC V6C 1T2", "GST/HST No. 81234 5678 RT0001"],
        meta=[("Invoice No:", "MLIT-0726-031"), ("Date:", "2026-07-15"), ("Due:", "2026-08-14"), ("Currency:", "CAD")],
        items=items, totals=[("Subtotal", fmt_us(sub), False), ("GST 5%", fmt_us(gst), False), ("PST 7%", fmt_us(pst), False), ("Total (CAD)", fmt_us(sub + gst + pst), True)],
        footer=["All amounts in Canadian dollars."],
        expected=dict(supplier_name="Maple Leaf IT Services Ltd.", invoice_number="MLIT-0726-031", invoice_date="2026-07-15", due_date="2026-08-14", subtotal=sub, tax=round(gst + pst, 2), currency="CAD"),
    ))

    # 6. French, space thousands separator, comma decimals, day-first dates.
    items = [("Séance photo produit (journée)", 1, 950.00), ("Retouche images", 25, 18.00), ("Frais de déplacement", 1, 100.00)]
    sub = sums(items); tax = round(sub * 0.20, 2)
    invs.append(dict(
        file="06_studio_lumiere.pdf", layout=layout_european, pagesize=A4, fmt=fmt_fr,
        labels=dict(title="FACTURE", bill_to="", desc="Désignation", qty="Qté", price="Prix unitaire", amount="Montant"),
        supplier="Studio Lumière SARL", supplier_address=["14 rue des Arts", "69002 Lyon", "TVA FR 45 512 345 678"],
        meta=[("N° de facture :", "F2026-0612"), ("Date :", "05/06/2026"), ("Échéance :", "05/07/2026")],
        items=items, totals=[("Total HT", f"{fmt_fr(sub)} €", False), ("TVA 20 %", f"{fmt_fr(tax)} €", False), ("Total TTC", f"{fmt_fr(sub + tax)} €", True)],
        footer=["Pénalités de retard : 3 fois le taux d'intérêt légal."],
        expected=dict(supplier_name="Studio Lumière SARL", invoice_number="F2026-0612", invoice_date="2026-06-05", due_date="2026-07-05", subtotal=sub, tax=tax, currency="EUR"),
    ))

    # 7. No due date and no terms anywhere -> due_date must stay blank, row flagged.
    items = [("Weekly office cleaning - September", 4, 135.00), ("Carpet shampoo, conference room", 1, 100.00)]
    sub = sums(items)
    invs.append(dict(
        file="07_greenfield_cleaning.pdf", layout=layout_classic, pagesize=LETTER, fmt=fmt_us, labels=EN,
        supplier="Greenfield Cleaning Services", supplier_address=["31 Elm Street", "Springfield, IL 62704"],
        meta=[("Invoice:", "GCS-1009"), ("Date:", "Oct 1, 2026")],
        items=items, totals=[("Subtotal", f"${fmt_us(sub)}", False), ("Tax", "$0.00", False), ("Total", f"${fmt_us(sub)}", True)],
        footer=["Services exempt from sales tax."],
        expected=dict(supplier_name="Greenfield Cleaning Services", invoice_number="GCS-1009", invoice_date="2026-10-01", due_date="", subtotal=sub, tax=0.0, currency="USD"),
    ))

    # 8. Two pages: line items continue, totals only on page 2.
    rnd = random.Random(8)
    parts = ["THHN wire 12AWG 500ft", "EMT conduit 3/4in 10ft", "Duplex receptacle 20A", "LED panel 2x4 40W", "Breaker 20A 1P",
             "Junction box 4in", "Wire nuts (100)", "GFCI outlet 20A", "Cable ties 8in (100)", "Toggle switch 15A"]
    items = [(f"{parts[i % 10]} (lot {i + 1})", rnd.randint(1, 12), round(rnd.uniform(3, 180), 2)) for i in range(48)]
    sub = sums(items); tax = round(sub * 0.0625, 2)
    invs.append(dict(
        file="08_apex_electrical_wholesale.pdf", layout="multipage", pagesize=LETTER, fmt=fmt_us, labels=EN,
        supplier="Apex Electrical Wholesale LLC", supplier_address=["7700 Industrial Pkwy", "Columbus, OH 43219"],
        meta=[("Invoice Number:", "AEW-778201"), ("Invoice Date:", "2026-09-30"), ("Due Date:", "2026-10-30"), ("Order:", "SO-33120")],
        items=items, totals=[("Merchandise Total", fmt_us(sub), False), ("Sales Tax 6.25%", fmt_us(tax), False), ("Invoice Total USD", fmt_us(sub + tax), True)],
        footer=["Page footer: Apex Electrical Wholesale LLC - returns within 30 days with receipt."],
        expected=dict(supplier_name="Apex Electrical Wholesale LLC", invoice_number="AEW-778201", invoice_date="2026-09-30", due_date="2026-10-30", subtotal=sub, tax=tax, currency="USD"),
    ))

    # 9. Scanned (image-only), Australian, GST.
    items = [("Sea freight LCL Shanghai-Sydney (cbm)", 6, 210.00), ("Customs clearance", 1, 380.00), ("Port & terminal fees", 1, 340.00)]
    sub = sums(items); tax = round(sub * 0.10, 2)
    invs.append(dict(
        file="09_harbor_freight_forwarding_SCANNED.pdf", layout=layout_classic, pagesize=A4, fmt=fmt_us, scan=True,
        labels=dict(EN, title="TAX INVOICE"),
        supplier="Harbour Freight Forwarding Pty Ltd", supplier_address=["Level 3, 20 Bridge St", "Sydney NSW 2000", "ABN 53 004 085 616"],
        meta=[("Invoice No:", "HFF-90412"), ("Date:", "12 August 2026"), ("Due:", "11 September 2026")],
        items=items, totals=[("Subtotal", f"A${fmt_us(sub)}", False), ("GST 10%", f"A${fmt_us(tax)}", False), ("Total (AUD)", f"A${fmt_us(sub + tax)}", True)],
        footer=["Please pay by EFT. BSB 062-000 Account 1234 5678"],
        expected=dict(supplier_name="Harbour Freight Forwarding Pty Ltd", invoice_number="HFF-90412", invoice_date="2026-08-12", due_date="2026-09-11", subtotal=sub, tax=tax, currency="AUD"),
    ))

    # 10. Scanned, skewed and noisy, Swedish, space thousands separators.
    items = [("Furu konstruktionsvirke 45x145 (m)", 400, 22.50), ("Plywood 12 mm (skiva)", 20, 170.00)]
    sub = sums(items); tax = round(sub * 0.25, 2)
    invs.append(dict(
        file="10_nordic_timber_SCANNED.pdf", layout=layout_banner, pagesize=A4, fmt=fmt_fr, scan=True, skew=1.2, color="#2F4F4F",
        labels=dict(title="FAKTURA", bill_to="KUND", desc="Beskrivning", qty="Antal", price="A-pris", amount="Belopp", due_box="ATT BETALA"),
        amount_due_text=f"{fmt_fr(sub + tax)} SEK",
        supplier="Nordic Timber AB", supplier_address=["Sågverksvägen 3", "903 27 Umeå", "Org.nr 556123-4567"],
        meta=[("Fakturanummer", "NT-26-3391"), ("Fakturadatum", "2026-10-10"), ("Förfallodatum", "2026-11-09"), ("Valuta", "SEK")],
        items=items, totals=[("Summa exkl. moms", fmt_fr(sub), False), ("Moms 25%", fmt_fr(tax), False), ("Att betala SEK", fmt_fr(sub + tax), True)],
        footer=["Bankgiro 5050-1055 · Dröjsmålsränta 8%"],
        expected=dict(supplier_name="Nordic Timber AB", invoice_number="NT-26-3391", invoice_date="2026-10-10", due_date="2026-11-09", subtotal=sub, tax=tax, currency="SEK"),
    ))
    return invs


def render_multipage(c: Canvas, inv: dict) -> None:
    w, h = inv["pagesize"]
    page1 = dict(inv, totals=[])
    first, rest = inv["items"][:24], inv["items"][24:]
    layout_classic(c, dict(page1, items=first, footer=["Continued on page 2"]))
    c.showPage()
    c.setFont("Helvetica-Bold", 12)
    c.drawString(20 * mm, h - 20 * mm, f"{inv['supplier']} - Invoice AEW-778201 (page 2 of 2)")
    y = draw_items(c, inv, rest, h - 35 * mm)
    draw_totals(c, inv, y - 8 * mm, inv["fmt"])
    draw_footer(c, inv)


def make_pdf(inv: dict) -> bytes:
    buf = io.BytesIO()
    c = Canvas(buf, pagesize=inv["pagesize"])
    c.setTitle(inv["file"])
    if inv["layout"] == "multipage":
        render_multipage(c, inv)
    else:
        inv["layout"](c, inv)
    c.showPage()
    c.save()
    return buf.getvalue()


def to_scanned(pdf_bytes: bytes, skew: float, seed: int) -> bytes:
    """Rasterise a PDF and degrade it like a office scanner would. No text layer."""
    rnd = random.Random(seed)
    src = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    out = pymupdf.open()
    for page in src:
        pix = page.get_pixmap(dpi=150, colorspace=pymupdf.csGRAY)
        img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("L")
        img = img.rotate(skew, expand=False, fillcolor=255, resample=Image.BICUBIC)
        img = img.filter(ImageFilter.GaussianBlur(0.6))
        px = img.load()
        for _ in range(img.width * img.height // 300):  # speckle noise
            x, y = rnd.randrange(img.width), rnd.randrange(img.height)
            px[x, y] = rnd.choice((0, 90, 180))
        jpg = io.BytesIO()
        img.save(jpg, "JPEG", quality=70)
        new = out.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, stream=jpg.getvalue())
    return out.tobytes()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=ROOT / "sample_invoices")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    rows = []
    for i, inv in enumerate(build_invoices(), start=1):
        data = make_pdf(inv)
        if inv.get("scan"):
            data = to_scanned(data, inv.get("skew", 0.4), seed=i)
        (args.out / inv["file"]).write_bytes(data)
        exp = inv["expected"]
        total = round(exp["subtotal"] + exp["tax"], 2)
        rows.append({
            "file_name": inv["file"],
            **{k: exp[k] for k in ("supplier_name", "invoice_number", "invoice_date", "due_date")},
            "subtotal": f"{exp['subtotal']:.2f}", "tax": f"{exp['tax']:.2f}", "total": f"{total:.2f}",
            "currency": exp["currency"],
        })
        print("wrote", inv["file"])

    expected = ROOT / "test_run" / "expected_results.csv"
    expected.parent.mkdir(exist_ok=True)
    with expected.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print("wrote", expected)


if __name__ == "__main__":
    main()
