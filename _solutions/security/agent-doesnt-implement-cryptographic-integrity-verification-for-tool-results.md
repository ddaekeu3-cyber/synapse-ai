---
title: "Agent Doesn't Implement Cryptographic Integrity Verification for Tool Results"
description: "Agents that accept tool results without verifying their integrity are vulnerable to man-in-the-middle tampering: a network adversary or compromised tool server can modify results in transit, injecting false data that the LLM treats as ground truth. Implement cryptographic integrity verification using HMAC signatures or content hashes that the tool signs at generation time and the agent verifies before injecting results into context."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-cryptographic-integrity-verification-for-tool-results
tags: [integrity-verification, hmac, tool-result-tampering, cryptographic-signing, man-in-the-middle, result-authenticity]
symptoms:
  - "Tool results travel over HTTP without integrity verification"
  - "No detection if a proxy modifies a database query result in transit"
  - "Tool results injected into LLM context without any authenticity check"
  - "Internal tool servers share no signing key — results are trust-by-default"
  - "No way to prove post-hoc that a tool result was unmodified when the agent used it"
---

## Why This Happens

Tool results are treated as trusted once they arrive — the assumption is that the transport layer (TLS) ensures integrity. TLS protects against external network interception but not against compromised intermediaries, buggy middleware that silently corrupts data, or insider-threat tool servers. Cryptographic integrity verification adds an application-layer guarantee: the tool signs its result with a shared HMAC key, and the agent verifies the signature before using the result. Tampering with the result invalidates the signature, making modification detectable even if TLS was bypassed.

## Solution 1: Signed Tool Result

```python
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class SignedToolResult:
    result_id: str
    tool_name: str
    payload: Any                   # the actual result data
    signature: str                 # HMAC-SHA256 hex digest
    signed_at: float
    tool_version: str = ""
    nonce: str = field(default_factory=lambda: uuid.uuid4().hex[:16])

    def canonical_bytes(self) -> bytes:
        """Deterministic serialization for signing — excludes signature field."""
        canon = json.dumps({
            "result_id": self.result_id,
            "tool_name": self.tool_name,
            "payload": self.payload,
            "signed_at": self.signed_at,
            "tool_version": self.tool_version,
            "nonce": self.nonce,
        }, sort_keys=True, separators=(",", ":"))
        return canon.encode("utf-8")

    @classmethod
    def create(
        cls,
        tool_name: str,
        payload: Any,
        signing_key: bytes,
        tool_version: str = "",
    ) -> "SignedToolResult":
        result_id = uuid.uuid4().hex
        signed_at = time.time()
        nonce = uuid.uuid4().hex[:16]

        # Build partial object to compute canonical bytes
        partial = cls(
            result_id=result_id,
            tool_name=tool_name,
            payload=payload,
            signature="",
            signed_at=signed_at,
            tool_version=tool_version,
            nonce=nonce,
        )
        mac = hmac.new(signing_key, partial.canonical_bytes(), hashlib.sha256)
        partial.signature = mac.hexdigest()
        return partial
```

## Solution 2: Tool Result Verifier

```python
import hashlib
import hmac
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class VerificationStatus(str, Enum):
    VALID = "valid"
    INVALID_SIGNATURE = "invalid_signature"
    EXPIRED = "expired"
    REPLAYED = "replayed"
    MALFORMED = "malformed"


@dataclass
class VerificationResult:
    status: VerificationStatus
    result_id: Optional[str]
    tool_name: Optional[str]
    age_seconds: Optional[float]
    detail: str = ""

    def is_valid(self) -> bool:
        return self.status == VerificationStatus.VALID


class ToolResultVerifier:
    """
    Verifies HMAC signatures on signed tool results.
    Also enforces a freshness window and tracks seen result IDs
    to detect replay of old valid results.
    """

    def __init__(
        self,
        signing_key: bytes,
        max_age_seconds: float = 300.0,
        replay_window_seconds: float = 300.0,
    ):
        self._key = signing_key
        self._max_age = max_age_seconds
        self._replay_window = replay_window_seconds
        self._seen_ids: dict = {}   # result_id -> seen_at

    def verify(self, result: SignedToolResult) -> VerificationResult:
        now = time.time()
        age = now - result.signed_at

        # Freshness check
        if age > self._max_age:
            return VerificationResult(
                status=VerificationStatus.EXPIRED,
                result_id=result.result_id,
                tool_name=result.tool_name,
                age_seconds=round(age, 1),
                detail=f"result is {age:.0f}s old (max {self._max_age}s)",
            )

        # Replay check
        self._purge_seen(now)
        if result.result_id in self._seen_ids:
            return VerificationResult(
                status=VerificationStatus.REPLAYED,
                result_id=result.result_id,
                tool_name=result.tool_name,
                age_seconds=round(age, 1),
                detail="result_id already seen — possible replay",
            )

        # Signature verification
        expected = hmac.new(self._key, result.canonical_bytes(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, result.signature):
            return VerificationResult(
                status=VerificationStatus.INVALID_SIGNATURE,
                result_id=result.result_id,
                tool_name=result.tool_name,
                age_seconds=round(age, 1),
                detail="HMAC signature mismatch — result may have been tampered",
            )

        self._seen_ids[result.result_id] = now
        return VerificationResult(
            status=VerificationStatus.VALID,
            result_id=result.result_id,
            tool_name=result.tool_name,
            age_seconds=round(age, 1),
        )

    def _purge_seen(self, now: float) -> None:
        cutoff = now - self._replay_window
        expired = [rid for rid, ts in self._seen_ids.items() if ts < cutoff]
        for rid in expired:
            del self._seen_ids[rid]
```

## Solution 3: Verification-Gated Context Injector

```python
from typing import Any, List, Optional


class VerificationGatedContextInjector:
    """
    Verifies each tool result before injecting it into the LLM context.
    Blocks injection of results with invalid or expired signatures.
    Returns only verified results and a list of blocked result IDs.
    """

    def __init__(self, verifier: ToolResultVerifier):
        self._verifier = verifier
        self._blocked: int = 0
        self._allowed: int = 0

    def filter_and_inject(
        self,
        results: List[SignedToolResult],
    ) -> tuple:
        """Returns (verified_payloads, blocked_details)."""
        verified = []
        blocked = []
        for result in results:
            check = self._verifier.verify(result)
            if check.is_valid():
                self._allowed += 1
                verified.append({
                    "tool_name": result.tool_name,
                    "payload": result.payload,
                    "result_id": result.result_id,
                })
            else:
                self._blocked += 1
                blocked.append({
                    "result_id": result.result_id,
                    "tool_name": result.tool_name,
                    "reason": check.status.value,
                    "detail": check.detail,
                })
        return verified, blocked

    def stats(self) -> dict:
        total = self._allowed + self._blocked
        return {
            "total": total,
            "allowed": self._allowed,
            "blocked": self._blocked,
            "block_rate": round(self._blocked / max(total, 1), 4),
        }
```

## Solution 4: Key Rotation Manager

```python
import time
from typing import Dict, List, Optional


class SigningKeyRotationManager:
    """
    Manages multiple signing keys with rotation support.
    New results are signed with the current key;
    verification tries all keys in the active window.
    """

    def __init__(self, rotation_interval_seconds: float = 86400.0):
        self._rotation_interval = rotation_interval_seconds
        self._keys: List[tuple] = []   # (key_id, key_bytes, created_at)
        self._current_index: int = -1

    def add_key(self, key_id: str, key_bytes: bytes) -> None:
        self._keys.append((key_id, key_bytes, time.time()))
        self._current_index = len(self._keys) - 1

    def current_key(self) -> Optional[bytes]:
        if self._current_index < 0:
            return None
        return self._keys[self._current_index][1]

    def verify_with_any_key(self, result: SignedToolResult) -> VerificationResult:
        """Try verification with all keys in the active window."""
        for key_id, key_bytes, created_at in reversed(self._keys):
            verifier = ToolResultVerifier(key_bytes)
            check = verifier.verify(result)
            if check.is_valid():
                return check
        # Return the last failure
        return VerificationResult(
            status=VerificationStatus.INVALID_SIGNATURE,
            result_id=result.result_id,
            tool_name=result.tool_name,
            age_seconds=None,
            detail="signature invalid against all active keys",
        )
```

## Solution 5: Integrity Violation Auditor

```python
import time
from typing import List


class IntegrityViolationAuditor:
    def __init__(self, max_records: int = 5000):
        self._records: List[dict] = []
        self._max = max_records

    def record(self, verification: VerificationResult, session_id: str = "") -> None:
        if verification.is_valid():
            return
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "session_id": session_id,
            "result_id": verification.result_id,
            "tool_name": verification.tool_name,
            "status": verification.status.value,
            "detail": verification.detail,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        by_status: dict = {}
        by_tool: dict = {}
        for r in recent:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
            if r["tool_name"]:
                by_tool[r["tool_name"]] = by_tool.get(r["tool_name"], 0) + 1
        return {
            "window_seconds": window_seconds,
            "violations": len(recent),
            "by_status": by_status,
            "by_tool": by_tool,
        }
```

## Solution 6: Integrity Verification Dashboard

```python
import time


class IntegrityVerificationDashboard:
    def __init__(
        self,
        injector: VerificationGatedContextInjector,
        auditor: IntegrityViolationAuditor,
    ):
        self._injector = injector
        self._auditor = auditor

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "injection_stats": self._injector.stats(),
            "violation_audit": self._auditor.summary(3600.0),
        }
```

## Comparison

| Approach | HMAC Signing | Freshness Check | Replay Prevention | Key Rotation | Audit Log |
|---|---|---|---|---|---|
| SignedToolResult | Yes (sign) | No | No | No | No |
| ToolResultVerifier | Yes (verify) | Yes | Yes (seen-IDs) | No | No |
| VerificationGatedContextInjector | Via verifier | Via verifier | Via verifier | No | No |
| SigningKeyRotationManager | Yes (multi-key) | No | No | Yes | No |
| IntegrityViolationAuditor | No | No | No | No | Yes |

**Best for production**: Deploy a shared HMAC key via a secrets manager (Vault, AWS Secrets Manager) rather than environment variables — key rotation should not require redeployment. Set `max_age_seconds=30` for real-time tool calls (short enough to prevent pre-computed result injection) and `max_age_seconds=300` for cached results. Monitor `IntegrityViolationAuditor.summary()`: any `invalid_signature` violations that are not from key rotation are security incidents that warrant immediate investigation. Log blocked results with full detail — the blocked payload content is evidence in a tampering investigation.
