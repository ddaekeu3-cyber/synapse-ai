---
title: "Agent Doesn't Implement Conversation History Tampering Detection"
description: "Agents that store conversation history in client-accessible storage — browser localStorage, mobile app storage, or API payloads controlled by the caller — are vulnerable to history tampering: an attacker modifies past assistant messages to inject false context, override prior decisions, or establish fake permissions. Implement conversation history tampering detection using cryptographic message authentication codes (HMAC) that verify the integrity of each turn before the history is injected into the next prompt."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-conversation-history-tampering-detection
tags: [history-tampering, hmac-integrity, conversation-integrity, message-authentication, replay-protection, history-injection]
symptoms:
  - "Client-side conversation history passed to the agent without server-side integrity verification"
  - "Attacker modifies past assistant message to say 'I have granted admin access' before the next turn"
  - "No MAC or signature on stored conversation turns"
  - "History retrieved from untrusted client storage is injected into prompts as-is"
  - "Turn ordering can be modified without detection — turns reordered or deleted"
---

## Why This Happens

Stateless API designs often push conversation history to the client for storage, requiring the client to include prior turns in each request. This is operationally convenient but creates a trust boundary: the agent must treat client-supplied history as untrusted input. Without integrity verification, the agent has no way to distinguish authentic history from attacker-injected messages. HMAC-based integrity tags, computed server-side when each turn is generated and verified server-side when the history is received, close this gap without requiring server-side conversation storage.

## Solution 1: Turn Integrity Tagger

```python
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class IntegrityTaggedTurn:
    role: str
    content: str
    turn_index: int
    session_id: str
    tag: str                # HMAC-SHA256 hex digest
    tagged_at: float = field(default_factory=time.time)
    nonce: str = ""

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "content": self.content,
            "turn_index": self.turn_index,
            "session_id": self.session_id,
            "tag": self.tag,
            "tagged_at": self.tagged_at,
            "nonce": self.nonce,
        }


class TurnIntegrityTagger:
    """
    Computes an HMAC-SHA256 tag over the canonical turn content.
    The key should be a server-side secret never exposed to clients.
    """

    def __init__(self, secret_key: bytes):
        self._key = secret_key

    def _canonical(self, role: str, content: str, turn_index: int,
                    session_id: str, nonce: str) -> bytes:
        payload = json.dumps({
            "role": role,
            "content": content,
            "turn_index": turn_index,
            "session_id": session_id,
            "nonce": nonce,
        }, sort_keys=True, separators=(",", ":"))
        return payload.encode()

    def tag(
        self,
        role: str,
        content: str,
        turn_index: int,
        session_id: str,
        nonce: Optional[str] = None,
    ) -> IntegrityTaggedTurn:
        import uuid
        n = nonce or str(uuid.uuid4())[:8]
        canonical = self._canonical(role, content, turn_index, session_id, n)
        digest = hmac.new(self._key, canonical, hashlib.sha256).hexdigest()
        return IntegrityTaggedTurn(
            role=role,
            content=content,
            turn_index=turn_index,
            session_id=session_id,
            tag=digest,
            nonce=n,
        )
```

## Solution 2: Turn Integrity Verifier

```python
import hmac
import hashlib
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class VerificationResult:
    valid: bool
    turn_index: int
    failure_reason: Optional[str] = None


class TurnIntegrityVerifier:
    """
    Verifies HMAC tags on incoming turns.
    Detects content modification, turn reordering, and turn deletion.
    """

    def __init__(self, tagger: TurnIntegrityTagger):
        self._tagger = tagger

    def verify_turn(self, turn: IntegrityTaggedTurn) -> VerificationResult:
        canonical = self._tagger._canonical(
            turn.role, turn.content, turn.turn_index, turn.session_id, turn.nonce
        )
        expected = hmac.new(self._tagger._key, canonical, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, turn.tag):
            return VerificationResult(
                valid=False,
                turn_index=turn.turn_index,
                failure_reason="tag_mismatch: content or metadata was modified",
            )
        return VerificationResult(valid=True, turn_index=turn.turn_index)

    def verify_sequence(
        self,
        turns: List[IntegrityTaggedTurn],
        session_id: str,
    ) -> List[VerificationResult]:
        results = []
        expected_index = 0
        for turn in turns:
            result = self.verify_turn(turn)
            if not result.valid:
                results.append(result)
                continue
            if turn.session_id != session_id:
                results.append(VerificationResult(
                    valid=False,
                    turn_index=turn.turn_index,
                    failure_reason=f"session_id_mismatch: expected {session_id}",
                ))
                continue
            if turn.turn_index != expected_index:
                results.append(VerificationResult(
                    valid=False,
                    turn_index=turn.turn_index,
                    failure_reason=(
                        f"sequence_violation: expected turn {expected_index}, got {turn.turn_index}"
                    ),
                ))
            else:
                results.append(result)
            expected_index = turn.turn_index + 1
        return results
```

## Solution 3: Tamper Detection Gate

```python
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class TamperDetectionOutcome:
    history_accepted: bool
    tampered_turn_count: int
    tamper_details: List[VerificationResult]
    clean_turns: List[IntegrityTaggedTurn]
    rejection_reason: Optional[str] = None


class ConversationHistoryTamperGate:
    """
    Validates an incoming history before allowing it to be injected
    into a prompt. Rejects histories with any tampered turns.
    """

    def __init__(
        self,
        verifier: TurnIntegrityVerifier,
        reject_on_any_failure: bool = True,
    ):
        self._verifier = verifier
        self._strict = reject_on_any_failure

    def evaluate(
        self,
        turns: List[IntegrityTaggedTurn],
        session_id: str,
    ) -> TamperDetectionOutcome:
        results = self._verifier.verify_sequence(turns, session_id)
        failures = [r for r in results if not r.valid]
        clean = [t for t, r in zip(turns, results) if r.valid]

        if failures and self._strict:
            return TamperDetectionOutcome(
                history_accepted=False,
                tampered_turn_count=len(failures),
                tamper_details=failures,
                clean_turns=[],
                rejection_reason=f"{len(failures)} turn(s) failed integrity verification",
            )

        return TamperDetectionOutcome(
            history_accepted=True,
            tampered_turn_count=len(failures),
            tamper_details=failures,
            clean_turns=clean if failures else turns,
        )
```

## Solution 4: Integrity-Protected History Manager

```python
from typing import Any, Dict, List, Optional


class IntegrityProtectedHistoryManager:
    """
    Manages conversation history with automatic tagging on write
    and verification on read. Returns only verified turns for prompt injection.
    """

    def __init__(
        self,
        tagger: TurnIntegrityTagger,
        gate: ConversationHistoryTamperGate,
        session_id: str,
    ):
        self._tagger = tagger
        self._gate = gate
        self._session_id = session_id
        self._turns: List[IntegrityTaggedTurn] = []

    def append(self, role: str, content: str) -> IntegrityTaggedTurn:
        turn = self._tagger.tag(
            role=role,
            content=content,
            turn_index=len(self._turns),
            session_id=self._session_id,
        )
        self._turns.append(turn)
        return turn

    def export(self) -> List[dict]:
        """Serializable form for client storage."""
        return [t.to_dict() for t in self._turns]

    def import_and_verify(
        self,
        raw_turns: List[dict],
    ) -> TamperDetectionOutcome:
        """Load history from client and verify integrity before use."""
        turns = []
        for raw in raw_turns:
            try:
                turns.append(IntegrityTaggedTurn(**raw))
            except (TypeError, KeyError):
                turns.append(IntegrityTaggedTurn(
                    role=raw.get("role", ""),
                    content=raw.get("content", ""),
                    turn_index=raw.get("turn_index", -1),
                    session_id=raw.get("session_id", ""),
                    tag="",
                    nonce="",
                ))

        outcome = self._gate.evaluate(turns, self._session_id)
        if outcome.history_accepted:
            self._turns = outcome.clean_turns
        return outcome

    def prompt_messages(self) -> List[Dict[str, str]]:
        return [{"role": t.role, "content": t.content} for t in self._turns]
```

## Solution 5: Tampering Incident Logger

```python
import time
from collections import deque
from threading import Lock
from typing import Deque


class TamperingIncidentLogger:
    """
    Records conversation history tampering detections for security audit.
    """

    def __init__(self, max_records: int = 10_000):
        self._max = max_records
        self._records: Deque[dict] = deque()
        self._lock = Lock()

    def record(self, outcome: TamperDetectionOutcome, session_id: str) -> None:
        if outcome.tampered_turn_count == 0:
            return
        with self._lock:
            self._records.append({
                "ts": time.time(),
                "session_id": session_id,
                "tampered_turns": outcome.tampered_turn_count,
                "rejected": not outcome.history_accepted,
                "failure_reasons": [r.failure_reason for r in outcome.tamper_details],
            })
            if len(self._records) > self._max:
                self._records.popleft()

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r["ts"] >= cutoff]
        return {
            "window_seconds": window_seconds,
            "tampering_incidents": len(recent),
            "rejected_sessions": sum(1 for r in recent if r["rejected"]),
            "unique_sessions": len({r["session_id"] for r in recent}),
        }
```

## Solution 6: Tamper-Proof History Dashboard

```python
import time


class TamperProofHistoryDashboard:
    """
    Renders tampering detection statistics and incident summaries
    for security monitoring.
    """

    def __init__(
        self,
        logger: TamperingIncidentLogger,
        tagger: TurnIntegrityTagger,
    ):
        self._logger = logger
        self._tagger = tagger

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "protection": {
                "algorithm": "HMAC-SHA256",
                "sequence_validation": True,
                "session_binding": True,
            },
            "incidents_1h": self._logger.summary(3600.0),
            "incidents_24h": self._logger.summary(86400.0),
        }
```

## Comparison

| Approach | HMAC Tagging | Tag Verification | Sequence Check | Session Binding | Incident Logging |
|---|---|---|---|---|---|
| TurnIntegrityTagger | Yes (SHA-256) | No | No | Yes | No |
| TurnIntegrityVerifier | No | Yes | Yes | Yes | No |
| ConversationHistoryTamperGate | No | Via verifier | Via verifier | Via verifier | No |
| IntegrityProtectedHistoryManager | Via tagger | Via gate | Via gate | Via tagger | No |
| TamperingIncidentLogger | No | No | No | No | Yes |

**Best for production**: Rotate the HMAC secret periodically (e.g., every 24 hours) and version the secret so old histories can still be verified during the rotation window. Use `hmac.compare_digest()` for all tag comparisons — never use `==` on MAC values, as timing side-channels can leak key material. Set `reject_on_any_failure=True` in production: a single tampered turn invalidates the entire history's trustworthiness. Alert via `TamperingIncidentLogger` when more than 3 tampering incidents occur from a single session — this pattern indicates active adversarial probing, not accidental corruption.
