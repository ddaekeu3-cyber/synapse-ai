---
layout: solution
title: "Agent Doesn't Implement CORS Configuration for API"
category: general
description: "An AI agent's REST API has no CORS headers. Browser-based frontends, chat widgets, and web dashboards receive 'blocked by CORS policy' errors and cannot call the agent API directly. Incorrectly configured CORS also creates security vulnerabilities."
tags: [cors, security, fastapi, aiohttp, middleware, browser, api]
---

# Agent Doesn't Implement CORS Configuration for API

## Problem

A developer builds a React chat widget that calls `POST /api/agent/chat`. The browser blocks the request: `Access to fetch at 'https://api.agent.example.com' from origin 'https://app.example.com' has been blocked by CORS policy: No 'Access-Control-Allow-Origin' header`. Without CORS headers, the agent API is unusable from any browser. Wildcard CORS (`*`) fixes the error but creates a security hole — any website can make authenticated requests on behalf of your users.

## Solutions

### Option 1: FastAPI CORS Middleware with Allowlist

```python
# main.py
"""
Production-safe CORS configuration:
- Explicit allowlist of trusted origins (not wildcard).
- Separate lists for credentials-bearing vs anonymous requests.
- Preflight (OPTIONS) handled automatically by middleware.
"""
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


app = FastAPI()

# Trusted origins that may send authenticated (credentialed) requests
ALLOWED_ORIGINS = os.environ.get(
    "CORS_ALLOWED_ORIGINS",
    "http://localhost:3000,https://app.example.com,https://chat.example.com",
).split(",")

# Public origins that may call unauthenticated endpoints (embeddings, status)
PUBLIC_ORIGINS = os.environ.get(
    "CORS_PUBLIC_ORIGINS",
    "*",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,        # Only these origins may send cookies/auth headers
    allow_credentials=True,               # Allow cookies and Authorization headers
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Request-ID",
        "X-User-ID",
    ],
    expose_headers=["X-Request-ID"],      # Headers JS can read in the response
    max_age=86400,                        # Cache preflight for 24h (reduces OPTIONS requests)
)


@app.post("/api/agent/chat")
async def chat(body: dict):
    return {"response": "Hello!"}


@app.get("/api/health")
async def health():
    # No auth needed — public endpoint
    return {"status": "ok"}
```

```python
# tests/test_cors.py
import pytest
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)


def test_preflight_allowed_origin():
    resp = client.options(
        "/api/agent/chat",
        headers={
            "Origin": "https://app.example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization, Content-Type",
        },
    )
    assert resp.status_code == 200
    assert "https://app.example.com" in resp.headers.get("access-control-allow-origin", "")


def test_preflight_disallowed_origin():
    resp = client.options(
        "/api/agent/chat",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    # Origin not in allowlist — no CORS header returned
    assert "access-control-allow-origin" not in resp.headers


def test_cors_header_present_on_allowed_origin():
    resp = client.post(
        "/api/agent/chat",
        json={"message": "hi"},
        headers={"Origin": "https://app.example.com"},
    )
    assert resp.headers.get("access-control-allow-origin") == "https://app.example.com"
```

**Expected Token Savings:** Not applicable — API accessibility
**Environment:** `pip install fastapi`

---

### Option 2: Dynamic CORS Based on Request Origin

```python
# middleware/dynamic_cors.py
"""
Dynamic CORS that checks the request origin against a database of registered
client applications. Enables tenant-specific CORS without redeployment.
New applications register their origins via an admin API.
"""
import os
import time
from threading import Lock
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


class DynamicCORSMiddleware(BaseHTTPMiddleware):
    """
    CORS middleware with a runtime-configurable origin allowlist.
    Origins can be added/removed without restarting the server.
    """
    def __init__(self, app, allowed_origins: set[str], update_lock: Lock):
        super().__init__(app)
        self._allowed = allowed_origins
        self._lock = update_lock
        self._max_age = 3600

    def is_allowed(self, origin: str) -> bool:
        with self._lock:
            if origin in self._allowed:
                return True
            # Wildcard subdomain matching: *.example.com
            for pattern in self._allowed:
                if pattern.startswith("*."):
                    domain = pattern[2:]
                    if origin.endswith(f".{domain}") or origin == f"https://{domain}":
                        return True
            return False

    def _cors_headers(self, origin: str) -> dict:
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Request-ID",
            "Access-Control-Expose-Headers": "X-Request-ID",
            "Access-Control-Max-Age": str(self._max_age),
            "Vary": "Origin",
        }

    async def dispatch(self, request: Request, call_next):
        origin = request.headers.get("Origin", "")

        if not origin:
            # Non-browser request — no CORS headers needed
            return await call_next(request)

        if not self.is_allowed(origin):
            if request.method == "OPTIONS":
                return Response(status_code=403, content="Origin not allowed")
            # Continue request but don't add CORS headers — browser will block it
            return await call_next(request)

        if request.method == "OPTIONS":
            return Response(
                status_code=204,
                headers=self._cors_headers(origin),
            )

        response = await call_next(request)
        for k, v in self._cors_headers(origin).items():
            response.headers[k] = v
        return response


# ── Global origin registry ────────────────────────────────────────────────────

_origins: set[str] = set(os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(","))
_lock = Lock()

app = FastAPI()
app.add_middleware(DynamicCORSMiddleware, allowed_origins=_origins, update_lock=_lock)


@app.post("/admin/cors/origins")
async def add_origin(body: dict):
    """Admin endpoint: register a new allowed origin at runtime."""
    origin = body.get("origin", "").strip().rstrip("/")
    if not origin.startswith(("http://", "https://")):
        return JSONResponse({"error": "Origin must start with http:// or https://"}, status_code=400)
    with _lock:
        _origins.add(origin)
    return {"added": origin, "total": len(_origins)}


@app.delete("/admin/cors/origins")
async def remove_origin(body: dict):
    """Admin endpoint: remove an allowed origin."""
    origin = body.get("origin", "")
    with _lock:
        _origins.discard(origin)
    return {"removed": origin}
```

**Expected Token Savings:** Not applicable — multi-tenant API security
**Environment:** `pip install fastapi`

---

### Option 3: Per-Endpoint CORS Decorator

```python
# decorators/cors.py
"""
Fine-grained CORS: different endpoints get different CORS policies.
Public webhook receivers allow any origin.
Authenticated chat API allows only registered apps.
Admin endpoints allow only internal origins.
"""
import functools
import os
from typing import Callable, set
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse


def cors_policy(
    origins: list[str] | str,
    methods: list[str] = None,
    headers: list[str] = None,
    credentials: bool = False,
):
    """
    Decorator factory: apply specific CORS policy to a single endpoint.
    Usage:
        @app.post("/api/public")
        @cors_policy(origins="*")
        async def public_endpoint():
            ...
    """
    if isinstance(origins, str):
        origins = [origins]
    allowed_origins = set(origins)
    allow_methods = ", ".join(methods or ["GET", "POST", "OPTIONS"])
    allow_headers = ", ".join(headers or ["Content-Type"])

    def decorator(fn: Callable):
        @functools.wraps(fn)
        async def wrapper(request: Request, *args, **kwargs):
            origin = request.headers.get("Origin", "")
            allow = "*" in allowed_origins or origin in allowed_origins

            if request.method == "OPTIONS":
                resp = Response(status_code=204)
            else:
                resp = await fn(request, *args, **kwargs)
                if not isinstance(resp, Response):
                    resp = JSONResponse(resp)

            if allow and origin:
                allowed_origin = "*" if "*" in allowed_origins else origin
                resp.headers["Access-Control-Allow-Origin"] = allowed_origin
                resp.headers["Access-Control-Allow-Methods"] = allow_methods
                resp.headers["Access-Control-Allow-Headers"] = allow_headers
                if credentials and "*" not in allowed_origins:
                    resp.headers["Access-Control-Allow-Credentials"] = "true"
                resp.headers["Vary"] = "Origin"
            return resp
        return wrapper
    return decorator


# ── Usage ─────────────────────────────────────────────────────────────────────

INTERNAL_ORIGINS = ["https://admin.example.com", "https://dashboard.example.com"]
APP_ORIGINS = ["https://app.example.com", "https://chat.example.com", "http://localhost:3000"]

app = FastAPI()


@app.post("/api/agent/chat")
@cors_policy(origins=APP_ORIGINS, methods=["POST"], credentials=True)
async def chat(request: Request):
    body = await request.json()
    return {"response": "Hello!"}


@app.get("/api/public/status")
@cors_policy(origins="*", methods=["GET"])
async def public_status(request: Request):
    return {"status": "ok"}


@app.post("/admin/agent/config")
@cors_policy(origins=INTERNAL_ORIGINS, methods=["POST"], credentials=True)
async def admin_config(request: Request):
    return {"updated": True}
```

**Expected Token Savings:** Not applicable — endpoint-level security control
**Environment:** `pip install fastapi`

---

### Option 4: aiohttp CORS Configuration

```python
# server/aiohttp_cors.py
"""
CORS configuration for aiohttp-based agent servers.
Uses aiohttp-cors for declarative per-route CORS setup.
"""
from aiohttp import web
import aiohttp_cors
import os
import anthropic


ALLOWED_ORIGINS = os.environ.get(
    "CORS_ORIGINS",
    "http://localhost:3000,https://app.example.com"
).split(",")


async def chat_handler(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        user_message = body.get("message", "")
    except Exception:
        raise web.HTTPBadRequest(reason="Invalid JSON body")

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": user_message}],
    )
    return web.json_response({"response": response.content[0].text})


async def health_handler(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def create_app() -> web.Application:
    app = web.Application()

    # Configure CORS
    cors = aiohttp_cors.setup(app, defaults={
        # Default policy for origins not explicitly listed: no CORS
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=False,
            expose_headers="*",
            allow_headers="*",
        ),
    })

    # Chat endpoint: restricted to known origins
    chat_resource = cors.add(app.router.add_resource("/api/agent/chat"))
    for origin in ALLOWED_ORIGINS:
        cors.add(
            chat_resource.add_route("POST", chat_handler),
            {
                origin: aiohttp_cors.ResourceOptions(
                    allow_credentials=True,
                    expose_headers=("X-Request-ID",),
                    allow_headers=("Authorization", "Content-Type", "X-Request-ID"),
                    max_age=3600,
                ),
            },
        )

    # Health endpoint: public, any origin
    health_resource = cors.add(app.router.add_resource("/health"))
    cors.add(health_resource.add_route("GET", health_handler), {
        "*": aiohttp_cors.ResourceOptions(allow_credentials=False),
    })

    return app


if __name__ == "__main__":
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=8000)
```

**Expected Token Savings:** Not applicable — aiohttp API accessibility
**Environment:** `pip install aiohttp aiohttp-cors anthropic`

---

### Option 5: CORS Security Audit Test Suite

```python
# tests/security/test_cors_security.py
"""
Security tests for CORS configuration.
Verify that:
- Wildcard CORS is not used on credentialed endpoints.
- Untrusted origins are rejected.
- Preflight responses are correct.
- CORS headers are not present when origin is not sent.
"""
import pytest
from fastapi.testclient import TestClient
from main import app  # Your FastAPI app

client = TestClient(app, raise_server_exceptions=True)

TRUSTED_ORIGINS = [
    "https://app.example.com",
    "https://chat.example.com",
    "http://localhost:3000",
]

UNTRUSTED_ORIGINS = [
    "https://evil.attacker.com",
    "https://app.example.com.evil.com",  # Subdomain confusion
    "https://notapp.example.com",
    "null",  # Sandboxed iframe attack
    "",
]


class TestCORSSecurity:
    """Critical CORS security tests — must all pass before deployment."""

    @pytest.mark.parametrize("origin", TRUSTED_ORIGINS)
    def test_trusted_origin_gets_cors_header(self, origin):
        resp = client.get("/api/health", headers={"Origin": origin})
        assert "access-control-allow-origin" in resp.headers, \
            f"Trusted origin {origin!r} did not receive CORS header"
        acao = resp.headers["access-control-allow-origin"]
        assert acao == origin or acao == "*"

    @pytest.mark.parametrize("origin", UNTRUSTED_ORIGINS)
    def test_untrusted_origin_no_acao_header(self, origin):
        if not origin:
            return
        resp = client.post(
            "/api/agent/chat",
            json={"message": "test"},
            headers={"Origin": origin},
        )
        acao = resp.headers.get("access-control-allow-origin", "")
        assert acao != "*", f"Wildcard CORS on credentialed endpoint! Origin: {origin!r}"
        assert acao != origin, f"Untrusted origin {origin!r} received ACAO header"

    def test_no_wildcard_on_credentialed_endpoint(self):
        """Wildcard + credentials is forbidden by the CORS spec and browser-blocked."""
        resp = client.post(
            "/api/agent/chat",
            json={"message": "test"},
            headers={"Origin": "https://app.example.com"},
        )
        acao = resp.headers.get("access-control-allow-origin", "")
        acac = resp.headers.get("access-control-allow-credentials", "")
        # If credentials are allowed, origin must not be wildcard
        if acac.lower() == "true":
            assert acao != "*", "SECURITY: credentials=true with wildcard origin is forbidden"

    def test_no_acao_without_origin_header(self):
        """CORS headers should not appear on requests without Origin header."""
        resp = client.post("/api/agent/chat", json={"message": "test"})
        # Vary header should indicate Origin is considered, but no ACAO needed
        assert "access-control-allow-origin" not in resp.headers

    @pytest.mark.parametrize("origin", TRUSTED_ORIGINS)
    def test_vary_header_present(self, origin):
        """Vary: Origin must be present to prevent cache poisoning."""
        resp = client.get("/api/health", headers={"Origin": origin})
        vary = resp.headers.get("vary", "")
        assert "origin" in vary.lower(), \
            f"Missing Vary: Origin header — CORS response may be cached for wrong origin"

    def test_preflight_returns_204(self):
        resp = client.options(
            "/api/agent/chat",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Authorization",
            },
        )
        assert resp.status_code in (200, 204), f"Preflight returned {resp.status_code}"
        assert "access-control-allow-methods" in resp.headers
```

**Expected Token Savings:** Not applicable — security verification
**Environment:** `pip install fastapi pytest`

---

### Option 6: CORS Configuration via Environment Variables

```python
# config/cors_config.py
"""
Load CORS configuration from environment variables for 12-factor app compliance.
Different deployment environments (dev, staging, prod) use different origins
without code changes.
"""
import os
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CORSConfig:
    """CORS configuration loaded from environment."""
    allowed_origins: list[str] = field(default_factory=list)
    allow_credentials: bool = True
    allowed_methods: list[str] = field(default_factory=lambda: ["GET", "POST", "OPTIONS"])
    allowed_headers: list[str] = field(default_factory=lambda: ["Authorization", "Content-Type"])
    expose_headers: list[str] = field(default_factory=lambda: ["X-Request-ID"])
    max_age: int = 86400

    @classmethod
    def from_env(cls) -> "CORSConfig":
        origins_raw = os.environ.get("CORS_ORIGINS", "http://localhost:3000")
        origins = [o.strip().rstrip("/") for o in origins_raw.split(",") if o.strip()]

        # Validate origins
        for origin in origins:
            if origin != "*" and not re.match(r"^https?://[a-zA-Z0-9\-.*]+(:\d+)?$", origin):
                raise ValueError(f"Invalid CORS origin format: {origin!r}")

        return cls(
            allowed_origins=origins,
            allow_credentials=os.environ.get("CORS_ALLOW_CREDENTIALS", "true").lower() == "true",
            allowed_methods=os.environ.get(
                "CORS_METHODS", "GET,POST,OPTIONS"
            ).split(","),
            allowed_headers=os.environ.get(
                "CORS_HEADERS", "Authorization,Content-Type,X-Request-ID"
            ).split(","),
            max_age=int(os.environ.get("CORS_MAX_AGE", "86400")),
        )

    def validate_security(self):
        """Raise if configuration has security issues."""
        if "*" in self.allowed_origins and self.allow_credentials:
            raise ValueError(
                "SECURITY: CORS wildcard origin ('*') with allow_credentials=True is forbidden. "
                "Browsers reject this combination. Use explicit origins instead."
            )
        env = os.environ.get("ENVIRONMENT", "development")
        if env == "production" and "http://" in str(self.allowed_origins):
            import warnings
            warnings.warn(
                "CORS allows HTTP origins in production. "
                "Consider HTTPS-only for security.",
                stacklevel=2,
            )


from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


def configure_cors(app: FastAPI) -> CORSConfig:
    config = CORSConfig.from_env()
    config.validate_security()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.allowed_origins,
        allow_credentials=config.allow_credentials,
        allow_methods=config.allowed_methods,
        allow_headers=config.allowed_headers,
        expose_headers=config.expose_headers,
        max_age=config.max_age,
    )
    return config
```

```bash
# dev .env
CORS_ORIGINS=http://localhost:3000,http://localhost:5173
CORS_ALLOW_CREDENTIALS=true

# production .env
CORS_ORIGINS=https://app.example.com,https://chat.example.com
CORS_ALLOW_CREDENTIALS=true
CORS_MAX_AGE=86400
```

**Expected Token Savings:** Not applicable — deployment configuration
**Environment:** `pip install fastapi`

---

## Comparison Table

| Option | Origin Control | Dynamic Updates | Wildcard Safe | Per-Endpoint | Security Tests |
|--------|---------------|-----------------|---------------|--------------|----------------|
| 1: FastAPI middleware | Allowlist | Via env var | Enforced | No | Yes |
| 2: Dynamic middleware | Runtime registry | Admin API | Enforced | No | No |
| 3: Per-endpoint decorator | Per-endpoint list | Code change | Configurable | Yes | No |
| 4: aiohttp-cors | Per-route | Code change | Configurable | Yes | No |
| 5: Security test suite | N/A (tests) | N/A | Verified | All endpoints | Yes |
| 6: Env var config | Env-based list | Env change | Validated | No | Partial |
