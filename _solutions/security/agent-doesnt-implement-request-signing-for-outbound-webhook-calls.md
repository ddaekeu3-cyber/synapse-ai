---
title: "Agent Doesn't Implement Request Signing for Outbound Webhook Calls"
description: "Agents that send outbound webhook notifications without cryptographic signatures allow recipients to have no way to verify the payload originated from the agent: a malicious actor who knows the webhook endpoint can send forged payloads that the recipient will process as legitimate. Implement HMAC-SHA256 request signing on all outbound webhook calls so recipients can verify authenticity before acting on payloads."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-request-signing-for-outbound-webhook-calls
tags: [webhook-signing, hmac, request-authentication, payload-integrity, outbound-security, signature-verification]
symptoms:
  - "Webhook payloads sent without any signature header"
  - "Recipients cannot distinguish agent-sent payloads from forged ones"
  - "No timestamp in webhook calls — replay attacks using captured payloads succeed"
  - "Signing keys are hardcoded strings rather than rotatable secrets"
  - "Different webhook endpoints share the same signing key"
---

## Why This Happens

Sending data to a webhook endpoint is a one-way push: the recipient has no established session with the sender and cannot verify identity through connection state. The sender must prove authenticity by including a signature computed over the payload using a shared secret. Without a signature, the endpoint URL is the only credential — anyone who discovers or guesses the endpoint can forge payloads. HMAC-SHA256 signatures over `timestamp + payload` prevent both forgery (requires the secret) and replay (timestamp binds the signature to a specific moment).

## Solution 1: Webhook Signing Key Registry

```python
import hashlib
import os
import time
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class WebhookSigningKey:
    key_id: str
    secret: bytes
    endpoint_pattern: str = ""    # regex pattern of endpoints this key covers
    created_at: float = field(default_factory=time.time)
    rotated_at: Optional[float] = None
    active: bool = True

    @classmethod
    def generate(cls, key_id: str, endpoint_pattern: str = "") -> "WebhookSigningKey":
        return cls(
            key_id=key_id,
            secret=os.urandom(32),
            endpoint_pattern=endpoint_pattern,
        )


class WebhookSigningKeyRegistry:
    """
    Manages per-endpoint signing keys with rotation support.
    Each endpoint or endpoint group gets its own key to limit blast radius.
    """

    def __init__(self):
        self._keys: Dict[str, WebhookSigningKey] = {}

    def register(self, key: WebhookSigningKey) -> None:
        self._keys[key.key_id] = key

    def get(self, key_id: str) -> Optional[WebhookSigningKey]:
        key = self._keys.get(key_id)
        return key if key and key.active else None

    def rotate(self, key_id: str) -> WebhookSigningKey:
        old = self._keys.get(key_id)
        if old:
            old.active = False
        new_key = WebhookSigningKey(
            key_id=key_id,
            secret=os.urandom(32),
            endpoint_pattern=old.endpoint_pattern if old else "",
            rotated_at=time.time(),
        )
        self._keys[f"{key_id}_v{int(time.time())}"] = old  # archive old key
        self._keys[key_id] = new_key
        return new_key

    def active_keys(self) -> list:
        return [k for k in self._keys.values() if k.active]
```

## Solution 2: HMAC Request Signer

```python
import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional, Tuple


class HMACRequestSigner:
    """
    Signs outbound webhook payloads using HMAC-SHA256.
    Signature covers timestamp + payload body to prevent replay attacks.
    Produces headers compatible with GitHub/Stripe webhook signature conventions.
    """

    SIGNATURE_HEADER = "X-Agent-Signature-256"
    TIMESTAMP_HEADER = "X-Agent-Timestamp"
    KEY_ID_HEADER = "X-Agent-Key-Id"
    TOLERANCE_SECONDS = 300   # recipients should reject signatures older than 5min

    def __init__(self, registry: WebhookSigningKeyRegistry):
        self._registry = registry

    def sign(
        self,
        key_id: str,
        payload: bytes,
        timestamp: Optional[float] = None,
    ) -> Dict[str, str]:
        """
        Returns headers dict containing signature, timestamp, and key ID.
        """
        key = self._registry.get(key_id)
        if key is None:
            raise SigningKeyNotFoundError(key_id)

        ts = int(timestamp or time.time())
        signed_content = f"{ts}.".encode() + payload
        signature = hmac.new(key.secret, signed_content, hashlib.sha256).hexdigest()

        return {
            self.SIGNATURE_HEADER: f"sha256={signature}",
            self.TIMESTAMP_HEADER: str(ts),
            self.KEY_ID_HEADER: key_id,
        }

    def sign_json(self, key_id: str, payload: Any) -> Tuple[bytes, Dict[str, str]]:
        """Serializes payload to JSON, signs it, returns (body_bytes, headers)."""
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = self.sign(key_id, body)
        return body, headers


class SigningKeyNotFoundError(Exception):
    def __init__(self, key_id: str):
        super().__init__(f"no active signing key found for key_id='{key_id}'")
        self.key_id = key_id
```

## Solution 3: Signed Webhook Dispatcher

```python
import asyncio
import time
from typing import Any, Callable, Dict, Optional


class SignedWebhookDispatcher:
    """
    Sends signed webhook requests to endpoints.
    Records delivery attempts and outcomes for audit purposes.
    """

    def __init__(
        self,
        signer: HMACRequestSigner,
        http_client,      # accepts any async HTTP client with .post(url, headers, data)
        default_key_id: str = "default",
        timeout_seconds: float = 10.0,
    ):
        self._signer = signer
        self._http = http_client
        self._default_key_id = default_key_id
        self._timeout = timeout_seconds
        self._delivery_log = []
        self._success_count = 0
        self._failure_count = 0

    async def deliver(
        self,
        endpoint_url: str,
        payload: Any,
        key_id: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> dict:
        kid = key_id or self._default_key_id
        body, sig_headers = self._signer.sign_json(kid, payload)

        headers = {
            "Content-Type": "application/json",
            **sig_headers,
            **(extra_headers or {}),
        }

        start = time.time()
        try:
            response = await asyncio.wait_for(
                self._http.post(endpoint_url, headers=headers, data=body),
                timeout=self._timeout,
            )
            latency_ms = round((time.time() - start) * 1000, 2)
            success = 200 <= response.status_code < 300
            if success:
                self._success_count += 1
            else:
                self._failure_count += 1
            result = {
                "success": success,
                "status_code": response.status_code,
                "latency_ms": latency_ms,
                "endpoint": endpoint_url,
                "key_id": kid,
            }
        except Exception as exc:
            latency_ms = round((time.time() - start) * 1000, 2)
            self._failure_count += 1
            result = {
                "success": False,
                "error": str(exc),
                "latency_ms": latency_ms,
                "endpoint": endpoint_url,
                "key_id": kid,
            }

        self._delivery_log.append({**result, "ts": time.time()})
        if len(self._delivery_log) > 1000:
            self._delivery_log.pop(0)
        return result

    def stats(self) -> dict:
        return {
            "success_count": self._success_count,
            "failure_count": self._failure_count,
            "total": self._success_count + self._failure_count,
        }
```

## Solution 4: Signature Verifier (for Testing)

```python
import hashlib
import hmac
import time
from typing import Dict


class WebhookSignatureVerifier:
    """
    Verifies incoming webhook signatures — useful for testing agent-sent
    webhooks from the recipient's perspective, and for agents that both
    send and receive signed webhooks.
    """

    def __init__(self, registry: WebhookSigningKeyRegistry):
        self._registry = registry

    def verify(
        self,
        headers: Dict[str, str],
        body: bytes,
        tolerance_seconds: float = 300.0,
    ) -> dict:
        sig_header = headers.get("X-Agent-Signature-256", "")
        ts_header = headers.get("X-Agent-Timestamp", "")
        key_id = headers.get("X-Agent-Key-Id", "default")

        if not sig_header or not ts_header:
            return {"valid": False, "error": "missing signature or timestamp headers"}

        try:
            ts = float(ts_header)
        except ValueError:
            return {"valid": False, "error": "invalid timestamp format"}

        age = time.time() - ts
        if abs(age) > tolerance_seconds:
            return {"valid": False, "error": f"timestamp too old or too new: age={age:.0f}s"}

        key = self._registry.get(key_id)
        if key is None:
            return {"valid": False, "error": f"unknown key_id='{key_id}'"}

        signed_content = f"{int(ts)}.".encode() + body
        expected = "sha256=" + hmac.new(key.secret, signed_content, hashlib.sha256).hexdigest()

        if not hmac.compare_digest(expected, sig_header):
            return {"valid": False, "error": "signature mismatch"}

        return {"valid": True, "key_id": key_id, "age_seconds": round(age, 1)}
```

## Solution 5: Key Rotation Scheduler

```python
import asyncio
import time
from typing import Callable, List, Optional


class WebhookKeyRotationScheduler:
    """
    Automatically rotates signing keys on a schedule.
    Notifies endpoints of new keys via a pre-rotation callback
    so recipients can update their verification secrets.
    """

    def __init__(
        self,
        registry: WebhookSigningKeyRegistry,
        rotation_interval_seconds: float = 86400.0,   # rotate daily
        notify_fn: Optional[Callable] = None,          # async fn(key_id, new_secret_hex)
    ):
        self._registry = registry
        self._interval = rotation_interval_seconds
        self._notify_fn = notify_fn
        self._rotation_count = 0
        self._running = False

    async def rotate_once(self, key_ids: List[str]) -> List[dict]:
        results = []
        for key_id in key_ids:
            new_key = self._registry.rotate(key_id)
            self._rotation_count += 1
            if self._notify_fn:
                try:
                    await self._notify_fn(key_id, new_key.secret.hex())
                except Exception as exc:
                    results.append({"key_id": key_id, "success": False, "error": str(exc)})
                    continue
            results.append({"key_id": key_id, "success": True, "rotated_at": new_key.rotated_at})
        return results

    async def run_loop(self, key_ids: List[str]) -> None:
        self._running = True
        while self._running:
            await asyncio.sleep(self._interval)
            await self.rotate_once(key_ids)

    def stop(self) -> None:
        self._running = False

    def stats(self) -> dict:
        return {"rotation_count": self._rotation_count}
```

## Solution 6: Webhook Signing Dashboard

```python
import time


class WebhookSigningDashboard:
    """
    Combines delivery stats, key registry status, and rotation
    history into a single operational view.
    """

    def __init__(
        self,
        dispatcher: SignedWebhookDispatcher,
        registry: WebhookSigningKeyRegistry,
        rotation_scheduler: WebhookKeyRotationScheduler,
    ):
        self._dispatcher = dispatcher
        self._registry = registry
        self._scheduler = rotation_scheduler

    def render(self) -> dict:
        active_keys = self._registry.active_keys()
        delivery = self._dispatcher.stats()

        return {
            "generated_at": time.time(),
            "delivery_stats": delivery,
            "active_key_count": len(active_keys),
            "key_ages_seconds": {
                k.key_id: round(time.time() - (k.rotated_at or k.created_at), 0)
                for k in active_keys
            },
            "rotation_stats": self._scheduler.stats(),
            "delivery_failure_rate": round(
                delivery["failure_count"] / max(delivery["total"], 1), 4
            ),
        }
```

## Comparison

| Approach | Payload Signing | Timestamp Binding | Key Rotation | Verification | Delivery Audit |
|---|---|---|---|---|---|
| WebhookSigningKeyRegistry | No | No | Yes | No | No |
| HMACRequestSigner | Yes (HMAC-SHA256) | Yes | Via registry | No | No |
| SignedWebhookDispatcher | Via signer | Via signer | No | No | Yes |
| WebhookSignatureVerifier | No | Yes (tolerance) | No | Yes | No |
| WebhookKeyRotationScheduler | No | No | Yes (automated) | No | No |
| WebhookSigningDashboard | No | No | No | No | Yes (aggregated) |

**Best for production**: Use per-endpoint key IDs so that a compromised key for one endpoint does not affect others. Include the timestamp in the signed content (not just as a separate header) — a timestamp header without being part of the signed content can be stripped and replaced without invalidating the signature. Set `rotation_interval_seconds=86400` (24 hours) and send key rotation notifications to endpoints at least 1 hour before the old key is deactivated to allow recipients to update without service interruption. Reject any webhook delivery that returns a non-2xx status as a signature failure on the recipient side and alert: it may indicate the recipient's key is out of sync.
