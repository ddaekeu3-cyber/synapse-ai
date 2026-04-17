---
title: "Agent Doesn't Implement Tool Call Audit Trail with Tamper Detection"
description: "Agents that log tool calls without integrity protection allow log tampering — an attacker or malicious insider who can write to log storage can delete or modify records of unauthorized tool invocations. Implement a tamper-evident audit trail using hash chaining that makes any modification to a prior record detectable, providing cryptographic evidence of the complete tool call history."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-tool-call-audit-trail-with-tamper-detection
tags: [audit-trail, tamper-detection, hash-chaining, tool-call-logging, integrity-protection, forensic-logging]
symptoms:
  - "Audit logs for tool calls can be silently modified without detection"
  - "No way to prove that the recorded tool call history is complete and unaltered"
  - "Compliance audits require tamper-evident logs but only plaintext JSONL exists"
  - "An insider with log write access can delete records of unauthorized API calls"
  - "No cryptographic linkage between log entries — each is independent"
---

## Why This Happens

Standard logging writes independent records to a file or database. Each record is self-contained — modifying, inserting, or deleting a record leaves no detectable trace. Hash chaining solves this by including the hash of the previous record in each new record: to tamper with record N you must also update record N+1 (which references N's hash), then N+2, and so on — and the final hash in the chain must still match a published reference. Any modification breaks the chain and is immediately detectable by recomputing hashes.

## Solution 1: Audit Event Model

```python
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class AuditEventType(str, Enum):
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_COMPLETE = "tool_call_complete"
    TOOL_CALL_FAILED = "tool_call_failed"
    TOOL_CALL_BLOCKED = "tool_call_blocked"
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    PERMISSION_CHECK = "permission_check"
    SECURITY_VIOLATION = "security_violation"


@dataclass
class AuditEvent:
    event_id: str
    sequence: int
    event_type: AuditEventType
    session_id: str
    user_id: str
    timestamp: float
    payload: Dict[str, Any]
    prev_hash: str              # SHA-256 of previous record's canonical form
    record_hash: str = ""       # SHA-256 of this record (computed after creation)

    @staticmethod
    def create(
        sequence: int,
        event_type: AuditEventType,
        session_id: str,
        user_id: str,
        payload: Dict[str, Any],
        prev_hash: str,
    ) -> "AuditEvent":
        event = AuditEvent(
            event_id=str(uuid.uuid4()),
            sequence=sequence,
            event_type=event_type,
            session_id=session_id,
            user_id=user_id,
            timestamp=time.time(),
            payload=payload,
            prev_hash=prev_hash,
        )
        event.record_hash = event._compute_hash()
        return event

    def _compute_hash(self) -> str:
        canonical = json.dumps({
            "event_id": self.event_id,
            "sequence": self.sequence,
            "event_type": self.event_type.value,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "timestamp": self.timestamp,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
        }, sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()

    def verify_integrity(self) -> bool:
        return self.record_hash == self._compute_hash()

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "sequence": self.sequence,
            "event_type": self.event_type.value,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "timestamp": self.timestamp,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "record_hash": self.record_hash,
        }
```

## Solution 2: Hash-Chained Audit Logger

```python
import json
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional


GENESIS_HASH = "0" * 64  # initial prev_hash for the first record


class HashChainedAuditLogger:
    """
    Appends audit events to a JSONL file with hash chaining.
    Each record includes the SHA-256 hash of the previous record,
    making any tampering detectable via chain verification.
    """

    def __init__(self, path: str = "/var/log/agent_audit.jsonl"):
        self._path = Path(path)
        self._lock = Lock()
        self._sequence = 0
        self._last_hash = GENESIS_HASH
        self._load_state()

    def _load_state(self) -> None:
        """Resume from last known state if the log file already exists."""
        if not self._path.exists():
            return
        last_line = None
        for line in self._path.read_text().splitlines():
            if line.strip():
                last_line = line
        if last_line:
            try:
                last = json.loads(last_line)
                self._sequence = last["sequence"] + 1
                self._last_hash = last["record_hash"]
            except (json.JSONDecodeError, KeyError):
                pass

    def record(
        self,
        event_type: AuditEventType,
        session_id: str,
        user_id: str,
        payload: Dict[str, Any],
    ) -> AuditEvent:
        with self._lock:
            event = AuditEvent.create(
                sequence=self._sequence,
                event_type=event_type,
                session_id=session_id,
                user_id=user_id,
                payload=self._sanitize(payload),
                prev_hash=self._last_hash,
            )
            self._sequence += 1
            self._last_hash = event.record_hash

            with self._path.open("a") as f:
                f.write(json.dumps(event.to_dict()) + "\n")

        return event

    @staticmethod
    def _sanitize(payload: dict) -> dict:
        result = {}
        for k, v in payload.items():
            if isinstance(v, str) and len(v) > 500:
                result[k] = v[:500] + "...[truncated]"
            else:
                result[k] = v
        return result

    @property
    def current_chain_tip(self) -> str:
        with self._lock:
            return self._last_hash
```

## Solution 3: Chain Integrity Verifier

```python
import json
from pathlib import Path
from typing import List, Optional


class ChainIntegrityVerificationResult:
    def __init__(self):
        self.valid = True
        self.total_records = 0
        self.violations: List[dict] = []
        self.first_violation_sequence: Optional[int] = None

    def add_violation(self, sequence: int, reason: str) -> None:
        self.valid = False
        if self.first_violation_sequence is None:
            self.first_violation_sequence = sequence
        self.violations.append({"sequence": sequence, "reason": reason})


class ChainIntegrityVerifier:
    """
    Reads the entire audit log and verifies:
    1. Each record's hash matches its content (tamper detection)
    2. Each record's prev_hash matches the previous record's hash (chain integrity)
    """

    def verify(self, path: str) -> ChainIntegrityVerificationResult:
        result = ChainIntegrityVerificationResult()
        log_path = Path(path)
        if not log_path.exists():
            return result

        prev_hash = GENESIS_HASH
        expected_sequence = 0

        for line in log_path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                result.total_records += 1
                seq = data["sequence"]

                # Reconstruct event and verify hash
                event = AuditEvent(
                    event_id=data["event_id"],
                    sequence=seq,
                    event_type=AuditEventType(data["event_type"]),
                    session_id=data["session_id"],
                    user_id=data["user_id"],
                    timestamp=data["timestamp"],
                    payload=data["payload"],
                    prev_hash=data["prev_hash"],
                    record_hash=data["record_hash"],
                )

                if not event.verify_integrity():
                    result.add_violation(seq, "record_hash_mismatch: record content was modified")

                if event.prev_hash != prev_hash:
                    result.add_violation(seq, f"chain_broken: prev_hash mismatch at sequence {seq}")

                if seq != expected_sequence:
                    result.add_violation(seq, f"sequence_gap: expected {expected_sequence}, got {seq}")

                prev_hash = data["record_hash"]
                expected_sequence = seq + 1

            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                result.add_violation(-1, f"parse_error: {exc}")

        return result
```

## Solution 4: Instrumented Tool Call Auditor

```python
import time
from typing import Any, Callable, Dict, Optional


class InstrumentedToolCallAuditor:
    """
    Wraps tool calls with audit logging at start, completion, and failure.
    Each event is chained to the previous, providing a tamper-evident record.
    """

    def __init__(self, logger: HashChainedAuditLogger):
        self._logger = logger

    async def audit_call(
        self,
        session_id: str,
        user_id: str,
        tool_name: str,
        args: Dict[str, Any],
        tool_fn: Callable,
    ) -> Any:
        # Log start
        self._logger.record(
            AuditEventType.TOOL_CALL_START,
            session_id=session_id,
            user_id=user_id,
            payload={
                "tool_name": tool_name,
                "args_keys": list(args.keys()),
            },
        )
        start = time.time()
        try:
            result = await tool_fn(**args)
            latency_ms = round((time.time() - start) * 1000, 2)
            self._logger.record(
                AuditEventType.TOOL_CALL_COMPLETE,
                session_id=session_id,
                user_id=user_id,
                payload={
                    "tool_name": tool_name,
                    "latency_ms": latency_ms,
                    "result_type": type(result).__name__,
                },
            )
            return result
        except Exception as exc:
            latency_ms = round((time.time() - start) * 1000, 2)
            self._logger.record(
                AuditEventType.TOOL_CALL_FAILED,
                session_id=session_id,
                user_id=user_id,
                payload={
                    "tool_name": tool_name,
                    "latency_ms": latency_ms,
                    "error_type": type(exc).__name__,
                    "error_msg": str(exc)[:200],
                },
            )
            raise
```

## Solution 5: Audit Trail Checkpoint Publisher

```python
import hashlib
import json
import time
from typing import Callable, Optional


class AuditTrailCheckpointPublisher:
    """
    Periodically publishes the current chain tip hash to an external,
    write-only store (e.g., a public ledger, S3 object, or signed webhook).
    This prevents retroactive tampering: an attacker who modifies old records
    must also forge the published checkpoints.
    """

    def __init__(
        self,
        logger: HashChainedAuditLogger,
        publish_fn: Callable[[dict], None],
        interval_seconds: float = 300.0,
    ):
        self._logger = logger
        self._publish_fn = publish_fn
        self._interval = interval_seconds
        self._last_published: Optional[float] = None
        self._published_count = 0

    def maybe_publish(self) -> bool:
        now = time.time()
        if self._last_published and now - self._last_published < self._interval:
            return False

        checkpoint = {
            "ts": now,
            "chain_tip": self._logger.current_chain_tip,
            "checkpoint_id": self._published_count,
        }
        checkpoint["checkpoint_hash"] = hashlib.sha256(
            json.dumps(checkpoint, sort_keys=True).encode()
        ).hexdigest()

        self._publish_fn(checkpoint)
        self._last_published = now
        self._published_count += 1
        return True
```

## Solution 6: Audit Trail Dashboard

```python
import time


class AuditTrailDashboard:
    """
    Provides operational visibility into audit trail health:
    chain integrity status, record count, and recent security events.
    """

    def __init__(
        self,
        logger: HashChainedAuditLogger,
        verifier: ChainIntegrityVerifier,
        log_path: str,
    ):
        self._logger = logger
        self._verifier = verifier
        self._log_path = log_path

    def render(self) -> dict:
        verification = self._verifier.verify(self._log_path)
        return {
            "generated_at": time.time(),
            "chain_tip": self._logger.current_chain_tip,
            "integrity_valid": verification.valid,
            "total_records": verification.total_records,
            "violations": len(verification.violations),
            "first_violation_sequence": verification.first_violation_sequence,
            "violation_details": verification.violations[:3],
        }
```

## Comparison

| Approach | Hash-Chained Records | Tamper Detection | Sequence Gaps | External Checkpoints | Dashboard |
|---|---|---|---|---|---|
| AuditEvent | Yes (per-record hash) | Via verify_integrity() | No | No | No |
| HashChainedAuditLogger | Yes (chain) | Via hash | No | No | No |
| ChainIntegrityVerifier | No | Yes (full scan) | Yes | No | No |
| InstrumentedToolCallAuditor | Via logger | Via logger | No | No | No |
| AuditTrailCheckpointPublisher | No | No | No | Yes | No |
| AuditTrailDashboard | No | Via verifier | Via verifier | No | Yes |

**Best for production**: Run `ChainIntegrityVerifier.verify()` daily as a scheduled job and alert immediately on any violation — a broken chain means either a bug or an active tampering incident. Publish checkpoints every 5 minutes to an append-only external store (S3 with Object Lock, a public ledger, or a signed time-stamping service) — these make retroactive tampering computationally infeasible. Store the audit log on a separate system from the agent with minimal write permissions — the agent should only append, never update or delete. Include `args_keys` but not full arg values in audit records to preserve privacy while still showing which tools were called with which parameter categories.
