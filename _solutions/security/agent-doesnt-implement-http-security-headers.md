---
title: "Agent Doesn't Implement HTTP Security Headers for Web Endpoints"
description: "Agent web endpoints that omit security headers are vulnerable to clickjacking, MIME-type sniffing, cross-site scripting via reflected content, and information leakage through server fingerprinting."
difficulty: beginner
category: security
tags: [http-headers, csp, hsts, security, web, xss, clickjacking, aiohttp, fastapi]
---

## Problem

When an agent exposes HTTP endpoints — for webhooks, admin interfaces, tool result callbacks, or streaming responses — missing security headers leave browsers and clients vulnerable. A single missing `Content-Security-Policy` can enable XSS via injected tool output. Missing `X-Frame-Options` allows clickjacking. Missing `Strict-Transport-Security` downgrades HTTPS to HTTP.

```python
# Broken: no security headers — browser defaults apply
from aiohttp import web

async def handle_result(request: web.Request) -> web.Response:
    data = await request.json()
    return web.json_response({"status": "ok", "echo": data})
    # Response has no CSP, no HSTS, reveals Python/aiohttp server version
```

---

## Solution 1: Core Security Headers Middleware (aiohttp)

```python
from aiohttp import web
from typing import Callable, Awaitable

CORE_SECURITY_HEADERS = {
    # Prevent MIME-type sniffing
    "X-Content-Type-Options": "nosniff",
    # Prevent clickjacking — deny all framing
    "X-Frame-Options": "DENY",
    # Enable browser XSS filter (legacy browsers)
    "X-XSS-Protection": "1; mode=block",
    # Referrer policy: don't leak URL to external sites
    "Referrer-Policy": "strict-origin-when-cross-origin",
    # Remove server fingerprint
    "Server": "agent",
    # Prevent caching of sensitive responses
    "Cache-Control": "no-store, max-age=0",
    # Permissions policy: disable unused browser features
    "Permissions-Policy": (
        "accelerometer=(), camera=(), geolocation=(), "
        "gyroscope=(), magnetometer=(), microphone=(), "
        "payment=(), usb=()"
    ),
}

HSTS_HEADER = "max-age=63072000; includeSubDomains; preload"

@web.middleware
async def security_headers_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    response = await handler(request)
    for header, value in CORE_SECURITY_HEADERS.items():
        response.headers[header] = value
    # Only add HSTS on HTTPS connections
    if request.secure:
        response.headers["Strict-Transport-Security"] = HSTS_HEADER
    return response

# Apply to app
def create_app() -> web.Application:
    app = web.Application(middlewares=[security_headers_middleware])
    app.router.add_post("/webhook", handle_webhook)
    app.router.add_get("/health", handle_health)
    return app

async def handle_webhook(request: web.Request) -> web.Response:
    payload = await request.json()
    return web.json_response({"received": True})

async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})
```

---

## Solution 2: Content Security Policy Builder

```python
from dataclasses import dataclass, field
from typing import Literal

CSPDirective = Literal[
    "default-src", "script-src", "style-src", "img-src",
    "connect-src", "font-src", "object-src", "media-src",
    "frame-src", "child-src", "worker-src", "form-action",
    "frame-ancestors", "base-uri", "upgrade-insecure-requests",
    "block-all-mixed-content", "report-uri", "report-to"
]

@dataclass
class CSPPolicy:
    """
    Fluent Content Security Policy builder.
    """
    _directives: dict[str, list[str]] = field(default_factory=dict)

    def allow(self, directive: str, *sources: str) -> "CSPPolicy":
        if directive not in self._directives:
            self._directives[directive] = []
        self._directives[directive].extend(sources)
        return self

    def none(self, directive: str) -> "CSPPolicy":
        self._directives[directive] = ["'none'"]
        return self

    def self_only(self, directive: str) -> "CSPPolicy":
        self._directives[directive] = ["'self'"]
        return self

    def flag(self, directive: str) -> "CSPPolicy":
        self._directives[directive] = []
        return self

    def build(self) -> str:
        parts = []
        for directive, sources in self._directives.items():
            if sources:
                parts.append(f"{directive} {' '.join(sources)}")
            else:
                parts.append(directive)
        return "; ".join(parts)

# Strict policy for an agent API (no HTML rendering)
AGENT_API_CSP = (
    CSPPolicy()
    .none("default-src")
    .self_only("connect-src")
    .none("script-src")
    .none("style-src")
    .none("img-src")
    .none("frame-ancestors")
    .none("object-src")
    .none("base-uri")
    .flag("upgrade-insecure-requests")
    .build()
)

# More permissive policy for an agent admin UI
AGENT_UI_CSP = (
    CSPPolicy()
    .self_only("default-src")
    .allow("script-src", "'self'", "'strict-dynamic'")
    .allow("style-src", "'self'", "'unsafe-inline'")  # if inline styles needed
    .self_only("img-src")
    .allow("img-src", "data:")                         # allow data URIs for icons
    .none("frame-ancestors")
    .none("object-src")
    .self_only("form-action")
    .self_only("base-uri")
    .flag("upgrade-insecure-requests")
    .build()
)

@web.middleware
async def csp_middleware(request: web.Request, handler) -> web.StreamResponse:
    response = await handler(request)
    # Use strict API policy for /api/* routes, UI policy for others
    if request.path.startswith("/api/"):
        response.headers["Content-Security-Policy"] = AGENT_API_CSP
    else:
        response.headers["Content-Security-Policy"] = AGENT_UI_CSP
    return response
```

---

## Solution 3: FastAPI Security Headers with Starlette Middleware

```python
from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp
import secrets

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp,
                 csp: str | None = None,
                 hsts_max_age: int = 63072000,
                 nonce_in_csp: bool = True):
        super().__init__(app)
        self._csp_template = csp
        self._hsts_max_age = hsts_max_age
        self._nonce_in_csp = nonce_in_csp

    async def dispatch(self, request: Request, call_next) -> Response:
        # Generate per-request nonce for CSP (prevents inline script injection)
        nonce = secrets.token_urlsafe(16) if self._nonce_in_csp else None
        request.state.csp_nonce = nonce

        response = await call_next(request)

        self._apply_headers(response, request, nonce)
        return response

    def _apply_headers(self, response: Response,
                       request: Request, nonce: str | None):
        headers = response.headers

        # Core headers
        headers["X-Content-Type-Options"] = "nosniff"
        headers["X-Frame-Options"] = "DENY"
        headers["X-XSS-Protection"] = "1; mode=block"
        headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        headers["Server"] = "agent"
        headers["Permissions-Policy"] = (
            "accelerometer=(), camera=(), geolocation=(), microphone=()"
        )

        # HSTS (only over HTTPS)
        if request.url.scheme == "https":
            headers["Strict-Transport-Security"] = (
                f"max-age={self._hsts_max_age}; includeSubDomains"
            )

        # CSP with optional nonce
        if self._csp_template:
            csp = self._csp_template
            if nonce:
                csp = csp.replace("{nonce}", nonce)
            headers["Content-Security-Policy"] = csp

        # Remove headers that leak server info
        for h in ("X-Powered-By", "X-AspNet-Version", "X-AspNetMvc-Version"):
            headers.pop(h, None)

# FastAPI app setup
def create_fastapi_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)  # disable docs in prod

    csp = (
        "default-src 'none'; "
        "script-src 'self' 'nonce-{nonce}'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "upgrade-insecure-requests"
    )

    app.add_middleware(
        SecurityHeadersMiddleware,
        csp=csp,
        nonce_in_csp=True
    )
    return app

app = create_fastapi_app()

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/tool-result")
async def tool_result(request: Request, payload: dict):
    nonce = getattr(request.state, "csp_nonce", "")
    return {"accepted": True, "nonce_for_inline_script": nonce}
```

---

## Solution 4: CORS Policy for Agent Cross-Origin APIs

```python
import re
from aiohttp import web
from typing import Callable, Awaitable

# Define allowed origins explicitly — never use "*" for credentialed requests
ALLOWED_ORIGINS = frozenset({
    "https://dashboard.example.com",
    "https://app.example.com",
})

ALLOWED_METHODS = "GET, POST, OPTIONS"
ALLOWED_HEADERS = "Content-Type, Authorization, X-Request-Id"
MAX_AGE = "600"  # seconds to cache preflight

def is_origin_allowed(origin: str) -> bool:
    return origin in ALLOWED_ORIGINS

@web.middleware
async def cors_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    origin = request.headers.get("Origin", "")

    # Handle preflight
    if request.method == "OPTIONS":
        if is_origin_allowed(origin):
            return web.Response(
                status=204,
                headers={
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Methods": ALLOWED_METHODS,
                    "Access-Control-Allow-Headers": ALLOWED_HEADERS,
                    "Access-Control-Allow-Credentials": "true",
                    "Access-Control-Max-Age": MAX_AGE,
                    "Vary": "Origin",
                }
            )
        return web.Response(status=403, text="Origin not allowed")

    response = await handler(request)

    if is_origin_allowed(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Vary"] = "Origin"
    elif origin:
        # Origin present but not allowed — don't add CORS headers
        # Browser will block the response
        pass

    return response
```

---

## Solution 5: Security Header Audit and Testing

```python
import httpx
import asyncio
from dataclasses import dataclass, field
from typing import Any

@dataclass
class HeaderAuditResult:
    header: str
    present: bool
    value: str | None
    severity: str  # "critical", "high", "medium", "low"
    recommendation: str

REQUIRED_HEADERS: list[dict[str, Any]] = [
    {
        "header": "Strict-Transport-Security",
        "severity": "critical",
        "recommendation": "Add: max-age=63072000; includeSubDomains",
        "validate": lambda v: "max-age=" in v and int(v.split("max-age=")[1].split(";")[0]) >= 31536000,
    },
    {
        "header": "Content-Security-Policy",
        "severity": "critical",
        "recommendation": "Define a strict CSP. Minimum: default-src 'none'",
        "validate": lambda v: "default-src" in v,
    },
    {
        "header": "X-Content-Type-Options",
        "severity": "high",
        "recommendation": "Add: nosniff",
        "validate": lambda v: v == "nosniff",
    },
    {
        "header": "X-Frame-Options",
        "severity": "high",
        "recommendation": "Add: DENY",
        "validate": lambda v: v.upper() in ("DENY", "SAMEORIGIN"),
    },
    {
        "header": "Referrer-Policy",
        "severity": "medium",
        "recommendation": "Add: strict-origin-when-cross-origin",
        "validate": lambda v: v in (
            "no-referrer", "strict-origin", "strict-origin-when-cross-origin"
        ),
    },
    {
        "header": "Permissions-Policy",
        "severity": "low",
        "recommendation": "Restrict unused browser features",
        "validate": lambda v: len(v) > 0,
    },
]

LEAKY_HEADERS = ["Server", "X-Powered-By", "X-AspNet-Version",
                  "X-Generator", "X-Drupal-Cache"]

async def audit_security_headers(url: str) -> list[HeaderAuditResult]:
    async with httpx.AsyncClient(verify=False, follow_redirects=True) as client:
        response = await client.get(url)

    results: list[HeaderAuditResult] = []

    for spec in REQUIRED_HEADERS:
        header = spec["header"]
        value = response.headers.get(header)
        present = value is not None
        valid = present and spec["validate"](value)

        results.append(HeaderAuditResult(
            header=header,
            present=present,
            value=value,
            severity=spec["severity"] if not valid else "ok",
            recommendation="" if valid else spec["recommendation"]
        ))

    for leaky in LEAKY_HEADERS:
        if leaky in response.headers:
            results.append(HeaderAuditResult(
                header=leaky,
                present=True,
                value=response.headers[leaky],
                severity="medium",
                recommendation=f"Remove '{leaky}' to reduce server fingerprinting"
            ))

    return results

def print_audit(results: list[HeaderAuditResult]):
    print(f"\n{'Header':<40} {'Status':<10} {'Severity':<10} Note")
    print("-" * 90)
    for r in results:
        status = "OK" if r.severity == "ok" else ("MISSING" if not r.present else "INVALID")
        print(f"{r.header:<40} {status:<10} {r.severity:<10} "
              f"{r.recommendation or r.value or ''}")

# Quick self-test
async def test_my_endpoint():
    results = await audit_security_headers("http://localhost:8080/health")
    print_audit(results)
    critical = [r for r in results if r.severity == "critical"]
    assert not critical, f"Critical header issues: {[r.header for r in critical]}"
```

---

## Solution 6: Unified Security Header Stack for Production

```python
import secrets
from aiohttp import web
from typing import Callable, Awaitable

class ProductionSecurityStack:
    """
    Single class that configures the full security header stack.
    Supports per-route overrides and report-only CSP mode for gradual rollout.
    """

    def __init__(self,
                 allowed_origins: frozenset[str] = frozenset(),
                 csp_report_only: bool = False,
                 report_uri: str | None = None,
                 hsts_preload: bool = True):
        self.allowed_origins = allowed_origins
        self.csp_header_name = (
            "Content-Security-Policy-Report-Only"
            if csp_report_only else
            "Content-Security-Policy"
        )
        self.report_uri = report_uri
        self.hsts_preload = hsts_preload

    def build_csp(self, nonce: str) -> str:
        parts = [
            f"default-src 'none'",
            f"script-src 'self' 'nonce-{nonce}'",
            "style-src 'self'",
            "img-src 'self' data:",
            "connect-src 'self'",
            "font-src 'self'",
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "upgrade-insecure-requests",
        ]
        if self.report_uri:
            parts.append(f"report-uri {self.report_uri}")
        return "; ".join(parts)

    def build_hsts(self) -> str:
        value = "max-age=63072000; includeSubDomains"
        if self.hsts_preload:
            value += "; preload"
        return value

    def middleware(self) -> "MiddlewareType":
        stack = self

        @web.middleware
        async def _middleware(request: web.Request,
                              handler: Callable) -> web.StreamResponse:
            nonce = secrets.token_urlsafe(16)
            request["csp_nonce"] = nonce
            response = await handler(request)

            # Core
            h = response.headers
            h["X-Content-Type-Options"] = "nosniff"
            h["X-Frame-Options"] = "DENY"
            h["Referrer-Policy"] = "strict-origin-when-cross-origin"
            h["Permissions-Policy"] = (
                "accelerometer=(), camera=(), geolocation=(), "
                "microphone=(), payment=(), usb=()"
            )
            h["Server"] = "agent"
            h.pop("X-Powered-By", None)

            # CSP
            h[stack.csp_header_name] = stack.build_csp(nonce)

            # HSTS (HTTPS only)
            if request.secure:
                h["Strict-Transport-Security"] = stack.build_hsts()

            # CORS
            origin = request.headers.get("Origin", "")
            if origin in stack.allowed_origins:
                h["Access-Control-Allow-Origin"] = origin
                h["Access-Control-Allow-Credentials"] = "true"
                h["Vary"] = "Origin"

            return response

        return _middleware

# Production wiring
def build_production_app() -> web.Application:
    security = ProductionSecurityStack(
        allowed_origins=frozenset({"https://dashboard.myapp.com"}),
        csp_report_only=False,
        report_uri="https://csp.myapp.com/report",
        hsts_preload=True,
    )
    app = web.Application(middlewares=[security.middleware()])
    app.router.add_get("/health", handle_health)
    app.router.add_post("/api/tool-result", handle_tool_result)
    return app

async def handle_tool_result(request: web.Request) -> web.Response:
    nonce = request.get("csp_nonce", "")
    data = await request.json()
    return web.json_response({"ok": True})
```

---

## Comparison

| Solution | Scope | Effort | CSP | HSTS | CORS | Best For |
|---|---|---|---|---|---|---|
| 1. Core middleware | All routes | Low | Basic | Yes | No | Minimal viable security |
| 2. CSP builder | CSP only | Low | Fluent API | No | No | Fine-tuning CSP per route |
| 3. FastAPI/Starlette | All routes | Low | Nonce support | Yes | No | FastAPI services |
| 4. CORS middleware | CORS only | Low | No | No | Yes | Cross-origin API |
| 5. Header auditor | Testing | Med | Audit | Audit | No | CI security checks |
| 6. Production stack | All routes | Med | Nonce + report | Preload | Yes | Production deployment |

**Key principle**: apply security headers at the middleware layer, never in individual route handlers — it's impossible to forget one route. Use `Content-Security-Policy-Report-Only` first to collect violations without blocking users, then switch to enforcement once false positives are resolved.
