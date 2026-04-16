---
title: "Agent Doesn't Implement Tool Response Integrity Verification"
description: "Agents that trust tool responses without integrity verification are vulnerable to man-in-the-middle tampering between the tool execution layer and the agent: a compromised intermediary can modify database query results, inject false API responses, or alter file contents before the agent acts on them. Implement response integrity verification using cryptographic signatures or content hashes that detect tampering before the agent processes tool output."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-tool-response-integrity-verification
tags: [integrity-verification, tamper-detection, response-signing, hmac, tool-security, supply-chain]
symptoms:
  - "Tool results pass through an intermediary layer with no tamper evidence"
  - "Agent acts on database results that could have been modified in transit"
  - "No checksum or signature on tool responses — any modification is undetectable"
  - "Compromised tool proxy could return false results without detection"
  - "Audit trail cannot prove tool results were not altered after execution"
---

## Why This Happens

In multi-layer agent architectures, tool calls often traverse intermediaries: API gateways, caching proxies, message brokers, or orchestration layers. Any of these can modify responses — intentionally (caching bugs, transformation errors) or maliciously (supply chain compromise, MITM). Without a cryptographic binding between the tool's output and what the agent receives, tampering is undetectable. Integrity verification requires the tool to sign its response with a secret key shared only between the tool and the agent, and the agent to verify the signature before acting on the result.

## Solution 1: Signed Tool Response

```python
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class SignedToolResponse:
    tool_name: str
    payload: Any
    signature: str
    signed_at: float
    nonce: str
    signer_id: str = ""
    version: int = 1

    def to_dict(self) -> dict:
        return {
            "tool_name": self.tool_name,
            "payload": self.payload,
            "signature": self.signature,
            "signed_at": self.signed_at,
            "nonce": self.nonce,
            "signer_id": self.signer_id,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SignedToolResponse":
        return cls(
            tool_name=data["tool_name"],
            payload=data["payload"],
            signature=data["signature"],
            signed_at=data["signed_at"],
            nonce=data["nonce"],
            signer_id=data.get("signer_id", ""),
            version=data.get("version", 1),
        )
```

## Solution 2: Tool Response Signer

```python
import hashlib
import hmac
import json
import os
import time


class ToolResponseSigner:
    """
    Signs tool responses using HMAC-SHA256.
    The signing key is shared between the tool execution layer
    and the agent verification layer.
    """

    def __init__(self, signing_key: bytes, signer_id: str = ""):
        self._key = signing_key
        self._signer_id = signer_id or f"signer-{os.getpid()}"

    def sign(self, tool_name: str, payload: Any) -> SignedToolResponse:
        nonce = os.urandom(16).hex()
        signed_at = time.time()
        canonical = self._canonical_string(tool_name, payload, signed_at, nonce)
        signature = hmac.new(self._key, canonical.encode(), hashlib.sha256).hexdigest()
        return SignedToolResponse(
            tool_name=tool_name,
            payload=payload,
            signature=signature,
            signed_at=signed_at,
            nonce=nonce,
            signer_id=self._signer_id,
        )

    @staticmethod
    def _canonical_string(
        tool_name: str,
        payload: Any,
        signed_at: float,
        nonce: str,
    ) -> str:
        payload_json = json.dumps(payload, sort_keys=True, ensure_ascii=True)
        return f"{tool_name}\n{signed_at}\n{nonce}\n{payload_json}"
```

## Solution 3: Tool Response Verifier

```python
import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class VerificationResult:
    valid: bool
    tool_name: str
    failure_reason: Optional[str] = None
    age_seconds: Optional[float] = None


class ToolResponseVerifier:
    """
    Verifies HMAC-SHA256 signatures on tool responses.
    Rejects responses that are expired, have invalid signatures,
    or carry a nonce already seen (replay prevention).
    """

    def __init__(
        self,
        signing_key: bytes,
        max_age_seconds: float = 30.0,
    ):
        self._key = signing_key
        self._max_age = max_age_seconds
        self._seen_nonces: set = set()
        self._verified_count = 0
        self._rejected_count = 0

    def verify(self, response: SignedToolResponse) -> VerificationResult:
        now = time.time()
        age = now - response.signed_at

        if age > self._max_age:
            self._rejected_count += 1
            return VerificationResult(
                valid=False,
                tool_name=response.tool_name,
                failure_reason=f"response expired: age={age:.1f}s > max={self._max_age}s",
                age_seconds=round(age, 2),
            )

        if response.nonce in self._seen_nonces:
            self._rejected_count += 1
            return VerificationResult(
                valid=False,
                tool_name=response.tool_name,
                failure_reason="nonce replay detected",
                age_seconds=round(age, 2),
            )

        canonical = ToolResponseSigner._canonical_string(
            response.tool_name,
            response.payload,
            response.signed_at,
            response.nonce,
        )
        expected = hmac.new(self._key, canonical.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, response.signature):
            self._rejected_count += 1
            return VerificationResult(
                valid=False,
                tool_name=response.tool_name,
                failure_reason="signature mismatch — possible tampering",
                age_seconds=round(age, 2),
            )

        self._seen_nonces.add(response.nonce)
        if len(self._seen_nonces) > 10000:
            # Prune oldest nonces (approximate)
            self._seen_nonces = set(list(self._seen_nonces)[-5000:])

        self._verified_count += 1
        return VerificationResult(
            valid=True,
            tool_name=response.tool_name,
            age_seconds=round(age, 2),
        )

    def stats(self) -> dict:
        return {
            "verified": self._verified_count,
            "rejected": self._rejected_count,
        }
```

## Solution 4: Integrity-Gated Tool Dispatcher

```python
import asyncio
from typing import Any, Callable, Dict


class IntegrityGatedToolDispatcher:
    """
    Wraps tool execution with signature verification.
    Tools that return unsigned responses are accepted with a warning
    unless strict mode is enabled, which blocks all unsigned responses.
    """

    def __init__(
        self,
        verifier: ToolResponseVerifier,
        strict_mode: bool = True,
    ):
        self._verifier = verifier
        self._strict = strict_mode
        self._tamper_detections = 0
        self._unsigned_responses = 0

    async def dispatch(
        self,
        tool_fn: Callable,
        tool_name: str,
        args: Dict[str, Any],
    ) -> dict:
        raw_result = await tool_fn(tool_name, args)

        # If tool returns a SignedToolResponse, verify it
        if isinstance(raw_result, SignedToolResponse):
            result = self._verifier.verify(raw_result)
            if not result.valid:
                self._tamper_detections += 1
                if self._strict:
                    raise IntegrityViolationError(
                        tool_name=tool_name,
                        reason=result.failure_reason or "unknown",
                    )
                return {
                    "payload": None,
                    "integrity": "failed",
                    "failure_reason": result.failure_reason,
                }
            return {"payload": raw_result.payload, "integrity": "verified"}

        # Unsigned response
        self._unsigned_responses += 1
        if self._strict:
            raise IntegrityViolationError(tool_name=tool_name, reason="unsigned response")
        return {"payload": raw_result, "integrity": "unsigned"}

    def stats(self) -> dict:
        return {
            "tamper_detections": self._tamper_detections,
            "unsigned_responses": self._unsigned_responses,
            **self._verifier.stats(),
        }


class IntegrityViolationError(Exception):
    def __init__(self, tool_name: str, reason: str):
        super().__init__(f"Integrity violation for tool '{tool_name}': {reason}")
        self.tool_name = tool_name
        self.reason = reason
```

## Solution 5: Integrity Violation Audit Log

```python
import time
from collections import Counter
from typing import List


class IntegrityViolationAuditLog:
    """
    Records integrity violations with tool name, failure reason,
    and session context for security investigation.
    """

    def __init__(self, max_records: int = 5000):
        self._max = max_records
        self._records: List[dict] = []

    def record(
        self,
        result: VerificationResult,
        session_id: str = "",
        source_ip: str = "",
    ) -> None:
        if result.valid:
            return
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "tool_name": result.tool_name,
            "failure_reason": result.failure_reason,
            "age_seconds": result.age_seconds,
            "session_id": session_id,
            "source_ip": source_ip,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "violations": 0}
        reason_counts: Counter = Counter(r["failure_reason"] for r in recent)
        return {
            "window_seconds": window_seconds,
            "violations": len(recent),
            "top_failure_reasons": reason_counts.most_common(5),
            "affected_tools": list({r["tool_name"] for r in recent}),
            "unique_sessions": len({r["session_id"] for r in recent}),
        }
```

## Solution 6: Integrity Verification Dashboard

```python
import time


class ToolIntegrityVerificationDashboard:
    """
    Combines dispatcher stats, verifier counts, and audit log summary
    into a single tool integrity health report.
    """

    def __init__(
        self,
        dispatcher: IntegrityGatedToolDispatcher,
        audit_log: IntegrityViolationAuditLog,
    ):
        self._dispatcher = dispatcher
        self._audit = audit_log

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "dispatcher": self._dispatcher.stats(),
            "strict_mode": self._dispatcher._strict,
            "audit_1h": self._audit.summary(window_seconds=3600.0),
            "audit_24h": self._audit.summary(window_seconds=86400.0),
        }
```

## Comparison

| Approach | HMAC Signing | Signature Verification | Replay Prevention | Tamper Blocking | Audit |
|---|---|---|---|---|---|
| ToolResponseSigner | Yes (HMAC-SHA256) | No | No | No | No |
| ToolResponseVerifier | No | Yes | Yes (nonce set) | No | No |
| IntegrityGatedToolDispatcher | No | Via verifier | Via verifier | Yes (strict mode) | No |
| IntegrityViolationAuditLog | No | No | No | No | Yes |
| ToolIntegrityVerificationDashboard | No | No | No | No | Yes |

**Best for production**: Use a per-tool signing key rather than a single shared key — this limits the blast radius if one key is compromised (only that tool's responses are affected). Set `max_age_seconds=30` to prevent replay attacks using cached signed responses. Enable `strict_mode=True` in production so unsigned responses from tools that are expected to sign are blocked immediately — a tool that stops signing signals a deployment or compromise event worth investigating. Rotate signing keys on a 30-day schedule and use the `signer_id` field to distinguish responses signed by different key versions during rotation overlap.
