# Invoice Extractor

Reads every PDF invoice in a folder, extracts the key fields with the OpenAI API
(GPT-4o by default) and writes **one CSV file with one row per invoice**.

| Column | Content |
|---|---|
| `file_name` | PDF file the row came from |
| `supplier_name` | Company that issued the invoice |
| `invoice_number` | Invoice number as printed |
| `invoice_date` | `YYYY-MM-DD` |
| `due_date` | `YYYY-MM-DD` (calculated from terms such as "Net 30" when no date is printed) |
| `subtotal`, `tax`, `total` | Plain numbers, dot decimal, 2 decimals, e.g. `1234.50` |
| `currency` | ISO code, e.g. `USD`, `EUR`, `GBP` |
| `needs_review` | `YES` / `NO` |
| `review_reasons` | Why the row was flagged (see below) |
| `extraction_method` | `text`, `image` (scanned) or `text+image` |

The CSV is UTF-8 with BOM, so it opens correctly in Excel (accented supplier names included).

## How it works

1. **Read the PDF** (PyMuPDF). Pages with selectable text are sent to the model as text.
   Pages without a text layer (scans) are rendered to images and sent to GPT-4o's vision input,
   so no separate OCR software is needed.
2. **Extract with structured output.** The request uses OpenAI *Structured Outputs*
   (a strict JSON schema), so every reply has exactly the same 8 fields; a field that is not on
   the invoice comes back as `null`.
3. **Normalise and check.** Dates and amounts are validated and converted to one format.
   A row is flagged `needs_review = YES` when:
   - any field is missing (left blank in the CSV),
   - a value could not be parsed as a date / number / currency code,
   - `subtotal + tax` differs from `total` by more than 0.02,
   - the due date is before the invoice date, or the invoice date is in the future,
   - the total is zero or negative (e.g. a credit note),
   - the same supplier + invoice number appears twice in the run (possible duplicate),
   - the file could not be processed at all (`ERROR: ...`).
4. **One bad file never stops the run.** A corrupt or password-protected PDF, or an API error
   on one invoice, becomes a flagged row with the error message; all other invoices are still
   processed. Rate limits, timeouts and server errors are retried automatically (5 retries with
   back-off).

## Setup (once)

Requires **Python 3.10+**.

```bash
cd invoice-extractor
python -m venv .venv
```

Activate the environment:

- Windows: `.venv\Scripts\activate`
- macOS / Linux: `source .venv/bin/activate`

```bash
pip install -r requirements.txt
```

Add your API key: copy `.env.example` to `.env` and put your key in it:

```
OPENAI_API_KEY=sk-...
```

## Running it

```bash
python extract_invoices.py path/to/invoices
```

The CSV is written to `output/invoices_<date>_<time>.csv` (a new file every run, so earlier
results are never overwritten). To choose the file yourself:

```bash
python extract_invoices.py path/to/invoices --output november_invoices.csv
```

Other options:

| Option | Default | Meaning |
|---|---|---|
| `--provider` | `openai` | `openai` or `anthropic` (see "Alternative provider" below). Can also be set with `LLM_PROVIDER` in `.env`. |
| `--model` | `gpt-4o` | Model name. Can also be set with `OPENAI_MODEL` (or `ANTHROPIC_MODEL`) in `.env`. |
| `--workers` | `4` | Invoices processed in parallel. Lower it if you hit rate limits on a new API account. |
| `--max-pages` | `5` | Pages read per invoice (header and totals are on the first pages). |
| `--recursive` | off | Also read PDFs in subfolders. |
| `--dry-run` | off | Only checks the PDFs (readable? text or scanned?). No API calls, no cost. |

Console output looks like this, and the same is saved to `logs/run_<date>_<time>.log`:

```
Found 10 PDF file(s) in sample_invoices
[1/10] OK     01_northwind_office_supplies.pdf
[7/10] REVIEW 07_greenfield_cleaning.pdf
...
Processed: 10 | OK: 9 | Needs review: 1 (of which errors: 0)
CSV written to: output/invoices_20261113_101500.csv
```

### Recommended monthly workflow

1. Put the month's PDFs in one folder and run the script.
2. Open the CSV, filter `needs_review = YES` and check only those rows against the PDF.
   `review_reasons` tells you what to look at.

## Cost

Each invoice is one API call. With GPT-4o a text invoice uses roughly 1,500 - 3,000 input
tokens, a scanned page roughly 1,000 - 1,500 more. At current GPT-4o prices that is around
1 cent per invoice, i.e. **about $1 - 2 for 100 - 200 invoices a month**. Please check
[OpenAI's pricing page](https://openai.com/api/pricing/) for current rates.

## Alternative provider: Anthropic (Claude)

OpenAI is the default, as specified. As an optional fallback (e.g. during an OpenAI outage)
the same extraction can run on Anthropic's Claude: same prompt, same strict JSON schema, same
CSV. Put `ANTHROPIC_API_KEY=...` in `.env` and run:

```bash
python extract_invoices.py path/to/invoices --provider anthropic
```

Default model is `claude-opus-5-5` (change with `--model` or `ANTHROPIC_MODEL`). Cost is in
the same range as GPT-4o: roughly 1 - 2 cents per invoice. If Anthropic's safety filter
declines a request, the API automatically retries it on a fallback model.

## Project layout

```
extract_invoices.py          command-line entry point
invoice_extractor/
    pdf_loader.py            PDF text extraction, scanned pages -> images
    llm.py                   prompt + strict JSON schema + OpenAI call
    llm_anthropic.py         optional Claude backend (same prompt and schema)
    normalize.py             date/amount/currency cleaning, review checks
    pipeline.py              folder processing, error isolation, CSV writer
scripts/
    make_sample_invoices.py  generates the 10 synthetic test invoices
    compare_results.py       accuracy check: output CSV vs. expected CSV
sample_invoices/             10 synthetic test invoices
test_run/                    expected results + test-run output
tests/                       automated tests (no API key needed)
```

## Testing

Automated tests use a fake OpenAI client, so they run without a key and cost nothing:

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Accuracy check against a hand-verified CSV (same columns as the output):

```bash
python scripts/compare_results.py test_run/expected_results.csv output/invoices_....csv
```

## Notes and limits

- **Numeric dates** such as `05/10/2026` are ambiguous. The model decides day-first vs.
  month-first from the supplier's country/currency (UK/EU day-first, US month-first). If one
  of your suppliers breaks this rule, tell me and I'll add a per-supplier override.
- **Amount due vs. total:** `total` is the invoice's own total including tax, not a running
  account balance ("previous balance + this invoice").
- **Multiple tax lines** (e.g. GST + PST) are added together in `tax`.
- Invoices are sent to the OpenAI API for processing. Under OpenAI's API terms, API data is not
  used for training by default.
- Out of scope (as agreed): accounting software integration, web interface, email inbox automation.
