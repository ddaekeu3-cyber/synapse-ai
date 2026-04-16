---
title: "Agent Doesn't Implement Session Fixation Prevention for Agent Handoffs"
description: "Agents that reuse session identifiers across handoffs between sub-agents or between user sessions are vulnerable to session fixation: an attacker who can predict or control the session ID used by a downstream agent can inject themselves into a privileged session. Implement session ID regeneration at every handoff boundary, cryptographically binding new session IDs to the originating context."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-session-fixation-prevention-for-agent-handoffs
tags: [session-fixation, handoff-security, session-management, agent-identity, cryptographic-binding, session-regeneration]
symptoms:
  - "Sub-agent sessions reuse the parent session ID passed in the handoff payload"
  - "Session IDs are predictable integers or timestamps rather than cryptographic random values"
  - "No session ID rotation occurs when a handoff elevates privileges or changes agent identity"
  - "An attacker who intercepts a handoff token can impersonate the receiving agent"
  - "Session IDs appear in URLs or log files where they can be harvested"
---

## Why This Happens

Session fixation occurs when the receiving party in a handoff accepts a session identifier chosen by the sender rather than generating its own. In agent systems, a parent agent that passes `{"session_id": "user-123-task-456"}` to a child agent creates a fixation opportunity: anyone who can guess or observe that ID before the child agent initializes can pre-establish state under that ID and hijack the session. The fix requires the child agent to generate a new cryptographic session ID at the moment of handoff, cryptographically binding it to a nonce from the parent, making pre-establishment impossible.

## Solution 1: Handoff Token Schema

```python
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class HandoffToken:
    """
    Carries context from a parent agent to a child agent at handoff.
    Does NOT carry the parent session ID — only a one-time nonce.
    """
    parent_agent_id: str
    child_agent_type: str
    handoff_nonce: str          # single-use random value from parent
    issued_at: float = field(default_factory=time.time)
    ttl_seconds: float = 30.0  # token expires quickly to prevent replay
    scope: str = ""            # permissions granted to child
    context_hash: str = ""     # HMAC of the context payload being handed off

    def is_expired(self) -> bool:
        return time.time() - self.issued_at > self.ttl_seconds


@dataclass
class RegeneratedSession:
    """Result of session ID regeneration at a handoff boundary."""
    new_session_id: str
    parent_nonce: str
    child_agent_id: str
    created_at: float
    scope: str
    binding_proof: str          # HMAC(new_session_id + parent_nonce + child_agent_id)
```

## Solution 2: Handoff Nonce Generator

```python
import hashlib
import hmac
import os
import time


class HandoffNonceGenerator:
    """
    Generates single-use cryptographic nonces for handoff tokens.
    Tracks used nonces to prevent replay attacks.
    """

    def __init__(self, signing_key: bytes):
        self._key = signing_key
        self._used_nonces: set = set()
        self._nonce_expiry: dict = {}   # nonce -> expiry time

    def generate(self) -> str:
        raw = os.urandom(32)
        nonce = raw.hex()
        self._used_nonces.add(nonce)
        self._nonce_expiry[nonce] = time.time() + 120.0
        return nonce

    def consume(self, nonce: str) -> bool:
        """Returns True if nonce is valid and unused. Consumes it on success."""
        self._evict_expired()
        if nonce not in self._used_nonces:
            return False
        expiry = self._nonce_expiry.get(nonce, 0)
        if time.time() > expiry:
            self._used_nonces.discard(nonce)
            self._nonce_expiry.pop(nonce, None)
            return False
        self._used_nonces.discard(nonce)
        self._nonce_expiry.pop(nonce, None)
        return True

    def _evict_expired(self) -> None:
        now = time.time()
        expired = [n for n, exp in self._nonce_expiry.items() if now > exp]
        for n in expired:
            self._used_nonces.discard(n)
            self._nonce_expiry.pop(n, None)

    def sign_token(self, token_data: str) -> str:
        return hmac.new(self._key, token_data.encode(), hashlib.sha256).hexdigest()
```

## Solution 3: Session ID Regenerator

```python
import hashlib
import hmac
import os
import time


class SessionIDRegenerator:
    """
    Generates a new cryptographically random session ID at handoff boundaries.
    Binds the new ID to the parent nonce so it cannot be pre-established.
    """

    def __init__(self, signing_key: bytes, agent_id: str):
        self._key = signing_key
        self._agent_id = agent_id

    def regenerate(self, handoff_token: HandoffToken) -> RegeneratedSession:
        if handoff_token.is_expired():
            raise HandoffTokenExpiredError(handoff_token.parent_agent_id)

        new_session_id = os.urandom(32).hex()
        binding_input = f"{new_session_id}:{handoff_token.handoff_nonce}:{self._agent_id}"
        binding_proof = hmac.new(
            self._key,
            binding_input.encode(),
            hashlib.sha256,
        ).hexdigest()

        return RegeneratedSession(
            new_session_id=new_session_id,
            parent_nonce=handoff_token.handoff_nonce,
            child_agent_id=self._agent_id,
            created_at=time.time(),
            scope=handoff_token.scope,
            binding_proof=binding_proof,
        )

    def verify_binding(self, session: RegeneratedSession) -> bool:
        binding_input = f"{session.new_session_id}:{session.parent_nonce}:{session.child_agent_id}"
        expected = hmac.new(
            self._key,
            binding_input.encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, session.binding_proof)


class HandoffTokenExpiredError(Exception):
    def __init__(self, parent_agent_id: str):
        super().__init__(f"handoff token from '{parent_agent_id}' has expired")
        self.parent_agent_id = parent_agent_id
```

## Solution 4: Secure Handoff Dispatcher

```python
import time
from typing import Any, Callable, Dict, Optional


class SecureHandoffDispatcher:
    """
    Manages the full handoff lifecycle: issues tokens with nonces, verifies
    them at receipt, and forces session regeneration before child agent work begins.
    """

    def __init__(
        self,
        nonce_generator: HandoffNonceGenerator,
        regenerator: SessionIDRegenerator,
        agent_id: str,
    ):
        self._nonces = nonce_generator
        self._regenerator = regenerator
        self._agent_id = agent_id
        self._active_sessions: Dict[str, RegeneratedSession] = {}

    def issue_handoff_token(
        self,
        child_agent_type: str,
        scope: str = "",
        context_hash: str = "",
    ) -> HandoffToken:
        nonce = self._nonces.generate()
        return HandoffToken(
            parent_agent_id=self._agent_id,
            child_agent_type=child_agent_type,
            handoff_nonce=nonce,
            scope=scope,
            context_hash=context_hash,
        )

    def receive_handoff(self, token: HandoffToken) -> RegeneratedSession:
        if not self._nonces.consume(token.handoff_nonce):
            raise HandoffNonceInvalidError(token.handoff_nonce)
        session = self._regenerator.regenerate(token)
        self._active_sessions[session.new_session_id] = session
        return session

    def get_session(self, session_id: str) -> Optional[RegeneratedSession]:
        return self._active_sessions.get(session_id)

    def revoke_session(self, session_id: str) -> None:
        self._active_sessions.pop(session_id, None)

    def active_session_count(self) -> int:
        return len(self._active_sessions)


class HandoffNonceInvalidError(Exception):
    def __init__(self, nonce: str):
        super().__init__(f"handoff nonce invalid or already consumed: {nonce[:8]}...")
        self.nonce = nonce
```

## Solution 5: Session Fixation Audit Logger

```python
import time
from typing import List


class SessionFixationAuditLogger:
    """
    Records handoff events and flags suspicious patterns:
    reused nonces, expired token usage attempts, and rapid
    session creation from a single parent that may indicate enumeration.
    """

    def __init__(self, max_records: int = 5000):
        self._records: List[dict] = []
        self._max = max_records

    def record_handoff(
        self,
        token: HandoffToken,
        outcome: str,                # "success" | "nonce_invalid" | "expired" | "error"
        new_session_id: str = "",
    ) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "parent_agent_id": token.parent_agent_id,
            "child_agent_type": token.child_agent_type,
            "nonce_prefix": token.handoff_nonce[:8],
            "outcome": outcome,
            "new_session_prefix": new_session_id[:8] if new_session_id else "",
            "token_age_ms": round((time.time() - token.issued_at) * 1000, 1),
        })

    def suspicious_parents(self, window_seconds: float = 60.0, failure_threshold: int = 3) -> List[str]:
        cutoff = time.time() - window_seconds
        failures: dict = {}
        for r in self._records:
            if r["ts"] < cutoff:
                continue
            if r["outcome"] != "success":
                pid = r["parent_agent_id"]
                failures[pid] = failures.get(pid, 0) + 1
        return [pid for pid, count in failures.items() if count >= failure_threshold]

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        return {
            "window_seconds": window_seconds,
            "total_handoffs": len(recent),
            "successful": sum(1 for r in recent if r["outcome"] == "success"),
            "nonce_failures": sum(1 for r in recent if r["outcome"] == "nonce_invalid"),
            "expired_attempts": sum(1 for r in recent if r["outcome"] == "expired"),
            "suspicious_parents": self.suspicious_parents(window_seconds),
        }
```

## Solution 6: Handoff Security Dashboard

```python
import time


class HandoffSecurityDashboard:
    """
    Combines dispatcher state, audit summary, and session binding verification
    into a single operational view for security monitoring.
    """

    def __init__(
        self,
        dispatcher: SecureHandoffDispatcher,
        audit_logger: SessionFixationAuditLogger,
        regenerator: SessionIDRegenerator,
    ):
        self._dispatcher = dispatcher
        self._audit = audit_logger
        self._regenerator = regenerator

    def render(self) -> dict:
        summary = self._audit.summary(window_seconds=3600.0)
        active = self._dispatcher.active_session_count()
        suspicious = self._audit.suspicious_parents(window_seconds=300.0)

        return {
            "generated_at": time.time(),
            "active_sessions": active,
            "handoff_summary_1h": summary,
            "suspicious_parents_5m": suspicious,
            "alert": len(suspicious) > 0 or summary["nonce_failures"] > 5,
        }
```

## Comparison

| Approach | Nonce Generation | Replay Prevention | Session Regeneration | Binding Proof | Audit |
|---|---|---|---|---|---|
| HandoffNonceGenerator | Yes (32-byte random) | Yes (consume once) | No | No | No |
| SessionIDRegenerator | No | No | Yes (new random ID) | Yes (HMAC) | No |
| SecureHandoffDispatcher | Via generator | Via generator | Via regenerator | Via regenerator | No |
| SessionFixationAuditLogger | No | No | No | No | Yes |
| HandoffSecurityDashboard | No | No | No | No | Yes (aggregated) |

**Best for production**: Set `HandoffToken.ttl_seconds=15` — a nonce that is valid for 30 seconds gives an attacker 30 seconds to intercept and replay; 15 seconds is sufficient for any legitimate handoff latency. Store consumed nonces in Redis with a TTL equal to `ttl_seconds + 5` for multi-instance deployments — in-process nonce tracking fails when the issuing and receiving instances differ. Monitor `suspicious_parents` in `SessionFixationAuditLogger`: a parent agent producing repeated nonce failures is either buggy (retry without new nonce) or actively probing the system.
