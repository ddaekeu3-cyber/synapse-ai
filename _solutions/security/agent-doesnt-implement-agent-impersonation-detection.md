---
title: "Agent Doesn't Implement Agent Impersonation Detection"
description: "Multi-agent pipelines where agents trust messages from other agents based solely on claimed identity are vulnerable to impersonation: a compromised or prompt-injected agent can forge messages appearing to come from a trusted coordinator, escalate privileges, or hijack task routing. Implement agent identity verification using signed messages, nonce challenges, and behavioral fingerprinting to detect impersonation attempts at runtime."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-agent-impersonation-detection
tags: [agent-impersonation, identity-verification, message-signing, multi-agent, zero-trust, security]
symptoms:
  - "Agent accepts task instructions from any message claiming to be from the orchestrator"
  - "No verification that a 'trusted coordinator' message is cryptographically authentic"
  - "Prompt injection causes a tool to emit messages that appear to come from another agent"
  - "Privilege escalation: low-privilege agent sends messages claiming to be high-privilege agent"
  - "No audit trail distinguishing real orchestrator messages from impersonated ones"
---

## Why This Happens

Multi-agent systems pass structured messages between agents, often including a `sender_id` or `from_agent` field. Without cryptographic verification, any agent (or injected content) can set `sender_id: "orchestrator"` and receive elevated trust. Real agent authentication requires that messages be signed with a private key held only by the legitimate sender, and that the receiving agent verifies the signature before acting on elevated-privilege instructions. Behavioral fingerprinting adds a second layer: legitimate agents behave consistently; impersonators often deviate from expected message patterns.

## Solution 1: Agent Identity Certificate

```python
import hashlib
import time
import uuid
from dataclasses import dataclass, field
from typing import FrozenSet, Optional

@dataclass
class AgentIdentityCertificate:
    """
    Declares the identity and capabilities of an agent instance.
    Issued by an identity authority at agent startup.
    Contains the agent's public key for message verification.
    """
    cert_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    agent_id: str = ""
    agent_type: str = ""
    capabilities: FrozenSet[str] = field(default_factory=frozenset)
    public_key_pem: str = ""           # PEM-encoded public key
    issuer: str = "agent-identity-authority"
    issued_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    environment: str = ""

    def is_valid(self) -> bool:
        if self.expires_at and time.time() > self.expires_at:
            return False
        return bool(self.public_key_pem and self.agent_id)

    def fingerprint(self) -> str:
        data = f"{self.agent_id}:{self.public_key_pem}:{self.issued_at}"
        return hashlib.sha256(data.encode()).hexdigest()[:16]
```

## Solution 2: Signed Agent Message

```python
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

@dataclass
class SignedAgentMessage:
    """
    A message between agents with cryptographic authenticity.
    The sender signs the payload + nonce + timestamp to prevent replay.
    """
    message_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    sender_agent_id: str = ""
    recipient_agent_id: str = ""
    message_type: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    nonce: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    timestamp: float = field(default_factory=time.time)
    signature: str = ""           # HMAC-SHA256 of canonical payload

    def canonical_bytes(self) -> bytes:
        """Deterministic serialization for signing."""
        canonical = {
            "message_id": self.message_id,
            "sender": self.sender_agent_id,
            "recipient": self.recipient_agent_id,
            "type": self.message_type,
            "payload": self.payload,
            "nonce": self.nonce,
            "timestamp": self.timestamp,
        }
        return json.dumps(canonical, sort_keys=True).encode("utf-8")

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "sender_agent_id": self.sender_agent_id,
            "recipient_agent_id": self.recipient_agent_id,
            "message_type": self.message_type,
            "payload": self.payload,
            "nonce": self.nonce,
            "timestamp": self.timestamp,
            "signature": self.signature,
        }
```

## Solution 3: Agent Message Authenticator

```python
import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

@dataclass
class AuthenticationResult:
    valid: bool
    agent_id: str
    reason: str
    replay_detected: bool = False

class AgentMessageAuthenticator:
    """
    Signs outgoing messages and verifies signatures on incoming messages.
    Uses HMAC-SHA256 with per-agent shared secrets (or asymmetric keys for
    production — swap _sign/_verify for RSA/Ed25519 implementations).
    Maintains a nonce cache to detect replay attacks.
    """

    def __init__(
        self,
        max_message_age_seconds: float = 30.0,
        nonce_cache_size: int = 10_000,
    ):
        self._secrets: Dict[str, bytes] = {}   # agent_id -> shared secret
        self._max_age = max_message_age_seconds
        self._seen_nonces: set = set()
        self._nonce_cache_size = nonce_cache_size

    def register_agent(self, agent_id: str, shared_secret: bytes) -> None:
        self._secrets[agent_id] = shared_secret

    def _sign(self, message: SignedAgentMessage, secret: bytes) -> str:
        return hmac.new(secret, message.canonical_bytes(), hashlib.sha256).hexdigest()

    def sign(self, message: SignedAgentMessage) -> SignedAgentMessage:
        secret = self._secrets.get(message.sender_agent_id)
        if not secret:
            raise ValueError(f"no secret registered for agent '{message.sender_agent_id}'")
        message.signature = self._sign(message, secret)
        return message

    def verify(self, message: SignedAgentMessage) -> AuthenticationResult:
        # Check timestamp freshness
        age = time.time() - message.timestamp
        if abs(age) > self._max_age:
            return AuthenticationResult(
                valid=False,
                agent_id=message.sender_agent_id,
                reason=f"message too old or future: age={age:.1f}s",
            )

        # Check replay
        nonce_key = f"{message.sender_agent_id}:{message.nonce}"
        if nonce_key in self._seen_nonces:
            return AuthenticationResult(
                valid=False,
                agent_id=message.sender_agent_id,
                reason="replay attack: nonce already seen",
                replay_detected=True,
            )

        # Verify signature
        secret = self._secrets.get(message.sender_agent_id)
        if not secret:
            return AuthenticationResult(
                valid=False,
                agent_id=message.sender_agent_id,
                reason=f"unknown sender agent: '{message.sender_agent_id}'",
            )

        expected = self._sign(message, secret)
        if not hmac.compare_digest(message.signature, expected):
            return AuthenticationResult(
                valid=False,
                agent_id=message.sender_agent_id,
                reason="invalid signature — possible impersonation",
            )

        # Record nonce
        if len(self._seen_nonces) >= self._nonce_cache_size:
            self._seen_nonces.pop()
        self._seen_nonces.add(nonce_key)

        return AuthenticationResult(
            valid=True,
            agent_id=message.sender_agent_id,
            reason="ok",
        )
```

## Solution 4: Behavioral Fingerprint Verifier

```python
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional

@dataclass
class AgentBehaviorProfile:
    agent_id: str
    expected_message_types: List[str]
    avg_message_rate_per_min: float
    expected_payload_size_range: tuple   # (min_bytes, max_bytes)
    typical_recipients: List[str]

@dataclass
class BehaviorDeviation:
    agent_id: str
    deviation_type: str
    detail: str
    severity: str   # "low" | "medium" | "high"
    timestamp: float

class BehavioralFingerprintVerifier:
    """
    Compares observed agent message behavior against expected profiles.
    A message from agent X that looks structurally different from X's
    normal behavior is flagged as a potential impersonation.
    """

    def __init__(self, window_seconds: float = 300.0):
        self._profiles: Dict[str, AgentBehaviorProfile] = {}
        self._message_history: Dict[str, Deque[SignedAgentMessage]] = defaultdict(
            lambda: deque(maxlen=200)
        )
        self._deviations: List[BehaviorDeviation] = []
        self._window = window_seconds

    def register_profile(self, profile: AgentBehaviorProfile) -> None:
        self._profiles[profile.agent_id] = profile

    def observe(self, message: SignedAgentMessage) -> List[BehaviorDeviation]:
        self._message_history[message.sender_agent_id].append(message)
        profile = self._profiles.get(message.sender_agent_id)
        if not profile:
            return []

        deviations = []

        # Check message type
        if message.message_type not in profile.expected_message_types:
            dev = BehaviorDeviation(
                agent_id=message.sender_agent_id,
                deviation_type="unexpected_message_type",
                detail=f"'{message.message_type}' not in expected types {profile.expected_message_types}",
                severity="high",
                timestamp=time.time(),
            )
            deviations.append(dev)

        # Check recipient
        if profile.typical_recipients and message.recipient_agent_id not in profile.typical_recipients:
            dev = BehaviorDeviation(
                agent_id=message.sender_agent_id,
                deviation_type="unusual_recipient",
                detail=f"message to '{message.recipient_agent_id}' is unusual for this agent",
                severity="medium",
                timestamp=time.time(),
            )
            deviations.append(dev)

        # Check message rate
        cutoff = time.time() - 60.0
        recent = [m for m in self._message_history[message.sender_agent_id]
                  if m.timestamp >= cutoff]
        if len(recent) > profile.avg_message_rate_per_min * 3:
            dev = BehaviorDeviation(
                agent_id=message.sender_agent_id,
                deviation_type="message_rate_spike",
                detail=f"rate {len(recent)}/min vs expected {profile.avg_message_rate_per_min}/min",
                severity="high",
                timestamp=time.time(),
            )
            deviations.append(dev)

        self._deviations.extend(deviations)
        return deviations
```

## Solution 5: Impersonation Incident Log

```python
import time
from dataclasses import dataclass, field
from typing import List, Optional

@dataclass
class ImpersonationIncident:
    incident_id: str
    claimed_agent_id: str
    detected_by: str       # "signature_failure" | "replay" | "behavioral_deviation"
    severity: str
    message_id: str
    detail: str
    timestamp: float

class ImpersonationIncidentLog:
    """
    Append-only log of all impersonation detection events.
    Supports querying by agent, timeframe, and detection method.
    """

    def __init__(self, max_entries: int = 10_000):
        self._incidents: List[ImpersonationIncident] = []
        self._max = max_entries
        import uuid
        self._counter = 0

    def log(
        self,
        claimed_agent_id: str,
        detected_by: str,
        severity: str,
        message_id: str,
        detail: str,
    ) -> ImpersonationIncident:
        self._counter += 1
        incident = ImpersonationIncident(
            incident_id=f"imp-{self._counter:06d}",
            claimed_agent_id=claimed_agent_id,
            detected_by=detected_by,
            severity=severity,
            message_id=message_id,
            detail=detail,
            timestamp=time.time(),
        )
        if len(self._incidents) >= self._max:
            self._incidents.pop(0)
        self._incidents.append(incident)
        return incident

    def recent(self, hours: float = 24.0) -> List[ImpersonationIncident]:
        cutoff = time.time() - hours * 3600
        return [i for i in self._incidents if i.timestamp >= cutoff]

    def summary(self) -> dict:
        recent = self.recent(24.0)
        return {
            "total_incidents_24h": len(recent),
            "by_detection_method": {
                method: sum(1 for i in recent if i.detected_by == method)
                for method in {"signature_failure", "replay", "behavioral_deviation"}
            },
            "targeted_agents": list({i.claimed_agent_id for i in recent}),
        }
```

## Solution 6: Impersonation Defense Orchestrator

```python
from dataclasses import dataclass
from typing import Callable, Optional

@dataclass
class MessageVerificationResult:
    allowed: bool
    message: SignedAgentMessage
    auth_result: AuthenticationResult
    behavior_deviations: list
    incident: Optional[ImpersonationIncident]
    block_reason: str

class ImpersonationDefenseOrchestrator:
    """
    Unified message reception layer that runs all impersonation checks.
    Messages are authenticated, replay-checked, and behavior-verified
    before being delivered to the receiving agent's handler.
    """

    def __init__(
        self,
        authenticator: AgentMessageAuthenticator,
        behavior_verifier: BehavioralFingerprintVerifier,
        incident_log: ImpersonationIncidentLog,
        block_on_signature_failure: bool = True,
        block_on_behavioral_deviation: bool = False,
    ):
        self._auth = authenticator
        self._behavior = behavior_verifier
        self._log = incident_log
        self._block_sig = block_on_signature_failure
        self._block_behavior = block_on_behavioral_deviation

    def receive(self, message: SignedAgentMessage) -> MessageVerificationResult:
        # Authentication check
        auth = self._auth.verify(message)
        incident = None
        block_reason = ""

        if not auth.valid:
            detection = "replay" if auth.replay_detected else "signature_failure"
            incident = self._log.log(
                claimed_agent_id=message.sender_agent_id,
                detected_by=detection,
                severity="critical" if not auth.replay_detected else "high",
                message_id=message.message_id,
                detail=auth.reason,
            )
            if self._block_sig:
                return MessageVerificationResult(
                    allowed=False,
                    message=message,
                    auth_result=auth,
                    behavior_deviations=[],
                    incident=incident,
                    block_reason=auth.reason,
                )

        # Behavioral check
        deviations = self._behavior.observe(message)
        high_deviations = [d for d in deviations if d.severity == "high"]
        if high_deviations:
            for dev in high_deviations:
                incident = self._log.log(
                    claimed_agent_id=message.sender_agent_id,
                    detected_by="behavioral_deviation",
                    severity=dev.severity,
                    message_id=message.message_id,
                    detail=dev.detail,
                )
            if self._block_behavior:
                block_reason = "; ".join(d.detail for d in high_deviations[:2])

        allowed = auth.valid and (not high_deviations or not self._block_behavior)
        return MessageVerificationResult(
            allowed=allowed,
            message=message,
            auth_result=auth,
            behavior_deviations=deviations,
            incident=incident,
            block_reason=block_reason,
        )
```

## Comparison

| Approach | Cryptographic Auth | Replay Prevention | Behavioral Check | Incident Logging |
|---|---|---|---|---|
| SignedAgentMessage | Yes (HMAC) | Via nonce | No | No |
| AgentMessageAuthenticator | Yes | Yes | No | No |
| BehavioralFingerprintVerifier | No | No | Yes | No |
| ImpersonationIncidentLog | No | No | No | Yes |
| ImpersonationDefenseOrchestrator | Via authenticator | Via authenticator | Via verifier | Yes |

**Best for production**: Register shared secrets for all agent pairs at deployment time via a secrets vault — never hardcode. Sign all inter-agent messages with `AgentMessageAuthenticator.sign()` before sending. Verify all received messages with `ImpersonationDefenseOrchestrator.receive()` before dispatching. Start with `block_on_signature_failure=True` and `block_on_behavioral_deviation=False` — behavioral deviation blocking requires tuning profiles first. Review `ImpersonationIncidentLog.summary()` daily; a spike in `signature_failure` for a specific agent usually means key rotation happened without updating receivers.
