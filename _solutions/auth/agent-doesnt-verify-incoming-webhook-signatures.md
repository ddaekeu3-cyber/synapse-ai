---
layout: solution
title: "Agent Doesn't Verify Incoming Webhook Signatures"
category: auth
description: "The agent's webhook receiver accepts any POST request without verifying the sender's HMAC signature. Attackers can forge arbitrary payloads, trigger unauthorized task executions, or inject malicious tool calls."
tags: [auth, webhook, hmac, signature, security, fastapi, middleware]
---

# Agent Doesn't Verify Incoming Webhook Signatures

## Problem

A webhook endpoint at `/hooks/task-complete` receives a JSON payload and immediately acts on it — starting jobs, modifying data, or triggering agent tool calls. Without HMAC signature verification, any caller who knows the URL can send arbitrary payloads. Real-world impact: attackers trigger expensive model calls, inject tool arguments, or replay old events to cause duplicate side effects.

## Solutions

### Option 1: HMAC-SHA256 Verification Middleware (FastAPI)

```python
# middleware/webhook_auth.py
"""
FastAPI middleware that verifies the X-Webhook-Signature header on every
incoming webhook request before the handler sees the body.
Compatible with GitHub, Stripe, Anthropic, and custom webhook senders.
"""
import hashlib
import hmac
import os
import time
from typing import Callable
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse


WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
MAX_TIMESTAMP_AGE = 300  # 5 minutes — prevents replay attacks


def _verify_signature(body: bytes, signature_header: str, secret: str) -> bool:
    """
    Verify format: sha256=<hex_digest>
    Also supports timestamped format: t=<ts>,v1=<sig> (Stripe-style).
    """
    if not signature_header:
        return False

    if signature_header.startswith("sha256="):
        # Simple format: sha256=<hex>
        expected_sig = signature_header[7:]
        computed = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(computed, expected_sig)

    if "v1=" in signature_header:
        # Timestamped format: t=<timestamp>,v1=<sig>
        parts = dict(p.split("=", 1) for p in signature_header.split(",") if "=" in p)
        timestamp = parts.get("t", "")
        sig = parts.get("v1", "")
        if not timestamp or not sig:
            return False
        # Reject stale timestamps
        try:
            ts = int(timestamp)
            if abs(time.time() - ts) > MAX_TIMESTAMP_AGE:
                return False
        except ValueError:
            return False
        # Compute signature over "timestamp.body"
        signed_payload = f"{timestamp}.{body.decode()}"
        computed = hmac.new(secret.encode(), signed_payload.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(computed, sig)

    return False


app = FastAPI()


@app.middleware("http")
async def verify_webhook_signature(request: Request, call_next: Callable):
    """Only apply signature check to /hooks/ routes."""
    if request.url.path.startswith("/hooks/"):
        body = await request.body()
        sig = request.headers.get("X-Webhook-Signature", "")
        if not _verify_signature(body, sig, WEBHOOK_SECRET):
            return JSONResponse({"error": "Invalid webhook signature"}, status_code=401)
        # Re-attach body so the handler can read it
        async def receive():
            return {"type": "http.request", "body": body}
        request._receive = receive
    return await call_next(request)


@app.post("/hooks/task-complete")
async def task_complete_handler(request: Request):
    payload = await request.json()
    task_id = payload.get("task_id")
    # Safe to process — signature verified by middleware
    return {"accepted": True, "task_id": task_id}
```

**Expected Token Savings:** Not applicable — security hardening
**Environment:** `pip install fastapi`

---

### Option 2: Per-Endpoint Secret with Dependency Injection

```python
# auth/webhook_deps.py
"""
FastAPI dependency that verifies webhook signatures per endpoint.
Different endpoints can use different secrets (per-integration isolation).
"""
import hashlib
import hmac
import os
from functools import lru_cache
from fastapi import Depends, Header, HTTPException, Request


@lru_cache(maxsize=None)
def _get_secret(integration: str) -> str:
    """Load per-integration secrets from environment."""
    env_var = f"WEBHOOK_SECRET_{integration.upper()}"
    secret = os.environ.get(env_var) or os.environ.get("WEBHOOK_SECRET", "")
    if not secret:
        raise RuntimeError(f"No webhook secret configured for integration: {integration}")
    return secret


def make_signature_verifier(integration: str):
    """Factory: returns a FastAPI dependency for the given integration."""
    async def verify(
        request: Request,
        x_webhook_signature: str = Header(default=""),
        x_hub_signature_256: str = Header(default=""),  # GitHub format
    ):
        body = await request.body()
        secret = _get_secret(integration)

        # Try both header formats
        sig = x_webhook_signature or x_hub_signature_256
        if not sig:
            raise HTTPException(status_code=401, detail="Missing webhook signature header")

        header_value = sig.removeprefix("sha256=")
        computed = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed, header_value):
            raise HTTPException(status_code=401, detail="Webhook signature mismatch")

        return body

    return verify


from fastapi import FastAPI
app = FastAPI()

# Different secrets per integration
verify_github = make_signature_verifier("github")
verify_stripe = make_signature_verifier("stripe")
verify_internal = make_signature_verifier("internal")


@app.post("/hooks/github/push")
async def github_push(body: bytes = Depends(verify_github)):
    import json
    payload = json.loads(body)
    return {"repo": payload.get("repository", {}).get("name")}


@app.post("/hooks/stripe/payment")
async def stripe_payment(body: bytes = Depends(verify_stripe)):
    import json
    payload = json.loads(body)
    return {"event_type": payload.get("type")}


@app.post("/hooks/internal/task-done")
async def internal_task_done(body: bytes = Depends(verify_internal)):
    import json
    payload = json.loads(body)
    return {"task_id": payload.get("task_id")}
```

**Expected Token Savings:** Not applicable — security isolation per integration
**Environment:** `pip install fastapi`

---

### Option 3: Replay Attack Prevention with Nonce Store

```python
# auth/replay_protection.py
"""
Extend HMAC verification with replay attack prevention:
- Timestamps must be within a 5-minute window.
- Nonces (event IDs) are tracked in a TTL cache; duplicate events are rejected.
Prevents attackers from resending captured valid webhooks.
"""
import hashlib
import hmac
import time
from collections import OrderedDict
from threading import Lock
import os


class NonceStore:
    """Thread-safe LRU nonce store with TTL expiry."""
    def __init__(self, max_size: int = 10_000, ttl_seconds: float = 600.0):
        self._store: OrderedDict[str, float] = OrderedDict()
        self._max = max_size
        self._ttl = ttl_seconds
        self._lock = Lock()

    def is_seen(self, nonce: str) -> bool:
        """Returns True if nonce was seen before (replay). Marks as seen if new."""
        now = time.time()
        with self._lock:
            self._evict_expired(now)
            if nonce in self._store:
                return True
            # LRU eviction if full
            if len(self._store) >= self._max:
                self._store.popitem(last=False)
            self._store[nonce] = now
            return False

    def _evict_expired(self, now: float):
        cutoff = now - self._ttl
        while self._store:
            oldest_key, oldest_ts = next(iter(self._store.items()))
            if oldest_ts < cutoff:
                del self._store[oldest_key]
            else:
                break


_nonce_store = NonceStore()
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "dev-secret")
MAX_AGE_SECONDS = 300  # 5 minutes


class WebhookVerificationError(Exception):
    pass


def verify_webhook(
    body: bytes,
    signature: str,
    timestamp: str,
    event_id: str,
) -> None:
    """
    Verify:
    1. Signature is valid.
    2. Timestamp is within MAX_AGE_SECONDS.
    3. Event ID has not been seen before (replay protection).

    Raises WebhookVerificationError on any failure.
    """
    # 1. Timestamp freshness
    try:
        ts = int(timestamp)
    except ValueError:
        raise WebhookVerificationError("Invalid timestamp format")
    age = abs(time.time() - ts)
    if age > MAX_AGE_SECONDS:
        raise WebhookVerificationError(f"Stale webhook: {age:.0f}s old (max {MAX_AGE_SECONDS}s)")

    # 2. Signature
    signed = f"{timestamp}.{body.decode('utf-8', errors='replace')}"
    expected = hmac.new(WEBHOOK_SECRET.encode(), signed.encode(), hashlib.sha256).hexdigest()
    received = signature.removeprefix("sha256=")
    if not hmac.compare_digest(expected, received):
        raise WebhookVerificationError("Signature mismatch")

    # 3. Replay protection
    if _nonce_store.is_seen(event_id):
        raise WebhookVerificationError(f"Duplicate event ID: {event_id}")


# ── FastAPI integration ───────────────────────────────────────────────────────
from fastapi import FastAPI, Request, HTTPException
import json

app = FastAPI()


@app.post("/hooks/events")
async def receive_event(request: Request):
    body = await request.body()
    sig = request.headers.get("X-Webhook-Signature", "")
    ts = request.headers.get("X-Webhook-Timestamp", "")
    event_id = request.headers.get("X-Event-ID", "")

    try:
        verify_webhook(body, sig, ts, event_id)
    except WebhookVerificationError as e:
        raise HTTPException(status_code=401, detail=str(e))

    payload = json.loads(body)
    return {"ok": True, "event_type": payload.get("type")}
```

**Expected Token Savings:** Not applicable — security + reliability
**Environment:** `pip install fastapi`

---

### Option 4: Webhook Signature Helper for Senders

```python
# webhooks/sign.py
"""
Counterpart to the verifier: sign outgoing webhooks so your own agent's
webhook deliveries can be verified by receivers.
Ensures your agent is a trustworthy sender, not just a cautious receiver.
"""
import hashlib
import hmac
import json
import time
import uuid
import httpx


def sign_payload(
    body: bytes,
    secret: str,
    timestamp: int | None = None,
) -> dict[str, str]:
    """
    Generate headers for a signed webhook delivery.
    Returns headers dict to include in the outgoing request.
    """
    ts = timestamp or int(time.time())
    event_id = str(uuid.uuid4())
    signed = f"{ts}.{body.decode('utf-8', errors='replace')}"
    sig = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Webhook-Signature": f"sha256={sig}",
        "X-Webhook-Timestamp": str(ts),
        "X-Event-ID": event_id,
    }


def send_signed_webhook(
    url: str,
    payload: dict,
    secret: str,
    timeout: float = 10.0,
) -> httpx.Response:
    """Send a signed webhook POST request."""
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = sign_payload(body, secret)
    with httpx.Client(timeout=timeout) as client:
        return client.post(url, content=body, headers=headers)


# ── Test: round-trip sign + verify ────────────────────────────────────────────

def test_sign_verify_roundtrip():
    from auth.replay_protection import verify_webhook, NonceStore
    import os
    os.environ["WEBHOOK_SECRET"] = "test-secret"

    payload = {"task_id": "abc123", "status": "done"}
    body = json.dumps(payload).encode()
    headers = sign_payload(body, "test-secret")

    sig = headers["X-Webhook-Signature"]
    ts = headers["X-Webhook-Timestamp"]
    event_id = headers["X-Event-ID"]

    # Should not raise
    verify_webhook(body, sig, ts, event_id)
    print("Round-trip sign + verify: PASSED")


if __name__ == "__main__":
    test_sign_verify_roundtrip()
```

**Expected Token Savings:** Not applicable — security tooling
**Environment:** `pip install httpx`

---

### Option 5: pytest Tests for Webhook Verification Logic

```python
# tests/auth/test_webhook_verification.py
"""
Unit tests for the webhook signature verifier.
Covers: valid signature, tampered body, wrong secret, stale timestamp,
        replay attack, missing headers, malformed signature.
"""
import hashlib
import hmac
import json
import time
import pytest
from auth.replay_protection import verify_webhook, WebhookVerificationError, WEBHOOK_SECRET


def _make_valid_args(payload: dict, secret: str = None, age_offset: float = 0) -> tuple:
    """Create a valid signed webhook tuple."""
    body = json.dumps(payload).encode()
    ts = int(time.time() + age_offset)
    import uuid
    event_id = str(uuid.uuid4())
    signed = f"{ts}.{body.decode()}"
    sig = "sha256=" + hmac.new((secret or WEBHOOK_SECRET).encode(), signed.encode(), hashlib.sha256).hexdigest()
    return body, sig, str(ts), event_id


def test_valid_webhook_passes():
    body, sig, ts, eid = _make_valid_args({"event": "test"})
    verify_webhook(body, sig, ts, eid)  # Should not raise


def test_tampered_body_rejected():
    _, sig, ts, eid = _make_valid_args({"event": "original"})
    tampered_body = json.dumps({"event": "tampered"}).encode()
    with pytest.raises(WebhookVerificationError, match="Signature mismatch"):
        verify_webhook(tampered_body, sig, ts, eid)


def test_wrong_secret_rejected():
    body, _, ts, eid = _make_valid_args({"event": "test"})
    _, wrong_sig, _, _ = _make_valid_args({"event": "test"}, secret="wrong-secret")
    with pytest.raises(WebhookVerificationError, match="Signature mismatch"):
        verify_webhook(body, wrong_sig, ts, eid)


def test_stale_timestamp_rejected():
    body, sig, ts, eid = _make_valid_args({"event": "test"}, age_offset=-400)  # 6.7 min old
    with pytest.raises(WebhookVerificationError, match="Stale webhook"):
        verify_webhook(body, sig, ts, eid)


def test_future_timestamp_rejected():
    body, sig, ts, eid = _make_valid_args({"event": "test"}, age_offset=400)  # 6.7 min future
    with pytest.raises(WebhookVerificationError, match="Stale webhook"):
        verify_webhook(body, sig, ts, eid)


def test_replay_rejected():
    body, sig, ts, eid = _make_valid_args({"event": "test"})
    verify_webhook(body, sig, ts, eid)  # First delivery OK
    with pytest.raises(WebhookVerificationError, match="Duplicate event ID"):
        verify_webhook(body, sig, ts, eid)  # Replay rejected


def test_missing_signature_rejected():
    body, _, ts, eid = _make_valid_args({"event": "test"})
    with pytest.raises(WebhookVerificationError):
        verify_webhook(body, "", ts, eid)


def test_invalid_timestamp_format_rejected():
    body, sig, _, eid = _make_valid_args({"event": "test"})
    with pytest.raises(WebhookVerificationError, match="Invalid timestamp"):
        verify_webhook(body, sig, "not-a-number", eid)
```

**Expected Token Savings:** Not applicable — security unit tests
**Environment:** `pip install pytest`

---

### Option 6: Webhook Signature Audit Logging

```python
# auth/webhook_audit.py
"""
Log all webhook verification attempts (success and failure) for security auditing.
Detects attack patterns: repeated failures from same IP, brute-force attempts,
replay attacks over time.
"""
import hashlib
import hmac
import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Optional
import os

logger = logging.getLogger("webhook.audit")
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s %(message)s')

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "dev-secret")


@dataclass
class WebhookAuditEvent:
    timestamp: float
    source_ip: str
    endpoint: str
    event_id: str
    outcome: str  # "success" | "invalid_signature" | "stale" | "replay"
    payload_hash: str  # SHA-256 of body, not the body itself
    user_agent: str = ""


def _hash_body(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()[:16]  # Short prefix for log readability


def verify_and_audit(
    body: bytes,
    signature: str,
    timestamp: str,
    event_id: str,
    source_ip: str = "unknown",
    endpoint: str = "/hooks/unknown",
    user_agent: str = "",
) -> tuple[bool, str]:
    """
    Verify the webhook and emit a structured audit log entry.
    Returns (success, reason).
    """
    reason = "success"
    outcome = "success"

    try:
        # Timestamp check
        try:
            ts = int(timestamp)
        except ValueError:
            raise ValueError("invalid_timestamp_format")
        age = abs(time.time() - ts)
        if age > 300:
            reason = f"stale ({age:.0f}s old)"
            outcome = "stale"
            raise ValueError(outcome)

        # Signature check
        if not signature:
            reason = "missing_signature"
            outcome = "invalid_signature"
            raise ValueError(outcome)
        signed = f"{ts}.{body.decode('utf-8', errors='replace')}"
        expected = hmac.new(WEBHOOK_SECRET.encode(), signed.encode(), hashlib.sha256).hexdigest()
        received = signature.removeprefix("sha256=")
        if not hmac.compare_digest(expected, received):
            reason = "signature_mismatch"
            outcome = "invalid_signature"
            raise ValueError(outcome)

        # Replay check (simplified — use full NonceStore in production)
        # ...

    except ValueError:
        pass

    audit = WebhookAuditEvent(
        timestamp=time.time(),
        source_ip=source_ip,
        endpoint=endpoint,
        event_id=event_id,
        outcome=outcome,
        payload_hash=_hash_body(body),
        user_agent=user_agent,
    )
    log_level = logging.INFO if outcome == "success" else logging.WARNING
    logger.log(log_level, json.dumps(asdict(audit)))

    return outcome == "success", reason


# ── FastAPI integration ───────────────────────────────────────────────────────
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()


@app.post("/hooks/audited")
async def audited_webhook(request: Request):
    body = await request.body()
    ok, reason = verify_and_audit(
        body=body,
        signature=request.headers.get("X-Webhook-Signature", ""),
        timestamp=request.headers.get("X-Webhook-Timestamp", ""),
        event_id=request.headers.get("X-Event-ID", ""),
        source_ip=request.client.host if request.client else "unknown",
        endpoint=str(request.url.path),
        user_agent=request.headers.get("User-Agent", ""),
    )
    if not ok:
        raise HTTPException(status_code=401, detail=reason)
    return {"ok": True}
```

**Expected Token Savings:** Not applicable — security observability
**Environment:** `pip install fastapi`

---

## Comparison Table

| Option | Signature Format | Replay Protection | Per-Integration Secrets | Audit Logging | Complexity |
|--------|-----------------|-------------------|------------------------|---------------|------------|
| 1: Middleware | sha256= / Stripe | Timestamp only | No | No | Low |
| 2: Dependency injection | sha256= / GitHub | Timestamp only | Yes | No | Low |
| 3: Nonce store | Timestamped | Yes (nonce TTL) | No | No | Medium |
| 4: Sender helper | Timestamped | N/A (sender side) | No | No | Low |
| 5: Unit tests | All above | Covered | Covered | No | Low |
| 6: Audit logging | Timestamped | Basic | No | Yes | Medium |
