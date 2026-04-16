---
title: "Agent Doesn't Implement Cross-Origin Request Validation for Webhook Tool Calls"
description: "Agents that accept webhook-triggered tool calls without validating origin signatures allow any party that can reach the webhook endpoint to trigger arbitrary tool executions — injecting data, causing the agent to take actions, or exhausting tool quotas. Implement webhook origin validation with HMAC signature verification, origin allowlisting, and replay attack prevention for every inbound webhook."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-cross-origin-request-validation-for-webhook-tool-calls
tags: [webhook-security, hmac-validation, origin-validation, replay-prevention, signature-verification, webhook-forgery]
symptoms:
  - "Any party that discovers the webhook URL can trigger tool executions"
  - "No HMAC signature verification on inbound webhook payloads"
  - "Replayed webhook requests trigger duplicate tool calls"
  - "No allowlist of accepted source origins or IP ranges"
  - "Webhook payloads are passed directly to tool handlers without authentication"
---

## Why This Happens

Webhook endpoints are HTTP endpoints that accept POST requests. Without authentication, they are public APIs — anyone who knows the URL can post to them. HMAC signature validation (signing the payload with a shared secret and verifying on receipt) proves the payload came from a party with the secret. Timestamp validation prevents replay attacks where a valid signed request from the past is resent. Origin allowlisting adds a defense-in-depth layer for known source IPs.

## Solution 1: Webhook Validation Policy

```python
from dataclasses import dataclass, field
from typing import Optional, Set


@dataclass
class WebhookValidationPolicy:
    source_name: str
    signing_secret: bytes
    signature_header: str = "X-Webhook-Signature"
    timestamp_header: str = "X-Webhook-Timestamp"
    max_age_seconds: float = 300.0       # reject requests older than 5 minutes
    allowed_origins: Set[str] = field(default_factory=set)   # empty = any origin
    require_https: bool = True
    algorithm: str = "sha256"
```

## Solution 2: HMAC Signature Verifier

```python
import hashlib
import hmac
import time
from typing import Optional


class WebhookHMACVerifier:
    """
    Verifies HMAC-SHA256 signatures on webhook payloads.
    Supports both raw body signing and payload+timestamp signing.
    """

    def verify(
        self,
        payload_bytes: bytes,
        received_signature: str,
        secret: bytes,
        algorithm: str = "sha256",
    ) -> bool:
        try:
            hash_fn = getattr(hashlib, algorithm)
        except AttributeError:
            return False
        expected = hmac.new(secret, payload_bytes, hash_fn).hexdigest()
        # Accept both raw hex and "sha256=..." prefixed formats
        received = received_signature
        if "=" in received:
            received = received.split("=", 1)[1]
        return hmac.compare_digest(expected, received)

    def verify_with_timestamp(
        self,
        payload_bytes: bytes,
        timestamp_str: str,
        received_signature: str,
        secret: bytes,
        max_age_seconds: float = 300.0,
    ) -> tuple:
        """Returns (valid: bool, reason: str)."""
        try:
            ts = float(timestamp_str)
        except (ValueError, TypeError):
            return False, "invalid_timestamp"

        age = time.time() - ts
        if age > max_age_seconds:
            return False, f"request_too_old: {age:.0f}s"
        if age < -30:
            return False, "timestamp_in_future"

        signed_payload = f"{timestamp_str}:{payload_bytes.decode(errors='replace')}".encode()
        if not self.verify(signed_payload, received_signature, secret):
            return False, "invalid_signature"

        return True, "ok"
```

## Solution 3: Replay Attack Preventer

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Set, Tuple


class ReplayAttackPreventer:
    """
    Tracks recently seen webhook nonces/signatures to prevent replay attacks.
    Uses a sliding window deque bounded by TTL.
    """

    def __init__(self, window_seconds: float = 600.0, max_entries: int = 10000):
        self._window = window_seconds
        self._max = max_entries
        self._seen: Set[str] = set()
        self._entries: Deque[Tuple[float, str]] = deque()
        self._lock = Lock()
        self._replays_blocked = 0

    def check_and_record(self, fingerprint: str) -> bool:
        """Returns True if new (not a replay), False if seen before."""
        now = time.time()
        with self._lock:
            self._evict(now)
            if fingerprint in self._seen:
                self._replays_blocked += 1
                return False
            self._seen.add(fingerprint)
            self._entries.append((now, fingerprint))
            if len(self._entries) > self._max:
                _, oldest_fp = self._entries.popleft()
                self._seen.discard(oldest_fp)
            return True

    def _evict(self, now: float) -> None:
        cutoff = now - self._window
        while self._entries and self._entries[0][0] < cutoff:
            _, fp = self._entries.popleft()
            self._seen.discard(fp)

    def replays_blocked(self) -> int:
        return self._replays_blocked
```

## Solution 4: Webhook Request Validator

```python
import hashlib
from typing import Dict, Optional


class WebhookRequestValidator:
    """
    Combines HMAC verification, timestamp validation, origin check,
    and replay prevention into a single validation call.
    """

    def __init__(
        self,
        verifier: WebhookHMACVerifier,
        replay_preventer: ReplayAttackPreventer,
        policies: Dict[str, WebhookValidationPolicy],
    ):
        self._verifier = verifier
        self._replay = replay_preventer
        self._policies = policies
        self._valid_count = 0
        self._rejected_count = 0

    def validate(
        self,
        source_name: str,
        payload_bytes: bytes,
        headers: Dict[str, str],
        source_ip: Optional[str] = None,
    ) -> dict:
        policy = self._policies.get(source_name)
        if policy is None:
            self._rejected_count += 1
            return {"valid": False, "reason": "unknown_source"}

        # Origin allowlist check
        if policy.allowed_origins and source_ip and source_ip not in policy.allowed_origins:
            self._rejected_count += 1
            return {"valid": False, "reason": "origin_not_allowed"}

        signature = headers.get(policy.signature_header, "")
        timestamp = headers.get(policy.timestamp_header, "")

        if not signature:
            self._rejected_count += 1
            return {"valid": False, "reason": "missing_signature"}

        if timestamp:
            valid, reason = self._verifier.verify_with_timestamp(
                payload_bytes, timestamp, signature,
                policy.signing_secret, policy.max_age_seconds,
            )
        else:
            valid = self._verifier.verify(payload_bytes, signature, policy.signing_secret)
            reason = "ok" if valid else "invalid_signature"

        if not valid:
            self._rejected_count += 1
            return {"valid": False, "reason": reason}

        # Replay check using payload hash + timestamp as fingerprint
        fingerprint = hashlib.sha256(
            f"{timestamp}:{signature}".encode()
        ).hexdigest()
        if not self._replay.check_and_record(fingerprint):
            self._rejected_count += 1
            return {"valid": False, "reason": "replay_attack"}

        self._valid_count += 1
        return {"valid": True, "source": source_name, "reason": "ok"}

    def stats(self) -> dict:
        return {
            "valid": self._valid_count,
            "rejected": self._rejected_count,
            "replays_blocked": self._replay.replays_blocked(),
        }
```

## Solution 5: Validated Webhook Tool Dispatcher

```python
import json
from typing import Any, Callable, Dict, Optional


class ValidatedWebhookToolDispatcher:
    """
    Accepts raw webhook requests, validates them, and dispatches
    to the appropriate tool handler only if validation passes.
    """

    def __init__(
        self,
        validator: WebhookRequestValidator,
        tool_handlers: Dict[str, Callable],
    ):
        self._validator = validator
        self._handlers = tool_handlers

    async def handle(
        self,
        source_name: str,
        payload_bytes: bytes,
        headers: Dict[str, str],
        source_ip: Optional[str] = None,
    ) -> dict:
        validation = self._validator.validate(
            source_name, payload_bytes, headers, source_ip
        )
        if not validation["valid"]:
            return {
                "success": False,
                "http_status": 401,
                "reason": validation["reason"],
            }

        try:
            payload = json.loads(payload_bytes)
        except json.JSONDecodeError:
            return {"success": False, "http_status": 400, "reason": "invalid_json"}

        event_type = payload.get("event_type", "")
        handler = self._handlers.get(event_type)
        if handler is None:
            return {"success": False, "http_status": 404, "reason": "unknown_event_type"}

        result = await handler(payload)
        return {"success": True, "http_status": 200, "result": result}
```

## Solution 6: Webhook Security Dashboard

```python
import time


class WebhookSecurityDashboard:
    """Combines validation stats, replay counts, and policy overview."""

    def __init__(
        self,
        validator: WebhookRequestValidator,
        replay_preventer: ReplayAttackPreventer,
        policies: dict,
    ):
        self._validator = validator
        self._replay = replay_preventer
        self._policies = policies

    def render(self) -> dict:
        stats = self._validator.stats()
        total = stats["valid"] + stats["rejected"]
        return {
            "generated_at": time.time(),
            "validation_stats": stats,
            "rejection_rate": round(stats["rejected"] / max(total, 1), 4),
            "configured_sources": list(self._policies.keys()),
            "replay_window_active": True,
        }
```

## Comparison

| Approach | HMAC Verification | Timestamp Validation | Replay Prevention | Origin Allowlist | Full Pipeline |
|---|---|---|---|---|---|
| WebhookHMACVerifier | Yes | Yes (+timestamp) | No | No | No |
| ReplayAttackPreventer | No | No | Yes (fingerprint) | No | No |
| WebhookRequestValidator | Via verifier | Via verifier | Via preventer | Yes | Yes |
| ValidatedWebhookToolDispatcher | Via validator | Via validator | Via validator | Via validator | Yes |
| WebhookSecurityDashboard | No | No | No | No | Yes |

**Best for production**: Always sign both the payload and the timestamp together (`f"{timestamp}:{payload}"`) — signing only the payload allows an attacker to replay a valid old request indefinitely. Set `max_age_seconds=300` (5 minutes) to tolerate clock skew while preventing meaningful replay windows. Generate signing secrets with at least 32 bytes of entropy (`secrets.token_bytes(32)`) and rotate them on a quarterly schedule using zero-downtime rotation (brief dual-key validation window). Never log the full payload before validation — logging a malicious payload could itself be an attack vector if your log processor evaluates embedded expressions.
