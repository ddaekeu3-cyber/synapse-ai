---
title: "Agent Doesn't Implement CSRF Protection for Web Endpoints"
description: "Agent web APIs exposed without CSRF protection allow malicious sites to trigger tool executions or state mutations on behalf of authenticated users via cross-site request forgery."
difficulty: intermediate
category: security
tags: [security, csrf, web, fastapi, authentication, cookies, tokens]
---

# Agent Doesn't Implement CSRF Protection for Web Endpoints

## Problem

When an AI agent exposes HTTP endpoints that rely on cookie-based authentication (session cookies, JWT in cookies), any website can craft a form or fetch request that the user's browser submits with their cookies attached. Without CSRF tokens, a malicious site can trigger the agent to send emails, execute payments, or delete data on behalf of an authenticated user — all without the user's knowledge.

**Symptoms:**
- Agent web endpoints accept POST with only cookie auth and no CSRF token
- Browser-based clients don't send/validate CSRF tokens on mutating requests
- Same-site cookie attribute not set to `Strict` or `Lax`
- API endpoints accept `application/x-www-form-urlencoded` (form-submittable) without token
- No Origin/Referer header validation on state-mutating endpoints

---

## Solution 1: Synchronizer Token Pattern (Double Submit)

Generate a CSRF token per session, store it server-side, and require it on every mutating request.

```python
import asyncio
import hashlib
import hmac
import secrets
import time
from typing import Optional
import anthropic
from fastapi import Cookie, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

app = FastAPI()
SECRET = b"csrf-signing-secret-change-in-prod"

# In-process session→token store (use Redis in production)
_csrf_tokens: dict[str, tuple[str, float]] = {}  # session_id -> (token, expires)
CSRF_TTL = 3600.0  # 1 hour


def generate_csrf_token(session_id: str) -> str:
    token = secrets.token_hex(32)
    _csrf_tokens[session_id] = (token, time.time() + CSRF_TTL)
    return token


def validate_csrf_token(session_id: str, submitted_token: str) -> bool:
    entry = _csrf_tokens.get(session_id)
    if not entry:
        return False
    stored_token, expires = entry
    if time.time() > expires:
        del _csrf_tokens[session_id]
        return False
    return hmac.compare_digest(stored_token, submitted_token)


@app.get("/csrf-token")
async def get_csrf_token(
    response: Response,
    session_id: Optional[str] = Cookie(default=None),
):
    """Issue a CSRF token for the current session."""
    if not session_id:
        session_id = secrets.token_hex(16)
        response.set_cookie(
            "session_id", session_id,
            httponly=True,
            samesite="lax",
            secure=True,
            max_age=3600,
        )
    token = generate_csrf_token(session_id)
    return JSONResponse({"csrf_token": token})


anthropic_client = anthropic.AsyncAnthropic(api_key="sk-...")


@app.post("/agent/chat")
async def agent_chat(
    request: Request,
    session_id: Optional[str] = Cookie(default=None),
    x_csrf_token: Optional[str] = Header(default=None, alias="X-CSRF-Token"),
):
    """Protected endpoint — requires valid CSRF token."""
    if not session_id:
        raise HTTPException(status_code=401, detail="not_authenticated")

    if not x_csrf_token or not validate_csrf_token(session_id, x_csrf_token):
        raise HTTPException(status_code=403, detail="csrf_token_invalid")

    body = await request.json()
    message = body.get("message", "")

    response = await anthropic_client.messages.create(
        model="claude-opus-4-6",
        max_tokens=256,
        messages=[{"role": "user", "content": message}],
    )
    return JSONResponse({"reply": response.content[0].text})
```

---

## Solution 2: Double-Submit Cookie Pattern

Set a random CSRF token in both a readable cookie and a request header; verify they match server-side.

```python
import secrets
import hmac
from typing import Optional
from fastapi import Cookie, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
import anthropic

app = FastAPI()
anthropic_client = anthropic.AsyncAnthropic(api_key="sk-...")


@app.middleware("http")
async def csrf_cookie_middleware(request: Request, call_next):
    """Ensure every response sets a CSRF cookie."""
    response = await call_next(request)

    # Set CSRF cookie if not present
    if "csrftoken" not in request.cookies:
        token = secrets.token_hex(32)
        response.set_cookie(
            "csrftoken",
            token,
            httponly=False,   # JS must be able to read it to send the header
            samesite="strict",
            secure=True,
            max_age=86400,
        )
    return response


def verify_double_submit(
    cookie_token: Optional[str],
    header_token: Optional[str],
) -> bool:
    """Double-submit: cookie value must equal header value."""
    if not cookie_token or not header_token:
        return False
    return hmac.compare_digest(cookie_token, header_token)


SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


@app.post("/agent/execute-tool")
async def execute_tool(
    request: Request,
    csrftoken: Optional[str] = Cookie(default=None),
    x_csrftoken: Optional[str] = Header(default=None, alias="X-CSRFToken"),
):
    if request.method not in SAFE_METHODS:
        if not verify_double_submit(csrftoken, x_csrftoken):
            raise HTTPException(status_code=403, detail="csrf_double_submit_failed")

    body = await request.json()
    tool = body.get("tool", "")
    args = body.get("args", {})

    # Execute via LLM
    response = await anthropic_client.messages.create(
        model="claude-opus-4-6",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Execute tool '{tool}' with args: {args}"
        }],
    )
    return JSONResponse({"result": response.content[0].text})
```

---

## Solution 3: Origin and Referer Header Validation

Reject requests whose Origin or Referer header doesn't match the expected host — a defense layer that requires no token state.

```python
import re
from typing import Optional
from urllib.parse import urlparse
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
import anthropic

app = FastAPI()
anthropic_client = anthropic.AsyncAnthropic(api_key="sk-...")

ALLOWED_ORIGINS = {
    "https://app.example.com",
    "https://www.example.com",
}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def validate_origin_or_referer(
    origin: Optional[str],
    referer: Optional[str],
) -> bool:
    # Prefer Origin header (more reliable)
    if origin:
        return origin.rstrip("/") in ALLOWED_ORIGINS

    # Fall back to Referer
    if referer:
        parsed = urlparse(referer)
        base = f"{parsed.scheme}://{parsed.netloc}"
        return base in ALLOWED_ORIGINS

    # Neither present — reject (conservative)
    return False


@app.middleware("http")
async def origin_validation_middleware(request: Request, call_next):
    if request.method not in SAFE_METHODS:
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")

        if not validate_origin_or_referer(origin, referer):
            return JSONResponse(
                status_code=403,
                content={"error": "forbidden_origin", "origin": origin},
            )

    return await call_next(request)


@app.post("/agent/action")
async def agent_action(request: Request):
    body = await request.json()
    message = body.get("message", "")

    response = await anthropic_client.messages.create(
        model="claude-opus-4-6",
        max_tokens=256,
        messages=[{"role": "user", "content": message}],
    )
    return JSONResponse({"reply": response.content[0].text})
```

---

## Solution 4: HMAC-Signed CSRF Tokens (Stateless)

Sign the CSRF token with a server secret and the session ID; verify the signature on submission — no server-side token storage needed.

```python
import hashlib
import hmac
import secrets
import time
from typing import Optional
from fastapi import Cookie, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
import anthropic

app = FastAPI()
anthropic_client = anthropic.AsyncAnthropic(api_key="sk-...")

CSRF_SECRET = b"stateless-csrf-signing-key"
CSRF_VALIDITY = 3600  # seconds


def generate_signed_csrf(session_id: str) -> str:
    """Generate a self-verifying CSRF token: ts.nonce.sig"""
    ts = int(time.time())
    nonce = secrets.token_hex(8)
    message = f"{ts}.{nonce}.{session_id}".encode()
    sig = hmac.new(CSRF_SECRET, message, hashlib.sha256).hexdigest()[:16]
    return f"{ts}.{nonce}.{sig}"


def verify_signed_csrf(token: str, session_id: str) -> bool:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return False
        ts_str, nonce, submitted_sig = parts
        ts = int(ts_str)

        # Check expiry
        if time.time() - ts > CSRF_VALIDITY:
            return False

        # Recompute signature
        message = f"{ts}.{nonce}.{session_id}".encode()
        expected_sig = hmac.new(CSRF_SECRET, message, hashlib.sha256).hexdigest()[:16]
        return hmac.compare_digest(expected_sig, submitted_sig)
    except Exception:
        return False


@app.get("/csrf")
async def issue_csrf(
    response: Response,
    session_id: Optional[str] = Cookie(default=None),
):
    if not session_id:
        session_id = secrets.token_hex(16)
        response.set_cookie("session_id", session_id, httponly=True, samesite="lax", secure=True)

    token = generate_signed_csrf(session_id)
    return JSONResponse({"token": token, "expires_in": CSRF_VALIDITY})


@app.post("/agent/run")
async def run_agent(
    request: Request,
    session_id: Optional[str] = Cookie(default=None),
    x_csrf_token: Optional[str] = Header(default=None, alias="X-CSRF-Token"),
):
    if not session_id:
        raise HTTPException(status_code=401, detail="no_session")

    if not x_csrf_token or not verify_signed_csrf(x_csrf_token, session_id):
        raise HTTPException(status_code=403, detail="invalid_csrf_token")

    body = await request.json()
    response = await anthropic_client.messages.create(
        model="claude-opus-4-6",
        max_tokens=256,
        messages=[{"role": "user", "content": body.get("prompt", "")}],
    )
    return JSONResponse({"output": response.content[0].text})
```

---

## Solution 5: SameSite Cookie Enforcement + CORS Lockdown

Configure cookies as `SameSite=Strict` and tighten CORS to eliminate the attack surface entirely.

```python
import secrets
from typing import Optional
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware
import anthropic

app = FastAPI()

# Strict CORS — only allow the exact production origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://app.example.com"],  # Never use "*" for credentialed requests
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
    expose_headers=["X-Request-ID"],
    max_age=600,
)

app.add_middleware(
    SessionMiddleware,
    secret_key=secrets.token_hex(32),
    session_cookie="__Host-session",  # __Host- prefix enforces Secure + no Domain
    https_only=True,
    same_site="strict",  # SameSite=Strict blocks cross-site submissions entirely
    max_age=3600,
)

anthropic_client = anthropic.AsyncAnthropic(api_key="sk-...")


@app.post("/agent/chat")
async def chat(request: Request):
    # With SameSite=Strict cookies + CORS lockdown, CSRF is effectively neutralized.
    # The cookie won't be sent on cross-site requests at all.
    session = request.session
    if not session.get("authenticated"):
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    body = await request.json()
    response = await anthropic_client.messages.create(
        model="claude-opus-4-6",
        max_tokens=256,
        messages=[{"role": "user", "content": body.get("message", "")}],
    )
    return JSONResponse({"reply": response.content[0].text})


@app.post("/auth/login")
async def login(request: Request):
    body = await request.json()
    # Validate credentials (omitted for brevity)
    if body.get("password") == "correct":
        request.session["authenticated"] = True
        request.session["user_id"] = body.get("username")
    return JSONResponse({"ok": True})
```

---

## Solution 6: Per-Request Action Token for High-Risk Operations

For destructive agent actions (delete, payment, send), require a one-time action token issued immediately before the operation.

```python
import asyncio
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Optional
from fastapi import Cookie, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
import anthropic

app = FastAPI()
anthropic_client = anthropic.AsyncAnthropic(api_key="sk-...")
ACTION_TOKEN_SECRET = b"action-token-secret"
ACTION_TOKEN_TTL = 60  # Action tokens expire in 60 seconds (single-use, tight window)

_action_tokens: dict[str, tuple[str, float]] = {}  # token -> (action, expires)
_token_lock = asyncio.Lock()


async def issue_action_token(action: str, session_id: str) -> str:
    token = secrets.token_hex(32)
    async with _token_lock:
        _action_tokens[token] = (f"{action}:{session_id}", time.time() + ACTION_TOKEN_TTL)
    return token


async def consume_action_token(token: str, action: str, session_id: str) -> bool:
    async with _token_lock:
        entry = _action_tokens.get(token)
        if not entry:
            return False
        expected_payload, expires = entry
        if time.time() > expires:
            del _action_tokens[token]
            return False
        if expected_payload != f"{action}:{session_id}":
            return False
        del _action_tokens[token]  # One-time use
    return True


HIGH_RISK_ACTIONS = {"delete_all_data", "send_bulk_email", "initiate_payment"}


@app.post("/agent/request-action-token")
async def request_action_token(
    request: Request,
    session_id: Optional[str] = Cookie(default=None),
):
    if not session_id:
        raise HTTPException(status_code=401, detail="not_authenticated")

    body = await request.json()
    action = body.get("action", "")

    if action not in HIGH_RISK_ACTIONS:
        raise HTTPException(status_code=400, detail="unknown_action")

    token = await issue_action_token(action, session_id)
    return JSONResponse({"action_token": token, "expires_in": ACTION_TOKEN_TTL, "action": action})


@app.post("/agent/execute-action")
async def execute_action(
    request: Request,
    session_id: Optional[str] = Cookie(default=None),
    x_action_token: Optional[str] = Header(default=None, alias="X-Action-Token"),
):
    if not session_id:
        raise HTTPException(status_code=401, detail="not_authenticated")

    body = await request.json()
    action = body.get("action", "")

    if action in HIGH_RISK_ACTIONS:
        if not x_action_token or not await consume_action_token(x_action_token, action, session_id):
            raise HTTPException(status_code=403, detail="action_token_invalid_or_expired")

    response = await anthropic_client.messages.create(
        model="claude-opus-4-6",
        max_tokens=256,
        messages=[{"role": "user", "content": f"Confirm and execute: {action}"}],
    )
    return JSONResponse({"result": response.content[0].text})
```

---

## Comparison

| Solution | Stateless | JS Readable | Multi-Tab Safe | Complexity | Best For |
|---|---|---|---|---|---|
| Synchronizer token | No | No | Yes (per-session) | Medium | Traditional server-rendered |
| Double-submit cookie | Yes | Yes (cookie) | Yes | Low | SPA + REST API |
| Origin/Referer validation | Yes | N/A | Yes | Very Low | Defense-in-depth layer |
| HMAC-signed token | Yes | No | Yes | Low | High-scale stateless API |
| SameSite=Strict + CORS | Yes | N/A | Yes | Very Low | Modern browser-only clients |
| Per-request action token | No | No | Yes | Medium | High-risk destructive ops |

**Recommendation:** Layer Solution 5 (SameSite=Strict cookies + CORS) as your baseline — it's free and eliminates most CSRF for modern browsers. Add Solution 4 (HMAC-signed token) for API endpoints accessed by older clients or non-browser callers. Use Solution 6 (one-time action token) for any endpoint that triggers irreversible actions.
