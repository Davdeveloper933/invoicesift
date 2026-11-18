"""Validation site (MVP): landing page, live demo, feedback, pricing "fake door" and waitlist.

Run locally from the project folder:
    uvicorn web.app:app --reload
Then open http://localhost:8000 (admin stats: http://localhost:8000/admin).
"""

from __future__ import annotations

import csv
import hashlib
import logging
import os
import re
import secrets
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

PROJECT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_DIR / ".env")

from extract_invoices import make_extractor  # noqa: E402  (needs .env loaded first)
from invoice_extractor.normalize import FIELDS  # noqa: E402
from invoice_extractor.pipeline import InvoiceResult, flag_duplicates, process_file  # noqa: E402

from .db import OWNER_INVOICE_EVENT, OWNER_IP_PREFIX, Store, make_engine  # noqa: E402
from .security import MaxBodySizeMiddleware, RateLimiter, SecurityHeadersMiddleware  # noqa: E402

log = logging.getLogger("web")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
for noisy in ("httpx", "httpx2", "httpcore", "openai", "anthropic"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

STATIC_DIR = Path(__file__).resolve().parent / "static"
SAMPLES_DIR = PROJECT_DIR / "sample_invoices"
SAMPLE_RESULTS = PROJECT_DIR / "test_run" / "output_sample_invoices_claude_manual.csv"

# Limits that protect your API credits. Override in .env / the host's environment.
FREE_INVOICES = int(os.getenv("DEMO_FREE_INVOICES", "8"))  # per visitor, in total
MAX_MB = int(os.getenv("DEMO_MAX_MB", "10"))  # per file
MAX_REQUEST_MB = int(os.getenv("DEMO_MAX_REQUEST_MB", "30"))  # all files in one upload together
GLOBAL_DAILY = int(os.getenv("DEMO_GLOBAL_DAILY", "300"))
MAX_PAGES = int(os.getenv("DEMO_MAX_PAGES", "3"))
# The site owner (any browser that has logged into /admin) has no free-invoice limit,
# only this cap on files per upload.
OWNER_MAX_FILES = int(os.getenv("DEMO_OWNER_MAX_FILES", "20"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
# Salt for hashing IPs (we never store raw IP addresses).
IP_SALT = os.getenv("IP_SALT") or ADMIN_TOKEN or "local-dev-salt"
# Public address of the site: used in robots.txt, sitemap.xml and the link-preview tags.
SITE_URL = os.getenv("SITE_URL", "https://invoicesift.onrender.com").rstrip("/")
# Optional: the code from Google Search Console's "HTML tag" verification method.
GOOGLE_SITE_VERIFICATION = os.getenv("GOOGLE_SITE_VERIFICATION", "").strip()
# Header set by the CDN in front of the site with the visitor's real IP (see client_ip).
TRUSTED_IP_HEADER = os.getenv("TRUSTED_IP_HEADER", "cf-connecting-ip").strip().lower()

ALLOWED_EVENTS = {"visit", "demo_sample", "csv_download", "pricing_view", "plan_click", "signup_open"}
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
MIN_ADMIN_TOKEN_LEN = 16

# Docs/OpenAPI are switched off so the API surface is not advertised.
app = FastAPI(title="Invoice extractor - validation site", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(MaxBodySizeMiddleware, max_bytes=MAX_REQUEST_MB * 1024 * 1024 + 1024 * 1024)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
store = Store(make_engine())

# Per-IP rate limits (hits per window in seconds).
limits = {
    "event": RateLimiter(120, 60),
    "extract": RateLimiter(10, 3600),
    "feedback": RateLimiter(10, 3600),
    "signup": RateLimiter(10, 3600),
    "admin_fail": RateLimiter(5, 900),   # 5 wrong admin tokens -> locked for 15 minutes
}
# At most this many demo extractions run at once; protects memory on a small free server.
extraction_slots = threading.BoundedSemaphore(int(os.getenv("DEMO_MAX_CONCURRENT", "4")))
in_flight: set[str] = set()
in_flight_lock = threading.Lock()


def rate_limit(name: str, request: Request) -> None:
    if not limits[name].allow(client_ip_hash(request)):
        raise HTTPException(429, "Too many requests. Please wait a while and try again.")

_extractor: Any = None  # built on first use; tests replace it


def get_extractor():
    """The model call used by the demo, or None when no API key is configured."""
    global _extractor
    if _extractor is None:
        # Short timeout and few retries: a stuck call must not hold one of the few
        # extraction slots (and the visitor's page) for many minutes.
        built = make_extractor(os.getenv("LLM_PROVIDER", "openai"), None,
                               timeout=float(os.getenv("DEMO_API_TIMEOUT", "60")),
                               max_retries=int(os.getenv("DEMO_API_RETRIES", "2")))
        _extractor = built[0] if built else False
    return _extractor or None


def client_ip(request: Request) -> str:
    """The visitor's real IP, using only values a visitor cannot forge.

    - ``TRUSTED_IP_HEADER`` (default CF-Connecting-IP): Cloudflare, which sits in front of
      Render, overwrites this header with the real address. Set it to empty on a host
      without Cloudflare, otherwise visitors could put any value there themselves.
    - Otherwise the LAST X-Forwarded-For entry: it is appended by our own proxy. A visitor
      can only add fake entries in front of it, never replace it.
    """
    if TRUSTED_IP_HEADER:
        trusted = request.headers.get(TRUSTED_IP_HEADER, "").strip()
        if trusted:
            return trusted
    forwarded = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",") if part.strip()]
    if forwarded:
        return forwarded[-1]
    return request.client.host if request.client else ""


def client_ip_hash(request: Request) -> str:
    return hashlib.sha256(f"{IP_SALT}:{client_ip(request)}".encode()).hexdigest()[:32]


def clean(value: str, limit: int) -> str:
    return (value or "").strip()[:limit]


# --- pages ---------------------------------------------------------------------

ASSET_LINK = re.compile(r'((?:src|href)=")(/static/([^"?#]+))"')
_page_cache: dict[str, tuple[tuple[float, ...], str]] = {}


def asset_version(rel: str) -> str:
    path = (STATIC_DIR / rel).resolve()
    if STATIC_DIR.resolve() not in path.parents or not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:10]


def page(name: str) -> HTMLResponse:
    """An HTML page whose /static/ links carry the file's content hash (?v=...).

    After an update the browser sees new URLs and fetches the new JS/CSS at once,
    instead of pairing a new page with an old cached script (dead buttons)."""
    html_path = STATIC_DIR / name
    stamp = tuple(f.stat().st_mtime for f in sorted(STATIC_DIR.rglob("*")) if f.is_file())
    cached = _page_cache.get(name)
    if cached is None or cached[0] != stamp:
        def versioned(m: re.Match) -> str:
            version = asset_version(m.group(3))
            return f'{m.group(1)}{m.group(2)}?v={version}"' if version else m.group(0)
        html = ASSET_LINK.sub(versioned, html_path.read_text(encoding="utf-8"))
        html = html.replace("%SITE_URL%", SITE_URL)
        if GOOGLE_SITE_VERIFICATION:
            tag = f'<meta name="google-site-verification" content="{escape(GOOGLE_SITE_VERIFICATION)}">'
            html = html.replace("</head>", f"  {tag}\n</head>", 1)
        cached = (stamp, html)
        _page_cache[name] = cached
    return HTMLResponse(cached[1])


@app.get("/", include_in_schema=False)
def index() -> HTMLResponse:
    return page("index.html")


@app.get("/admin", include_in_schema=False)
def admin_page() -> HTMLResponse:
    return page("admin.html")


@app.get("/samples/{name}", include_in_schema=False)
def sample_pdf(name: str) -> FileResponse:
    path = SAMPLES_DIR / name
    if path.parent != SAMPLES_DIR or path.suffix.lower() != ".pdf" or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="application/pdf")


@app.get("/robots.txt", include_in_schema=False)
def robots() -> PlainTextResponse:
    return PlainTextResponse(
        "User-agent: *\nAllow: /\nDisallow: /admin\nDisallow: /api/\n\n"
        f"Sitemap: {SITE_URL}/sitemap.xml\n")


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap() -> Response:
    updated = datetime.fromtimestamp((STATIC_DIR / "index.html").stat().st_mtime).strftime("%Y-%m-%d")
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
           f"  <url><loc>{SITE_URL}/</loc><lastmod>{updated}</lastmod></url>\n"
           "</urlset>\n")
    return Response(xml, media_type="application/xml")


@app.get("/healthz", include_in_schema=False)
def health() -> PlainTextResponse:
    return PlainTextResponse("ok")


# --- demo ------------------------------------------------------------------------

def is_owner(visitor_id: str) -> bool:
    # visitor_id is a random id kept in the browser; it is marked as the owner's only
    # after a successful login to /admin from that browser (see admin_stats).
    if not visitor_id or visitor_id == "anonymous" or visitor_id.startswith(OWNER_IP_PREFIX):
        return False
    return store.is_owner(visitor_id)


def free_left(ip_hash: str, visitor_id: str) -> int:
    if is_owner(visitor_id):
        return OWNER_MAX_FILES
    return max(0, FREE_INVOICES - store.invoices_used(ip_hash, visitor_id))


@app.get("/api/config")
def config(request: Request, visitor_id: str = "") -> dict[str, Any]:
    visitor_id = clean(visitor_id, 64)
    return {
        "owner": is_owner(visitor_id),
        "upload_enabled": get_extractor() is not None,
        "free_invoices": FREE_INVOICES,
        "free_left": free_left(client_ip_hash(request), visitor_id),
        "busy_today": store.invoices_today() >= GLOBAL_DAILY,
        "max_mb": MAX_MB,
        "max_request_mb": MAX_REQUEST_MB,
    }


@app.get("/api/sample-results")
def sample_results() -> dict[str, Any]:
    with SAMPLE_RESULTS.open(newline="", encoding="utf-8-sig") as fh:
        rows = [public_row(row) for row in csv.DictReader(fh)]
    return {"rows": rows}


def public_row(row: dict[str, str]) -> dict[str, str]:
    keep = ["file_name", *FIELDS, "needs_review", "review_reasons"]
    return {key: row.get(key, "") for key in keep}


def public_result(result: InvoiceResult) -> dict[str, str]:
    row = result.to_row()
    # Raw API errors can contain account details; show a plain message instead.
    reasons = []
    for reason in result.reasons:
        if reason.startswith("ERROR: PdfReadError"):
            reasons.append("Could not read this PDF (damaged or password-protected)")
        elif reason.startswith("ERROR"):
            reasons.append("Could not process this invoice right now - please try again later")
        else:
            reasons.append(reason)
    row["review_reasons"] = "; ".join(reasons)
    return public_row(row)


@app.post("/api/extract")
def extract(
    request: Request,
    files: list[UploadFile] = File(...),
    visitor_id: str = Form(""),
    source: str = Form(""),
) -> dict[str, Any]:
    extractor = get_extractor()
    if extractor is None:
        raise HTTPException(503, "Live upload is switched off right now. Try the sample invoices instead.")
    if not files:
        raise HTTPException(400, "Please choose at least one PDF.")
    ip_hash = client_ip_hash(request)
    visitor_id, source = clean(visitor_id, 64) or "anonymous", clean(source, 100)
    owner = is_owner(visitor_id)
    if owner:
        if len(files) > OWNER_MAX_FILES:
            raise HTTPException(400, f"Up to {OWNER_MAX_FILES} invoices per upload.")
    else:
        check_visitor_limits(request, files, ip_hash, visitor_id)

    # One upload at a time per visitor: parallel requests can't sneak past the free limit.
    with in_flight_lock:
        if ip_hash in in_flight or visitor_id in in_flight:
            raise HTTPException(429, "Your previous upload is still being processed.")
        in_flight.update({ip_hash, visitor_id})
    try:
        if not extraction_slots.acquire(blocking=False):
            raise HTTPException(503, "The demo is busy right now. Please try again in a minute.")
        try:
            results = _extract_files(files, extractor)
        finally:
            extraction_slots.release()
        record_usage(results, visitor_id, source, ip_hash, owner)
    finally:
        with in_flight_lock:
            in_flight.difference_update({ip_hash, visitor_id})

    flag_duplicates(results)
    left = free_left(ip_hash, visitor_id)
    if left == 0 and not owner:
        store.add_event(visitor_id, "free_limit_reached", "", source, ip_hash)
    return {"rows": [public_result(r) for r in results], "free_left": left}


def check_visitor_limits(request: Request, files: list[UploadFile], ip_hash: str, visitor_id: str) -> None:
    """Free-demo limits for ordinary visitors (the owner skips these)."""
    if len(files) > FREE_INVOICES:
        raise HTTPException(400, f"Up to {FREE_INVOICES} invoices in the free demo.")
    rate_limit("extract", request)
    left = free_left(ip_hash, visitor_id)
    if left <= 0:
        # The page answers this by opening the pre-order + feedback window.
        raise HTTPException(429, {"code": "free_limit", "message":
                            f"You've used your {FREE_INVOICES} free invoices."})
    if store.invoices_today() >= GLOBAL_DAILY:
        raise HTTPException(429, {"code": "busy", "message":
                            "The free demo is at capacity for today. Please come back tomorrow."})
    if len(files) > left:
        raise HTTPException(400, f"You have {left} free invoice(s) left - please choose {left} file(s) or fewer.")


def record_usage(results: list[InvoiceResult], visitor_id: str, source: str, ip_hash: str,
                 owner: bool = False) -> None:
    store.add_event(visitor_id, "demo_upload", f"{len(results)} file(s)", source, ip_hash)
    event = OWNER_INVOICE_EVENT if owner else "invoice_processed"
    for result in results:
        if not any(r.startswith("ERROR") for r in result.reasons):
            store.add_event(visitor_id, event, result.method, source, ip_hash)


def _extract_files(files: list[UploadFile], extractor) -> list[InvoiceResult]:
    """Save uploads to a private temp folder, extract them, and delete them again."""
    limit = MAX_MB * 1024 * 1024
    with tempfile.TemporaryDirectory(prefix="demo_") as tmp:
        paths = []
        for index, upload in enumerate(files):
            data = upload.file.read(limit + 1)
            name = Path(upload.filename or f"invoice_{index + 1}.pdf").name[:120]
            if len(data) > limit:
                raise HTTPException(413, f"{name} is larger than {MAX_MB} MB.")
            if not data.startswith(b"%PDF"):
                raise HTTPException(400, f"{name} is not a PDF file.")
            path = Path(tmp) / f"{index}_{re.sub(r'[^A-Za-z0-9._-]', '_', name)[:60]}.pdf"
            path.write_bytes(data)
            paths.append((name, path))

        with ThreadPoolExecutor(max_workers=min(len(paths), 4)) as pool:
            results = list(pool.map(lambda item: process_file(item[1], extractor, MAX_PAGES), paths))
        for (name, _), result in zip(paths, results):
            result.file_name = name
    # Uploaded files are deleted here, when the temporary folder is closed.
    return results


# --- tracking, feedback, waitlist --------------------------------------------------
# Every field has a hard length cap, so oversized payloads are rejected (422) before
# reaching the database. ``website`` is a honeypot: hidden from people, filled by bots.

class EventIn(BaseModel):
    visitor_id: str = Field(max_length=64)
    event: str = Field(max_length=40)
    detail: str = Field("", max_length=200)
    source: str = Field("", max_length=100)


class FeedbackIn(BaseModel):
    visitor_id: str = Field(max_length=64)
    rating: str = Field("", max_length=20)
    monthly_volume: str = Field("", max_length=40)
    comment: str = Field("", max_length=4000)
    source: str = Field("", max_length=100)
    website: str = Field("", max_length=200)


class SignupIn(BaseModel):
    visitor_id: str = Field(max_length=64)
    email: str = Field(max_length=254)
    plan: str = Field("", max_length=40)
    monthly_volume: str = Field("", max_length=40)
    integrations: list[str] = Field(default_factory=list, max_length=10)
    comment: str = Field("", max_length=4000)
    source: str = Field("", max_length=100)
    website: str = Field("", max_length=200)


# Link-preview bots, security scanners that "click" links, uptime monitors and scripts.
# Some of them run JavaScript, so without this filter they show up as visitors.
BOT_UA = re.compile(
    r"bot|crawl|spider|slurp|preview|headless|lighthouse|pagespeed|python|curl|wget|httpclient"
    r"|java/|go-http|okhttp|axios|node-fetch|scrapy|facebookexternalhit|embedly|whatsapp"
    r"|skypeuripreview|monitor|uptime|pingdom|phantomjs|selenium|puppeteer|playwright|scanner",
    re.IGNORECASE)


def is_bot(user_agent: str) -> bool:
    return not user_agent.strip() or bool(BOT_UA.search(user_agent))


def device_of(ua: str) -> str:
    """A short, human-readable device label, e.g. "LinkedIn app · iPhone"."""
    apps = [("LinkedInApp", "LinkedIn app"), ("FBAN", "Facebook app"), ("FBAV", "Facebook app"),
            ("Instagram", "Instagram app"), ("Telegram", "Telegram")]
    browsers = [("Edg/", "Edge"), ("OPR/", "Opera"), ("YaBrowser", "Yandex"), ("SamsungBrowser", "Samsung"),
                ("Firefox/", "Firefox"), ("CriOS", "Chrome"), ("Chrome/", "Chrome"), ("Safari/", "Safari")]
    systems = [("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Windows", "Windows"),
               ("Macintosh", "Mac"), ("CrOS", "ChromeOS"), ("Linux", "Linux")]
    pick = lambda pairs: next((name for key, name in pairs if key in ua), "")  # noqa: E731
    browser = pick(apps) or pick(browsers) or "Other"
    system = pick(systems)
    return f"{browser} · {system}" if system else browser


@app.post("/api/event")
def track(body: EventIn, request: Request) -> dict[str, bool]:
    rate_limit("event", request)
    if body.event not in ALLOWED_EVENTS:
        raise HTTPException(400, "unknown event")
    user_agent = request.headers.get("user-agent", "")
    if is_bot(user_agent):
        return {"ok": True}  # pretend success, store nothing
    detail = clean(body.detail, 200)
    if body.event == "visit":
        # "page | device" - shown in the admin's list of visits.
        detail = f"{detail[:120]} | {device_of(user_agent)}"
    store.add_event(clean(body.visitor_id, 64), body.event, detail,
                    clean(body.source, 100), client_ip_hash(request))
    return {"ok": True}


@app.post("/api/feedback")
def add_feedback(body: FeedbackIn, request: Request) -> dict[str, bool]:
    rate_limit("feedback", request)
    if body.website:  # bot filled the hidden field: pretend success, store nothing
        return {"ok": True}
    if not (body.rating or body.comment.strip()):
        raise HTTPException(400, "Please pick a rating or write a comment.")
    store.add_feedback(
        visitor_id=clean(body.visitor_id, 64), rating=clean(body.rating, 20),
        monthly_volume=clean(body.monthly_volume, 40), comment=clean(body.comment, 4000),
        source=clean(body.source, 100),
    )
    return {"ok": True}


@app.post("/api/signup")
def add_signup(body: SignupIn, request: Request) -> dict[str, bool]:
    rate_limit("signup", request)
    if body.website:
        return {"ok": True}
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "Please enter a valid email address.")
    store.add_signup(
        visitor_id=clean(body.visitor_id, 64), email=email, plan=clean(body.plan, 40),
        monthly_volume=clean(body.monthly_volume, 40),
        integrations=", ".join(clean(i, 40) for i in body.integrations[:10]),
        comment=clean(body.comment, 4000), source=clean(body.source, 100),
    )
    return {"ok": True}


# --- admin -----------------------------------------------------------------------

def check_admin(token: str | None, request: Request) -> None:
    if len(ADMIN_TOKEN) < MIN_ADMIN_TOKEN_LEN:
        raise HTTPException(403, f"Admin page is off: set ADMIN_TOKEN (at least {MIN_ADMIN_TOKEN_LEN} characters).")
    key = client_ip_hash(request)
    if limits["admin_fail"].count(key) >= limits["admin_fail"].limit:
        raise HTTPException(429, "Too many wrong tokens. Try again in 15 minutes.")
    if not token or not secrets.compare_digest(token.encode(), ADMIN_TOKEN.encode()):
        limits["admin_fail"].allow(key)
        log.warning("Wrong admin token from ip_hash=%s", key[:8])
        raise HTTPException(401, "Wrong admin token.")


@app.get("/api/admin/stats")
def admin_stats(request: Request, x_admin_token: str | None = Header(None),
                x_visitor_id: str | None = Header(None), start: str = "", end: str = "",
                tz: int = 0, mine: int = 1) -> dict[str, Any]:
    """``start``/``end``: ISO times with offset, e.g. 2026-11-17T00:00:00+03:00 (empty = all time).
    ``tz``: the admin's UTC offset in minutes, for "today" and the per-day chart.
    ``mine``: 1 counts the owner's own activity too, 0 hides it."""
    check_admin(x_admin_token, request)
    # The browser that opened the admin page is the owner's (no free limit; its visits
    # are tagged "you" and can be hidden)...
    store.add_owner(clean(x_visitor_id or "", 64))
    # ...and its network too: visits from the same IP in other browsers (the LinkedIn
    # app, a phone on the same Wi-Fi, a private window) are yours as well.
    store.add_owner(OWNER_IP_PREFIX + client_ip_hash(request))
    if not -14 * 60 <= tz <= 14 * 60:
        raise HTTPException(400, "Bad time zone.")
    start_at, end_at = parse_moment(start), parse_moment(end)
    if start_at and end_at and end_at <= start_at:
        raise HTTPException(400, "The period ends before it starts.")
    return store.stats(start_at, end_at, tz, include_owner=bool(mine))


def parse_moment(value: str) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value[:40])
    except ValueError:
        raise HTTPException(400, "Bad date.") from None
    if moment.tzinfo is None:
        raise HTTPException(400, "Date needs a time zone offset.")
    return moment


def csv_cell(value: Any) -> str:
    text = str(value)
    # Stop "formula injection": a cell starting with = + - @ would run as a formula in Excel.
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


@app.get("/api/admin/export/{table}.csv")
def admin_export(table: str, request: Request, x_admin_token: str | None = Header(None)) -> PlainTextResponse:
    check_admin(x_admin_token, request)
    if table not in ("signups", "feedback"):
        raise HTTPException(404)
    rows = store.stats()[table]
    if not rows:
        return PlainTextResponse("", media_type="text/csv")
    lines = [",".join(rows[0])]
    lines += [",".join(csv_cell(v) for v in row.values()) for row in rows]
    return PlainTextResponse("﻿" + "\n".join(lines), media_type="text/csv; charset=utf-8")
