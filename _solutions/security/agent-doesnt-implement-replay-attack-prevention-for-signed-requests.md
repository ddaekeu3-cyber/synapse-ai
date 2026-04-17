---
title: "Agent Doesn't Implement Replay Attack Prevention for Signed Requests"
description: "Agents that verify HMAC signatures on incoming requests without replay prevention allow an attacker who intercepts a valid signed request to re-submit it indefinitely — the signature is valid, the payload is legitimate, and the agent executes the action again. Implement replay attack prevention using a combination of request timestamps (reject requests older than a window) and nonce tracking (reject previously-seen request IDs within the window)."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-replay-attack-prevention-for-signed-requests
tags: [replay-attack, request-signing, nonce, hmac, timestamp-validation, webhook-security]
symptoms:
  - "A captured webhook request replayed 60 seconds later is accepted and processed again"
  - "HMAC signature verification passes but there is no check on request age"
  - "No nonce or request ID tracking — the same signed payload can be submitted multiple times"
  - "Payment trigger webhook replayed by an attacker causes double-charge"
  - "No maximum request age enforced — a request signed last week can still be submitted today"
---

## Why This Happens

HMAC signature verification proves that a request was signed by someone with the shared secret — but it does not prove the request is new. A valid signed request captured from a legitimate transaction can be replayed by an attacker who does not know the secret. Replay prevention requires two independent defenses: timestamp validation (reject requests whose `timestamp` field is outside a tight window, e.g. ±60 seconds) and nonce tracking (store every `request_id` seen within the window and reject duplicates). Both are necessary: timestamp validation alone is broken by an attacker who replays within the window; nonce tracking alone requires unbounded storage without a window.

## Solution 1: Signed Request Model

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class SignedRequest:
    request_id: str          # unique per request, included in HMAC input
    timestamp: float         # unix timestamp at signing time
    payload: Dict[str, Any]  # the actual request body
    signature: str           # HMAC-SHA256 hex digest
    version: str = "v1"      # signing scheme version

    def canonical_string(self) -> str:
        """Deterministic string over which the HMAC is computed."""
        import json
        canonical_payload = json.dumps(self.payload, sort_keys=True, separators=(",", ":"))
        return f"{self.version}:{self.request_id}:{self.timestamp}:{canonical_payload}"
```

## Solution 2: Timestamp Validator

```python
import time
from dataclasses import dataclass


@dataclass
class TimestampValidationResult:
    valid: bool
    age_seconds: float
    reason: str = ""


class RequestTimestampValidator:
    """
    Rejects requests whose timestamp is outside the acceptable window.
    A tight window (60–300 seconds) prevents replay without significant
    impact on legitimate requests from clients with minor clock skew.
    """

    def __init__(
        self,
        max_age_seconds: float = 60.0,
        max_future_seconds: float = 30.0,
    ):
        self._max_age = max_age_seconds
        self._max_future = max_future_seconds

    def validate(self, request_timestamp: float) -> TimestampValidationResult:
        now = time.time()
        age = now - request_timestamp

        if age > self._max_age:
            return TimestampValidationResult(
                valid=False,
                age_seconds=round(age, 2),
                reason=f"request_too_old: {round(age, 1)}s > {self._max_age}s window",
            )

        if age < -self._max_future:
            return TimestampValidationResult(
                valid=False,
                age_seconds=round(age, 2),
                reason=f"request_from_future: {round(-age, 1)}s ahead",
            )

        return TimestampValidationResult(valid=True, age_seconds=round(age, 2))
```

## Solution 3: Nonce Store

```python
import time
from threading import Lock
from typing import Dict, Optional


class RequestNonceStore:
    """
    Tracks request IDs seen within the replay window.
    Uses a time-bucketed structure to allow efficient expiry
    without scanning the entire store on every request.
    """

    def __init__(
        self,
        window_seconds: float = 60.0,
        bucket_seconds: float = 10.0,
    ):
        self._window = window_seconds
        self._bucket_size = bucket_seconds
        self._buckets: Dict[int, Dict[str, float]] = {}
        self._lock = Lock()

    def _bucket_key(self, ts: float) -> int:
        return int(ts // self._bucket_size)

    def _evict_expired(self, now: float) -> None:
        cutoff_bucket = self._bucket_key(now - self._window)
        expired = [k for k in self._buckets if k < cutoff_bucket]
        for k in expired:
            del self._buckets[k]

    def claim(self, request_id: str, request_timestamp: float) -> bool:
        """
        Returns True if the nonce was successfully claimed (first time seen).
        Returns False if the nonce was already seen (replay attempt).
        """
        now = time.time()
        with self._lock:
            self._evict_expired(now)

            # Search all active buckets for this nonce
            for bucket in self._buckets.values():
                if request_id in bucket:
                    return False

            # Claim it in the appropriate bucket
            bucket_key = self._bucket_key(request_timestamp)
            if bucket_key not in self._buckets:
                self._buckets[bucket_key] = {}
            self._buckets[bucket_key][request_id] = request_timestamp
            return True

    def seen_count(self) -> int:
        with self._lock:
            return sum(len(b) for b in self._buckets.values())
```

## Solution 4: HMAC Signature Verifier

```python
import hashlib
import hmac
from typing import Optional


class HMACSignatureVerifier:
    """
    Verifies the HMAC-SHA256 signature on a SignedRequest.
    Uses timing-safe comparison to prevent timing attacks.
    """

    def __init__(self, secret_key: bytes):
        self._key = secret_key

    def verify(self, request: SignedRequest) -> bool:
        canonical = request.canonical_string()
        expected_sig = hmac.new(
            self._key, canonical.encode(), hashlib.sha256
        ).hexdigest()
        try:
            return hmac.compare_digest(expected_sig, request.signature)
        except (TypeError, ValueError):
            return False

    def sign(self, request: SignedRequest) -> str:
        """Generate a signature for a new request (used by the sending side)."""
        canonical = request.canonical_string()
        return hmac.new(self._key, canonical.encode(), hashlib.sha256).hexdigest()
```

## Solution 5: Replay-Safe Request Authenticator

```python
from dataclasses import dataclass
from typing import Optional


@dataclass
class AuthenticationResult:
    authenticated: bool
    reason: str = ""
    request_id: str = ""
    age_seconds: float = 0.0


class ReplaySafeRequestAuthenticator:
    """
    Combines HMAC verification, timestamp validation, and nonce tracking
    into a single authentication pipeline.
    Order matters: signature check first (cheapest rejection for forged requests),
    then timestamp (cheap), then nonce claim (state mutation).
    """

    def __init__(
        self,
        signature_verifier: HMACSignatureVerifier,
        timestamp_validator: RequestTimestampValidator,
        nonce_store: RequestNonceStore,
    ):
        self._sig = signature_verifier
        self._ts = timestamp_validator
        self._nonce = nonce_store
        self._total = 0
        self._rejected_sig = 0
        self._rejected_ts = 0
        self._rejected_replay = 0

    def authenticate(self, request: SignedRequest) -> AuthenticationResult:
        self._total += 1

        # Step 1: Verify HMAC signature
        if not self._sig.verify(request):
            self._rejected_sig += 1
            return AuthenticationResult(
                authenticated=False,
                reason="invalid_signature",
                request_id=request.request_id,
            )

        # Step 2: Validate timestamp
        ts_result = self._ts.validate(request.timestamp)
        if not ts_result.valid:
            self._rejected_ts += 1
            return AuthenticationResult(
                authenticated=False,
                reason=ts_result.reason,
                request_id=request.request_id,
                age_seconds=ts_result.age_seconds,
            )

        # Step 3: Claim nonce (detect replay)
        if not self._nonce.claim(request.request_id, request.timestamp):
            self._rejected_replay += 1
            return AuthenticationResult(
                authenticated=False,
                reason="replay_detected",
                request_id=request.request_id,
                age_seconds=ts_result.age_seconds,
            )

        return AuthenticationResult(
            authenticated=True,
            reason="ok",
            request_id=request.request_id,
            age_seconds=ts_result.age_seconds,
        )

    def stats(self) -> dict:
        return {
            "total_requests": self._total,
            "rejected_signature": self._rejected_sig,
            "rejected_timestamp": self._rejected_ts,
            "rejected_replay": self._rejected_replay,
            "authenticated": self._total - self._rejected_sig - self._rejected_ts - self._rejected_replay,
        }
```

## Solution 6: Replay Attack Audit Logger

```python
import json
import time
from pathlib import Path
from threading import Lock


class ReplayAttackAuditLogger:
    """
    Logs authentication failures and replay detections for
    security incident investigation.
    """

    def __init__(self, log_path: str = "/tmp/replay_attack_audit.jsonl"):
        self._path = Path(log_path)
        self._lock = Lock()

    def log(self, result: AuthenticationResult, source_ip: str = "") -> None:
        if result.authenticated:
            return
        record = {
            "ts": time.time(),
            "reason": result.reason,
            "request_id": result.request_id,
            "age_seconds": result.age_seconds,
            "source_ip": source_ip,
        }
        with self._lock:
            with self._path.open("a") as f:
                f.write(json.dumps(record) + "\n")

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        records = []
        if not self._path.exists():
            return {"window_seconds": window_seconds, "failures": 0}
        with self._lock:
            for line in self._path.read_text().splitlines():
                try:
                    r = json.loads(line)
                    if r.get("ts", 0) >= cutoff:
                        records.append(r)
                except json.JSONDecodeError:
                    continue
        by_reason: dict = {}
        for r in records:
            by_reason[r.get("reason", "unknown")] = by_reason.get(r.get("reason"), 0) + 1
        return {
            "window_seconds": window_seconds,
            "failures": len(records),
            "by_reason": by_reason,
            "unique_request_ids": len({r.get("request_id") for r in records}),
        }
```

## Comparison

| Approach | HMAC Verification | Timestamp Check | Nonce Tracking | Full Auth Pipeline | Audit Logging |
|---|---|---|---|---|---|
| HMACSignatureVerifier | Yes (timing-safe) | No | No | No | No |
| RequestTimestampValidator | No | Yes (window) | No | No | No |
| RequestNonceStore | No | No | Yes (bucketed) | No | No |
| ReplaySafeRequestAuthenticator | Via verifier | Via validator | Via store | Yes | No |
| ReplayAttackAuditLogger | No | No | No | No | Yes |

**Best for production**: Set `max_age_seconds=60` for high-security operations (payments, admin actions) and `max_age_seconds=300` for lower-risk webhooks — tighter windows reduce the replay attack surface. Use `bucket_seconds=10` in the nonce store so that each 60-second window creates six buckets, each evicted individually after expiry, avoiding the cost of scanning the full nonce set. Always check signature before timestamp before nonce — this ordering rejects cheap forgeries without touching mutable state. Log every `replay_detected` event with the source IP: a series of replays from the same IP indicates a systematic attack, while scattered IPs may indicate a compromised log store leaking signed requests.
