---
title: "Agent Doesn't Implement User Consent Tracking for Data Processing"
description: "Agents that process user data without recording what the user consented to expose GDPR, CCPA, and HIPAA compliance gaps: there is no audit trail of which processing activities were authorized, consent records are not linked to the specific data processed, and users cannot exercise their right to withdraw consent and stop ongoing processing. Implement consent tracking that captures consent grants and withdrawals, links processing decisions to consent records, and gates data-processing operations on valid unexpired consent."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-user-consent-tracking-for-data-processing
tags: [consent-tracking, gdpr, data-privacy, compliance, consent-audit, data-processing-authorization]
symptoms:
  - "No record of which users consented to which data processing activities"
  - "Consent is captured in a UI checkbox but never linked to the agent's processing decisions"
  - "Users who withdraw consent continue to have their data processed by the agent"
  - "Compliance audit cannot reconstruct what data was processed under what consent basis"
  - "No mechanism to check whether consent is still valid before processing sensitive data"
---

## Why This Happens

Consent is often collected at account creation as a single checkbox and never re-evaluated. The agent's processing logic has no connection to the consent record: it receives a user ID and a request, and processes it without checking whether the user authorized that specific processing activity. GDPR and similar regulations require: (1) a lawful basis for each processing activity, (2) a record of consent granted, (3) a mechanism to withdraw consent, (4) cessation of processing on withdrawal, and (5) linkage between the consent record and the data processed. Implementing this requires a consent store, a per-operation consent gate, and an audit trail of processing decisions.

## Solution 1: Consent Record

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class ConsentBasis(str, Enum):
    EXPLICIT_CONSENT = "explicit_consent"      # user actively opted in
    LEGITIMATE_INTEREST = "legitimate_interest"
    CONTRACT_PERFORMANCE = "contract_performance"
    LEGAL_OBLIGATION = "legal_obligation"
    VITAL_INTEREST = "vital_interest"


class ConsentStatus(str, Enum):
    ACTIVE = "active"
    WITHDRAWN = "withdrawn"
    EXPIRED = "expired"


@dataclass
class ConsentRecord:
    consent_id: str
    user_id: str
    processing_purpose: str        # e.g., "agent_conversation_history"
    basis: ConsentBasis
    status: ConsentStatus
    granted_at: float
    expires_at: Optional[float]    # None = no expiry
    withdrawn_at: Optional[float] = None
    version: str = "1.0"           # consent document version at time of grant
    ip_address: str = ""
    user_agent: str = ""
    metadata: dict = field(default_factory=dict)

    @classmethod
    def grant(
        cls,
        user_id: str,
        processing_purpose: str,
        basis: ConsentBasis,
        ttl_seconds: Optional[float] = None,
        version: str = "1.0",
        **metadata,
    ) -> "ConsentRecord":
        now = time.time()
        return cls(
            consent_id=str(uuid.uuid4()),
            user_id=user_id,
            processing_purpose=processing_purpose,
            basis=basis,
            status=ConsentStatus.ACTIVE,
            granted_at=now,
            expires_at=now + ttl_seconds if ttl_seconds else None,
            version=version,
            metadata=metadata,
        )

    def is_valid(self) -> bool:
        if self.status != ConsentStatus.ACTIVE:
            return False
        if self.expires_at and time.time() > self.expires_at:
            return False
        return True

    def withdraw(self) -> None:
        self.status = ConsentStatus.WITHDRAWN
        self.withdrawn_at = time.time()
```

## Solution 2: Consent Store

```python
import time
from threading import Lock
from typing import Dict, List, Optional


class ConsentStore:
    """
    Stores and retrieves consent records per user per processing purpose.
    In production, back this with a durable database — in-memory for testing only.
    """

    def __init__(self):
        self._records: Dict[str, List[ConsentRecord]] = {}   # user_id -> records
        self._lock = Lock()

    def grant(self, record: ConsentRecord) -> None:
        with self._lock:
            if record.user_id not in self._records:
                self._records[record.user_id] = []
            self._records[record.user_id].append(record)

    def withdraw(self, user_id: str, processing_purpose: str) -> int:
        """Withdraws all active consents for the given user and purpose. Returns count."""
        with self._lock:
            records = self._records.get(user_id, [])
            count = 0
            for record in records:
                if record.processing_purpose == processing_purpose and record.is_valid():
                    record.withdraw()
                    count += 1
            return count

    def withdraw_all(self, user_id: str) -> int:
        """Withdraws all active consents for a user (right to erasure request)."""
        with self._lock:
            records = self._records.get(user_id, [])
            count = sum(1 for r in records if r.is_valid())
            for record in records:
                if record.is_valid():
                    record.withdraw()
            return count

    def get_valid(self, user_id: str, processing_purpose: str) -> Optional[ConsentRecord]:
        with self._lock:
            records = self._records.get(user_id, [])
            for record in reversed(records):
                if record.processing_purpose == processing_purpose and record.is_valid():
                    return record
            return None

    def get_all(self, user_id: str) -> List[ConsentRecord]:
        with self._lock:
            return list(self._records.get(user_id, []))
```

## Solution 3: Processing Purpose Registry

```python
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class ProcessingPurposeDescriptor:
    purpose_id: str
    display_name: str
    description: str
    required_basis: List[ConsentBasis]
    sensitive_data: bool = False      # triggers stricter consent requirements
    ttl_seconds: Optional[float] = None   # suggested consent lifetime
    legal_reference: str = ""


class ProcessingPurposeRegistry:
    """
    Defines all processing purposes the agent may perform.
    Used to validate that consent covers the specific purpose before processing.
    """

    def __init__(self):
        self._purposes: Dict[str, ProcessingPurposeDescriptor] = {}

    def register(self, descriptor: ProcessingPurposeDescriptor) -> None:
        self._purposes[descriptor.purpose_id] = descriptor

    def get(self, purpose_id: str) -> Optional[ProcessingPurposeDescriptor]:
        return self._purposes.get(purpose_id)

    def all_purposes(self) -> List[ProcessingPurposeDescriptor]:
        return list(self._purposes.values())
```

## Solution 4: Consent Gate

```python
from dataclasses import dataclass
from typing import Optional


@dataclass
class ConsentCheckResult:
    allowed: bool
    user_id: str
    processing_purpose: str
    consent_id: Optional[str]
    basis: Optional[ConsentBasis]
    denial_reason: str = ""


class ConsentGate:
    """
    Checks whether a user has valid consent for a processing purpose
    before allowing the operation to proceed.
    """

    def __init__(
        self,
        store: ConsentStore,
        purpose_registry: ProcessingPurposeRegistry,
        audit_logger: "ConsentAuditLogger",
    ):
        self._store = store
        self._registry = purpose_registry
        self._logger = audit_logger

    def check(self, user_id: str, processing_purpose: str) -> ConsentCheckResult:
        purpose_desc = self._registry.get(processing_purpose)
        if purpose_desc is None:
            result = ConsentCheckResult(
                allowed=False,
                user_id=user_id,
                processing_purpose=processing_purpose,
                consent_id=None,
                basis=None,
                denial_reason=f"unknown processing purpose '{processing_purpose}'",
            )
            self._logger.record_check(result)
            return result

        record = self._store.get_valid(user_id, processing_purpose)

        if record is None:
            result = ConsentCheckResult(
                allowed=False,
                user_id=user_id,
                processing_purpose=processing_purpose,
                consent_id=None,
                basis=None,
                denial_reason="no valid consent found for this processing purpose",
            )
        else:
            result = ConsentCheckResult(
                allowed=True,
                user_id=user_id,
                processing_purpose=processing_purpose,
                consent_id=record.consent_id,
                basis=record.basis,
            )

        self._logger.record_check(result)
        return result

    def enforce(self, user_id: str, processing_purpose: str) -> ConsentRecord:
        result = self.check(user_id, processing_purpose)
        if not result.allowed:
            raise ConsentRequiredError(user_id, processing_purpose, result.denial_reason)
        return self._store.get_valid(user_id, processing_purpose)


class ConsentRequiredError(Exception):
    def __init__(self, user_id: str, purpose: str, reason: str):
        super().__init__(
            f"consent required for user '{user_id}' to perform '{purpose}': {reason}"
        )
        self.user_id = user_id
        self.purpose = purpose
        self.denial_reason = reason
```

## Solution 5: Processing Activity Logger

```python
import time
from typing import List, Optional


class ProcessingActivityLogger:
    """
    Records every data processing activity with the consent basis that authorized it.
    Provides the audit trail required by GDPR Article 30 (records of processing).
    """

    def __init__(self, max_records: int = 100000):
        self._max = max_records
        self._records: List[dict] = []

    def record(
        self,
        user_id: str,
        processing_purpose: str,
        consent_id: Optional[str],
        basis: Optional[ConsentBasis],
        data_categories: List[str],
        operation: str,
        session_id: str = "",
    ) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "user_id": user_id,
            "processing_purpose": processing_purpose,
            "consent_id": consent_id,
            "basis": basis.value if basis else None,
            "data_categories": data_categories,
            "operation": operation,
            "session_id": session_id,
        })

    def records_for_user(self, user_id: str) -> List[dict]:
        return [r for r in self._records if r["user_id"] == user_id]

    def records_for_consent(self, consent_id: str) -> List[dict]:
        return [r for r in self._records if r.get("consent_id") == consent_id]
```

## Solution 6: Consent Audit Logger

```python
import time
from typing import List


class ConsentAuditLogger:
    """
    Records all consent check decisions for compliance auditing.
    """

    def __init__(self, max_records: int = 50000):
        self._max = max_records
        self._records: List[dict] = []

    def record_check(self, result: ConsentCheckResult) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "user_id": result.user_id,
            "purpose": result.processing_purpose,
            "allowed": result.allowed,
            "consent_id": result.consent_id,
            "denial_reason": result.denial_reason,
        })

    def record_grant(self, record: ConsentRecord) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "event": "consent_granted",
            "user_id": record.user_id,
            "purpose": record.processing_purpose,
            "consent_id": record.consent_id,
            "basis": record.basis.value,
            "version": record.version,
        })

    def record_withdrawal(self, user_id: str, purpose: str, count: int) -> None:
        self._records.append({
            "ts": time.time(),
            "event": "consent_withdrawn",
            "user_id": user_id,
            "purpose": purpose,
            "withdrawn_count": count,
        })

    def summary(self, window_seconds: float = 86400.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        checks = [r for r in recent if "allowed" in r]
        denials = [r for r in checks if not r["allowed"]]
        return {
            "window_seconds": window_seconds,
            "consent_checks": len(checks),
            "denied": len(denials),
            "denial_rate": round(len(denials) / max(len(checks), 1), 4),
            "grants": sum(1 for r in recent if r.get("event") == "consent_granted"),
            "withdrawals": sum(1 for r in recent if r.get("event") == "consent_withdrawn"),
        }
```

## Comparison

| Approach | Consent Storage | Purpose Registry | Pre-Op Gate | Withdrawal Support | Audit Trail |
|---|---|---|---|---|---|
| ConsentStore | Yes | No | No | Yes | No |
| ProcessingPurposeRegistry | No | Yes | No | No | No |
| ConsentGate | Via store | Via registry | Yes | No | Via logger |
| ProcessingActivityLogger | No | No | No | No | Yes (Art.30) |
| ConsentAuditLogger | No | No | No | No | Yes (decisions) |

**Best for production**: Persist consent records in a durable database with a UUID primary key — never store only the current consent status, as regulatory audits require the full history including every grant, modification, and withdrawal. When `ConsentGate.enforce()` raises `ConsentRequiredError`, route the user to a consent collection flow rather than returning a generic error — the agent should explain what processing the task requires and why. Implement `ConsentStore.withdraw_all()` as the handler for right-to-erasure requests: calling it stops all ongoing processing and provides the deletion basis required to purge the user's data from downstream systems. Run `ConsentAuditLogger.summary()` daily and alert if `denial_rate` spikes — a sudden increase means users are attempting operations without consent, which may indicate a UI bug that skipped the consent flow.
