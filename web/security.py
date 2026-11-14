"""Security hardening for the validation site.

- Strict security headers (CSP, HSTS, clickjacking, MIME sniffing, referrer, permissions).
- Request size cap before the body is read (stops huge uploads from filling memory/disk).
- Per-IP rate limits for every write endpoint (stops spam bots and brute force).
- Admin login lockout after repeated wrong tokens.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

# Only our own files plus Google Fonts. No inline scripts or styles are allowed,
# so injected HTML cannot run code even if it ever reached the page.
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' https://fonts.googleapis.com",
    "font-src https://fonts.gstatic.com",
    "img-src 'self' data:",
    "connect-src 'self'",
    "frame-src 'self'",          # sample PDFs open in the browser's own viewer
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",    # nobody can embed the site (clickjacking)
    "upgrade-insecure-requests",
])

SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.url.path.startswith(("/api/", "/admin")):
            response.headers["Cache-Control"] = "no-store"
        if request.url.path.startswith("/admin"):
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response


class MaxBodySizeMiddleware:
    """Reject request bodies above ``max_bytes`` - checked while streaming, so a
    client lying about Content-Length (or omitting it) is still cut off."""

    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > self.max_bytes):
            await JSONResponse({"detail": "Upload too large."}, status_code=413)(scope, receive, send)
            return

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise RequestTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except RequestTooLarge:
            await JSONResponse({"detail": "Upload too large."}, status_code=413)(scope, receive, send)


class RequestTooLarge(Exception):
    pass


class RateLimiter:
    """In-memory sliding window: at most ``limit`` hits per ``window`` seconds per key."""

    def __init__(self, limit: int, window: float, max_keys: int = 50_000):
        self.limit, self.window, self.max_keys = limit, window, max_keys
        self.hits: dict[str, deque[float]] = defaultdict(deque)
        self.lock = threading.Lock()

    def _prune(self, now: float) -> None:
        """Memory guard against IP-rotating floods. Drops only expired entries, so a
        flood cannot wipe the counters of IPs that are currently limited (e.g. the
        admin lockout). If memory is still over the cap, the oldest keys go first."""
        for key in [k for k, b in self.hits.items() if not b or now - b[-1] > self.window]:
            del self.hits[key]
        overflow = len(self.hits) - self.max_keys
        if overflow > 0:
            oldest = sorted(self.hits, key=lambda k: self.hits[k][-1])[:overflow]
            for key in oldest:
                del self.hits[key]

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            if len(self.hits) > self.max_keys:
                self._prune(now)
            bucket = self.hits[key]
            while bucket and now - bucket[0] > self.window:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False
            bucket.append(now)
            return True

    def count(self, key: str) -> int:
        now = time.monotonic()
        with self.lock:
            bucket = self.hits.get(key, deque())
            return sum(1 for t in bucket if now - t <= self.window)

    def reset(self, key: str) -> None:
        with self.lock:
            self.hits.pop(key, None)
