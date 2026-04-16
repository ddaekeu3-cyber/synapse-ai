---
title: "Agent Doesn't Implement Cryptographic Audit Trail for Agent Decisions"
description: "Agents that log decisions to mutable storage allow post-hoc alteration of audit records — a security or compliance requirement in regulated industries. Implement a cryptographic audit trail that chains each decision record to the previous one via HMAC, making tampering detectable, and publishes a verifiable chain that auditors can validate without access to the live system."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-cryptographic-audit-trail-for-agent-decisions
tags: [audit-trail, cryptographic-integrity, tamper-detection, chain-of-custody, compliance, hmac-chain]
symptoms:
  - "Audit logs are plain JSON files that can be edited without detection"
  - "No way to prove that a logged decision was not altered after the fact"
  - "Compliance audit requires evidence that agent decisions are immutable records"
  - "Log storage permissions allow writes — an attacker with log access can cover tracks"
  - "Incident investigation cannot rule out that logs were modified after the incident"
---

## Why This Happens

Structured logging writes JSON lines to a file or log aggregator. Both are mutable: a privileged user or compromised process can overwrite, delete, or insert entries. A cryptographic chain binds each entry to the previous one: entry N includes the HMAC of entry N-1, so any alteration of entry N-1 invalidates all subsequent HMACs. An auditor with the chain key can verify the entire chain without trusting the storage layer.

## Solution 1: Audit Decision Record

```python
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class AuditDecisionRecord:
    record_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    user_id: str = ""
    decision_type: str = ""   # "tool_call" | "response_generated" | "access_granted" | etc.
    decision_summary: str = ""
    inputs_hash: str = ""      # SHA-256 of input data (not the data itself)
    outputs_hash: str = ""     # SHA-256 of output data
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)
    # Chain fields — populated by AuditChainBuilder
    sequence_number: int = 0
    previous_record_hmac: Optional[str] = None
    record_hmac: Optional[str] = None

    def canonical_bytes(self) -> bytes:
        """Deterministic serialization for HMAC computation."""
        payload = {
            "record_id": self.record_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "decision_type": self.decision_type,
            "decision_summary": self.decision_summary,
            "inputs_hash": self.inputs_hash,
            "outputs_hash": self.outputs_hash,
            "timestamp": self.timestamp,
            "sequence_number": self.sequence_number,
            "previous_record_hmac": self.previous_record_hmac or "",
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def hash_data(data: Any) -> str:
        payload = json.dumps(data, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()
```

## Solution 2: Audit Chain Builder

```python
import hashlib
import hmac as _hmac
from typing import List, Optional


class AuditChainBuilder:
    """
    Builds a cryptographically linked chain of AuditDecisionRecords.
    Each record's HMAC covers its content plus the previous record's HMAC,
    forming a chain where tampering with any record invalidates all that follow.
    """

    def __init__(self, chain_key: bytes):
        self._key = chain_key
        self._last_hmac: Optional[str] = None
        self._sequence = 0

    def _compute_hmac(self, record: AuditDecisionRecord) -> str:
        return _hmac.new(self._key, record.canonical_bytes(), hashlib.sha256).hexdigest()

    def append(self, record: AuditDecisionRecord) -> AuditDecisionRecord:
        record.sequence_number = self._sequence
        record.previous_record_hmac = self._last_hmac
        record.record_hmac = self._compute_hmac(record)
        self._last_hmac = record.record_hmac
        self._sequence += 1
        return record

    def current_chain_tip(self) -> Optional[str]:
        return self._last_hmac

    def chain_length(self) -> int:
        return self._sequence
```

## Solution 3: Chain Verifier

```python
import hashlib
import hmac as _hmac
from typing import List, Tuple


class AuditChainVerifier:
    """
    Verifies the integrity of an audit chain.
    Returns a list of (record_id, error) pairs for any broken links.
    An empty list means the chain is intact.
    """

    def __init__(self, chain_key: bytes):
        self._key = chain_key

    def _compute_hmac(self, record: AuditDecisionRecord) -> str:
        return _hmac.new(self._key, record.canonical_bytes(), hashlib.sha256).hexdigest()

    def verify(self, records: List[AuditDecisionRecord]) -> List[Tuple[str, str]]:
        errors = []
        sorted_records = sorted(records, key=lambda r: r.sequence_number)

        for i, record in enumerate(sorted_records):
            # Verify sequence is contiguous
            if record.sequence_number != i:
                errors.append((record.record_id, f"sequence gap: expected {i}, got {record.sequence_number}"))
                continue

            # Verify previous HMAC link
            if i == 0:
                if record.previous_record_hmac is not None and record.previous_record_hmac != "":
                    errors.append((record.record_id, "first record should have no previous HMAC"))
            else:
                expected_prev = sorted_records[i - 1].record_hmac
                if record.previous_record_hmac != expected_prev:
                    errors.append((
                        record.record_id,
                        f"chain broken: previous_record_hmac mismatch at sequence {i}",
                    ))

            # Verify own HMAC
            expected_hmac = self._compute_hmac(record)
            if not _hmac.compare_digest(record.record_hmac or "", expected_hmac):
                errors.append((record.record_id, f"record HMAC invalid at sequence {i}"))

        return errors

    def is_intact(self, records: List[AuditDecisionRecord]) -> bool:
        return len(self.verify(records)) == 0
```

## Solution 4: Audit Trail Store

```python
import json
import time
from typing import List, Optional


class AuditTrailStore:
    """
    Stores chained audit records.
    In production, replace in-memory storage with an append-only database table
    or an immutable object store (S3 with object lock, WORM storage).
    Provides export for external audit without requiring live system access.
    """

    def __init__(self):
        self._records: List[AuditDecisionRecord] = []

    def append(self, record: AuditDecisionRecord) -> None:
        self._records.append(record)

    def all_records(self) -> List[AuditDecisionRecord]:
        return list(self._records)

    def records_for_session(self, session_id: str) -> List[AuditDecisionRecord]:
        return [r for r in self._records if r.session_id == session_id]

    def export_jsonl(self) -> str:
        lines = []
        for record in sorted(self._records, key=lambda r: r.sequence_number):
            lines.append(json.dumps({
                "record_id": record.record_id,
                "session_id": record.session_id,
                "user_id": record.user_id,
                "decision_type": record.decision_type,
                "decision_summary": record.decision_summary,
                "timestamp": record.timestamp,
                "sequence_number": record.sequence_number,
                "previous_record_hmac": record.previous_record_hmac,
                "record_hmac": record.record_hmac,
            }))
        return "\n".join(lines)

    def record_count(self) -> int:
        return len(self._records)
```

## Solution 5: Decision Auditor

```python
from typing import Any, Dict


class AgentDecisionAuditor:
    """
    High-level interface for recording agent decisions to the audit trail.
    Handles hashing of inputs/outputs and chain building automatically.
    """

    def __init__(
        self,
        chain_builder: AuditChainBuilder,
        store: AuditTrailStore,
    ):
        self._builder = chain_builder
        self._store = store

    def record_tool_call(
        self,
        session_id: str,
        user_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        result: Any,
    ) -> AuditDecisionRecord:
        record = AuditDecisionRecord(
            session_id=session_id,
            user_id=user_id,
            decision_type="tool_call",
            decision_summary=f"tool={tool_name}",
            inputs_hash=AuditDecisionRecord.hash_data({"tool": tool_name, "args": arguments}),
            outputs_hash=AuditDecisionRecord.hash_data(result),
        )
        self._builder.append(record)
        self._store.append(record)
        return record

    def record_access_decision(
        self,
        session_id: str,
        user_id: str,
        resource: str,
        granted: bool,
        reason: str,
    ) -> AuditDecisionRecord:
        record = AuditDecisionRecord(
            session_id=session_id,
            user_id=user_id,
            decision_type="access_decision",
            decision_summary=f"resource={resource} granted={granted} reason={reason}",
            inputs_hash=AuditDecisionRecord.hash_data({"resource": resource, "user": user_id}),
            outputs_hash=AuditDecisionRecord.hash_data({"granted": granted}),
        )
        self._builder.append(record)
        self._store.append(record)
        return record
```

## Solution 6: Audit Verification Report

```python
import time


class AuditVerificationReport:
    """
    Produces a structured verification report suitable for compliance auditors.
    """

    def __init__(self, store: AuditTrailStore, verifier: AuditChainVerifier):
        self._store = store
        self._verifier = verifier

    def generate(self) -> dict:
        records = self._store.all_records()
        errors = self._verifier.verify(records)
        chain_intact = len(errors) == 0

        return {
            "report_generated_at": time.time(),
            "total_records": len(records),
            "chain_intact": chain_intact,
            "verification_errors": [
                {"record_id": rid, "error": err}
                for rid, err in errors
            ],
            "first_record_id": records[0].record_id if records else None,
            "last_record_id": records[-1].record_id if records else None,
            "chain_tip_hmac": records[-1].record_hmac if records else None,
            "decision_types": list({r.decision_type for r in records}),
        }
```

## Comparison

| Approach | HMAC Chain | Tamper Detection | Sequence Verification | Export | Compliance Report |
|---|---|---|---|---|---|
| AuditChainBuilder | Yes | No (builds only) | No | No | No |
| AuditChainVerifier | Via chain | Yes | Yes (gaps + HMAC) | No | No |
| AuditTrailStore | No | No | No | Yes (JSONL) | No |
| AgentDecisionAuditor | Via builder | No | No | No | No |
| AuditVerificationReport | Via verifier | Via verifier | Via verifier | No | Yes |

**Best for production**: Store `chain_key` in a Hardware Security Module (HSM) or AWS KMS — never in the same system as the audit logs. Use an append-only storage backend (PostgreSQL INSERT-only table, S3 object lock) to enforce physical immutability alongside cryptographic integrity. Export the full JSONL chain nightly to cold storage for long-term retention. Run `AuditVerificationReport.generate()` daily and alert on any `chain_intact=False` — a broken chain indicates either a bug in the audit code or tampering, both of which require immediate investigation. The `chain_tip_hmac` serves as a point-in-time checkpoint that auditors can store externally to prove the state of the log at a given moment.
