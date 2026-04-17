---
title: "Agent Doesn't Implement Request Signing for Webhook Callbacks"
description: "Agents that receive webhook callbacks without verifying request signatures accept payloads from anyone who knows the endpoint URL — an attacker can forge callbacks that appear to be from a trusted source, injecting false tool results or triggering unauthorized actions. Implement HMAC-SHA256 request signing with timestamp validation, replay attack prevention, and per-integration shared secrets to authenticate every inbound webhook."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-request-signing-for-webhook-callbacks
tags: [webhook-security, request-signing, hmac, replay-prevention, callback-authentication, signature-verification]
symptoms:
  - "Agent accepts webhook callbacks without any signature check"
  - "Any HTTP client that knows the endpoint URL can inject fake tool results via webhooks"
  - "No timestamp validation — replayed requests from hours ago are accepted"
  - "All integrations share the same webhook secret — one leaked secret compromises all sources"
  - "Signature verification is implemented inconsistently across different webhook handlers"
---

## Why This Happens

Webhook endpoints are HTTP endpoints, and HTTP endpoints are public by default. Developers often add the webhook handler first and plan to add authentication later — but "later" never arrives. Even when a shared secret exists, it is often checked superficially (string equality on a header) without verifying the signature covers the full body, without checking request freshness, and without preventing replay attacks. Proper webhook authentication requires: computing an HMAC-SHA256 over the raw request body with a shared secret, including a timestamp in the signed payload, rejecting requests outside a freshness window, and maintaining a short-lived nonce store to block replayed signatures.

## Solution 1: Webhook Signing Secret Registry

```python
import hashlib
import os
import time
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class WebhookSigningSecret:
    integration_id: str
    secret: bytes               # raw secret bytes; never store as plain str
    algorithm: str = "sha256"
    max_age_seconds: float = 300.0   # request freshness window
    created_at: float = field(default_factory=time.time)
    rotated_at: Optional[float] = None

    @classmethod
    def generate(cls, integration_id: str, max_age_seconds: float = 300.0) -> "WebhookSigningSecret":
        return cls(
            integration_id=integration_id,
            secret=os.urandom(32),
            max_age_seconds=max_age_seconds,
        )

    def hex_secret(self) -> str:
        return self.secret.hex()


class WebhookSecretRegistry:
    """
    Stores per-integration signing secrets. Each integration gets its
    own secret so that compromising one does not affect others.
    """

    def __init__(self):
        self._secrets: Dict[str, WebhookSigningSecret] = {}

    def register(self, secret: WebhookSigningSecret) -> None:
        self._secrets[secret.integration_id] = secret

    def get(self, integration_id: str) -> Optional[WebhookSigningSecret]:
        return self._secrets.get(integration_id)

    def rotate(self, integration_id: str) -> Optional[WebhookSigningSecret]:
        existing = self._secrets.get(integration_id)
        if not existing:
            return None
        new_secret = WebhookSigningSecret(
            integration_id=integration_id,
            secret=os.urandom(32),
            algorithm=existing.algorithm,
            max_age_seconds=existing.max_age_seconds,
            rotated_at=time.time(),
        )
        self._secrets[integration_id] = new_secret
        return new_secret

    def all_integration_ids(self) -> list:
        return list(self._secrets.keys())
```

## Solution 2: Webhook Signature Signer

```python
import hashlib
import hmac
import time


class WebhookRequestSigner:
    """
    Produces HMAC-SHA256 signatures for outbound webhook requests.
    Includes a timestamp in the signed payload to enable freshness checks.
    """

    HEADER_SIGNATURE = "X-Webhook-Signature"
    HEADER_TIMESTAMP = "X-Webhook-Timestamp"
    HEADER_INTEGRATION = "X-Webhook-Integration"

    def __init__(self, secret: WebhookSigningSecret):
        self._secret = secret

    def sign(self, body: bytes) -> dict:
        """
        Returns headers dict to attach to the outbound request.
        Signed payload: "{timestamp}.{hex(body_sha256)}"
        """
        ts = str(int(time.time()))
        body_hash = hashlib.sha256(body).hexdigest()
        message = f"{ts}.{body_hash}".encode()

        sig = hmac.new(self._secret.secret, message, hashlib.sha256).hexdigest()

        return {
            self.HEADER_SIGNATURE: f"sha256={sig}",
            self.HEADER_TIMESTAMP: ts,
            self.HEADER_INTEGRATION: self._secret.integration_id,
        }
```

## Solution 3: Replay Attack Nonce Store

```python
import time
from threading import Lock
from typing import Dict, Set


class WebhookNonceStore:
    """
    Tracks recently seen (timestamp, signature) pairs to block replayed requests.
    Entries expire after the freshness window to bound memory usage.
    """

    def __init__(self, ttl_seconds: float = 300.0):
        self._ttl = ttl_seconds
        self._seen: Dict[str, float] = {}   # nonce -> first_seen_at
        self._lock = Lock()

    def _nonce(self, timestamp: str, signature: str) -> str:
        return f"{timestamp}:{signature}"

    def check_and_record(self, timestamp: str, signature: str) -> bool:
        """
        Returns True if this (timestamp, signature) pair is new.
        Returns False if it has been seen before (replay attempt).
        """
        nonce = self._nonce(timestamp, signature)
        now = time.time()

        with self._lock:
            self._evict(now)
            if nonce in self._seen:
                return False
            self._seen[nonce] = now
            return True

    def _evict(self, now: float) -> None:
        cutoff = now - self._ttl
        expired = [k for k, ts in self._seen.items() if ts < cutoff]
        for k in expired:
            del self._seen[k]

    def size(self) -> int:
        with self._lock:
            return len(self._seen)
```

## Solution 4: Webhook Signature Verifier

```python
import hashlib
import hmac
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class VerificationFailureReason(str, Enum):
    MISSING_HEADERS = "missing_headers"
    UNKNOWN_INTEGRATION = "unknown_integration"
    TIMESTAMP_TOO_OLD = "timestamp_too_old"
    TIMESTAMP_FUTURE = "timestamp_future"
    SIGNATURE_MISMATCH = "signature_mismatch"
    REPLAY_DETECTED = "replay_detected"


@dataclass
class VerificationResult:
    valid: bool
    integration_id: str = ""
    failure_reason: Optional[VerificationFailureReason] = None
    age_seconds: Optional[float] = None


class WebhookSignatureVerifier:
    """
    Verifies inbound webhook requests: checks timestamp freshness,
    recomputes the HMAC, and prevents replays via the nonce store.
    """

    FUTURE_TOLERANCE_SECONDS = 30.0   # allow slight clock skew

    def __init__(
        self,
        registry: WebhookSecretRegistry,
        nonce_store: WebhookNonceStore,
    ):
        self._registry = registry
        self._nonces = nonce_store

    def verify(
        self,
        body: bytes,
        signature_header: str,    # "sha256=<hex>"
        timestamp_header: str,    # unix epoch as string
        integration_id: str,
    ) -> VerificationResult:
        if not signature_header or not timestamp_header or not integration_id:
            return VerificationResult(valid=False, failure_reason=VerificationFailureReason.MISSING_HEADERS)

        secret = self._registry.get(integration_id)
        if not secret:
            return VerificationResult(valid=False, failure_reason=VerificationFailureReason.UNKNOWN_INTEGRATION)

        try:
            ts_int = int(timestamp_header)
        except ValueError:
            return VerificationResult(valid=False, failure_reason=VerificationFailureReason.MISSING_HEADERS)

        now = time.time()
        age = now - ts_int

        if age > secret.max_age_seconds:
            return VerificationResult(valid=False, failure_reason=VerificationFailureReason.TIMESTAMP_TOO_OLD, age_seconds=round(age, 1))

        if ts_int > now + self.FUTURE_TOLERANCE_SECONDS:
            return VerificationResult(valid=False, failure_reason=VerificationFailureReason.TIMESTAMP_FUTURE)

        body_hash = hashlib.sha256(body).hexdigest()
        message = f"{timestamp_header}.{body_hash}".encode()
        expected_sig = hmac.new(secret.secret, message, hashlib.sha256).hexdigest()

        provided_sig = signature_header.removeprefix("sha256=")

        if not hmac.compare_digest(expected_sig, provided_sig):
            return VerificationResult(valid=False, failure_reason=VerificationFailureReason.SIGNATURE_MISMATCH, integration_id=integration_id)

        if not self._nonces.check_and_record(timestamp_header, provided_sig):
            return VerificationResult(valid=False, failure_reason=VerificationFailureReason.REPLAY_DETECTED, integration_id=integration_id)

        return VerificationResult(valid=True, integration_id=integration_id, age_seconds=round(age, 1))
```

## Solution 5: Verified Webhook Handler

```python
from typing import Any, Callable, Optional


class VerifiedWebhookHandler:
    """
    Wraps an application-level webhook processor with signature verification.
    Rejects unverified requests before any payload parsing or processing occurs.
    """

    def __init__(
        self,
        verifier: WebhookSignatureVerifier,
        audit_logger: "WebhookVerificationAuditLogger",
    ):
        self._verifier = verifier
        self._logger = audit_logger

    async def handle(
        self,
        body: bytes,
        headers: dict,
        processor_fn: Callable[[bytes, str], Any],
    ) -> dict:
        sig = headers.get("X-Webhook-Signature", "")
        ts = headers.get("X-Webhook-Timestamp", "")
        integration_id = headers.get("X-Webhook-Integration", "")

        result = self._verifier.verify(body, sig, ts, integration_id)
        self._logger.record(result, integration_id)

        if not result.valid:
            return {
                "accepted": False,
                "reason": result.failure_reason.value if result.failure_reason else "unknown",
            }

        try:
            output = await processor_fn(body, integration_id)
            return {"accepted": True, "output": output}
        except Exception as exc:
            return {"accepted": True, "output": None, "processing_error": str(exc)}
```

## Solution 6: Webhook Verification Audit Logger

```python
import time
from typing import List


class WebhookVerificationAuditLogger:
    """
    Records all webhook verification outcomes for security auditing.
    Surfaces rejection rates and attack patterns per integration.
    """

    def __init__(self, max_records: int = 10000):
        self._max = max_records
        self._records: List[dict] = []

    def record(self, result: VerificationResult, integration_id: str = "") -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "valid": result.valid,
            "integration_id": integration_id or result.integration_id,
            "failure_reason": result.failure_reason.value if result.failure_reason else None,
            "age_seconds": result.age_seconds,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "requests": 0}

        total = len(recent)
        valid = sum(1 for r in recent if r["valid"])
        reason_counts: dict = {}
        for r in recent:
            if r["failure_reason"]:
                rc = r["failure_reason"]
                reason_counts[rc] = reason_counts.get(rc, 0) + 1

        return {
            "window_seconds": window_seconds,
            "requests": total,
            "accepted": valid,
            "rejected": total - valid,
            "rejection_rate": round((total - valid) / total, 4),
            "rejection_reasons": reason_counts,
        }
```

## Comparison

| Approach | Per-Integration Secrets | HMAC-SHA256 Signing | Timestamp Freshness | Replay Prevention | Audit Log |
|---|---|---|---|---|---|
| WebhookSecretRegistry | Yes | No | No | No | No |
| WebhookRequestSigner | Via secret | Yes | Yes (ts header) | No | No |
| WebhookNonceStore | No | No | No | Yes (TTL eviction) | No |
| WebhookSignatureVerifier | Via registry | Yes | Yes (max_age check) | Via nonce store | No |
| VerifiedWebhookHandler | Via verifier | Via verifier | Via verifier | Via verifier | Via logger |
| WebhookVerificationAuditLogger | No | No | No | No | Yes |

**Best for production**: Set `max_age_seconds=300` — five minutes is ample for any legitimate webhook delivery retry and prevents all practical replay attacks. Use `hmac.compare_digest` for signature comparison (already done above) — never use `==` on signature strings, which is vulnerable to timing attacks. Issue per-integration secrets via `WebhookSecretRegistry` from the start: rotating a single shared secret requires coordinating with every integration simultaneously, while per-integration secrets can be rotated independently. Monitor `rejection_rate` and `replay_detected` counts via `WebhookVerificationAuditLogger`: a spike in replay rejections from a single integration indicates a misconfigured retry loop or an active attack.
