---
title: "Agent Doesn't Implement Agent-to-Agent Authentication for Multi-Agent Pipelines"
description: "Multi-agent systems where sub-agents call other agents without authentication allow any process that can reach the agent's endpoint to impersonate a trusted orchestrator: a compromised sub-agent can call the planner agent directly, a prompt injection in one agent can redirect calls to a malicious agent, or a rogue process can inject results into the pipeline. Implement mutual authentication between agents using signed tokens to verify caller identity before processing any inter-agent request."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-agent-to-agent-authentication-for-multi-agent-pipelines
tags: [agent-authentication, multi-agent, jwt, mutual-auth, pipeline-security, inter-agent-trust]
symptoms:
  - "Any process that knows the agent's endpoint can send requests impersonating the orchestrator"
  - "Sub-agent receives tool results from an unverified caller and acts on them"
  - "No way to verify that a request claiming to be from agent-A actually originated there"
  - "Prompt injection in agent-B can forge requests to agent-A by knowing its URL"
  - "Multi-agent pipeline has no audit trail of which agent called which"
---

## Why This Happens

Agent-to-agent calls are typically plain HTTP or function calls with no authentication layer. The receiving agent has no way to verify that the caller is the expected upstream agent rather than an attacker or a compromised sibling agent. Authentication requires each agent to have an identity (a signing key or certificate), to sign outgoing requests with that identity, and for receiving agents to verify the signature before processing the request. This establishes a chain of trust through the pipeline.

## Solution 1: Agent Identity

```python
import hashlib
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AgentIdentity:
    agent_id: str
    agent_name: str
    signing_secret: bytes          # shared secret for HMAC signing
    trusted_callers: set = field(default_factory=set)  # agent_ids allowed to call this agent
    key_version: str = "v1"

    def __post_init__(self):
        if not self.agent_id:
            raise ValueError("agent_id must not be empty")
        if not self.signing_secret:
            raise ValueError("signing_secret must not be empty")

    def fingerprint(self) -> str:
        return hashlib.sha256(self.signing_secret).hexdigest()[:12]
```

## Solution 2: Inter-Agent Request Signer

```python
import hashlib
import hmac
import json
import time
import uuid
from typing import Any, Dict


class InterAgentRequestSigner:
    """
    Signs inter-agent requests with HMAC-SHA256.
    The signature covers: caller_id, callee_id, payload hash, timestamp, and nonce.
    """

    def __init__(self, identity: AgentIdentity):
        self._identity = identity

    def sign(
        self,
        callee_id: str,
        payload: Dict[str, Any],
        ttl_seconds: float = 30.0,
    ) -> dict:
        now = time.time()
        nonce = str(uuid.uuid4())[:8]
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()

        message = "|".join([
            self._identity.agent_id,
            callee_id,
            payload_hash,
            f"{now:.3f}",
            nonce,
        ])
        signature = hmac.new(
            self._identity.signing_secret,
            message.encode(),
            hashlib.sha256,
        ).hexdigest()

        return {
            "caller_id": self._identity.agent_id,
            "caller_name": self._identity.agent_name,
            "callee_id": callee_id,
            "issued_at": now,
            "expires_at": now + ttl_seconds,
            "nonce": nonce,
            "payload_hash": payload_hash,
            "signature": signature,
            "key_version": self._identity.key_version,
        }
```

## Solution 3: Inter-Agent Request Verifier

```python
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class VerificationOutcome:
    valid: bool
    caller_id: str = ""
    reason: str = ""


class InterAgentRequestVerifier:
    """
    Verifies signed inter-agent requests.
    Checks signature, expiry, caller authorization, and replay prevention.
    """

    def __init__(
        self,
        my_identity: AgentIdentity,
        caller_secrets: Dict[str, bytes],   # caller_id → shared secret
        max_clock_skew_seconds: float = 10.0,
    ):
        self._identity = my_identity
        self._caller_secrets = caller_secrets
        self._max_skew = max_clock_skew_seconds
        self._seen_nonces: set = set()

    def verify(
        self,
        auth_header: dict,
        payload: Dict[str, Any],
    ) -> VerificationOutcome:
        caller_id = auth_header.get("caller_id", "")
        callee_id = auth_header.get("callee_id", "")
        issued_at = auth_header.get("issued_at", 0)
        expires_at = auth_header.get("expires_at", 0)
        nonce = auth_header.get("nonce", "")
        payload_hash = auth_header.get("payload_hash", "")
        signature = auth_header.get("signature", "")

        now = time.time()

        # Callee check
        if callee_id != self._identity.agent_id:
            return VerificationOutcome(False, caller_id, "request not addressed to this agent")

        # Expiry
        if now > expires_at:
            return VerificationOutcome(False, caller_id, f"request expired at {expires_at:.0f}")

        # Clock skew
        if abs(now - issued_at) > self._max_skew + (expires_at - issued_at):
            return VerificationOutcome(False, caller_id, "clock skew exceeded")

        # Replay
        if nonce in self._seen_nonces:
            return VerificationOutcome(False, caller_id, "nonce replay detected")

        # Caller authorization
        if self._identity.trusted_callers and caller_id not in self._identity.trusted_callers:
            return VerificationOutcome(False, caller_id, f"caller '{caller_id}' not in trusted_callers")

        # Payload hash
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        expected_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        if not hmac.compare_digest(expected_hash, payload_hash):
            return VerificationOutcome(False, caller_id, "payload hash mismatch")

        # Signature
        secret = self._caller_secrets.get(caller_id)
        if not secret:
            return VerificationOutcome(False, caller_id, f"no shared secret for caller '{caller_id}'")

        message = "|".join([caller_id, callee_id, payload_hash, f"{issued_at:.3f}", nonce])
        expected_sig = hmac.new(secret, message.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_sig, signature):
            return VerificationOutcome(False, caller_id, "HMAC signature invalid")

        self._seen_nonces.add(nonce)
        if len(self._seen_nonces) > 100000:
            self._seen_nonces = set(list(self._seen_nonces)[-50000:])

        return VerificationOutcome(True, caller_id)
```

## Solution 4: Authenticated Inter-Agent Client

```python
from typing import Any, Callable, Dict, Optional


class AuthenticatedInterAgentClient:
    """
    Sends authenticated requests to other agents.
    Attaches a signed auth header to every outgoing inter-agent call.
    """

    def __init__(
        self,
        signer: InterAgentRequestSigner,
        transport_fn: Callable,   # async (callee_id, auth_header, payload) -> response
    ):
        self._signer = signer
        self._transport = transport_fn

    async def call(
        self,
        callee_id: str,
        payload: Dict[str, Any],
        ttl_seconds: float = 30.0,
    ) -> Any:
        auth_header = self._signer.sign(callee_id, payload, ttl_seconds)
        return await self._transport(callee_id, auth_header, payload)
```

## Solution 5: Auth-Gated Inter-Agent Handler

```python
import time
from typing import Any, Callable, Dict, Optional


class AuthenticationFailedError(Exception):
    def __init__(self, outcome: VerificationOutcome):
        super().__init__(f"Inter-agent authentication failed: {outcome.reason}")
        self.outcome = outcome


class AuthGatedInterAgentHandler:
    """
    Validates incoming inter-agent requests before passing them to the agent logic.
    Logs authentication events for audit.
    """

    def __init__(
        self,
        verifier: InterAgentRequestVerifier,
        audit_log_fn: Optional[Callable[[dict], None]] = None,
    ):
        self._verifier = verifier
        self._audit_log = audit_log_fn
        self._accepted = 0
        self._rejected = 0

    async def handle(
        self,
        auth_header: dict,
        payload: Dict[str, Any],
        handler_fn: Callable,
    ) -> Any:
        outcome = self._verifier.verify(auth_header, payload)

        if self._audit_log:
            self._audit_log({
                "event": "inter_agent_auth",
                "ts": time.time(),
                "valid": outcome.valid,
                "caller_id": outcome.caller_id,
                "reason": outcome.reason,
            })

        if not outcome.valid:
            self._rejected += 1
            raise AuthenticationFailedError(outcome)

        self._accepted += 1
        return await handler_fn(payload, caller_id=outcome.caller_id)

    def stats(self) -> dict:
        return {
            "accepted": self._accepted,
            "rejected": self._rejected,
            "rejection_rate": round(self._rejected / max(self._accepted + self._rejected, 1), 4),
        }
```

## Solution 6: Pipeline Trust Chain Auditor

```python
import time
from typing import List


class PipelineTrustChainAuditor:
    """
    Records the chain of authenticated agent calls for a single pipeline execution.
    Provides a call graph for debugging and compliance audit.
    """

    def __init__(self):
        self._calls: List[dict] = []

    def record(self, caller_id: str, callee_id: str, valid: bool, payload_preview: str = "") -> None:
        self._calls.append({
            "ts": time.time(),
            "caller_id": caller_id,
            "callee_id": callee_id,
            "valid": valid,
            "payload_preview": payload_preview[:100],
        })

    def chain(self) -> List[dict]:
        return list(self._calls)

    def has_untrusted_calls(self) -> bool:
        return any(not c["valid"] for c in self._calls)

    def summary(self) -> dict:
        return {
            "total_calls": len(self._calls),
            "authenticated": sum(1 for c in self._calls if c["valid"]),
            "rejected": sum(1 for c in self._calls if not c["valid"]),
            "agents_involved": list({c["caller_id"] for c in self._calls} | {c["callee_id"] for c in self._calls}),
        }
```

## Comparison

| Approach | HMAC Signing | Replay Prevention | Caller Authorization | Audit Trail | Pipeline Graph |
|---|---|---|---|---|---|
| InterAgentRequestSigner | Yes | Yes (nonce) | No | No | No |
| InterAgentRequestVerifier | Yes | Yes | Yes (trusted_callers) | No | No |
| AuthGatedInterAgentHandler | Via verifier | Via verifier | Via verifier | Yes | No |
| AuthenticatedInterAgentClient | Via signer | Via signer | No | No | No |
| PipelineTrustChainAuditor | No | No | No | Yes | Yes |

**Best for production**: Use asymmetric keys (Ed25519) rather than shared HMAC secrets for production multi-agent systems — asymmetric signing means each agent only needs its own private key, and public keys can be distributed without secret exposure. Set `ttl_seconds=30` for inter-agent calls — they should complete quickly, and a 30-second window minimizes replay risk. Populate `trusted_callers` explicitly for every agent; the orchestrator should be the only caller allowed for most sub-agents. Log every authentication event to `PipelineTrustChainAuditor` and alert on `rejection_rate > 0.01` — any non-zero rejection rate warrants immediate investigation.
