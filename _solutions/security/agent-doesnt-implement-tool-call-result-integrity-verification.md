---
title: "Agent Doesn't Implement Tool Call Result Integrity Verification"
description: "Agents that trust tool results without integrity verification are vulnerable to man-in-the-middle tampering: a proxy between the agent and a database tool modifies query results, an internal service returns altered data, or a cached result is stale and undetected. Implement tool call result integrity verification using cryptographic signatures or content hashes to detect tampered or corrupted results before they influence LLM decisions."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-tool-call-result-integrity-verification
tags: [integrity-verification, result-tampering, hmac, content-hash, chain-of-custody, tool-security]
symptoms:
  - "No mechanism to detect if a tool result was modified in transit or by a compromised cache"
  - "Agent acts on database results that were altered by a misconfigured proxy"
  - "Cached tool results served without any staleness or integrity check"
  - "No audit trail proving which tool produced which result at what time"
  - "Internal service returns results that pass the agent's validation but were silently modified"
---

## Why This Happens

Tool results travel from a data source through multiple layers — network, reverse proxy, cache, serialization — before reaching the agent. Any layer can corrupt or tamper with the data, intentionally or accidentally. Without integrity verification, the agent has no way to detect this. Verification requires the tool or data source to sign or hash the result at the point of production, and the agent to verify that signature before acting on the result. This provides a cryptographic chain of custody from data source to agent decision.

## Solution 1: Signed Tool Result

```python
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class SignedToolResult:
    tool_name: str
    result: Any
    produced_at: float
    signature: str                # HMAC-SHA256 hex digest
    result_hash: str              # SHA256 of canonical result JSON
    signing_key_id: str = ""      # which key was used
    nonce: str = ""               # replay prevention
    ttl_seconds: float = 300.0   # result valid for N seconds

    def is_expired(self) -> bool:
        return time.time() - self.produced_at > self.ttl_seconds

    def to_dict(self) -> dict:
        return {
            "tool_name": self.tool_name,
            "result": self.result,
            "produced_at": self.produced_at,
            "signature": self.signature,
            "result_hash": self.result_hash,
            "signing_key_id": self.signing_key_id,
            "nonce": self.nonce,
            "ttl_seconds": self.ttl_seconds,
        }
```

## Solution 2: Result Signer

```python
import hashlib
import hmac
import json
import time
import uuid
from typing import Any


class ToolResultSigner:
    """
    Signs tool results at the point of production using HMAC-SHA256.
    The signing secret must be shared between the tool and the agent.
    """

    def __init__(self, secret: bytes, key_id: str = "default"):
        self._secret = secret
        self._key_id = key_id

    def _canonical(self, result: Any) -> str:
        return json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    def sign(
        self,
        tool_name: str,
        result: Any,
        ttl_seconds: float = 300.0,
    ) -> SignedToolResult:
        now = time.time()
        nonce = str(uuid.uuid4())[:8]
        canonical = self._canonical(result)
        result_hash = hashlib.sha256(canonical.encode()).hexdigest()

        message = f"{tool_name}:{result_hash}:{now:.3f}:{nonce}"
        signature = hmac.new(
            self._secret,
            message.encode(),
            hashlib.sha256,
        ).hexdigest()

        return SignedToolResult(
            tool_name=tool_name,
            result=result,
            produced_at=now,
            signature=signature,
            result_hash=result_hash,
            signing_key_id=self._key_id,
            nonce=nonce,
            ttl_seconds=ttl_seconds,
        )
```

## Solution 3: Result Integrity Verifier

```python
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class VerificationResult:
    valid: bool
    tool_name: str
    reason: str = ""
    age_seconds: float = 0.0


class ToolResultIntegrityVerifier:
    """
    Verifies a SignedToolResult before the agent acts on it.
    Checks signature validity, result hash, TTL, and replay prevention.
    """

    def __init__(self, secret: bytes, max_clock_skew_seconds: float = 30.0):
        self._secret = secret
        self._max_skew = max_clock_skew_seconds
        self._seen_nonces: set = set()   # replay prevention (bounded in production)

    def _canonical(self, result: Any) -> str:
        return json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    def verify(self, signed: SignedToolResult) -> VerificationResult:
        now = time.time()
        age = now - signed.produced_at

        # Clock skew check
        if abs(age) > self._max_skew + signed.ttl_seconds:
            return VerificationResult(
                valid=False,
                tool_name=signed.tool_name,
                reason=f"result age {age:.1f}s exceeds TTL {signed.ttl_seconds}s",
                age_seconds=age,
            )

        # TTL check
        if signed.is_expired():
            return VerificationResult(
                valid=False,
                tool_name=signed.tool_name,
                reason=f"result expired after {signed.ttl_seconds}s",
                age_seconds=age,
            )

        # Replay check
        if signed.nonce and signed.nonce in self._seen_nonces:
            return VerificationResult(
                valid=False,
                tool_name=signed.tool_name,
                reason="nonce replay detected",
                age_seconds=age,
            )

        # Hash check
        canonical = self._canonical(signed.result)
        expected_hash = hashlib.sha256(canonical.encode()).hexdigest()
        if not hmac.compare_digest(expected_hash, signed.result_hash):
            return VerificationResult(
                valid=False,
                tool_name=signed.tool_name,
                reason="result hash mismatch — content may have been tampered",
                age_seconds=age,
            )

        # Signature check
        message = f"{signed.tool_name}:{signed.result_hash}:{signed.produced_at:.3f}:{signed.nonce}"
        expected_sig = hmac.new(self._secret, message.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_sig, signed.signature):
            return VerificationResult(
                valid=False,
                tool_name=signed.tool_name,
                reason="HMAC signature invalid",
                age_seconds=age,
            )

        if signed.nonce:
            self._seen_nonces.add(signed.nonce)
            if len(self._seen_nonces) > 100000:
                self._seen_nonces = set(list(self._seen_nonces)[-50000:])

        return VerificationResult(
            valid=True,
            tool_name=signed.tool_name,
            age_seconds=age,
        )
```

## Solution 4: Integrity-Gated Tool Dispatcher

```python
from typing import Any, Callable, Dict, Optional


class IntegrityViolationError(Exception):
    def __init__(self, tool_name: str, reason: str):
        super().__init__(f"Integrity violation for '{tool_name}': {reason}")
        self.tool_name = tool_name
        self.reason = reason


class IntegrityGatedToolDispatcher:
    """
    Wraps tool calls that return SignedToolResult and verifies integrity
    before returning the unwrapped result to the agent.
    """

    def __init__(
        self,
        verifier: ToolResultIntegrityVerifier,
        audit_log_fn: Optional[Callable[[dict], None]] = None,
    ):
        self._verifier = verifier
        self._audit_log = audit_log_fn
        self._violations = 0
        self._verified = 0

    async def dispatch(
        self,
        tool_name: str,
        tool_fn: Callable,
        **kwargs: Any,
    ) -> Any:
        raw_result = await tool_fn(**kwargs)

        if isinstance(raw_result, SignedToolResult):
            verification = self._verifier.verify(raw_result)
            self._verified += 1

            if not verification.valid:
                self._violations += 1
                if self._audit_log:
                    import time
                    self._audit_log({
                        "event": "integrity_violation",
                        "ts": time.time(),
                        "tool_name": tool_name,
                        "reason": verification.reason,
                    })
                raise IntegrityViolationError(tool_name, verification.reason)

            return raw_result.result

        # Unsigned result: pass through but log absence of signature
        return raw_result

    def stats(self) -> dict:
        return {
            "verified": self._verified,
            "violations": self._violations,
            "violation_rate": round(self._violations / max(self._verified, 1), 4),
        }
```

## Solution 5: Result Hash Chain Auditor

```python
import hashlib
import time
from typing import List


class ResultHashChainAuditor:
    """
    Maintains a rolling hash chain of all verified tool results
    within a session, providing an append-only audit log that
    can prove the sequence and content of results the agent saw.
    """

    def __init__(self):
        self._chain: List[dict] = []
        self._chain_hash = "0" * 64   # genesis

    def append(self, verification: VerificationResult, result_hash: str) -> str:
        entry = {
            "ts": time.time(),
            "tool_name": verification.tool_name,
            "result_hash": result_hash,
            "prev_hash": self._chain_hash,
        }
        entry_json = str(sorted(entry.items()))
        self._chain_hash = hashlib.sha256(entry_json.encode()).hexdigest()
        entry["chain_hash"] = self._chain_hash
        self._chain.append(entry)
        return self._chain_hash

    def chain_head(self) -> str:
        return self._chain_hash

    def entries(self) -> List[dict]:
        return list(self._chain)
```

## Solution 6: Integrity Monitoring Dashboard

```python
import time


class IntegrityMonitoringDashboard:
    """
    Combines dispatcher stats and chain auditor state into one view.
    """

    def __init__(
        self,
        dispatcher: IntegrityGatedToolDispatcher,
        auditor: ResultHashChainAuditor,
    ):
        self._dispatcher = dispatcher
        self._auditor = auditor

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "verification_stats": self._dispatcher.stats(),
            "chain_entries": len(self._auditor.entries()),
            "chain_head": self._auditor.chain_head(),
        }
```

## Comparison

| Approach | HMAC Signing | Hash Verification | Replay Prevention | TTL Check | Audit Chain |
|---|---|---|---|---|---|
| ToolResultSigner | Yes | Yes (at sign time) | Yes (nonce) | Yes | No |
| ToolResultIntegrityVerifier | Yes | Yes | Yes | Yes | No |
| IntegrityGatedToolDispatcher | Via verifier | Via verifier | Via verifier | Via verifier | No |
| ResultHashChainAuditor | No | No | No | No | Yes |

**Best for production**: Share the HMAC secret via environment variable or secrets manager — never hardcode it. Rotate signing keys quarterly and use `signing_key_id` to support key rotation without a flag day. Set `ttl_seconds=60` for real-time query results and `ttl_seconds=3600` for static reference data — short TTLs prevent replay of stale results. Use `ResultHashChainAuditor` for compliance-sensitive workflows where you must prove the exact sequence of data the agent received. Monitor `violation_rate` via `IntegrityGatedToolDispatcher.stats()`: any non-zero rate in production warrants immediate investigation.
