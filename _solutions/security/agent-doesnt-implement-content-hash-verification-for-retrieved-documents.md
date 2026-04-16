---
title: "Agent Doesn't Implement Content Hash Verification for Retrieved Documents"
description: "Agents that retrieve documents from external sources and inject them into context without integrity verification are vulnerable to document substitution attacks: a compromised retrieval backend or a cache poisoning attack can serve a maliciously modified document that passes the agent's relevance filter but contains injected instructions. Implement content hash verification that checks retrieved documents against pre-computed or authority-signed hashes before they enter the context window."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-content-hash-verification-for-retrieved-documents
tags: [content-integrity, hash-verification, document-retrieval, rag-security, cache-poisoning, document-substitution]
symptoms:
  - "Retrieved documents injected into context without any integrity check"
  - "Cache layer between retrieval backend and agent could serve modified documents"
  - "No way to detect if a document changed between when it was indexed and when it was retrieved"
  - "Document provenance not tracked — agent cannot distinguish authentic from tampered content"
  - "Retrieval backend compromise goes undetected because no expected hash exists"
---

## Why This Happens

RAG pipelines index documents at ingestion time and retrieve them at query time. Between these two events — which can be hours or days apart — the document may have been modified: by a legitimate update, by cache poisoning, or by a compromised retrieval backend. The agent has no way to detect this without a stored reference hash from the time of indexing. Content hash verification requires computing and storing a hash at ingestion, retrieving it alongside the document at query time, and rejecting documents whose current content does not match the stored hash. For high-trust use cases, this is combined with an HMAC or signature that only the indexing service can produce.

## Solution 1: Document Integrity Record

```python
import hashlib
import hmac
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DocumentIntegrityRecord:
    doc_id: str
    content_hash: str            # SHA-256 of canonical content
    algorithm: str = "sha256"
    indexed_at: float = field(default_factory=time.time)
    source_url: str = ""
    hmac_signature: str = ""     # HMAC-SHA256 with server secret, if used
    version: int = 1

    @classmethod
    def compute(cls, doc_id: str, content: str, source_url: str = "") -> "DocumentIntegrityRecord":
        canonical = content.encode("utf-8")
        content_hash = hashlib.sha256(canonical).hexdigest()
        return cls(
            doc_id=doc_id,
            content_hash=content_hash,
            source_url=source_url,
        )

    def sign(self, secret_key: bytes) -> None:
        payload = f"{self.doc_id}:{self.content_hash}:{self.indexed_at}".encode()
        self.hmac_signature = hmac.new(secret_key, payload, hashlib.sha256).hexdigest()

    def verify_signature(self, secret_key: bytes) -> bool:
        if not self.hmac_signature:
            return False
        payload = f"{self.doc_id}:{self.content_hash}:{self.indexed_at}".encode()
        expected = hmac.new(secret_key, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(self.hmac_signature, expected)
```

## Solution 2: Integrity Record Store

```python
import json
import time
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional


class IntegrityRecordStore:
    """
    Persists document integrity records indexed at ingestion time.
    Retrieved at query time to verify document content has not changed.
    """

    def __init__(self, path: str = "/tmp/doc_integrity_store.json"):
        self._path = Path(path)
        self._lock = Lock()
        self._records: Dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._records = json.loads(self._path.read_text())
            except (json.JSONDecodeError, OSError):
                self._records = {}

    def _save(self) -> None:
        self._path.write_text(json.dumps(self._records, indent=2))

    def store(self, record: DocumentIntegrityRecord) -> None:
        with self._lock:
            self._records[record.doc_id] = {
                "doc_id": record.doc_id,
                "content_hash": record.content_hash,
                "algorithm": record.algorithm,
                "indexed_at": record.indexed_at,
                "source_url": record.source_url,
                "hmac_signature": record.hmac_signature,
                "version": record.version,
            }
            self._save()

    def get(self, doc_id: str) -> Optional[DocumentIntegrityRecord]:
        with self._lock:
            data = self._records.get(doc_id)
            if not data:
                return None
            return DocumentIntegrityRecord(**data)

    def remove(self, doc_id: str) -> bool:
        with self._lock:
            if doc_id in self._records:
                del self._records[doc_id]
                self._save()
                return True
            return False

    def stats(self) -> dict:
        with self._lock:
            return {"stored_records": len(self._records)}
```

## Solution 3: Content Hash Verifier

```python
import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class VerificationStatus(str, Enum):
    VERIFIED = "verified"
    HASH_MISMATCH = "hash_mismatch"
    NO_RECORD = "no_record"
    SIGNATURE_INVALID = "signature_invalid"
    ALGORITHM_UNSUPPORTED = "algorithm_unsupported"


@dataclass
class VerificationResult:
    doc_id: str
    status: VerificationStatus
    expected_hash: Optional[str]
    actual_hash: str
    trusted: bool
    detail: str = ""


class ContentHashVerifier:
    """
    Verifies a retrieved document's content against its stored integrity record.
    Optionally verifies the HMAC signature on the record itself.
    """

    def __init__(
        self,
        store: IntegrityRecordStore,
        signing_key: Optional[bytes] = None,
        require_signature: bool = False,
    ):
        self._store = store
        self._key = signing_key
        self._require_sig = require_signature

    def verify(self, doc_id: str, content: str) -> VerificationResult:
        actual_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        record = self._store.get(doc_id)

        if record is None:
            return VerificationResult(
                doc_id=doc_id,
                status=VerificationStatus.NO_RECORD,
                expected_hash=None,
                actual_hash=actual_hash,
                trusted=False,
                detail="No integrity record found — document was not indexed through verified pipeline",
            )

        if record.algorithm != "sha256":
            return VerificationResult(
                doc_id=doc_id,
                status=VerificationStatus.ALGORITHM_UNSUPPORTED,
                expected_hash=record.content_hash,
                actual_hash=actual_hash,
                trusted=False,
                detail=f"Unsupported hash algorithm: {record.algorithm}",
            )

        if self._require_sig and self._key:
            if not record.verify_signature(self._key):
                return VerificationResult(
                    doc_id=doc_id,
                    status=VerificationStatus.SIGNATURE_INVALID,
                    expected_hash=record.content_hash,
                    actual_hash=actual_hash,
                    trusted=False,
                    detail="HMAC signature on integrity record is invalid",
                )

        if actual_hash != record.content_hash:
            return VerificationResult(
                doc_id=doc_id,
                status=VerificationStatus.HASH_MISMATCH,
                expected_hash=record.content_hash,
                actual_hash=actual_hash,
                trusted=False,
                detail="Document content has changed since indexing",
            )

        return VerificationResult(
            doc_id=doc_id,
            status=VerificationStatus.VERIFIED,
            expected_hash=record.content_hash,
            actual_hash=actual_hash,
            trusted=True,
        )
```

## Solution 4: Integrity-Gated Retrieval Filter

```python
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class IntegrityPolicy(str, Enum):
    STRICT = "strict"         # reject unverified documents
    WARN = "warn"             # allow but flag unverified documents
    AUDIT_ONLY = "audit_only" # pass through, only log


@dataclass
class RetrievedDocument:
    doc_id: str
    content: str
    metadata: Dict[str, Any]
    integrity_status: Optional[VerificationStatus] = None
    trusted: bool = False


class IntegrityGatedRetrievalFilter:
    """
    Filters a batch of retrieved documents through content hash verification.
    Under STRICT policy, untrusted documents are dropped before context injection.
    """

    def __init__(
        self,
        verifier: ContentHashVerifier,
        policy: IntegrityPolicy = IntegrityPolicy.STRICT,
        audit_logger: Optional["IntegrityViolationLogger"] = None,
    ):
        self._verifier = verifier
        self._policy = policy
        self._audit = audit_logger

    def filter(
        self, documents: List[RetrievedDocument]
    ) -> Tuple[List[RetrievedDocument], List[VerificationResult]]:
        passed = []
        violations = []

        for doc in documents:
            result = self._verifier.verify(doc.doc_id, doc.content)
            doc.integrity_status = result.status
            doc.trusted = result.trusted

            if self._audit and not result.trusted:
                self._audit.record(result)

            if result.trusted:
                passed.append(doc)
            elif self._policy == IntegrityPolicy.STRICT:
                violations.append(result)
            elif self._policy == IntegrityPolicy.WARN:
                passed.append(doc)   # include but mark as untrusted
                violations.append(result)
            else:
                passed.append(doc)   # audit only

        return passed, violations
```

## Solution 5: Integrity Violation Logger

```python
import time
from typing import List


class IntegrityViolationLogger:
    """
    Records every integrity verification failure for security audit.
    Surfaces tampered documents, cache poisoning attempts, and unindexed content.
    """

    def __init__(self, max_records: int = 10000):
        self._max = max_records
        self._records: List[dict] = []

    def record(self, result: VerificationResult) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "doc_id": result.doc_id,
            "status": result.status.value,
            "expected_hash": result.expected_hash,
            "actual_hash": result.actual_hash[:16] + "…" if result.actual_hash else None,
            "detail": result.detail,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        by_status: dict = {}
        for r in recent:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        return {
            "window_seconds": window_seconds,
            "total_violations": len(recent),
            "by_status": by_status,
            "hash_mismatches": by_status.get("hash_mismatch", 0),
        }
```

## Solution 6: Content Integrity Dashboard

```python
import time


class ContentIntegrityDashboard:
    """
    Combines integrity record store stats, violation log summaries,
    and policy configuration into a single security view.
    """

    def __init__(
        self,
        store: IntegrityRecordStore,
        verifier: ContentHashVerifier,
        logger: IntegrityViolationLogger,
        policy: IntegrityPolicy,
    ):
        self._store = store
        self._verifier = verifier
        self._logger = logger
        self._policy = policy

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "policy": self._policy.value,
            "require_signature": self._verifier._require_sig,
            "store_stats": self._store.stats(),
            "violations_1h": self._logger.summary(3600.0),
            "violations_24h": self._logger.summary(86400.0),
        }
```

## Comparison

| Approach | Hash Storage | Content Verification | Signature Verification | Context Filtering | Audit |
|---|---|---|---|---|---|
| IntegrityRecordStore | Yes | No | No | No | No |
| ContentHashVerifier | Via store | Yes (SHA-256) | Yes (HMAC) | No | No |
| IntegrityGatedRetrievalFilter | No | Via verifier | Via verifier | Yes | Via logger |
| IntegrityViolationLogger | No | No | No | No | Yes |
| ContentIntegrityDashboard | No | No | No | No | Yes (aggregate) |

**Best for production**: Store integrity records in a separate, read-only data store from the document content — if the retrieval backend is compromised and serves modified documents, a separately controlled integrity store cannot be poisoned simultaneously. Enable HMAC signatures (`require_signature=True`) for documents from high-trust sources (internal knowledge bases, compliance documents) and use `IntegrityPolicy.WARN` for documents from external sources where content legitimately changes — this avoids blocking on every valid update while still surfacing tampering. Monitor `hash_mismatches` in `IntegrityViolationLogger`: a sudden spike of hash mismatches from a specific document source indicates either bulk content updates (legitimate) or a cache poisoning attack (investigate immediately). Refresh integrity records as part of the re-indexing pipeline so legitimate document updates do not trigger false positive violation alerts.
