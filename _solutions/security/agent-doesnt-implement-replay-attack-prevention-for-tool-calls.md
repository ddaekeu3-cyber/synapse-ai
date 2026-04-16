---
title: "Agent Doesn't Implement Replay Attack Prevention for Tool Calls"
description: "Agents that accept tool call requests without nonce or timestamp validation are vulnerable to replay attacks: a captured tool invocation — including its authorization credentials — can be re-submitted seconds or minutes later to trigger unintended side effects such as duplicate payments, repeated file writes, or re-executed state-changing operations. Implement replay attack prevention using per-request nonces, short-lived timestamps, and a nonce deduplication store that rejects any repeated invocation within the validity window."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-replay-attack-prevention-for-tool-calls
tags: [replay-attack, nonce, request-deduplication, idempotency, hmac, tool-security]
symptoms:
  - "Duplicate tool invocations arrive with identical parameters seconds apart"
  - "State-changing tools (payment, write, send) can be triggered multiple times from one user action"
  - "No timestamp validation — a tool request from an hour ago is accepted as valid"
  - "No per-request unique identifier — requests are indistinguishable from replays"
  - "Captured tool call credentials can be reused without the original caller's involvement"
---

## Why This Happens

Tool calls carry authorization — API keys, session tokens, HMAC signatures. If these credentials authenticate the caller but do not bind the request to a specific moment in time and a unique invocation identifier, anyone who captures the request can re-submit it and be treated as the legitimate caller. This is particularly dangerous for state-changing tools: a captured payment tool call re-submitted seconds later processes a second payment with no user involvement. Replay prevention requires two controls working together: a short-lived timestamp that bounds the validity window (rejecting old requests) and a per-request nonce that ensures even requests within the window are accepted only once.

## Solution 1: Signed Tool Call Envelope

```python
import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class SignedToolCallEnvelope:
    tool_name: str
    args: Dict[str, Any]
    nonce: str
    issued_at: float
    signature: str = ""
    caller_id: str = ""
    validity_seconds: float = 30.0

    @classmethod
    def create(
        cls,
        tool_name: str,
        args: Dict[str, Any],
        secret: bytes,
        caller_id: str = "",
        validity_seconds: float = 30.0,
    ) -> "SignedToolCallEnvelope":
        nonce = str(uuid.uuid4())
        issued_at = time.time()
        envelope = cls(
            tool_name=tool_name,
            args=args,
            nonce=nonce,
            issued_at=issued_at,
            caller_id=caller_id,
            validity_seconds=validity_seconds,
        )
        envelope.signature = envelope._compute_signature(secret)
        return envelope

    def _compute_signature(self, secret: bytes) -> str:
        import json
        payload = f"{self.tool_name}|{self.nonce}|{self.issued_at}|{self.caller_id}|"
        payload += json.dumps(self.args, sort_keys=True)
        return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()

    def is_expired(self, clock_skew: float = 5.0) -> bool:
        age = time.time() - self.issued_at
        return age > self.validity_seconds + clock_skew
```

## Solution 2: Nonce Store

```python
import threading
import time
from typing import Dict, Optional


class NonceStore:
    """
    Records seen nonces and their expiry times.
    Rejects nonces that have been seen before within the validity window.
    Evicts expired nonces periodically to bound memory usage.
    """

    def __init__(self, eviction_interval_seconds: float = 60.0):
        self._nonces: Dict[str, float] = {}   # nonce -> expires_at
        self._lock = threading.Lock()
        self._eviction_interval = eviction_interval_seconds
        self._last_eviction = time.time()

    def check_and_store(
        self,
        nonce: str,
        validity_seconds: float = 30.0,
    ) -> bool:
        """Returns True if nonce is new (accepted), False if seen before (replay)."""
        now = time.time()
        self._maybe_evict(now)

        with self._lock:
            if nonce in self._nonces:
                return False   # replay detected
            self._nonces[nonce] = now + validity_seconds
            return True

    def _maybe_evict(self, now: float) -> None:
        if now - self._last_eviction < self._eviction_interval:
            return
        with self._lock:
            expired = [n for n, exp in self._nonces.items() if exp <= now]
            for n in expired:
                del self._nonces[n]
            self._last_eviction = now

    def size(self) -> int:
        with self._lock:
            return len(self._nonces)
```

## Solution 3: Replay Attack Detector

```python
import hashlib
import hmac
import time
from dataclasses import dataclass


@dataclass
class ValidationResult:
    accepted: bool
    rejection_reason: str = ""


class ReplayAttackDetector:
    """
    Validates a SignedToolCallEnvelope for freshness, signature integrity,
    and nonce uniqueness. Rejects on any failure.
    """

    def __init__(
        self,
        nonce_store: NonceStore,
        secret: bytes,
        clock_skew_seconds: float = 5.0,
    ):
        self._store = nonce_store
        self._secret = secret
        self._skew = clock_skew_seconds
        self._accepted = 0
        self._rejected_expired = 0
        self._rejected_signature = 0
        self._rejected_replay = 0

    def validate(self, envelope: SignedToolCallEnvelope) -> ValidationResult:
        # Check timestamp
        if envelope.is_expired(self._skew):
            self._rejected_expired += 1
            age = time.time() - envelope.issued_at
            return ValidationResult(
                accepted=False,
                rejection_reason=f"expired: age={age:.1f}s > validity={envelope.validity_seconds}s",
            )

        # Check signature
        expected = envelope._compute_signature(self._secret)
        if not hmac.compare_digest(expected, envelope.signature):
            self._rejected_signature += 1
            return ValidationResult(
                accepted=False,
                rejection_reason="invalid signature",
            )

        # Check nonce
        if not self._store.check_and_store(envelope.nonce, envelope.validity_seconds):
            self._rejected_replay += 1
            return ValidationResult(
                accepted=False,
                rejection_reason=f"replay detected: nonce '{envelope.nonce}' already seen",
            )

        self._accepted += 1
        return ValidationResult(accepted=True)

    def stats(self) -> dict:
        total = self._accepted + self._rejected_expired + self._rejected_signature + self._rejected_replay
        return {
            "total": total,
            "accepted": self._accepted,
            "rejected_expired": self._rejected_expired,
            "rejected_invalid_signature": self._rejected_signature,
            "rejected_replay": self._rejected_replay,
        }
```

## Solution 4: Replay-Protected Tool Dispatcher

```python
import time
from typing import Any, Callable, Optional


class ReplayProtectedToolDispatcher:
    """
    Wraps tool execution with replay attack validation.
    Raises ReplayAttackError when a request fails validation.
    Emits structured audit events for accepted and rejected calls.
    """

    def __init__(
        self,
        detector: ReplayAttackDetector,
        audit_fn: Optional[Callable[[dict], None]] = None,
    ):
        self._detector = detector
        self._audit = audit_fn or (lambda ev: None)

    async def dispatch(
        self,
        envelope: SignedToolCallEnvelope,
        tool_fn: Callable,
    ) -> Any:
        result = self._detector.validate(envelope)

        if not result.accepted:
            self._audit({
                "event": "tool_call_rejected",
                "tool_name": envelope.tool_name,
                "caller_id": envelope.caller_id,
                "nonce": envelope.nonce,
                "reason": result.rejection_reason,
                "timestamp": time.time(),
            })
            raise ReplayAttackError(
                envelope.tool_name,
                envelope.nonce,
                result.rejection_reason,
            )

        self._audit({
            "event": "tool_call_accepted",
            "tool_name": envelope.tool_name,
            "caller_id": envelope.caller_id,
            "nonce": envelope.nonce,
            "timestamp": time.time(),
        })

        return await tool_fn(**envelope.args)


class ReplayAttackError(Exception):
    def __init__(self, tool_name: str, nonce: str, reason: str):
        super().__init__(
            f"replay attack rejected for tool '{tool_name}' (nonce={nonce}): {reason}"
        )
        self.tool_name = tool_name
        self.nonce = nonce
        self.reason = reason
```

## Solution 5: Replay Attack Audit Logger

```python
import time
from collections import Counter
from threading import Lock
from typing import List


class ReplayAttackAuditLogger:
    """
    Accumulates replay rejection events and surfaces attack frequency,
    top targeted tools, and repeat offender callers.
    """

    def __init__(self, max_records: int = 10000):
        self._max = max_records
        self._records: List[dict] = []
        self._lock = Lock()

    def record_rejection(
        self,
        tool_name: str,
        caller_id: str,
        nonce: str,
        reason: str,
    ) -> None:
        with self._lock:
            if len(self._records) >= self._max:
                self._records.pop(0)
            self._records.append({
                "ts": time.time(),
                "tool_name": tool_name,
                "caller_id": caller_id,
                "nonce": nonce,
                "reason": reason,
            })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r["ts"] >= cutoff]

        if not recent:
            return {"window_seconds": window_seconds, "rejections": 0}

        tool_counts = Counter(r["tool_name"] for r in recent)
        caller_counts = Counter(r["caller_id"] for r in recent if r["caller_id"])
        reason_counts = Counter(r["reason"].split(":")[0] for r in recent)

        return {
            "window_seconds": window_seconds,
            "rejections": len(recent),
            "top_targeted_tools": tool_counts.most_common(5),
            "top_offender_callers": caller_counts.most_common(5),
            "by_reason": dict(reason_counts),
        }
```

## Solution 6: Replay Prevention Dashboard

```python
import time


class ReplayPreventionDashboard:
    """
    Combines detector statistics, nonce store size, and audit summary
    into a single security operations view.
    """

    def __init__(
        self,
        detector: ReplayAttackDetector,
        nonce_store: NonceStore,
        audit_logger: ReplayAttackAuditLogger,
    ):
        self._detector = detector
        self._store = nonce_store
        self._audit = audit_logger

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "detector_stats": self._detector.stats(),
            "nonce_store_size": self._store.size(),
            "recent_rejections": self._audit.summary(window_seconds=3600.0),
        }
```

## Comparison

| Approach | Timestamp Validation | Signature Verification | Nonce Deduplication | Audit Trail | Dashboard |
|---|---|---|---|---|---|
| SignedToolCallEnvelope | Yes (issued_at) | Yes (HMAC-SHA256) | No | No | No |
| NonceStore | No | No | Yes (thread-safe) | No | No |
| ReplayAttackDetector | Via envelope | Via envelope | Via store | No | No |
| ReplayProtectedToolDispatcher | Via detector | Via detector | Via detector | Yes | No |
| ReplayAttackAuditLogger | No | No | No | Yes | No |
| ReplayPreventionDashboard | No | No | No | No | Yes |

**Best for production**: Set `validity_seconds=30` for most tool calls — this bounds the replay window to 30 seconds while tolerating typical network latency and clock skew. Use a Redis-backed `NonceStore` in multi-instance deployments so that a nonce accepted by instance A cannot be replayed against instance B. Alert when `rejected_replay` count exceeds 10 in a 5-minute window from the same `caller_id` — this pattern indicates an active replay attack or a misbehaving client, not a one-off duplicate.
