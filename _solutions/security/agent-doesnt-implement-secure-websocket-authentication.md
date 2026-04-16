---
title: "Agent Doesn't Implement Secure WebSocket Authentication"
description: "Agent WebSocket endpoints that rely on cookie-based or header-based auth checked only at connection time are vulnerable to token theft, connection hijacking, and missing re-authentication when tokens expire mid-session."
difficulty: intermediate
category: security
tags: [websocket, authentication, jwt, token, security, aiohttp, fastapi, real-time]
---

## Problem

HTTP upgrade to WebSocket happens once; after that the connection is a persistent bidirectional channel. Agents that only check auth at connection time leave the channel open indefinitely even after the token expires or is revoked. Agents that pass tokens in query strings expose them in server logs and browser history. Unauthenticated WebSocket endpoints let any script on the page connect.

```python
# Broken: token in query string, checked only at connect time
from aiohttp import web

async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    token = request.rel_url.query.get("token")  # visible in logs
    if not verify_token(token):
        raise web.HTTPUnauthorized()
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    async for msg in ws:
        await ws.send_str(process(msg.data))  # token may have expired
    return ws
```

---

## Solution 1: Token-in-First-Message Authentication

```python
import asyncio
import json
import time
import hmac
import hashlib
import secrets
from aiohttp import web, WSMsgType

# Never put tokens in query strings — use first-message auth instead
AUTH_TIMEOUT = 5.0  # seconds to authenticate after connect

async def authenticated_ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)

    # Give the client AUTH_TIMEOUT seconds to send auth message
    try:
        auth_msg = await asyncio.wait_for(ws.receive(), timeout=AUTH_TIMEOUT)
    except asyncio.TimeoutError:
        await ws.close(code=4001, message=b"Authentication timeout")
        return ws

    if auth_msg.type != WSMsgType.TEXT:
        await ws.close(code=4002, message=b"Expected text auth message")
        return ws

    try:
        payload = json.loads(auth_msg.data)
    except json.JSONDecodeError:
        await ws.close(code=4003, message=b"Invalid JSON")
        return ws

    token = payload.get("token", "")
    user_id = await verify_and_decode_token(token)
    if not user_id:
        await ws.close(code=4004, message=b"Invalid or expired token")
        return ws

    # Acknowledge authentication
    await ws.send_json({"type": "auth_ok", "user_id": user_id})
    print(f"[WS] Authenticated: user={user_id}")

    # Now handle normal messages
    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            await handle_ws_message(ws, user_id, msg.data)
        elif msg.type == WSMsgType.ERROR:
            print(f"[WS] Error: {ws.exception()}")
            break

    return ws

async def verify_and_decode_token(token: str) -> str | None:
    """Returns user_id if valid, None if invalid/expired."""
    try:
        import base64
        # Minimal JWT-like verification (use PyJWT in production)
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        if payload.get("exp", 0) < time.time():
            return None
        return payload.get("sub")
    except Exception:
        return None

async def handle_ws_message(ws: web.WebSocketResponse,
                             user_id: str, data: str):
    await ws.send_json({"type": "response", "echo": data, "user": user_id})
```

---

## Solution 2: Per-Message Authentication with Rotating Nonces

```python
import asyncio
import hashlib
import hmac
import json
import time
from collections import defaultdict

SECRET_KEY = b"your-secret-key-here"

def compute_message_mac(user_id: str, nonce: str, timestamp: float,
                         payload: str) -> str:
    """HMAC-SHA256 over canonical message fields."""
    canonical = f"{user_id}:{nonce}:{timestamp:.0f}:{payload}"
    return hmac.new(SECRET_KEY, canonical.encode(), hashlib.sha256).hexdigest()

class NonceTracker:
    """
    Tracks used nonces to prevent replay attacks.
    Uses a time-windowed approach: nonces are valid for WINDOW seconds.
    """
    WINDOW = 60.0  # seconds

    def __init__(self):
        # bucket by minute for efficient cleanup
        self._used: dict[int, set[str]] = defaultdict(set)

    def is_replay(self, nonce: str, timestamp: float) -> bool:
        now = time.time()
        if abs(now - timestamp) > self.WINDOW:
            return True  # too old or too far in future
        bucket = int(timestamp // self.WINDOW)
        if nonce in self._used[bucket]:
            return True  # replay
        self._used[bucket].add(nonce)
        # Cleanup old buckets
        for old_bucket in list(self._used.keys()):
            if old_bucket < bucket - 1:
                del self._used[old_bucket]
        return False

nonce_tracker = NonceTracker()

async def verify_ws_message(raw: str, expected_user_id: str) -> dict | None:
    """
    Verify a signed WebSocket message.
    Message format: {"payload": ..., "user_id": ..., "nonce": ...,
                     "timestamp": ..., "mac": ...}
    """
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return None

    user_id = msg.get("user_id", "")
    nonce = msg.get("nonce", "")
    timestamp = float(msg.get("timestamp", 0))
    payload = msg.get("payload", "")
    mac = msg.get("mac", "")

    # Identity check
    if user_id != expected_user_id:
        return None

    # Replay check
    if nonce_tracker.is_replay(nonce, timestamp):
        return None

    # MAC verification
    expected_mac = compute_message_mac(user_id, nonce, timestamp, str(payload))
    if not hmac.compare_digest(mac, expected_mac):
        return None

    return {"user_id": user_id, "payload": payload}

async def signed_message_ws_handler(request, ws, user_id: str):
    """Handle loop that verifies each incoming message's signature."""
    from aiohttp import WSMsgType
    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            continue
        verified = await verify_ws_message(msg.data, user_id)
        if not verified:
            await ws.close(code=4005, message=b"Message authentication failed")
            return
        # Process verified message
        await ws.send_json({"type": "ack", "payload": verified["payload"]})
```

---

## Solution 3: Token Refresh Mid-Session

```python
import asyncio
import json
import time
from dataclasses import dataclass

@dataclass
class TokenSession:
    user_id: str
    token: str
    expires_at: float
    refresh_token: str | None = None

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at

    @property
    def needs_refresh(self) -> bool:
        # Refresh when within 60 seconds of expiry
        return time.time() >= self.expires_at - 60

class TokenRefreshingWSSession:
    """
    Long-lived WebSocket session that proactively refreshes tokens
    and notifies the client when a new token is issued.
    """

    def __init__(self, ws, session: TokenSession,
                 refresh_fn,  # async (refresh_token) -> TokenSession | None
                 check_interval: float = 30.0):
        self._ws = ws
        self._session = session
        self._refresh_fn = refresh_fn
        self._check_interval = check_interval
        self._closed = False

    async def run(self):
        refresh_task = asyncio.create_task(self._token_refresh_loop())
        try:
            from aiohttp import WSMsgType
            async for msg in self._ws:
                if self._session.is_expired:
                    await self._ws.close(code=4010, message=b"Session expired")
                    return
                if msg.type == WSMsgType.TEXT:
                    await self._handle(msg.data)
        finally:
            self._closed = True
            refresh_task.cancel()

    async def _token_refresh_loop(self):
        while not self._closed:
            await asyncio.sleep(self._check_interval)
            if self._session.needs_refresh and self._session.refresh_token:
                new_session = await self._refresh_fn(self._session.refresh_token)
                if new_session:
                    self._session = new_session
                    # Notify client of new token (client stores for reconnect)
                    await self._ws.send_json({
                        "type": "token_refreshed",
                        "token": new_session.token,
                        "expires_at": new_session.expires_at,
                    })
                    print(f"[WS] Token refreshed for {self._session.user_id}")
                else:
                    # Refresh failed — close connection
                    await self._ws.close(code=4011, message=b"Token refresh failed")
                    return

    async def _handle(self, data: str):
        await self._ws.send_json({"type": "echo", "data": data})
```

---

## Solution 4: Origin Validation and CSRF Prevention

```python
from aiohttp import web
from urllib.parse import urlparse

ALLOWED_ORIGINS = frozenset({
    "https://dashboard.example.com",
    "https://app.example.com",
})

WS_TICKET_STORE: dict[str, dict] = {}  # In production: Redis with TTL

import secrets
import time

async def issue_ws_ticket(request: web.Request) -> web.Response:
    """
    REST endpoint to issue a short-lived WebSocket ticket.
    Called from authenticated session; ticket exchanged at WS connect.
    This avoids passing the session token in the WS URL or query string.
    """
    user_id = request.get("user_id")
    if not user_id:
        raise web.HTTPUnauthorized()

    ticket = secrets.token_urlsafe(32)
    WS_TICKET_STORE[ticket] = {
        "user_id": user_id,
        "issued_at": time.time(),
        "ttl": 30.0,  # 30 seconds to use the ticket
        "used": False,
    }
    return web.json_response({"ticket": ticket, "expires_in": 30})

async def secure_ws_handler(request: web.Request) -> web.WebSocketResponse:
    # 1. Validate Origin header
    origin = request.headers.get("Origin", "")
    if origin and origin not in ALLOWED_ORIGINS:
        raise web.HTTPForbidden(reason=f"Origin not allowed: {origin}")

    # 2. Validate ticket (passed as query param — safe because short-lived)
    ticket = request.rel_url.query.get("ticket", "")
    entry = WS_TICKET_STORE.get(ticket)
    if not entry:
        raise web.HTTPUnauthorized(reason="Invalid ticket")
    if entry["used"]:
        raise web.HTTPUnauthorized(reason="Ticket already used")
    if time.time() - entry["issued_at"] > entry["ttl"]:
        del WS_TICKET_STORE[ticket]
        raise web.HTTPUnauthorized(reason="Ticket expired")

    # Consume ticket (one-time use)
    entry["used"] = True
    user_id = entry["user_id"]
    del WS_TICKET_STORE[ticket]

    ws = web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)
    print(f"[WS] Connected: user={user_id} origin={origin}")

    from aiohttp import WSMsgType
    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            await ws.send_json({"user": user_id, "echo": msg.data})

    return ws
```

---

## Solution 5: FastAPI WebSocket with JWT Authentication

```python
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, status
from fastapi.websockets import WebSocketState
import asyncio
import json
import time

app = FastAPI()

async def decode_jwt(token: str) -> dict | None:
    """Decode and verify JWT. Returns claims dict or None."""
    try:
        # Use PyJWT in production: jwt.decode(token, SECRET, algorithms=["HS256"])
        import base64
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        if claims.get("exp", 0) < time.time():
            return None
        return claims
    except Exception:
        return None

class ConnectionManager:
    def __init__(self):
        self._active: dict[str, WebSocket] = {}

    async def connect(self, user_id: str, ws: WebSocket):
        await ws.accept()
        self._active[user_id] = ws

    def disconnect(self, user_id: str):
        self._active.pop(user_id, None)

    async def broadcast(self, message: dict, exclude: str | None = None):
        dead = []
        for uid, ws in self._active.items():
            if uid == exclude:
                continue
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(uid)
        for uid in dead:
            self.disconnect(uid)

manager = ConnectionManager()

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # Step 1: Accept but don't start processing until authenticated
    await ws.accept()

    # Step 2: Expect auth message within 5 seconds
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=5.0)
        msg = json.loads(raw)
    except (asyncio.TimeoutError, json.JSONDecodeError):
        await ws.close(code=4001)
        return

    token = msg.get("token", "")
    claims = await decode_jwt(token)
    if not claims:
        await ws.close(code=4004)
        return

    user_id = claims.get("sub", "")
    await ws.send_json({"type": "auth_ok", "user_id": user_id})

    manager.disconnect(user_id)  # disconnect stale session if any
    await manager.connect(user_id, ws)

    try:
        while True:
            data = await ws.receive_text()
            # Echo back with user context
            await ws.send_json({"type": "message", "from": user_id, "data": data})
    except WebSocketDisconnect:
        manager.disconnect(user_id)
        print(f"[WS] Disconnected: {user_id}")
    except Exception as e:
        manager.disconnect(user_id)
        print(f"[WS] Error for {user_id}: {e}")
```

---

## Solution 6: WebSocket Session Revocation

```python
import asyncio
import time
from typing import Callable, Awaitable

class RevocableWSSession:
    """
    WebSocket session that can be remotely terminated when:
    - User logs out from another device
    - Admin revokes access
    - Token is blacklisted after security incident
    """

    def __init__(self, ws, user_id: str, session_id: str,
                 revocation_check: Callable[[str, str], Awaitable[bool]],
                 check_interval: float = 30.0):
        self._ws = ws
        self._user_id = user_id
        self._session_id = session_id
        self._revocation_check = revocation_check  # (user_id, session_id) -> is_revoked
        self._check_interval = check_interval
        self._revoked = False

    async def run(self, message_handler: Callable[[str, str], Awaitable[None]]):
        revocation_task = asyncio.create_task(self._revocation_poll())
        try:
            from aiohttp import WSMsgType
            async for msg in self._ws:
                if self._revoked:
                    await self._ws.close(code=4009, message=b"Session revoked")
                    return
                if msg.type == WSMsgType.TEXT:
                    await message_handler(self._user_id, msg.data)
        finally:
            revocation_task.cancel()

    async def _revocation_poll(self):
        while not self._revoked:
            await asyncio.sleep(self._check_interval)
            try:
                is_revoked = await self._revocation_check(
                    self._user_id, self._session_id
                )
                if is_revoked:
                    self._revoked = True
                    await self._ws.close(code=4009, message=b"Session revoked")
                    print(f"[WS] Session revoked: user={self._user_id} "
                          f"session={self._session_id}")
            except Exception as e:
                print(f"[WS] Revocation check failed: {e}")

# Revocation store (Redis in production)
class InMemoryRevocationStore:
    def __init__(self):
        self._revoked: set[str] = set()

    def revoke_session(self, session_id: str):
        self._revoked.add(session_id)

    def revoke_user(self, user_id: str):
        # Wildcard: revoke all sessions for user (store user_id as pattern)
        self._revoked.add(f"user:{user_id}")

    async def is_revoked(self, user_id: str, session_id: str) -> bool:
        return (session_id in self._revoked or
                f"user:{user_id}" in self._revoked)
```

---

## Comparison

| Solution | Token Exposure | Expiry Handling | CSRF Safe | Revocation | Complexity | Best For |
|---|---|---|---|---|---|---|
| 1. First-message auth | None (no query string) | At connect | Depends on origin check | No | Low | Simple authenticated streams |
| 2. Per-message MAC | None | Per message | Yes (MAC covers payload) | No | Med | High-security message integrity |
| 3. Token refresh | None | Proactive refresh | No | On refresh fail | Med | Long-lived streaming sessions |
| 4. Ticket exchange | Ticket (short-lived) | At ticket issuance | Yes (one-time ticket) | Implicit (ticket TTL) | Med | Standard browser WebSockets |
| 5. FastAPI + JWT | None | At auth message | No | No | Low | FastAPI services |
| 6. Revocation polling | None | Via revocation check | No | Yes | Med | Multi-device logout, admin control |

**Key principle**: never put session tokens or JWTs in WebSocket query strings — they appear in access logs, browser history, and referrer headers. Use the ticket-exchange pattern (short-lived one-time token issued by a REST endpoint) or first-message authentication. For long-lived connections, implement proactive token refresh and revocation polling.
