---
layout: solution
title: "Agent Doesn't Implement Request Signing for Webhook Callbacks"
category: auth
description: "Sign outbound webhook payloads with HMAC-SHA256 and verify inbound webhook signatures to prevent spoofing, replay attacks, and unauthorized payload injection."
tags: [webhook, hmac, request-signing, security, authentication]
---

# Agent Doesn't Implement Request Signing for Webhook Callbacks

## Problem

Agents that send or receive webhooks without signature verification are vulnerable to payload spoofing (attacker sends fake events), replay attacks (attacker resends captured legitimate requests), and SSRF via malicious callback URLs.

## Solution Options

### Option 1: HMAC-SHA256 Outbound Signing

```python
import anthropic
import hashlib
import hmac
import json
import time
import urllib.request
import urllib.error
from dataclasses import dataclass

client = anthropic.Anthropic()

WEBHOOK_SECRET = "whsec_supersecretkey_changeinprod"

@dataclass
class SignedWebhookPayload:
    event_type: str
    data: dict
    timestamp: int
    signature: str

def sign_payload(payload: dict, secret: str) -> tuple[str, int]:
    """Generate HMAC-SHA256 signature with timestamp for replay protection."""
    timestamp = int(time.time())
    payload_str = json.dumps(payload, separators=(',', ':'), sort_keys=True)
    message = f"{timestamp}.{payload_str}"
    signature = hmac.new(
        secret.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={signature}", timestamp

def send_signed_webhook(url: str, event_type: str, data: dict) -> bool:
    payload = {"event": event_type, "data": data}
    signature_header, timestamp = sign_payload(payload, WEBHOOK_SECRET)

    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": signature_header,
            "X-Webhook-Timestamp": str(timestamp),
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except urllib.error.URLError as e:
        print(f"Webhook delivery failed: {e}")
        return False

def verify_incoming_webhook(body: bytes, signature_header: str, secret: str,
                              max_age_seconds: int = 300) -> bool:
    """Verify signature and reject replayed requests older than max_age_seconds."""
    try:
        parts = dict(p.split("=", 1) for p in signature_header.split(","))
        ts = int(parts["t"])
        provided_sig = parts["v1"]
    except (KeyError, ValueError):
        return False

    # Reject stale requests
    if abs(time.time() - ts) > max_age_seconds:
        print(f"Webhook rejected: timestamp {abs(time.time() - ts):.0f}s old")
        return False

    # Recompute signature
    payload_str = body.decode()
    message = f"{ts}.{payload_str}"
    expected_sig = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()

    return hmac.compare_digest(expected_sig, provided_sig)

# Demo: agent generates a response, then sends signed webhook notification
resp = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=128,
    messages=[{"role": "user", "content": "Summarize: webhooks enable event-driven integrations."}]
)
summary = resp.content[0].text

# Simulate signing
payload = {"task_id": "t_001", "result": summary[:100], "model": "claude-haiku-4-5-20251001"}
sig, ts = sign_payload(payload, WEBHOOK_SECRET)

# Simulate verification
body = json.dumps({"event": "task.complete", "data": payload}, separators=(',', ':')).encode()
is_valid = verify_incoming_webhook(body, sig, WEBHOOK_SECRET)
print(f"Signature valid: {is_valid}")
print(f"Signature header: {sig}")

# Expected Token Savings: N/A; prevents spoofed callbacks from triggering unauthorized agent actions
# Environment: agent-to-agent callbacks, CI/CD integrations, payment event handlers
```

### Option 2: Signed Request with Nonce-Based Replay Prevention

```python
import anthropic
import hashlib
import hmac
import json
import time
import uuid
from collections import OrderedDict

client = anthropic.Anthropic()

SECRET_KEY = "agent_webhook_secret_v1"

class NonceStore:
    """In-memory nonce store — use Redis in production."""
    def __init__(self, ttl_seconds: int = 300):
        self.store: OrderedDict[str, float] = OrderedDict()
        self.ttl = ttl_seconds

    def _evict_expired(self) -> None:
        cutoff = time.time() - self.ttl
        while self.store and next(iter(self.store.values())) < cutoff:
            self.store.popitem(last=False)

    def check_and_store(self, nonce: str) -> bool:
        """Returns True if nonce is fresh (not seen before)."""
        self._evict_expired()
        if nonce in self.store:
            return False
        self.store[nonce] = time.time()
        return True

nonce_store = NonceStore(ttl_seconds=300)

def build_signed_request(payload: dict, secret: str) -> dict:
    nonce = str(uuid.uuid4())
    timestamp = int(time.time())
    canonical = json.dumps(payload, separators=(',', ':'), sort_keys=True)
    signing_string = f"{timestamp}\n{nonce}\n{canonical}"
    signature = hmac.new(secret.encode(), signing_string.encode(), hashlib.sha256).hexdigest()
    return {
        "payload": payload,
        "auth": {
            "timestamp": timestamp,
            "nonce": nonce,
            "signature": signature,
            "version": "v1"
        }
    }

def verify_signed_request(signed_request: dict, secret: str, max_age: int = 300) -> tuple[bool, str]:
    auth = signed_request.get("auth", {})
    payload = signed_request.get("payload", {})

    ts = auth.get("timestamp", 0)
    nonce = auth.get("nonce", "")
    provided_sig = auth.get("signature", "")

    if abs(time.time() - ts) > max_age:
        return False, f"Request expired ({abs(time.time()-ts):.0f}s old)"

    if not nonce_store.check_and_store(nonce):
        return False, f"Replay attack detected: nonce {nonce[:8]} already used"

    canonical = json.dumps(payload, separators=(',', ':'), sort_keys=True)
    signing_string = f"{ts}\n{nonce}\n{canonical}"
    expected_sig = hmac.new(secret.encode(), signing_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected_sig, provided_sig):
        return False, "Invalid signature"

    return True, "OK"

# Simulate agent sending a callback with nonce
resp = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=64,
    messages=[{"role": "user", "content": "What is HMAC?"}]
)
callback_payload = {"result": resp.content[0].text[:80], "task_id": "task_42"}
signed = build_signed_request(callback_payload, SECRET_KEY)

valid, reason = verify_signed_request(signed, SECRET_KEY)
print(f"First request: {valid} ({reason})")

# Replay attack simulation
valid2, reason2 = verify_signed_request(signed, SECRET_KEY)
print(f"Replay attempt: {valid2} ({reason2})")

# Expected Token Savings: N/A; nonce store prevents replay at O(1) lookup cost
# Environment: financial webhooks, agent action confirmations, security-critical callbacks
```

### Option 3: Webhook Signature Middleware for FastAPI/Flask

```python
import anthropic
import hashlib
import hmac
import json
import time
from functools import wraps
from dataclasses import dataclass

client = anthropic.Anthropic()

WEBHOOK_SECRETS = {
    "github": "github_webhook_secret",
    "stripe": "stripe_webhook_secret",
    "internal": "internal_agent_secret",
}

@dataclass
class WebhookEvent:
    source: str
    event_type: str
    payload: dict
    verified: bool
    received_at: float

class WebhookVerifier:
    def __init__(self, secrets: dict[str, str], tolerance_seconds: int = 300):
        self.secrets = secrets
        self.tolerance = tolerance_seconds

    def verify(self, source: str, body: bytes, headers: dict) -> tuple[bool, str]:
        secret = self.secrets.get(source)
        if not secret:
            return False, f"Unknown webhook source: {source}"

        if source == "github":
            return self._verify_github_style(body, headers, secret)
        elif source == "stripe":
            return self._verify_stripe_style(body, headers, secret)
        else:
            return self._verify_internal_style(body, headers, secret)

    def _verify_github_style(self, body: bytes, headers: dict, secret: str) -> tuple[bool, str]:
        provided = headers.get("X-Hub-Signature-256", "")
        if not provided.startswith("sha256="):
            return False, "Missing or malformed signature"
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, provided), "OK" if hmac.compare_digest(expected, provided) else "Invalid"

    def _verify_stripe_style(self, body: bytes, headers: dict, secret: str) -> tuple[bool, str]:
        sig_header = headers.get("Stripe-Signature", "")
        parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
        ts = int(parts.get("t", 0))
        v1 = parts.get("v1", "")
        if abs(time.time() - ts) > self.tolerance:
            return False, "Timestamp outside tolerance"
        payload_str = f"{ts}.{body.decode()}"
        expected = hmac.new(secret.encode(), payload_str.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, v1), "OK" if hmac.compare_digest(expected, v1) else "Invalid"

    def _verify_internal_style(self, body: bytes, headers: dict, secret: str) -> tuple[bool, str]:
        provided = headers.get("X-Agent-Signature", "")
        ts_str = headers.get("X-Agent-Timestamp", "0")
        try:
            ts = int(ts_str)
        except ValueError:
            return False, "Invalid timestamp"
        if abs(time.time() - ts) > self.tolerance:
            return False, "Request too old"
        message = f"{ts}.{body.decode()}"
        expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, provided), "OK"

verifier = WebhookVerifier(WEBHOOK_SECRETS)

def simulate_incoming_webhook(source: str, payload: dict, tampered: bool = False) -> WebhookEvent:
    body = json.dumps(payload).encode()
    ts = int(time.time())
    secret = WEBHOOK_SECRETS.get(source, "")
    message = f"{ts}.{body.decode()}"
    sig = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()

    headers = {
        "X-Agent-Signature": sig if not tampered else "tampered_sig",
        "X-Agent-Timestamp": str(ts),
    }

    valid, reason = verifier.verify(source, body, headers)
    return WebhookEvent(
        source=source,
        event_type=payload.get("event", "unknown"),
        payload=payload,
        verified=valid,
        received_at=time.time()
    )

# Test legit and tampered webhooks
resp = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=64,
    messages=[{"role": "user", "content": "Summarize: agent task completed."}]
)
for tampered in [False, True]:
    event = simulate_incoming_webhook("internal", {"event": "task.done", "result": resp.content[0].text[:50]}, tampered=tampered)
    print(f"{'Tampered' if tampered else 'Legit'} webhook: verified={event.verified}")

# Expected Token Savings: N/A; multi-source verifier handles GitHub/Stripe/internal uniformly
# Environment: webhook aggregators, event-driven agents, multi-integration platforms
```

### Option 4: Signed Webhook Queue with Delivery Confirmation

```python
import anthropic
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field

client = anthropic.Anthropic()
SIGNING_KEY = "wh_sign_key_v1"

@dataclass
class WebhookDelivery:
    delivery_id: str
    event_type: str
    payload: dict
    signature: str
    attempt: int = 1
    delivered_at: float | None = None
    confirmed: bool = False

@dataclass
class WebhookQueue:
    pending: list[WebhookDelivery] = field(default_factory=list)
    delivered: list[WebhookDelivery] = field(default_factory=list)
    failed: list[WebhookDelivery] = field(default_factory=list)

def sign_delivery(payload: dict, key: str) -> str:
    ts = int(time.time())
    canonical = json.dumps(payload, separators=(',', ':'), sort_keys=True)
    message = f"{ts}.{canonical}"
    sig = hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"

def verify_delivery_confirmation(delivery_id: str, confirm_sig: str, key: str) -> bool:
    expected = hmac.new(key.encode(), delivery_id.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, confirm_sig)

def enqueue_webhook(queue: WebhookQueue, event_type: str, payload: dict) -> str:
    delivery_id = str(uuid.uuid4())[:12]
    signature = sign_delivery(payload, SIGNING_KEY)
    delivery = WebhookDelivery(
        delivery_id=delivery_id,
        event_type=event_type,
        payload=payload,
        signature=signature
    )
    queue.pending.append(delivery)
    return delivery_id

def process_queue(queue: WebhookQueue, max_retries: int = 3) -> None:
    """Process pending deliveries with confirmation handling."""
    for delivery in list(queue.pending):
        print(f"[{delivery.delivery_id}] Sending {delivery.event_type} (attempt {delivery.attempt})")

        # Simulate delivery success
        delivery.delivered_at = time.time()

        # Simulate receiving confirmation
        confirm_sig = hmac.new(SIGNING_KEY.encode(), delivery.delivery_id.encode(), hashlib.sha256).hexdigest()
        delivery.confirmed = verify_delivery_confirmation(delivery.delivery_id, confirm_sig, SIGNING_KEY)

        if delivery.confirmed:
            queue.pending.remove(delivery)
            queue.delivered.append(delivery)
            print(f"  -> Confirmed delivery {delivery.delivery_id}")
        elif delivery.attempt < max_retries:
            delivery.attempt += 1
            print(f"  -> Will retry (attempt {delivery.attempt})")
        else:
            queue.pending.remove(delivery)
            queue.failed.append(delivery)
            print(f"  -> Failed after {max_retries} attempts")

queue = WebhookQueue()

# Agent generates outputs and enqueues signed callbacks
for i in range(3):
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": f"Generate task result #{i}"}]
    )
    delivery_id = enqueue_webhook(queue, "task.complete",
                                   {"task_id": f"t_{i:03d}", "result": resp.content[0].text[:60]})
    print(f"Enqueued delivery {delivery_id}")

process_queue(queue)
print(f"\nDelivered: {len(queue.delivered)} | Failed: {len(queue.failed)} | Pending: {len(queue.pending)}")

# Expected Token Savings: N/A; delivery confirmation prevents double-processing agent side-effects
# Environment: at-least-once delivery systems, payment callbacks, agent task completion hooks
```

### Option 5: Mutual TLS Alternative with API Key + Signature

```python
import anthropic
import hashlib
import hmac
import json
import time
import base64
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class SignedAPIRequest:
    api_key_id: str
    timestamp: int
    method: str
    path: str
    body_hash: str
    signature: str

API_KEYS = {
    "key_prod_01": {"secret": "prod_secret_abc123", "scopes": ["webhook:send", "webhook:receive"]},
    "key_dev_01":  {"secret": "dev_secret_xyz789",  "scopes": ["webhook:receive"]},
}

def build_canonical_request(method: str, path: str, body: bytes, timestamp: int) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{method}\n{path}\n{timestamp}\n{body_hash}"

def sign_api_request(api_key_id: str, method: str, path: str, body: bytes) -> SignedAPIRequest:
    key_config = API_KEYS.get(api_key_id)
    if not key_config:
        raise ValueError(f"Unknown API key: {api_key_id}")

    secret = key_config["secret"]
    timestamp = int(time.time())
    body_hash = hashlib.sha256(body).hexdigest()
    canonical = build_canonical_request(method, path, body, timestamp)
    signature = base64.b64encode(
        hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).digest()
    ).decode()

    return SignedAPIRequest(
        api_key_id=api_key_id,
        timestamp=timestamp,
        method=method,
        path=path,
        body_hash=body_hash,
        signature=signature
    )

def verify_api_request(signed: SignedAPIRequest, body: bytes,
                         required_scope: str = "webhook:receive",
                         max_age: int = 300) -> tuple[bool, str]:
    key_config = API_KEYS.get(signed.api_key_id)
    if not key_config:
        return False, "Unknown API key"

    scopes = key_config.get("scopes", [])
    if required_scope not in scopes:
        return False, f"Insufficient scope: needs {required_scope}, has {scopes}"

    if abs(time.time() - signed.timestamp) > max_age:
        return False, "Request timestamp expired"

    actual_hash = hashlib.sha256(body).hexdigest()
    if actual_hash != signed.body_hash:
        return False, "Body hash mismatch (payload tampered)"

    secret = key_config["secret"]
    canonical = build_canonical_request(signed.method, signed.path, body, signed.timestamp)
    expected_sig = base64.b64encode(
        hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).digest()
    ).decode()

    if not hmac.compare_digest(expected_sig, signed.signature):
        return False, "Signature verification failed"

    return True, "Authorized"

# Agent sends a signed webhook
resp = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=64,
    messages=[{"role": "user", "content": "Generate a short status update."}]
)
body = json.dumps({"status": "complete", "message": resp.content[0].text[:60]}).encode()

# Prod key can send
prod_signed = sign_api_request("key_prod_01", "POST", "/webhooks/callback", body)
valid, reason = verify_api_request(prod_signed, body, required_scope="webhook:send")
print(f"Prod key send: {valid} ({reason})")

# Dev key cannot send (insufficient scope)
dev_signed = sign_api_request("key_dev_01", "POST", "/webhooks/callback", body)
valid2, reason2 = verify_api_request(dev_signed, body, required_scope="webhook:send")
print(f"Dev key send: {valid2} ({reason2})")

# Expected Token Savings: N/A; scope-limited keys prevent privilege escalation
# Environment: multi-tenant agent platforms, partner API integrations, scoped webhook access
```

### Option 6: Webhook Signature Rotation with Zero-Downtime Key Upgrade

```python
import anthropic
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class SigningKey:
    key_id: str
    secret: str
    created_at: float
    expires_at: float | None
    is_active: bool = True

class RotatingKeyStore:
    """Supports multiple valid keys during rotation window."""
    def __init__(self):
        self.keys: dict[str, SigningKey] = {}
        self.active_key_id: str | None = None

    def add_key(self, key_id: str, secret: str, ttl_seconds: int = 86400) -> None:
        self.keys[key_id] = SigningKey(
            key_id=key_id,
            secret=secret,
            created_at=time.time(),
            expires_at=time.time() + ttl_seconds
        )
        self.active_key_id = key_id

    def sign(self, payload: dict) -> tuple[str, str]:
        """Sign with current active key."""
        if not self.active_key_id:
            raise RuntimeError("No active signing key")
        key = self.keys[self.active_key_id]
        ts = int(time.time())
        canonical = json.dumps(payload, separators=(',', ':'), sort_keys=True)
        message = f"{ts}.{canonical}"
        sig = hmac.new(key.secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        return f"kid={key.key_id},t={ts},v1={sig}", key.key_id

    def verify(self, body: bytes, sig_header: str, max_age: int = 300) -> tuple[bool, str]:
        """Try all non-expired keys — supports overlap window during rotation."""
        try:
            parts = dict(p.split("=", 1) for p in sig_header.split(","))
            key_id = parts.get("kid")
            ts = int(parts.get("t", 0))
            provided_sig = parts.get("v1", "")
        except Exception:
            return False, "Malformed signature header"

        if abs(time.time() - ts) > max_age:
            return False, f"Timestamp {abs(time.time()-ts):.0f}s old"

        # Try specified key first, then fall back to all valid keys
        candidates = []
        if key_id and key_id in self.keys:
            candidates.append(self.keys[key_id])
        candidates.extend(k for k in self.keys.values() if k.key_id != key_id
                          and k.is_active and (k.expires_at is None or k.expires_at > time.time()))

        message = f"{ts}.{body.decode()}"
        for key in candidates:
            expected = hmac.new(key.secret.encode(), message.encode(), hashlib.sha256).hexdigest()
            if hmac.compare_digest(expected, provided_sig):
                return True, f"Verified with key {key.key_id}"

        return False, "No matching key found"

store = RotatingKeyStore()
store.add_key("key_v1", "old_secret_abc", ttl_seconds=3600)

# Simulate rotation: add new key before retiring old
store.add_key("key_v2", "new_secret_xyz", ttl_seconds=86400)

resp = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=64,
    messages=[{"role": "user", "content": "What is key rotation?"}]
)
payload = {"result": resp.content[0].text[:60]}
body = json.dumps(payload).encode()
sig_header, used_key = store.sign(payload)
print(f"Signed with: {used_key}")

valid, reason = store.verify(body, sig_header)
print(f"Verification: {valid} ({reason})")

# Simulate old message signed with key_v1 still verifying during rotation window
old_ts = int(time.time())
old_sig = hmac.new("old_secret_abc".encode(), f"{old_ts}.{body.decode()}".encode(), hashlib.sha256).hexdigest()
old_header = f"kid=key_v1,t={old_ts},v1={old_sig}"
valid2, reason2 = store.verify(body, old_header)
print(f"Old key still valid during rotation: {valid2} ({reason2})")

# Expected Token Savings: N/A; zero-downtime rotation prevents auth failures during key changes
# Environment: long-lived webhook integrations, high-availability agents, compliance-required key rotation
```

## Comparison

| Option | Replay Prevention | Multi-Key | Scope Control | Best For |
|--------|------------------|-----------|---------------|----------|
| 1 | Timestamp only | No | No | Simple webhooks |
| 2 | Nonce + timestamp | No | No | Security-critical callbacks |
| 3 | Per-source strategy | No | No | Multi-integration platforms |
| 4 | Delivery confirmation | No | No | At-least-once delivery |
| 5 | Scoped API keys | No | Yes | Multi-tenant platforms |
| 6 | Key rotation overlap | Yes | No | Production key lifecycle |
