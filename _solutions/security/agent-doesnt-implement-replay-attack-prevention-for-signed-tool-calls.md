---
title: "Agent Doesn't Implement Replay Attack Prevention for Signed Tool Calls"
description: "Agents that sign tool call requests but never validate nonces or timestamps allow replay attacks: an attacker who intercepts a signed 'transfer funds' tool call can resubmit it minutes later and the signature check passes because the signature is still valid. Implement replay attack prevention that requires a nonce and timestamp in every signed tool call, rejects calls outside a narrow time window, and deduplicates nonces to prevent reuse."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-replay-attack-prevention-for-signed-tool-calls
tags: [replay-attack, nonce, signature-validation, timestamp-validation, tool-security, hmac]
symptoms:
  - "Signed tool calls can be captured and resubmitted successfully after the original operation"
  - "No nonce field in tool call requests — same signature is valid indefinitely"
  - "No timestamp validation — signed calls from hours ago are accepted"
  - "Tool call signatures verified for authenticity but not for freshness"
  - "Replay of a payment or state-mutation tool call succeeds without detection"
---

## Why This Happens

HMAC or RSA signatures prove that a message was created by a party holding the signing key. They do not prove that the message is being submitted for the first time, or that it was created recently. An attacker who captures a valid signed tool call — through network interception, log access, or a compromised intermediate — can replay it verbatim and the signature verification passes. Replay prevention requires two mechanisms: a timestamp window that rejects calls created more than N seconds ago, and a nonce registry that rejects calls whose nonce has been seen before, even if the timestamp is within the window.

## Solution 1: Signed Tool Call Envelope

```python
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class SignedToolCallEnvelope:
    tool_name: str
    args: Dict[str, Any]
    nonce: str                    # unique per call, never reused
    timestamp: float              # unix epoch seconds at time of signing
    signature: str = ""           # HMAC-SHA256 of canonical payload
    signer_id: str = ""           # identifies which key was used


def _canonical_payload(
    tool_name: str,
    args: Dict[str, Any],
    nonce: str,
    timestamp: float,
) -> bytes:
    """Deterministic serialization for signing and verification."""
    payload = {
        "tool_name": tool_name,
        "args": args,
        "nonce": nonce,
        "timestamp": timestamp,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


class ToolCallSigner:
    """Signs tool call envelopes with HMAC-SHA256."""

    def __init__(self, secret_key: bytes, signer_id: str = "agent"):
        self._key = secret_key
        self._signer_id = signer_id

    def sign(
        self,
        tool_name: str,
        args: Dict[str, Any],
        nonce: Optional[str] = None,
    ) -> SignedToolCallEnvelope:
        nonce = nonce or uuid.uuid4().hex
        ts = time.time()
        payload = _canonical_payload(tool_name, args, nonce, ts)
        sig = hmac.new(self._key, payload, hashlib.sha256).hexdigest()
        return SignedToolCallEnvelope(
            tool_name=tool_name,
            args=args,
            nonce=nonce,
            timestamp=ts,
            signature=sig,
            signer_id=self._signer_id,
        )
```

## Solution 2: Nonce Registry

```python
import time
from threading import Lock
from typing import Set


class NonceRegistry:
    """
    Stores recently seen nonces and rejects duplicates.
    Entries expire after TTL to bound memory consumption.
    TTL should be at least as long as the timestamp window.
    """

    def __init__(self, ttl_seconds: float = 300.0, max_entries: int = 100000):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._seen: dict = {}   # nonce -> recorded_at
        self._lock = Lock()

    def check_and_register(self, nonce: str) -> bool:
        """
        Returns True if nonce is new (not a replay).
        Returns False if nonce has been seen before.
        """
        with self._lock:
            self._evict_expired()
            if nonce in self._seen:
                return False
            if len(self._seen) >= self._max:
                # Evict oldest to stay within bounds
                oldest = min(self._seen, key=lambda k: self._seen[k])
                del self._seen[oldest]
            self._seen[nonce] = time.time()
            return True

    def _evict_expired(self) -> None:
        now = time.time()
        expired = [n for n, ts in self._seen.items() if now - ts > self._ttl]
        for n in expired:
            del self._seen[n]

    def stats(self) -> dict:
        with self._lock:
            return {"registered_nonces": len(self._seen), "ttl_seconds": self._ttl}
```

## Solution 3: Timestamp Window Validator

```python
import time
from dataclasses import dataclass


@dataclass
class TimestampWindowConfig:
    max_age_seconds: float = 60.0     # reject calls older than this
    max_future_seconds: float = 10.0  # reject calls timestamped in the future beyond this
    clock_skew_tolerance: float = 5.0  # allow slight clock drift


class TimestampWindowValidator:
    """
    Validates that a tool call timestamp falls within an acceptable window
    relative to the current time. Rejects stale and future-dated calls.
    """

    def __init__(self, config: TimestampWindowConfig):
        self._cfg = config

    def validate(self, timestamp: float) -> tuple[bool, str]:
        now = time.time()
        age = now - timestamp

        if age > self._cfg.max_age_seconds + self._cfg.clock_skew_tolerance:
            return False, (
                f"Call timestamp is {age:.1f}s old — "
                f"exceeds max_age_seconds ({self._cfg.max_age_seconds})"
            )

        if timestamp > now + self._cfg.max_future_seconds + self._cfg.clock_skew_tolerance:
            skew = timestamp - now
            return False, (
                f"Call timestamp is {skew:.1f}s in the future — "
                f"exceeds max_future_seconds ({self._cfg.max_future_seconds})"
            )

        return True, ""
```

## Solution 4: Replay-Resistant Tool Call Verifier

```python
import hashlib
import hmac
from typing import Dict, Optional


class ReplayResistantToolCallVerifier:
    """
    Verifies signed tool call envelopes for:
    1. Signature authenticity (HMAC-SHA256)
    2. Timestamp freshness (within window)
    3. Nonce uniqueness (not seen before)
    All three checks must pass for the call to be accepted.
    """

    def __init__(
        self,
        secret_key: bytes,
        nonce_registry: NonceRegistry,
        timestamp_validator: TimestampWindowValidator,
    ):
        self._key = secret_key
        self._nonces = nonce_registry
        self._ts_validator = timestamp_validator

    def verify(self, envelope: SignedToolCallEnvelope) -> tuple[bool, str]:
        # 1. Timestamp check first — cheap, avoids HMAC on stale calls
        ts_ok, ts_reason = self._ts_validator.validate(envelope.timestamp)
        if not ts_ok:
            return False, f"timestamp_rejected: {ts_reason}"

        # 2. Signature check
        payload = _canonical_payload(
            envelope.tool_name, envelope.args,
            envelope.nonce, envelope.timestamp,
        )
        expected_sig = hmac.new(self._key, payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(envelope.signature, expected_sig):
            return False, "signature_invalid: HMAC mismatch"

        # 3. Nonce uniqueness check — after signature to avoid nonce pollution on forged calls
        if not self._nonces.check_and_register(envelope.nonce):
            return False, f"nonce_replayed: nonce '{envelope.nonce}' already seen"

        return True, ""
```

## Solution 5: Replay Attack Audit Logger

```python
import time
from typing import List


class ReplayAttackAuditLogger:
    """
    Records every rejected tool call with rejection reason.
    Surfaces replay attempt patterns by nonce, tool name, and time window.
    """

    def __init__(self, max_records: int = 10000):
        self._max = max_records
        self._records: List[dict] = []

    def record_rejection(
        self,
        envelope: SignedToolCallEnvelope,
        reason: str,
        session_id: str = "",
    ) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "tool_name": envelope.tool_name,
            "nonce": envelope.nonce,
            "envelope_timestamp": envelope.timestamp,
            "signer_id": envelope.signer_id,
            "reason": reason,
            "session_id": session_id,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        reasons: dict = {}
        for r in recent:
            cat = r["reason"].split(":")[0]
            reasons[cat] = reasons.get(cat, 0) + 1
        return {
            "window_seconds": window_seconds,
            "total_rejections": len(recent),
            "by_reason": reasons,
            "replay_attempts": reasons.get("nonce_replayed", 0),
        }
```

## Solution 6: Replay Prevention Dashboard

```python
import time


class ReplayPreventionDashboard:
    """
    Combines nonce registry stats, timestamp window config, and
    audit log summary into a single security operational view.
    """

    def __init__(
        self,
        nonce_registry: NonceRegistry,
        timestamp_validator: TimestampWindowValidator,
        audit_logger: ReplayAttackAuditLogger,
    ):
        self._nonces = nonce_registry
        self._ts = timestamp_validator
        self._audit = audit_logger

    def render(self) -> dict:
        cfg = self._ts._cfg
        return {
            "generated_at": time.time(),
            "timestamp_window": {
                "max_age_seconds": cfg.max_age_seconds,
                "max_future_seconds": cfg.max_future_seconds,
                "clock_skew_tolerance": cfg.clock_skew_tolerance,
            },
            "nonce_registry": self._nonces.stats(),
            "rejections_1h": self._audit.summary(3600.0),
            "rejections_24h": self._audit.summary(86400.0),
        }
```

## Comparison

| Approach | Signature Verification | Timestamp Window | Nonce Deduplication | Audit | Dashboard |
|---|---|---|---|---|---|
| ToolCallSigner | Yes (signs) | No | No | No | No |
| NonceRegistry | No | No | Yes (TTL-backed) | No | No |
| TimestampWindowValidator | No | Yes | No | No | No |
| ReplayResistantToolCallVerifier | Yes (verifies) | Via validator | Via registry | No | No |
| ReplayAttackAuditLogger | No | No | No | Yes | No |
| ReplayPreventionDashboard | No | No | No | No | Yes |

**Best for production**: Set `max_age_seconds=60` — this bounds the replay window to one minute, which is short enough to prevent most attacks while accommodating normal network latency. Set `NonceRegistry.ttl_seconds` to at least `max_age_seconds * 2` so that nonces from valid calls are still registered when a replay attempt arrives within the timestamp window. Verify signature before registering the nonce (as `ReplayResistantToolCallVerifier` does) — otherwise an attacker can poison the nonce registry with forged calls, causing denial of service for valid calls that happen to collide. Monitor `replay_attempts` in `ReplayAttackAuditLogger`: any non-zero count in production warrants investigation of how valid signed calls are leaking to attackers.
