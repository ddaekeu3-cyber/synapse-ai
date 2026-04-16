---
title: "Agent Doesn't Implement Replay Attack Prevention for Tool Calls"
description: "Agents that process tool call requests without replay protection allow attackers who intercept a signed tool call to re-submit it multiple times — triggering the same payment, message, or state mutation repeatedly. Implement replay attack prevention using nonce-based deduplication that rejects any tool call whose request ID has been seen within the validity window."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-replay-attack-prevention-for-tool-calls
tags: [replay-attack, nonce, idempotency, request-deduplication, tool-security, hmac]
symptoms:
  - "Intercepted tool call re-submitted 10 minutes later triggers a duplicate payment"
  - "No idempotency enforcement — the same signed request can be submitted multiple times"
  - "Tool call logs show identical request IDs at different timestamps from the same session"
  - "No timestamp validation — requests from hours ago are accepted as valid"
  - "Automated retry logic accidentally triggers duplicate side effects due to missing deduplication"
---

## Why This Happens

Tool calls are typically validated for schema correctness and authorization, but not for replay. A valid, signed request is re-executable indefinitely without nonce protection. Replay prevention requires three components: a timestamp bound (reject requests older than N seconds), a nonce/request-ID uniqueness check (reject requests whose ID was already processed), and a rolling nonce store (expire old nonces to bound memory usage).

## Solution 1: Tool Call Request Envelope

```python
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ToolCallEnvelope:
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    tool_name: str = ""
    arguments: Dict[str, Any] = field(default_factory=dict)
    issued_at: float = field(default_factory=time.time)
    session_id: str = ""
    signature: Optional[str] = None   # HMAC of canonical form

    def canonical_form(self) -> str:
        """Deterministic string for signing."""
        import json
        return json.dumps({
            "request_id": self.request_id,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "issued_at": self.issued_at,
            "session_id": self.session_id,
        }, sort_keys=True)

    def age_seconds(self) -> float:
        return time.time() - self.issued_at
```

## Solution 2: Request Timestamp Validator

```python
from dataclasses import dataclass


@dataclass
class TimestampValidationPolicy:
    max_age_seconds: float = 300.0    # reject requests older than 5 minutes
    max_future_seconds: float = 30.0  # reject requests more than 30s in the future
    clock_skew_tolerance: float = 5.0 # allow 5s of clock skew


class RequestTimestampValidator:
    """
    Validates that a tool call envelope's timestamp is within the acceptable window.
    Rejects replays that are outside the freshness window.
    """

    def __init__(self, policy: TimestampValidationPolicy):
        self._policy = policy

    def validate(self, envelope: ToolCallEnvelope) -> tuple:
        """Returns (is_valid, reason)."""
        import time
        now = time.time()
        age = now - envelope.issued_at

        if age > self._policy.max_age_seconds + self._policy.clock_skew_tolerance:
            return False, (
                f"request too old: age={age:.1f}s, "
                f"max={self._policy.max_age_seconds}s"
            )

        if envelope.issued_at > now + self._policy.max_future_seconds:
            return False, (
                f"request timestamp in the future: "
                f"issued_at={envelope.issued_at}, now={now}"
            )

        return True, "timestamp valid"
```

## Solution 3: Nonce Store

```python
import time
from typing import Dict, Optional


class NonceStore:
    """
    Stores seen request IDs with expiry.
    Rejects duplicate request IDs within the validity window.
    Trims expired entries periodically to bound memory usage.
    """

    def __init__(
        self,
        validity_window_seconds: float = 300.0,
        trim_interval_seconds: float = 60.0,
    ):
        self._window = validity_window_seconds
        self._trim_interval = trim_interval_seconds
        self._seen: Dict[str, float] = {}   # request_id -> first_seen_at
        self._last_trim = time.time()

    def check_and_record(self, request_id: str) -> tuple:
        """
        Returns (is_new, reason).
        is_new=True means this request_id is fresh and has been recorded.
        is_new=False means it was already seen — replay detected.
        """
        self._maybe_trim()
        now = time.time()

        if request_id in self._seen:
            first_seen = self._seen[request_id]
            return False, (
                f"replay detected: request_id '{request_id}' "
                f"first seen {now - first_seen:.1f}s ago"
            )

        self._seen[request_id] = now
        return True, "new request"

    def _maybe_trim(self) -> None:
        now = time.time()
        if now - self._last_trim < self._trim_interval:
            return
        cutoff = now - self._window
        expired = [rid for rid, ts in self._seen.items() if ts < cutoff]
        for rid in expired:
            del self._seen[rid]
        self._last_trim = now

    def stats(self) -> dict:
        return {
            "tracked_nonces": len(self._seen),
            "validity_window_seconds": self._window,
        }
```

## Solution 4: Request Signature Verifier

```python
import hashlib
import hmac
from typing import Optional


class RequestSignatureVerifier:
    """
    Verifies HMAC-SHA256 signatures on tool call envelopes.
    The signature is computed over the canonical form of the envelope.
    """

    def __init__(self, secret_key: bytes):
        self._key = secret_key

    def sign(self, envelope: ToolCallEnvelope) -> str:
        canonical = envelope.canonical_form().encode("utf-8")
        return hmac.new(self._key, canonical, hashlib.sha256).hexdigest()

    def verify(self, envelope: ToolCallEnvelope) -> tuple:
        """Returns (is_valid, reason)."""
        if not envelope.signature:
            return False, "no signature present"
        expected = self.sign(envelope)
        if not hmac.compare_digest(envelope.signature, expected):
            return False, "signature mismatch"
        return True, "signature valid"
```

## Solution 5: Replay Prevention Gateway

```python
from typing import Any, Callable, Dict, Optional


class ReplayPreventionGateway:
    """
    Combines timestamp validation, signature verification, and nonce checking
    into a single gate before tool call execution.
    All three checks must pass for the tool call to proceed.
    """

    def __init__(
        self,
        nonce_store: NonceStore,
        timestamp_validator: RequestTimestampValidator,
        signature_verifier: Optional[RequestSignatureVerifier] = None,
    ):
        self._nonces = nonce_store
        self._ts_validator = timestamp_validator
        self._sig_verifier = signature_verifier

    async def validate_and_dispatch(
        self,
        envelope: ToolCallEnvelope,
        tool_fn: Callable,
    ) -> Any:
        # 1. Timestamp check
        ts_valid, ts_reason = self._ts_validator.validate(envelope)
        if not ts_valid:
            raise ValueError(f"Timestamp validation failed: {ts_reason}")

        # 2. Signature check
        if self._sig_verifier:
            sig_valid, sig_reason = self._sig_verifier.verify(envelope)
            if not sig_valid:
                raise ValueError(f"Signature verification failed: {sig_reason}")

        # 3. Nonce / replay check
        is_new, nonce_reason = self._nonces.check_and_record(envelope.request_id)
        if not is_new:
            raise ValueError(f"Replay attack detected: {nonce_reason}")

        # All checks passed — execute
        return await tool_fn(**envelope.arguments)
```

## Solution 6: Replay Attack Audit Log

```python
import time
from dataclasses import dataclass, field
from typing import List


@dataclass
class ReplayAttemptRecord:
    request_id: str
    tool_name: str
    session_id: str
    rejection_reason: str
    detected_at: float = field(default_factory=time.time)


class ReplayAttackAuditLog:
    """
    Records rejected replay attempts for security analysis.
    A spike in replay attempts may indicate an active attack.
    """

    def __init__(self, max_entries: int = 5000):
        self._records: List[ReplayAttemptRecord] = []
        self._max = max_entries

    def record_rejection(
        self,
        envelope: ToolCallEnvelope,
        reason: str,
    ) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append(ReplayAttemptRecord(
            request_id=envelope.request_id,
            tool_name=envelope.tool_name,
            session_id=envelope.session_id,
            rejection_reason=reason,
        ))

    def recent_attempts(self, window_seconds: float = 3600.0) -> List[ReplayAttemptRecord]:
        cutoff = time.time() - window_seconds
        return [r for r in self._records if r.detected_at >= cutoff]

    def summary(self, window_seconds: float = 3600.0) -> dict:
        recent = self.recent_attempts(window_seconds)
        by_session: Dict[str, int] = {}
        for r in recent:
            by_session[r.session_id] = by_session.get(r.session_id, 0) + 1
        return {
            "total_rejected": len(recent),
            "by_session": dict(sorted(by_session.items(), key=lambda x: -x[1])[:10]),
            "most_replayed_tool": max(
                (r.tool_name for r in recent), key=lambda t: sum(1 for r in recent if r.tool_name == t)
            ) if recent else None,
        }


from typing import Dict
```

## Comparison

| Approach | Timestamp Validation | Nonce Deduplication | Signature Verification | Audit Log | Gateway |
|---|---|---|---|---|---|
| RequestTimestampValidator | Yes | No | No | No | No |
| NonceStore | No | Yes (with expiry) | No | No | No |
| RequestSignatureVerifier | No | No | Yes (HMAC-SHA256) | No | No |
| ReplayPreventionGateway | Via validator | Via nonce store | Via verifier | No | Yes |
| ReplayAttackAuditLog | No | No | No | Yes | No |

**Best for production**: Set `max_age_seconds=300` (5 minutes) — this is short enough to prevent replay attacks but long enough to tolerate network delays and retry logic. The nonce store window must match or exceed `max_age_seconds` so every valid request ID is tracked until it can no longer be replayed. Use HMAC-SHA256 signatures for tool calls that mutate state (payments, sends, deletes) — read-only tool calls may not need signature verification. Monitor `ReplayAttackAuditLog.summary()` for spikes: more than 10 replay attempts from the same session in an hour warrants investigation and possible session revocation.
