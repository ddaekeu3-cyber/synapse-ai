---
layout: solution
title: "Agent Doesn't Implement Webhook Signature Verification"
category: general
description: "Agent accepts webhook payloads from third-party services without verifying HMAC signatures, allowing any caller to forge events and trigger arbitrary agent actions."
tags: [security, webhooks, hmac, authentication, prompt-injection]
---

## Symptom

The agent processes webhook events blindly. An attacker sends a crafted POST to the webhook endpoint with a fake `user_id`, `event_type`, or even injected prompt content, and the agent acts on it without question.

```bash
# Attacker sends forged event — no signature required
curl -X POST https://your-agent.com/webhook \
  -H "Content-Type: application/json" \
  -d '{"event": "payment.refunded", "amount": 99999, "user_id": "victim-123"}'
```

The agent executes the refund, sends a notification, or injects the payload into an LLM prompt — all without verifying the request came from the legitimate service.

## Root Cause

Webhook handlers skip signature verification:

```python
from fastapi import FastAPI, Request
import anthropic

app = FastAPI()
client = anthropic.Anthropic(api_key="sk-live-...")

@app.post("/webhook")
async def handle_webhook(request: Request):
    payload = await request.json()
    # No signature check — accepts any POST from anyone
    event_type = payload["event"]
    user_id = payload["user_id"]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"Handle {event_type} for user {user_id}"}]
    )
    return {"ok": True}
```

This is especially dangerous when webhook payloads flow into LLM prompts — a forged event becomes a prompt injection vector.

---

## Fix

### Option 1 — HMAC-SHA256 signature verification (Stripe-style)

Verify the `X-Signature` header against an HMAC-SHA256 digest of the raw request body using your shared secret.

```python
import hashlib
import hmac
import os
from fastapi import FastAPI, Request, HTTPException
import anthropic

app = FastAPI()
client = anthropic.Anthropic(api_key="sk-live-...")

WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]  # e.g. "whsec_abc123..."


def verify_signature(payload: bytes, signature_header: str, secret: str) -> bool:
    """
    Verify HMAC-SHA256 signature.
    Expected header format: "sha256=<hex_digest>"
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected_sig = signature_header[len("sha256="):]
    computed_sig = hmac.new(
        secret.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()

    # Use hmac.compare_digest to prevent timing attacks
    return hmac.compare_digest(computed_sig, expected_sig)


@app.post("/webhook")
async def handle_webhook(request: Request):
    raw_body = await request.body()
    signature = request.headers.get("X-Signature", "")

    if not verify_signature(raw_body, signature, WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload = await request.json()  # safe to parse after verification
    event_type = payload.get("event", "unknown")
    user_id = payload.get("user_id", "unknown")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"Handle event: {event_type} for user: {user_id}"}]
    )
    return {"ok": True, "result": response.content[0].text}

# Expected Token Savings: prevents forged events from consuming API budget
# Environment: any FastAPI/Flask agent receiving webhooks from external services
```

---

### Option 2 — Timestamp-inclusive signature with replay protection

Include the request timestamp in the signed payload and reject requests older than 5 minutes, preventing replay attacks.

```python
import hashlib
import hmac
import os
import time
from fastapi import FastAPI, Request, HTTPException
import anthropic

app = FastAPI()
client = anthropic.Anthropic(api_key="sk-live-...")

WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
MAX_TIMESTAMP_DRIFT_SECONDS = 300  # 5 minutes


def verify_with_replay_protection(
    raw_body: bytes,
    timestamp_header: str,
    signature_header: str,
    secret: str,
) -> bool:
    # 1. Validate timestamp exists and is recent
    try:
        request_time = int(timestamp_header)
    except (TypeError, ValueError):
        return False

    age = abs(time.time() - request_time)
    if age > MAX_TIMESTAMP_DRIFT_SECONDS:
        return False  # Reject replayed or future-dated requests

    # 2. Signed payload = timestamp + "." + body (same as Stripe's v1 scheme)
    signed_payload = f"{timestamp_header}.".encode() + raw_body
    expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()

    if not signature_header:
        return False

    return hmac.compare_digest(expected, signature_header)


@app.post("/webhook")
async def handle_webhook(request: Request):
    raw_body = await request.body()
    timestamp = request.headers.get("X-Timestamp", "")
    signature = request.headers.get("X-Signature", "")

    if not verify_with_replay_protection(raw_body, timestamp, signature, WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid or expired webhook signature")

    import json
    payload = json.loads(raw_body)
    event_type = payload.get("event", "")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": f"Summarise this event: {event_type}"}]
    )
    return {"summary": response.content[0].text}

# Expected Token Savings: blocks replayed events from re-triggering expensive LLM calls
# Environment: production webhooks where replay attacks are a concern
```

---

### Option 3 — Provider-specific verification (GitHub, Stripe, Slack)

Each provider has its own signature scheme. Use provider-specific verification logic.

```python
import hashlib
import hmac
import os
import time
from fastapi import FastAPI, Request, HTTPException
import anthropic

app = FastAPI()
client = anthropic.Anthropic(api_key="sk-live-...")


def verify_github(raw_body: bytes, signature: str, secret: str) -> bool:
    """GitHub uses X-Hub-Signature-256: sha256=<hex>"""
    if not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_stripe(raw_body: bytes, signature_header: str, secret: str) -> bool:
    """Stripe uses Stripe-Signature: t=<ts>,v1=<sig>"""
    try:
        parts = dict(p.split("=", 1) for p in signature_header.split(","))
        timestamp = parts["t"]
        sig_v1 = parts["v1"]
    except (KeyError, ValueError):
        return False

    if abs(time.time() - int(timestamp)) > 300:
        return False

    signed = f"{timestamp}.".encode() + raw_body
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig_v1)


def verify_slack(raw_body: bytes, timestamp: str, signature: str, secret: str) -> bool:
    """Slack uses X-Slack-Signature: v0=<sig> with X-Slack-Request-Timestamp"""
    if abs(time.time() - int(timestamp)) > 300:
        return False
    basestring = f"v0:{timestamp}:".encode() + raw_body
    expected = "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


VERIFIERS = {
    "github":  (verify_github, "X-Hub-Signature-256", None, "GITHUB_WEBHOOK_SECRET"),
    "stripe":  (verify_stripe, "Stripe-Signature", None, "STRIPE_WEBHOOK_SECRET"),
    "slack":   (verify_slack, "X-Slack-Signature", "X-Slack-Request-Timestamp", "SLACK_SIGNING_SECRET"),
}


@app.post("/webhook/{provider}")
async def handle_webhook(provider: str, request: Request):
    raw_body = await request.body()

    if provider not in VERIFIERS:
        raise HTTPException(status_code=400, detail="Unknown provider")

    verify_fn, sig_header, ts_header, secret_env = VERIFIERS[provider]
    secret = os.environ.get(secret_env, "")
    signature = request.headers.get(sig_header, "")
    timestamp = request.headers.get(ts_header, "") if ts_header else ""

    args = (raw_body, timestamp, signature, secret) if ts_header else (raw_body, signature, secret)
    if not verify_fn(*args):
        raise HTTPException(status_code=401, detail=f"Invalid {provider} signature")

    import json
    payload = json.loads(raw_body)
    return {"provider": provider, "verified": True, "event": payload.get("type") or payload.get("event")}

# Expected Token Savings: blocks forged events per provider; one endpoint handles all
# Environment: agents integrated with GitHub, Stripe, or Slack webhooks
```

---

### Option 4 — Decorator-based verification middleware

Wrap any webhook handler with a reusable `@require_webhook_signature` decorator.

```python
import hashlib
import hmac
import os
import functools
from fastapi import FastAPI, Request, HTTPException
import anthropic

app = FastAPI()
client = anthropic.Anthropic(api_key="sk-live-...")

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "dev-secret")


def require_webhook_signature(secret_env: str = "WEBHOOK_SECRET", header: str = "X-Signature"):
    """Decorator that verifies HMAC-SHA256 signature before calling the handler."""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(request: Request, *args, **kwargs):
            raw_body = await request.body()
            secret = os.environ.get(secret_env, "")
            signature = request.headers.get(header, "")

            if not signature:
                raise HTTPException(status_code=401, detail="Missing signature header")

            computed = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(computed, signature.removeprefix("sha256=")):
                raise HTTPException(status_code=401, detail="Signature mismatch")

            return await func(request, *args, **kwargs)
        return wrapper
    return decorator


@app.post("/webhook/payments")
@require_webhook_signature(secret_env="PAYMENT_WEBHOOK_SECRET")
async def payment_webhook(request: Request):
    import json
    payload = json.loads(await request.body())
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": f"Process payment event: {payload.get('event')}"}]
    )
    return {"result": response.content[0].text}


@app.post("/webhook/users")
@require_webhook_signature(secret_env="USER_WEBHOOK_SECRET")
async def user_webhook(request: Request):
    import json
    payload = json.loads(await request.body())
    return {"received": payload.get("event")}

# Expected Token Savings: reusable decorator — zero boilerplate per handler
# Environment: multi-endpoint agents with several webhook sources
```

---

### Option 5 — Signature verification + prompt injection guard

After verifying the signature, sanitise the payload before injecting into an LLM prompt to prevent second-order prompt injection via legitimate but attacker-controlled webhook data.

```python
import hashlib
import hmac
import json
import os
import re
from fastapi import FastAPI, Request, HTTPException
import anthropic

app = FastAPI()
client = anthropic.Anthropic(api_key="sk-live-...")

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "dev-secret")

PROMPT_INJECTION_PATTERNS = [
    r"ignore (all |previous |above |prior )?instructions",
    r"new instructions?:",
    r"system prompt",
    r"jailbreak",
    r"<\|.*?\|>",       # token boundary injection
    r"\[INST\]",        # Llama-style instruction tags
]


def verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    if not signature:
        return False
    computed = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, signature.removeprefix("sha256="))


def sanitise_for_prompt(value: str, max_length: int = 200) -> str:
    """Remove prompt injection patterns and truncate."""
    for pattern in PROMPT_INJECTION_PATTERNS:
        value = re.sub(pattern, "[REDACTED]", value, flags=re.IGNORECASE)
    return value[:max_length]


def safe_event_summary(payload: dict) -> str:
    event_type = sanitise_for_prompt(str(payload.get("event", "unknown")))
    user_id = sanitise_for_prompt(str(payload.get("user_id", "unknown")))
    amount = sanitise_for_prompt(str(payload.get("amount", "")))
    return f"Event: {event_type} | User: {user_id} | Amount: {amount}"


@app.post("/webhook")
async def handle_webhook(request: Request):
    raw_body = await request.body()
    signature = request.headers.get("X-Signature", "")

    if not verify_signature(raw_body, signature, WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = json.loads(raw_body)
    summary = safe_event_summary(payload)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system="You are an event processor. Summarise and log the incoming event.",
        messages=[{"role": "user", "content": summary}]
    )
    return {"result": response.content[0].text}

# Expected Token Savings: prevents prompt injection from turning one malicious event
#   into a multi-turn attack that burns tokens on attacker-directed tasks
# Environment: any agent that injects webhook payload fields into LLM prompts
```

---

### Option 6 — Async signature verification with request ID deduplication

Verify signatures asynchronously and reject duplicate request IDs to prevent replay attacks without a central timestamp check.

```python
import hashlib
import hmac
import os
import asyncio
from fastapi import FastAPI, Request, HTTPException
import anthropic

app = FastAPI()
client = anthropic.AsyncAnthropic(api_key="sk-live-...")

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "dev-secret")

# In-memory dedup set — use Redis in production
_seen_request_ids: set[str] = set()
_seen_lock = asyncio.Lock()
MAX_SEEN = 10_000  # evict when full


async def is_duplicate(request_id: str) -> bool:
    async with _seen_lock:
        if request_id in _seen_request_ids:
            return True
        if len(_seen_request_ids) >= MAX_SEEN:
            # Evict oldest 20% (approximation — use LRU in production)
            to_remove = list(_seen_request_ids)[:MAX_SEEN // 5]
            for rid in to_remove:
                _seen_request_ids.discard(rid)
        _seen_request_ids.add(request_id)
        return False


@app.post("/webhook")
async def handle_webhook(request: Request):
    raw_body = await request.body()
    signature = request.headers.get("X-Signature", "")
    request_id = request.headers.get("X-Request-Id", "")

    # 1. Verify signature
    if not signature:
        raise HTTPException(status_code=401, detail="Missing signature")

    computed = hmac.new(WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, signature.removeprefix("sha256=")):
        raise HTTPException(status_code=401, detail="Invalid signature")

    # 2. Deduplication
    if request_id and await is_duplicate(request_id):
        return {"ok": True, "deduplicated": True}

    import json
    payload = json.loads(raw_body)

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"Process: {payload.get('event')}"}]
    )
    return {"result": response.content[0].text, "request_id": request_id}

# Expected Token Savings: deduplication prevents retried webhooks from triggering
#   duplicate LLM calls; signature check prevents all forged events
# Environment: async FastAPI agents where webhook providers retry on network errors
```

---

## Comparison

| Option | Signature Verification | Replay Protection | Injection Guard | Multi-Provider | Async |
|--------|----------------------|-------------------|-----------------|----------------|-------|
| 1 | HMAC-SHA256 | No | No | No | No |
| 2 | HMAC + timestamp | Yes (5 min window) | No | No | No |
| 3 | Provider-specific | Yes (Stripe/Slack) | No | Yes | No |
| 4 | Decorator (HMAC) | No | No | No | No |
| 5 | HMAC | No | Yes | No | No |
| 6 | HMAC | Yes (dedup IDs) | No | No | Yes |

**Recommended starting point:** Option 1 for any new webhook handler — add the 8-line `verify_signature()` check immediately. Add Option 2's timestamp window for production. Add Option 5's injection guard whenever webhook payload fields flow into LLM prompts.
