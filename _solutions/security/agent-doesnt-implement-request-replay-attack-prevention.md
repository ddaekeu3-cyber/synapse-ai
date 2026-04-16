---
title: "Agent Doesn't Implement Request Replay Attack Prevention"
description: "Agents that don't validate request freshness and uniqueness are vulnerable to replay attacks where an intercepted signed request is resubmitted to trigger duplicate tool executions, payments, or state mutations."
difficulty: advanced
category: security
tags: [security, replay-attack, nonce, idempotency, hmac, authentication]
---

# Agent Doesn't Implement Request Replay Attack Prevention

## Problem

An attacker who intercepts a legitimate signed API request can resend it minutes or hours later. If the agent doesn't validate request freshness (timestamp) and uniqueness (nonce), the same tool call executes twice: a payment charges twice, a file deletes twice, an email sends twice. This is especially dangerous for agents with destructive or financial tool access.

**Symptoms:**
- Duplicate payments or transactions traced to identical request bodies
- Email notifications sent multiple times to the same recipient
- File deletion or write executed on retried webhook deliveries
- API logs showing identical request signatures minutes apart
- State mutations applied twice when webhook provider retries on timeout

---

## Solution 1: Timestamp + Nonce Validation (HMAC-SHA256)

Reject requests older than a window and track used nonces to prevent replays.

```python
import asyncio
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Optional
import anthropic


REPLAY_WINDOW_SECONDS = 300  # 5-minute validity window
_used_nonces: dict[str, float] = {}  # nonce -> expiry timestamp
_nonce_lock = asyncio.Lock()

SECRET_KEY = b"super-secret-signing-key"


def sign_request(payload: str, timestamp: int, nonce: str) -> str:
    """HMAC-SHA256 over 'timestamp.nonce.payload'."""
    message = f"{timestamp}.{nonce}.{payload}".encode()
    return hmac.new(SECRET_KEY, message, hashlib.sha256).hexdigest()


async def validate_request(
    payload: str,
    timestamp: int,
    nonce: str,
    signature: str,
) -> tuple[bool, str]:
    """Returns (is_valid, reason)."""
    now = int(time.time())

    # 1. Check timestamp freshness
    age = now - timestamp
    if age < 0:
        return False, "timestamp_in_future"
    if age > REPLAY_WINDOW_SECONDS:
        return False, f"request_too_old_{age}s"

    # 2. Verify HMAC signature
    expected = sign_request(payload, timestamp, nonce)
    if not hmac.compare_digest(expected, signature):
        return False, "invalid_signature"

    # 3. Check nonce uniqueness
    async with _nonce_lock:
        # Evict expired nonces
        now_f = time.time()
        expired = [n for n, exp in _used_nonces.items() if exp < now_f]
        for n in expired:
            del _used_nonces[n]

        if nonce in _used_nonces:
            return False, "nonce_already_used"

        _used_nonces[nonce] = now_f + REPLAY_WINDOW_SECONDS

    return True, "ok"


@dataclass
class SecureRequest:
    payload: str
    timestamp: int
    nonce: str
    signature: str

    @classmethod
    def create(cls, payload: str) -> "SecureRequest":
        ts = int(time.time())
        nonce = secrets.token_hex(16)
        sig = sign_request(payload, ts, nonce)
        return cls(payload=payload, timestamp=ts, nonce=nonce, signature=sig)


class ReplayProtectedAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def handle_request(self, req: SecureRequest) -> dict:
        valid, reason = await validate_request(
            req.payload, req.timestamp, req.nonce, req.signature
        )
        if not valid:
            print(f"[security] Rejected request: {reason}")
            return {"error": reason, "accepted": False}

        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=256,
            messages=[{"role": "user", "content": req.payload}],
        )
        return {"text": response.content[0].text, "accepted": True}


async def demo():
    agent = ReplayProtectedAgent(api_key="sk-...")
    req = SecureRequest.create("Transfer $100 to account 9876.")

    # First submission — accepted
    result1 = await agent.handle_request(req)
    print(f"First: {result1}")

    # Replay — rejected
    result2 = await agent.handle_request(req)
    print(f"Replay: {result2}")

# asyncio.run(demo())
```

---

## Solution 2: Redis-Backed Nonce Store for Distributed Agents

Use Redis SETNX with TTL for nonce storage, safe across multiple agent processes.

```python
import asyncio
import hashlib
import hmac
import secrets
import time
from typing import Optional
import anthropic

# Requires: pip install redis[asyncio]
try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None  # type: ignore

SECRET_KEY = b"distributed-signing-key"
NONCE_TTL = 300  # seconds


def _sign(ts: int, nonce: str, body: str) -> str:
    msg = f"{ts}.{nonce}.{body}".encode()
    return hmac.new(SECRET_KEY, msg, hashlib.sha256).hexdigest()


class RedisNonceStore:
    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self._client = aioredis.from_url(redis_url, decode_responses=True)

    async def claim(self, nonce: str, ttl: int = NONCE_TTL) -> bool:
        """Returns True if nonce was newly claimed; False if already used."""
        key = f"nonce:{nonce}"
        result = await self._client.set(key, "1", nx=True, ex=ttl)
        return result is True  # SETNX returns None if key existed

    async def close(self) -> None:
        await self._client.aclose()


class DistributedReplayGuard:
    def __init__(self, nonce_store: RedisNonceStore, window: int = NONCE_TTL):
        self.store = nonce_store
        self.window = window

    async def verify(self, ts: int, nonce: str, body: str, sig: str) -> tuple[bool, str]:
        now = int(time.time())
        if abs(now - ts) > self.window:
            return False, "timestamp_outside_window"

        expected = _sign(ts, nonce, body)
        if not hmac.compare_digest(expected, sig):
            return False, "signature_mismatch"

        claimed = await self.store.claim(nonce)
        if not claimed:
            return False, "nonce_replayed"

        return True, "ok"


class DistributedAgent:
    def __init__(self, api_key: str, guard: DistributedReplayGuard):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.guard = guard

    async def process(
        self,
        body: str,
        ts: int,
        nonce: str,
        sig: str,
    ) -> dict:
        ok, reason = await self.guard.verify(ts, nonce, body, sig)
        if not ok:
            return {"error": reason}

        resp = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=128,
            messages=[{"role": "user", "content": body}],
        )
        return {"text": resp.content[0].text}


async def demo():
    store = RedisNonceStore("redis://localhost:6379")
    guard = DistributedReplayGuard(store)
    agent = DistributedAgent(api_key="sk-...", guard=guard)

    ts = int(time.time())
    nonce = secrets.token_hex(16)
    body = "Delete file config.yaml"
    sig = _sign(ts, nonce, body)

    r1 = await agent.process(body, ts, nonce, sig)
    r2 = await agent.process(body, ts, nonce, sig)  # Replay
    print(f"First: {r1.get('text', r1.get('error'))[:40]}")
    print(f"Replay: {r2}")
    await store.close()

# asyncio.run(demo())
```

---

## Solution 3: Idempotency Key with Result Caching

Accept an `Idempotency-Key` header; cache the response for the key's lifetime so retries return the cached result rather than executing again.

```python
import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Optional
import anthropic


@dataclass
class CachedResponse:
    result: Any
    created_at: float
    request_hash: str   # Hash of request body — rejects key reuse with different payload


class IdempotencyStore:
    def __init__(self, ttl: float = 86400.0):  # 24 hours
        self._store: dict[str, CachedResponse] = {}
        self._ttl = ttl
        self._lock = asyncio.Lock()

    @staticmethod
    def _hash(payload: str) -> str:
        return hashlib.sha256(payload.encode()).hexdigest()

    async def get_or_reserve(
        self,
        key: str,
        payload: str,
    ) -> tuple[Optional[Any], bool, str]:
        """
        Returns (cached_result, is_cached, error).
        Call set() after computing the result.
        """
        payload_hash = self._hash(payload)
        async with self._lock:
            if key in self._store:
                entry = self._store[key]
                if time.time() - entry.created_at > self._ttl:
                    del self._store[key]
                elif entry.request_hash != payload_hash:
                    return None, False, "idempotency_key_payload_mismatch"
                else:
                    return entry.result, True, ""
        return None, False, ""

    async def set(self, key: str, payload: str, result: Any) -> None:
        async with self._lock:
            self._store[key] = CachedResponse(
                result=result,
                created_at=time.time(),
                request_hash=self._hash(payload),
            )


class IdempotentAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.store = IdempotencyStore(ttl=3600.0)

    async def execute(
        self,
        idempotency_key: str,
        tool_name: str,
        payload: str,
    ) -> dict:
        cache_payload = f"{tool_name}:{payload}"
        cached, is_cached, error = await self.store.get_or_reserve(
            idempotency_key, cache_payload
        )

        if error:
            return {"error": error}

        if is_cached:
            print(f"[idempotency] Cache hit for key={idempotency_key}")
            return {**cached, "idempotent": True}

        # Actually execute the tool
        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=256,
            messages=[{"role": "user", "content": f"Execute {tool_name}: {payload}"}],
        )
        result = {
            "text": response.content[0].text,
            "tokens": response.usage.output_tokens,
            "idempotent": False,
        }
        await self.store.set(idempotency_key, cache_payload, result)
        return result


async def demo():
    agent = IdempotentAgent(api_key="sk-...")
    idem_key = "idem_payment_txn_789"
    payload = "Send payment of $50 to merchant ID 12345"

    r1 = await agent.execute(idem_key, "payment", payload)
    r2 = await agent.execute(idem_key, "payment", payload)  # Retry — returns cached
    r3 = await agent.execute(idem_key, "payment", "DIFFERENT PAYLOAD")  # Key reuse — error

    print(f"First: idempotent={r1['idempotent']}")
    print(f"Retry: idempotent={r2['idempotent']}")
    print(f"Mismatch: {r3}")

# asyncio.run(demo())
```

---

## Solution 4: Ed25519 Request Signing with Timestamp Binding

Use Ed25519 asymmetric signatures so only the holder of the private key can create valid requests.

```python
import asyncio
import base64
import json
import secrets
import time
from dataclasses import dataclass
from typing import Optional

# pip install cryptography
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.exceptions import InvalidSignature
import anthropic

_used_nonces: set[str] = set()
_nonce_lock = asyncio.Lock()
WINDOW = 300


def generate_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    private = Ed25519PrivateKey.generate()
    return private, private.public_key()


@dataclass
class SignedRequest:
    body: str           # JSON string
    timestamp: int
    nonce: str
    signature: str      # base64-encoded Ed25519 signature

    def signing_payload(self) -> bytes:
        return f"{self.timestamp}.{self.nonce}.{self.body}".encode()

    @classmethod
    def sign(cls, body: str, private_key: Ed25519PrivateKey) -> "SignedRequest":
        ts = int(time.time())
        nonce = secrets.token_hex(16)
        payload = f"{ts}.{nonce}.{body}".encode()
        sig = private_key.sign(payload)
        return cls(
            body=body,
            timestamp=ts,
            nonce=nonce,
            signature=base64.b64encode(sig).decode(),
        )


class Ed25519ReplayGuard:
    def __init__(self, public_key: Ed25519PublicKey, window: int = WINDOW):
        self.public_key = public_key
        self.window = window

    async def verify(self, req: SignedRequest) -> tuple[bool, str]:
        now = int(time.time())
        if abs(now - req.timestamp) > self.window:
            return False, "timestamp_expired"

        # Verify Ed25519 signature
        try:
            sig_bytes = base64.b64decode(req.signature)
            self.public_key.verify(sig_bytes, req.signing_payload())
        except InvalidSignature:
            return False, "invalid_signature"
        except Exception as exc:
            return False, f"signature_error:{exc}"

        # Check nonce
        async with _nonce_lock:
            if req.nonce in _used_nonces:
                return False, "nonce_replayed"
            _used_nonces.add(req.nonce)

        return True, "ok"


class Ed25519Agent:
    def __init__(self, api_key: str, guard: Ed25519ReplayGuard):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.guard = guard

    async def handle(self, req: SignedRequest) -> dict:
        ok, reason = await self.guard.verify(req)
        if not ok:
            return {"error": reason, "accepted": False}

        resp = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=128,
            messages=[{"role": "user", "content": req.body}],
        )
        return {"text": resp.content[0].text, "accepted": True}


async def demo():
    private_key, public_key = generate_keypair()
    guard = Ed25519ReplayGuard(public_key)
    agent = Ed25519Agent(api_key="sk-...", guard=guard)

    req = SignedRequest.sign("Delete all temporary files.", private_key)
    r1 = await agent.handle(req)
    r2 = await agent.handle(req)  # Replay
    print(f"First: accepted={r1['accepted']}")
    print(f"Replay: {r2}")

# asyncio.run(demo())
```

---

## Solution 5: Webhook Replay Protection with Delivery ID Tracking

For webhook-driven agents, validate the provider-supplied delivery ID and reject duplicates.

```python
import asyncio
import hashlib
import hmac
import time
from collections import OrderedDict
from typing import Optional
import anthropic
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse


WEBHOOK_SECRET = b"webhook-secret-from-provider"
DELIVERY_ID_TTL = 3600       # Track IDs for 1 hour
MAX_TRACKED_IDS = 100_000    # Memory cap


class DeliveryIDStore:
    """LRU-bounded set of seen delivery IDs with TTL."""

    def __init__(self, ttl: int = DELIVERY_ID_TTL, max_size: int = MAX_TRACKED_IDS):
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._ttl = ttl
        self._max = max_size
        self._lock = asyncio.Lock()

    async def claim(self, delivery_id: str) -> bool:
        """Returns True if first time seeing this ID."""
        now = time.time()
        async with self._lock:
            # Evict expired
            while self._seen and next(iter(self._seen.values())) < now - self._ttl:
                self._seen.popitem(last=False)

            # Enforce size cap
            while len(self._seen) >= self._max:
                self._seen.popitem(last=False)

            if delivery_id in self._seen:
                return False

            self._seen[delivery_id] = now
            return True


delivery_store = DeliveryIDStore()
app = FastAPI()
client = anthropic.AsyncAnthropic(api_key="sk-...")


def verify_webhook_signature(body: bytes, signature: str, timestamp: str) -> bool:
    """Verify Svix/Stripe-style webhook signature."""
    message = f"{timestamp}.".encode() + body
    expected = "v1," + hmac.new(WEBHOOK_SECRET, message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@app.post("/webhook/agent-trigger")
async def webhook_trigger(
    request: Request,
    svix_id: str = Header(alias="svix-id"),
    svix_timestamp: str = Header(alias="svix-timestamp"),
    svix_signature: str = Header(alias="svix-signature"),
):
    body = await request.body()

    # 1. Verify signature
    if not verify_webhook_signature(body, svix_signature, svix_timestamp):
        raise HTTPException(status_code=401, detail="invalid_signature")

    # 2. Check timestamp freshness (within 5 minutes)
    try:
        ts = int(svix_timestamp)
        if abs(time.time() - ts) > 300:
            raise HTTPException(status_code=400, detail="timestamp_expired")
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_timestamp")

    # 3. Deduplicate by delivery ID
    is_new = await delivery_store.claim(svix_id)
    if not is_new:
        print(f"[webhook] Duplicate delivery ID: {svix_id} — returning cached 200")
        return JSONResponse({"status": "already_processed", "delivery_id": svix_id})

    # 4. Process the webhook
    import json
    payload = json.loads(body)
    user_message = payload.get("message", "")

    response = await client.messages.create(
        model="claude-opus-4-6",
        max_tokens=256,
        messages=[{"role": "user", "content": user_message}],
    )
    return JSONResponse({
        "status": "processed",
        "delivery_id": svix_id,
        "reply": response.content[0].text,
    })
```

---

## Solution 6: Request Sequence Number Anti-Replay

For persistent sessions, enforce monotonically increasing sequence numbers so out-of-order or replayed requests are rejected.

```python
import asyncio
import hmac
import hashlib
import secrets
from dataclasses import dataclass
from typing import Optional
import anthropic

SECRET = b"session-signing-key"


@dataclass
class SequencedRequest:
    session_id: str
    sequence_number: int   # Must be strictly greater than last seen
    payload: str
    signature: str

    @classmethod
    def create(cls, session_id: str, seq: int, payload: str) -> "SequencedRequest":
        msg = f"{session_id}.{seq}.{payload}".encode()
        sig = hmac.new(SECRET, msg, hashlib.sha256).hexdigest()
        return cls(session_id=session_id, sequence_number=seq, payload=payload, signature=sig)

    def verify_sig(self) -> bool:
        msg = f"{self.session_id}.{self.sequence_number}.{self.payload}".encode()
        expected = hmac.new(SECRET, msg, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, self.signature)


class SequenceTracker:
    def __init__(self):
        self._last_seq: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def validate_and_advance(self, req: SequencedRequest) -> tuple[bool, str]:
        if not req.verify_sig():
            return False, "invalid_signature"

        async with self._lock:
            last = self._last_seq.get(req.session_id, -1)
            if req.sequence_number <= last:
                return False, f"sequence_replay_or_reorder: got={req.sequence_number} last={last}"
            self._last_seq[req.session_id] = req.sequence_number

        return True, "ok"


class SequencedAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.tracker = SequenceTracker()

    async def process(self, req: SequencedRequest) -> dict:
        ok, reason = await self.tracker.validate_and_advance(req)
        if not ok:
            return {"error": reason}

        resp = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=128,
            messages=[{"role": "user", "content": req.payload}],
        )
        return {"seq": req.sequence_number, "text": resp.content[0].text}


async def demo():
    agent = SequencedAgent(api_key="sk-...")
    sid = "session_abc"

    r1 = await agent.process(SequencedRequest.create(sid, 1, "Hello"))
    r2 = await agent.process(SequencedRequest.create(sid, 2, "How are you?"))
    r3 = await agent.process(SequencedRequest.create(sid, 1, "Hello"))  # Replay seq=1
    r4 = await agent.process(SequencedRequest.create(sid, 3, "Goodbye"))

    print(f"seq=1: ok={('text' in r1)}")
    print(f"seq=2: ok={('text' in r2)}")
    print(f"seq=1 replay: {r3}")
    print(f"seq=3: ok={('text' in r4)}")

# asyncio.run(demo())
```

---

## Comparison

| Solution | Nonce Storage | Signature | Distributed | Webhook-Native | Complexity |
|---|---|---|---|---|---|
| Timestamp + nonce (HMAC) | In-process dict | HMAC-SHA256 | No | No | Low |
| Redis nonce store | Redis SETNX | HMAC-SHA256 | Yes | No | Medium |
| Idempotency key + cache | In-process dict | None (key-based) | No | No | Low |
| Ed25519 asymmetric | In-process set | Ed25519 | No | No | Medium |
| Webhook delivery ID | In-process LRU | Provider HMAC | No | Yes | Low |
| Sequence numbers | In-process dict | HMAC-SHA256 | No | No | Low |

**Recommendation:** Use Solution 1 (timestamp + nonce + HMAC) as the baseline for API-to-agent calls. Use Solution 2 (Redis) when agents run across multiple processes. Use Solution 5 (delivery ID) for webhook-triggered agents — it matches what providers like Svix and Stripe already send. Layer Solution 3 (idempotency key) on top for any tool that has side effects (payments, emails, file writes).
