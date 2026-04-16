---
title: "Agent Doesn't Implement Tool Call Replay Attack Prevention"
description: "Agents that process tool call requests without replay detection allow attackers to capture a valid signed or authenticated tool invocation and re-submit it later to trigger the same action repeatedly — withdrawing funds twice, sending a message again, or re-executing a privileged operation. Implement replay attack prevention with per-call nonces, short-lived timestamps, and a seen-nonce store that rejects duplicate invocations within the replay window."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-tool-call-replay-attack-prevention
tags: [replay-attack, nonce, idempotency, tool-security, request-integrity, anti-replay]
symptoms:
  - "Same tool call request submitted twice executes the action twice"
  - "Captured tool invocation tokens can be replayed hours later"
  - "No timestamp validation — a week-old signed request is still accepted"
  - "Tool calls lack unique identifiers that could be checked against a seen-set"
  - "Side-effectful tools (payments, sends, deletes) have no duplicate execution guard"
---

## Why This Happens

Replay attacks exploit valid credentials: the attacker does not need to forge anything, only capture and re-submit. Standard authentication proves who made the request but not when or whether it has already been processed. Preventing replay requires two independent controls: a short expiry window enforced by a timestamp (so old captures become invalid) and a per-call nonce stored in a seen-set (so two identical calls within the window are detected). Neither alone is sufficient — timestamps without nonces allow replay within the window; nonces without timestamps require an ever-growing seen-set.

## Solution 1: Replay-Protected Tool Call Envelope

```python
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ToolCallEnvelope:
    tool_name: str
    args: Dict[str, Any]
    nonce: str = field(default_factory=lambda: uuid.uuid4().hex)
    issued_at: float = field(default_factory=time.time)
    session_id: str = ""
    signature: Optional[str] = None   # HMAC-SHA256 over canonical payload

    def canonical_payload(self) -> str:
        """Deterministic string for signing — excludes signature field."""
        payload = {
            "tool_name": self.tool_name,
            "args": self.args,
            "nonce": self.nonce,
            "issued_at": self.issued_at,
            "session_id": self.session_id,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def sign(self, secret: bytes) -> None:
        mac = hmac.new(secret, self.canonical_payload().encode(), hashlib.sha256)
        self.signature = mac.hexdigest()

    def verify_signature(self, secret: bytes) -> bool:
        if not self.signature:
            return False
        expected = hmac.new(
            secret, self.canonical_payload().encode(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(self.signature, expected)
```

## Solution 2: Nonce Store

```python
import time
from threading import Lock
from typing import Dict, Optional


class NonceStore:
    """
    Tracks seen nonces within a rolling expiry window.
    Expired nonces are purged lazily on each operation.
    Thread-safe for concurrent tool dispatchers.
    """

    def __init__(self, window_seconds: float = 300.0):
        self._window = window_seconds
        self._seen: Dict[str, float] = {}   # nonce -> stored_at
        self._lock = Lock()

    def _purge_expired(self, now: float) -> None:
        cutoff = now - self._window
        expired = [n for n, ts in self._seen.items() if ts < cutoff]
        for n in expired:
            del self._seen[n]

    def check_and_store(self, nonce: str) -> bool:
        """
        Returns True if the nonce is fresh (first time seen).
        Returns False if the nonce has already been seen (replay).
        """
        now = time.time()
        with self._lock:
            self._purge_expired(now)
            if nonce in self._seen:
                return False
            self._seen[nonce] = now
            return True

    def size(self) -> int:
        with self._lock:
            return len(self._seen)

    def stats(self) -> dict:
        with self._lock:
            return {
                "window_seconds": self._window,
                "stored_nonces": len(self._seen),
            }
```

## Solution 3: Timestamp Validator

```python
import time
from dataclasses import dataclass


@dataclass
class TimestampValidationResult:
    valid: bool
    reason: str
    age_seconds: float


class ToolCallTimestampValidator:
    """
    Rejects tool call envelopes whose issued_at timestamp is outside
    the acceptable window. Handles both expired (too old) and
    future-dated (clock skew / pre-computation attack) envelopes.
    """

    def __init__(
        self,
        max_age_seconds: float = 300.0,
        max_future_seconds: float = 30.0,
    ):
        self._max_age = max_age_seconds
        self._max_future = max_future_seconds

    def validate(self, envelope: "ToolCallEnvelope") -> TimestampValidationResult:
        now = time.time()
        age = now - envelope.issued_at

        if age > self._max_age:
            return TimestampValidationResult(
                valid=False,
                reason=f"envelope expired: {age:.0f}s old (max {self._max_age}s)",
                age_seconds=age,
            )
        if age < -self._max_future:
            return TimestampValidationResult(
                valid=False,
                reason=f"envelope issued too far in the future: {-age:.0f}s ahead",
                age_seconds=age,
            )
        return TimestampValidationResult(valid=True, reason="ok", age_seconds=age)
```

## Solution 4: Replay-Protected Tool Dispatcher

```python
import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Optional


class ReplayCheckResult(str, Enum):
    ALLOWED = "allowed"
    REPLAY_NONCE = "replay_nonce"
    EXPIRED_TIMESTAMP = "expired_timestamp"
    INVALID_SIGNATURE = "invalid_signature"
    MISSING_ENVELOPE = "missing_envelope"


@dataclass
class DispatchOutcome:
    result: Any
    replay_check: ReplayCheckResult
    blocked: bool


class ReplayProtectedToolDispatcher:
    """
    Validates signature, timestamp, and nonce before dispatching a tool call.
    All three checks must pass. On any failure, the call is blocked and logged.
    """

    def __init__(
        self,
        nonce_store: NonceStore,
        timestamp_validator: ToolCallTimestampValidator,
        signing_secret: bytes,
        require_signature: bool = True,
    ):
        self._nonces = nonce_store
        self._ts_validator = timestamp_validator
        self._secret = signing_secret
        self._require_sig = require_signature
        self._blocked_count = 0
        self._allowed_count = 0

    async def dispatch(
        self,
        envelope: "ToolCallEnvelope",
        tool_fn: Callable,
    ) -> DispatchOutcome:
        # 1. Signature check
        if self._require_sig:
            if not envelope.verify_signature(self._secret):
                self._blocked_count += 1
                return DispatchOutcome(
                    result=None,
                    replay_check=ReplayCheckResult.INVALID_SIGNATURE,
                    blocked=True,
                )

        # 2. Timestamp check
        ts_result = self._ts_validator.validate(envelope)
        if not ts_result.valid:
            self._blocked_count += 1
            return DispatchOutcome(
                result=None,
                replay_check=ReplayCheckResult.EXPIRED_TIMESTAMP,
                blocked=True,
            )

        # 3. Nonce check
        if not self._nonces.check_and_store(envelope.nonce):
            self._blocked_count += 1
            return DispatchOutcome(
                result=None,
                replay_check=ReplayCheckResult.REPLAY_NONCE,
                blocked=True,
            )

        # All checks passed — dispatch
        result = await tool_fn(**envelope.args)
        self._allowed_count += 1
        return DispatchOutcome(
            result=result,
            replay_check=ReplayCheckResult.ALLOWED,
            blocked=False,
        )

    def stats(self) -> dict:
        total = self._blocked_count + self._allowed_count
        return {
            "total_dispatches": total,
            "allowed": self._allowed_count,
            "blocked": self._blocked_count,
            "block_rate": round(self._blocked_count / max(total, 1), 4),
        }
```

## Solution 5: Replay Attempt Auditor

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ReplayAuditRecord:
    tool_name: str
    nonce: str
    session_id: str
    check_result: str
    issued_at: float
    detected_at: float = field(default_factory=time.time)
    envelope_age_seconds: float = 0.0


class ReplayAttemptAuditor:
    """
    Records blocked tool call dispatches for security audit and
    incident response. High rates of REPLAY_NONCE blocks may indicate
    an active replay attack; EXPIRED_TIMESTAMP blocks suggest
    captured credentials being submitted after the expiry window.
    """

    def __init__(self, max_records: int = 10000):
        self._max = max_records
        self._records: List[ReplayAuditRecord] = []

    def record_block(
        self,
        envelope: "ToolCallEnvelope",
        outcome: "DispatchOutcome",
        age_seconds: float = 0.0,
    ) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append(ReplayAuditRecord(
            tool_name=envelope.tool_name,
            nonce=envelope.nonce,
            session_id=envelope.session_id,
            check_result=outcome.replay_check.value,
            issued_at=envelope.issued_at,
            envelope_age_seconds=age_seconds,
        ))

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r.detected_at >= cutoff]
        by_reason: Dict[str, int] = {}
        by_tool: Dict[str, int] = {}
        for r in recent:
            by_reason[r.check_result] = by_reason.get(r.check_result, 0) + 1
            by_tool[r.tool_name] = by_tool.get(r.tool_name, 0) + 1
        return {
            "window_seconds": window_seconds,
            "blocked_calls": len(recent),
            "by_reason": by_reason,
            "by_tool": dict(sorted(by_tool.items(), key=lambda x: -x[1])),
        }
```

## Solution 6: Envelope Factory for Agent Sessions

```python
import time
from typing import Any, Dict, Optional


class ToolCallEnvelopeFactory:
    """
    Produces signed ToolCallEnvelopes for outgoing tool call requests.
    Ensures each envelope gets a unique nonce and is signed before dispatch.
    """

    def __init__(self, signing_secret: bytes, session_id: str = ""):
        self._secret = signing_secret
        self._session_id = session_id

    def create(
        self,
        tool_name: str,
        args: Dict[str, Any],
    ) -> "ToolCallEnvelope":
        envelope = ToolCallEnvelope(
            tool_name=tool_name,
            args=args,
            session_id=self._session_id,
        )
        envelope.sign(self._secret)
        return envelope

    def create_batch(
        self,
        calls: list,
    ) -> list:
        """calls: list of (tool_name, args) tuples"""
        return [self.create(name, args) for name, args in calls]
```

## Comparison

| Approach | Nonce Dedup | Timestamp Expiry | Signature Verification | Audit Log | Batch Support |
|---|---|---|---|---|---|
| NonceStore | Yes (sliding window) | No | No | No | No |
| ToolCallTimestampValidator | No | Yes (max age + future) | No | No | No |
| ReplayProtectedToolDispatcher | Via store | Via validator | Yes (HMAC-SHA256) | No | No |
| ReplayAttemptAuditor | No | No | No | Yes | No |
| ToolCallEnvelopeFactory | No | No | Yes (signs) | No | Yes |

**Best for production**: Set `max_age_seconds=300` (5 minutes) — long enough to tolerate clock skew and network delays, short enough to limit the replay window. Use a distributed nonce store (Redis with TTL equal to max_age_seconds) in multi-instance deployments — an in-process dict only prevents replays hitting the same instance. Apply replay protection to all side-effectful tools (payments, sends, deletes) and skip read-only tools to reduce overhead. Monitor `ReplayAttemptAuditor.summary()`: a spike in `replay_nonce` blocks from a single session_id indicates an active attack; a spike in `expired_timestamp` blocks suggests captured credentials being tested post-expiry.
