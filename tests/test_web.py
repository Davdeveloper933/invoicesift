"""Validation website: demo upload, free-invoice limit, feedback, waitlist, admin."""

import os

os.environ["ADMIN_TOKEN"] = "test-admin-token-123456"
os.environ.setdefault("OPENAI_API_KEY", "")

import pymupdf
import pytest
from fastapi.testclient import TestClient

from web import app as web_app
from web.db import Store, make_engine
from web.security import RateLimiter

REPLY = {
    "supplier_name": "Acme Ltd", "invoice_number": "INV-1", "invoice_date": "2026-03-01",
    "due_date": "2026-03-31", "subtotal": 100, "tax": 20, "total": 120, "currency": "GBP",
}


def pdf_bytes(text="INVOICE INV-1 from Acme Ltd, total 120.00 GBP, date 2026-03-01"):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text)
    return doc.tobytes()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "store", Store(make_engine(f"sqlite:///{tmp_path / 't.db'}")))
    monkeypatch.setattr(web_app, "_extractor", lambda content: dict(REPLY))
    monkeypatch.setattr(web_app, "ADMIN_TOKEN", "test-admin-token-123456")
    fresh = {name: RateLimiter(lim.limit, lim.window) for name, lim in web_app.limits.items()}
    monkeypatch.setattr(web_app, "limits", fresh)
    test_client = TestClient(web_app.app, headers={"CF-Connecting-IP": VISITOR_IP})
    test_client.event_hooks = {"request": [admin_ip], "response": []}
    return test_client


# Visitors and the admin come from different networks, like in real life: the admin's
# IP is marked as the owner's, so visits from it are left out of the stats.
VISITOR_IP, ADMIN_IP = "198.51.100.1", "203.0.113.9"


def admin_ip(request):
    if "x-admin-token" in request.headers:
        request.headers["CF-Connecting-IP"] = ADMIN_IP


def upload(client, n, visitor="v1"):
    files = [("files", (f"inv{i}.pdf", pdf_bytes(), "application/pdf")) for i in range(n)]
    return client.post("/api/extract", files=files, data={"visitor_id": visitor, "source": "test"})


def test_pages_load(client):
    assert "InvoiceSift" in client.get("/").text
    assert client.get("/admin").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_sample_results_and_pdfs(client):
    rows = client.get("/api/sample-results").json()["rows"]
    assert len(rows) == 10 and "extraction_method" not in rows[0]
    assert client.get("/samples/" + rows[0]["file_name"]).headers["content-type"] == "application/pdf"
    assert client.get("/samples/..%2F.env").status_code == 404


FREE = web_app.FREE_INVOICES


def test_free_limit_is_eight_invoices_by_default():
    assert web_app.FREE_INVOICES == 8 or os.getenv("DEMO_FREE_INVOICES")


def test_free_limit_then_blocked(client):
    assert client.get("/api/config?visitor_id=v1").json()["free_left"] == FREE
    res = upload(client, FREE - 1)
    assert res.status_code == 200
    body = res.json()
    assert body["free_left"] == 1
    assert body["rows"][0]["total"] == "120.00"
    assert "possible duplicate" in body["rows"][0]["review_reasons"]  # same invoice uploaded twice

    assert upload(client, 2).status_code == 400          # only 1 left
    assert upload(client, 1).json()["free_left"] == 0
    res = upload(client, 1)
    assert res.status_code == 429 and res.json()["detail"]["code"] == "free_limit"
    # A new browser id from the same IP does not reset the trial.
    assert upload(client, 1, visitor="v2").status_code == 429


def test_failed_invoices_do_not_use_up_the_free_limit(client, monkeypatch):
    def boom(content):
        raise RuntimeError("secret account detail sk-123")
    monkeypatch.setattr(web_app, "_extractor", boom)
    body = upload(client, 1).json()
    assert body["free_left"] == FREE
    reason = body["rows"][0]["review_reasons"]
    assert "sk-123" not in reason and "try again later" in reason


def test_rejects_non_pdf(client):
    res = client.post("/api/extract", files=[("files", ("a.pdf", b"hello", "application/pdf"))],
                      data={"visitor_id": "v1"})
    assert res.status_code == 400


def test_upload_disabled_without_api_key(client, monkeypatch):
    monkeypatch.setattr(web_app, "_extractor", False)
    assert client.get("/api/config").json()["upload_enabled"] is False
    assert upload(client, 1).status_code == 503


def test_feedback_signup_and_admin_stats(client):
    client.post("/api/event", json={"visitor_id": "v1", "event": "visit", "source": "reddit"})
    client.post("/api/event", json={"visitor_id": "v1", "event": "plan_click", "detail": "Pro $29"})
    assert client.post("/api/event", json={"visitor_id": "v1", "event": "hack"}).status_code == 400
    upload(client, FREE)
    assert client.post("/api/feedback", json={"visitor_id": "v1", "rating": "mostly",
                                              "comment": "Need line items"}).status_code == 200
    assert client.post("/api/signup", json={"visitor_id": "v1", "email": "bad"}).status_code == 400
    assert client.post("/api/signup", json={"visitor_id": "v1", "email": "Me@Example.com", "plan": "Pro $29",
                                            "integrations": ["Xero"], "source": "reddit"}).status_code == 200

    assert client.get("/api/admin/stats").status_code == 401
    stats = client.get("/api/admin/stats", headers={"X-Admin-Token": "test-admin-token-123456"}).json()
    funnel = {f["step"]: f["visitors"] for f in stats["funnel"]}
    assert funnel["Visited the site"] == 1
    assert funnel["Used all free invoices"] == 1
    assert funnel["Joined the waitlist"] == 1
    assert stats["signups"][0]["email"] == "me@example.com"
    assert stats["feedback"][0]["comment"] == "Need line items"
    assert {"plan": "Pro $29", "clicks": 1, "signups": 1} in stats["plans"]
    assert stats["sources"][0] == {"source": "reddit", "visitors": 1, "signups": 1}
    export = client.get("/api/admin/export/signups.csv", headers={"X-Admin-Token": "test-admin-token-123456"})
    assert "me@example.com" in export.text


def test_owner_activity_counted_and_hideable(client):
    token = {"X-Admin-Token": "test-admin-token-123456"}
    for vid in ("owner", "guest"):
        client.post("/api/event", json={"visitor_id": vid, "event": "visit"})
        client.post("/api/feedback", json={"visitor_id": vid, "rating": "great", "comment": vid})
    upload(client, 1, visitor="owner")
    # Opening the admin page marks this browser as the owner's.
    client.get("/api/admin/stats", headers={**token, "X-Visitor-Id": "owner"})
    upload(client, 2, visitor="owner")  # admin mode: logged as owner invoices

    # By default everything counts, the owner's own visits and invoices too.
    stats = client.get("/api/admin/stats", headers=token).json()
    assert stats["funnel"][0]["visitors"] == 2
    assert stats["invoices_processed"]["total"] == 3
    assert sorted(v["visitor"] for v in stats["visits"]) == ["guest", "you"]

    # "Hide my activity" leaves the owner's browser out, including its past visits.
    stats = client.get("/api/admin/stats", headers=token, params={"mine": 0}).json()
    assert stats["funnel"][0]["visitors"] == 1
    assert [f["comment"] for f in stats["feedback"]] == ["guest"]
    assert stats["invoices_processed"]["total"] == 0
    # A wrong token marks nobody as owner.
    client.get("/api/admin/stats", headers={"X-Admin-Token": "wrong", "X-Visitor-Id": "guest"})
    stats = client.get("/api/admin/stats", headers=token, params={"mine": 0}).json()
    assert stats["funnel"][0]["visitors"] == 1


# --- security -------------------------------------------------------------------

ADMIN = {"X-Admin-Token": "test-admin-token-123456"}


def test_security_headers(client):
    headers = client.get("/").headers
    csp = headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    assert "max-age" in headers["strict-transport-security"]
    assert client.get("/api/config").headers["cache-control"] == "no-store"


def test_no_inline_scripts_or_styles_so_csp_holds():
    static = web_app.STATIC_DIR
    for page in ("index.html", "admin.html"):
        html = (static / page).read_text(encoding="utf-8")
        assert "<script>" not in html and "style=" not in html and "<style" not in html
        assert "onclick=" not in html.lower()


def test_api_docs_are_hidden(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_admin_locks_out_after_wrong_tokens(client):
    for _ in range(5):
        assert client.get("/api/admin/stats", headers={"X-Admin-Token": "guess"}).status_code == 401
    assert client.get("/api/admin/stats", headers={"X-Admin-Token": "guess"}).status_code == 429
    # Even the right token is refused while locked, so brute force gains nothing.
    assert client.get("/api/admin/stats", headers=ADMIN).status_code == 429


def test_admin_disabled_with_weak_token(client, monkeypatch):
    monkeypatch.setattr(web_app, "ADMIN_TOKEN", "short")
    assert client.get("/api/admin/stats", headers={"X-Admin-Token": "short"}).status_code == 403


def test_honeypot_stores_nothing(client):
    res = client.post("/api/signup", json={"visitor_id": "bot", "email": "bot@spam.com", "website": "http://spam"})
    assert res.status_code == 200
    assert client.get("/api/admin/stats", headers=ADMIN).json()["signups"] == []


def test_signup_spam_is_rate_limited(client):
    codes = [client.post("/api/signup", json={"visitor_id": "v", "email": f"a{i}@ex.com",
                                              "monthly_volume": "Under 20"}).status_code
             for i in range(12)]
    assert codes[:10] == [200] * 10 and codes[10:] == [429, 429]


def test_oversized_fields_rejected(client):
    res = client.post("/api/feedback", json={"visitor_id": "v", "comment": "x" * 5000})
    assert res.status_code == 422


def test_huge_request_body_rejected_before_reading(client):
    big = b"%PDF" + b"0" * (31 * 1024 * 1024)
    res = client.post("/api/extract", files=[("files", ("a.pdf", big, "application/pdf"))],
                      data={"visitor_id": "v"})
    assert res.status_code == 413


def test_too_many_files_rejected(client):
    assert upload(client, FREE + 1).status_code == 400


def test_csv_export_neutralises_formulas(client):
    client.post("/api/signup", json={"visitor_id": "v", "email": "x@ex.com", "monthly_volume": "Under 20",
                                     "comment": "=HYPERLINK(\"http://evil\")"})
    text = client.get("/api/admin/export/signups.csv", headers=ADMIN).text
    assert "'=HYPERLINK" in text


def test_upload_errors_do_not_leak_details(client):
    res = client.post("/api/extract", files=[("files", ("../../etc/passwd.pdf", pdf_bytes(), "application/pdf"))],
                      data={"visitor_id": "v"})
    assert res.status_code == 200
    assert res.json()["rows"][0]["file_name"] == "passwd.pdf"


def _ip(headers):
    from starlette.requests import Request
    scope = {"type": "http", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
             "client": ("10.0.0.1", 1234)}
    return web_app.client_ip(Request(scope))


def test_client_ip_cannot_be_spoofed_through_forwarded_for(monkeypatch):
    monkeypatch.setattr(web_app, "TRUSTED_IP_HEADER", "")
    # The visitor can only prepend fake entries; the last one comes from our proxy.
    assert _ip({"X-Forwarded-For": "6.6.6.6, 1.2.3.4"}) == "1.2.3.4"
    # Without a trusted header, CF-Connecting-IP / True-Client-IP sent by a visitor are ignored.
    assert _ip({"CF-Connecting-IP": "6.6.6.6", "True-Client-IP": "7.7.7.7"}) == "10.0.0.1"


def test_client_ip_uses_cdn_header_when_trusted(monkeypatch):
    monkeypatch.setattr(web_app, "TRUSTED_IP_HEADER", "cf-connecting-ip")
    assert _ip({"CF-Connecting-IP": "1.2.3.4", "X-Forwarded-For": "5.5.5.5"}) == "1.2.3.4"


def test_flood_of_new_ips_does_not_reset_active_limits():
    # Expired keys are dropped first; the locked "admin" key keeps its count.
    limiter2 = RateLimiter(2, 900, max_keys=10)
    for i in range(10):
        limiter2.allow(f"old{i}")
        limiter2.hits[f"old{i}"][-1] -= 1000  # expired
    assert limiter2.allow("admin") and limiter2.allow("admin")
    limiter2.allow("new")
    assert not limiter2.allow("admin")
    assert not any(k.startswith("old") for k in limiter2.hits)


def test_owner_has_no_free_limit(client):
    owner, token = "owner-browser", {"X-Admin-Token": "test-admin-token-123456"}
    assert client.get("/api/config", params={"visitor_id": owner}).json()["owner"] is False
    client.get("/api/admin/stats", headers={**token, "X-Visitor-Id": owner})

    cfg = client.get("/api/config", params={"visitor_id": owner}).json()
    assert cfg["owner"] is True and cfg["free_left"] == web_app.OWNER_MAX_FILES
    for _ in range(3):  # 15 invoices: well past the visitor limit of 8
        assert upload(client, 5, visitor=owner).status_code == 200
    # The owner's invoices do not use up visitors' limits on the same IP, or the daily cap.
    assert web_app.store.invoices_today() == 0
    assert upload(client, 1, visitor="someone-else").status_code == 200


def test_owner_cannot_be_claimed_without_token(client):
    client.get("/api/admin/stats", headers={"X-Admin-Token": "wrong", "X-Visitor-Id": "attacker"})
    assert client.get("/api/config", params={"visitor_id": "attacker"}).json()["owner"] is False
    upload(client, web_app.FREE_INVOICES, visitor="attacker")
    assert upload(client, 1, visitor="attacker").status_code == 429


def test_admin_stats_for_one_day(client, monkeypatch):
    from datetime import datetime, timezone

    from web import db

    utc = timezone.utc
    moments = iter([
        datetime(2026, 11, 15, 12, 0, tzinfo=utc),   # 15 Nov
        datetime(2026, 11, 16, 20, 30, tzinfo=utc),  # 16 Nov in UTC, but 17 Nov 00:30 in UTC+4
        datetime(2026, 11, 17, 9, 0, tzinfo=utc),    # 17 Nov
    ])
    monkeypatch.setattr(db, "now", lambda: next(moments))
    for visitor in ("a", "b", "c"):
        web_app.store.add_event(visitor, "visit")
    monkeypatch.setattr(db, "now", lambda: datetime(2026, 11, 17, 10, 0, tzinfo=utc))

    token = {"X-Admin-Token": "test-admin-token-123456"}
    visitors = lambda r: r.json()["funnel"][0]["visitors"]  # noqa: E731

    all_time = client.get("/api/admin/stats", headers=token)
    assert visitors(all_time) == 3
    day = {"start": "2026-11-17T00:00:00+04:00", "end": "2026-11-18T00:00:00+04:00", "tz": 240}
    assert visitors(client.get("/api/admin/stats", headers=token, params=day)) == 2
    day_utc = {"start": "2026-11-17T00:00:00+00:00", "end": "2026-11-18T00:00:00+00:00"}
    assert visitors(client.get("/api/admin/stats", headers=token, params=day_utc)) == 1

    chart = {d["day"]: d["visitors"] for d in client.get(
        "/api/admin/stats", headers=token, params={"tz": 240}).json()["daily_visitors"]}
    assert chart["2026-11-17"] == 2 and chart["2026-11-16"] == 0 and chart["2026-11-15"] == 1
    assert len(chart) == 14 and max(chart) == "2026-11-17"



def test_admin_stats_rejects_bad_period(client):
    token = {"X-Admin-Token": "test-admin-token-123456"}
    bad = [{"start": "yesterday"}, {"start": "2026-11-17T00:00:00"}, {"tz": 5000},
           {"start": "2026-11-18T00:00:00+00:00", "end": "2026-11-17T00:00:00+00:00"}]
    for params in bad:
        assert client.get("/api/admin/stats", headers=token, params=params).status_code == 400


def test_pages_link_assets_with_content_version(client):
    import re
    for path, script in (("/", "app.js"), ("/admin", "admin.js")):
        html = client.get(path).text
        match = re.search(rf'src="/static/{script}\?v=([0-9a-f]{{10}})"', html)
        assert match, f"{path} should link a versioned {script}"
        asset = client.get(f"/static/{script}?v={match.group(1)}")
        assert asset.status_code == 200 and "immutable" in asset.headers["cache-control"]
        assert 'href="/static/styles.css?v=' in html
    # Unversioned static files must be re-validated, so an update is never missed.
    assert client.get("/static/admin.js").headers["cache-control"] == "no-cache"


def test_bots_and_scanners_are_not_counted(client):
    for ua in ("LinkedInBot/1.0 (compatible; Mozilla/5.0)", "Mozilla/5.0 HeadlessChrome/120.0", "python-requests/2.31", ""):
        assert client.post("/api/event", json={"visitor_id": "b", "event": "visit"},
                           headers={"User-Agent": ua}).status_code == 200
    stats = client.get("/api/admin/stats", headers=ADMIN).json()
    assert stats["funnel"][0]["visitors"] == 0 and stats["visits"] == []


def test_visit_list_shows_device_and_source(client):
    ua = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Mobile/15E148 [LinkedInApp]/9.29")
    client.post("/api/event", json={"visitor_id": "abcdef123", "event": "visit", "detail": "/", "source": "linkedin"},
                headers={"User-Agent": ua})
    visit = client.get("/api/admin/stats", headers=ADMIN).json()["visits"][0]
    assert visit["device"] == "LinkedIn app · iPhone"
    assert visit["source"] == "linkedin" and visit["visitor"] == "abcdef" and visit["page"] == "/"


def test_owner_network_left_out_of_stats(client):
    # The owner opens the LinkedIn link in another browser on the same Wi-Fi as the admin page.
    client.post("/api/event", json={"visitor_id": "linkedin-app", "event": "visit"},
                headers={"CF-Connecting-IP": ADMIN_IP})
    client.post("/api/event", json={"visitor_id": "stranger", "event": "visit"})
    stats = client.get("/api/admin/stats", headers=ADMIN, params={"mine": 0}).json()
    assert stats["funnel"][0]["visitors"] == 1
    assert [v["visitor"] for v in stats["visits"]] == ["strang"]
    # Shown by default, tagged as yours.
    stats = client.get("/api/admin/stats", headers=ADMIN).json()
    assert sorted(v["visitor"] for v in stats["visits"]) == ["strang", "you"]
    # The owner-network marker cannot be used as a browser id to skip the demo limit.
    owner_row = next(r for r in web_app.store.engine.connect().exec_driver_sql("select visitor_id from owners"))
    assert owner_row[0].startswith("ip:") and not web_app.is_owner(owner_row[0])


def test_robots_and_sitemap(client):
    robots = client.get("/robots.txt")
    assert robots.status_code == 200 and "Disallow: /admin" in robots.text and "Disallow: /api/" in robots.text
    assert f"Sitemap: {web_app.SITE_URL}/sitemap.xml" in robots.text
    sitemap = client.get("/sitemap.xml")
    assert sitemap.headers["content-type"].startswith("application/xml")
    assert f"<loc>{web_app.SITE_URL}/</loc>" in sitemap.text


def test_link_preview_and_structured_data(client):
    import json
    import re
    html = client.get("/").text
    assert "%SITE_URL%" not in html
    image = re.search(r'<meta property="og:image" content="([^"]+)"', html).group(1)
    assert image == f"{web_app.SITE_URL}/static/img/og.jpg"
    assert client.get("/static/img/og.jpg").status_code == 200
    assert f'<link rel="canonical" href="{web_app.SITE_URL}/">' in html
    # The only inline <script> is the JSON-LD data block (not executed, so the CSP still holds).
    blocks = re.findall(r"<script(?![^>]*\bsrc=)([^>]*)>(.*?)</script>", html, re.S)
    assert [attrs.strip() for attrs, _ in blocks] == ['type="application/ld+json"']
    data = json.loads(blocks[0][1])
    assert data["@type"] == "SoftwareApplication" and data["url"] == f"{web_app.SITE_URL}/"


def test_google_verification_tag(client, monkeypatch):
    assert "google-site-verification" not in client.get("/").text
    monkeypatch.setattr(web_app, "GOOGLE_SITE_VERIFICATION", 'abc123"><script>')
    monkeypatch.setattr(web_app, "_page_cache", {})
    html = client.get("/").text
    assert '<meta name="google-site-verification" content="abc123&quot;&gt;&lt;script&gt;">' in html
