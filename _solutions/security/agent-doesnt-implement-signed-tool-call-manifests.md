---
title: "Agent Doesn't Implement Signed Tool Call Manifests"
description: "Agents that log tool calls without cryptographic signatures cannot prove that a log record was not altered after the fact — a security-relevant tool call (file write, API mutation, database update) could be modified or deleted from logs without detection. Implement signed tool call manifests that attach an HMAC signature to each tool call record at the time of execution, enabling tamper detection during audit."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-signed-tool-call-manifests
tags: [signed-manifests, hmac, tool-call-integrity, tamper-detection, audit-log, non-repudiation]
symptoms:
  - "Tool call logs can be edited or deleted without leaving any trace of tampering"
  - "Security audit cannot verify that a logged tool call reflects what actually executed"
  - "No cryptographic binding between a tool call record and its execution context"
  - "Compliance audit requires non-repudiation but log records have no integrity proof"
  - "Incident investigation cannot distinguish authentic logs from tampered ones"
---

## Why This Happens

Log records are files or database rows — mutable by anyone with write access to the log store. Without a signature computed at execution time using a secret key, a log record can be silently altered: the arguments changed, the outcome flipped, or the record deleted. HMAC-SHA256 signatures computed at tool call time and stored with the record allow a verifier to detect any post-execution modification. The signature binds the tool name, arguments, outcome, timestamp, and session context into a single unforgeable integrity token.

## Solution 1: Tool Call Manifest

```python
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ToolCallManifest:
    manifest_id: str
    tool_name: str
    arguments: Dict[str, Any]
    outcome: str              # "success" | "error" | "timeout"
    result_hash: str          # SHA-256 of serialized result (not stored in plain)
    session_id: str
    agent_id: str
    executed_at: float
    latency_ms: float
    error: Optional[str] = None
    signature: str = ""       # HMAC-SHA256 computed over canonical fields
    metadata: Dict[str, Any] = field(default_factory=dict)

    def canonical_bytes(self) -> bytes:
        """Deterministic serialization of integrity-relevant fields."""
        doc = {
            "manifest_id": self.manifest_id,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "outcome": self.outcome,
            "result_hash": self.result_hash,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "executed_at": self.executed_at,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }
        return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
```

## Solution 2: Manifest Signer

```python
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Dict, Optional


class ToolCallManifestSigner:
    """
    Signs tool call manifests with HMAC-SHA256 using a shared secret.
    The signature covers all integrity-relevant fields, preventing
    post-execution modification of arguments, outcome, or timestamps.
    """

    def __init__(self, signing_key: bytes):
        self._key = signing_key

    def sign(self, manifest: ToolCallManifest) -> ToolCallManifest:
        payload = manifest.canonical_bytes()
        sig = hmac.new(self._key, payload, hashlib.sha256).hexdigest()
        manifest.signature = sig
        return manifest

    def verify(self, manifest: ToolCallManifest) -> bool:
        if not manifest.signature:
            return False
        payload = manifest.canonical_bytes()
        expected = hmac.new(self._key, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, manifest.signature)

    @staticmethod
    def hash_result(result: Any) -> str:
        try:
            serialized = json.dumps(result, sort_keys=True, default=str).encode()
        except Exception:
            serialized = str(result).encode()
        return hashlib.sha256(serialized).hexdigest()[:16]

    def create_manifest(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        result: Any,
        outcome: str,
        session_id: str,
        agent_id: str,
        executed_at: float,
        latency_ms: float,
        error: Optional[str] = None,
    ) -> ToolCallManifest:
        manifest = ToolCallManifest(
            manifest_id=secrets.token_hex(12),
            tool_name=tool_name,
            arguments=arguments,
            outcome=outcome,
            result_hash=self.hash_result(result),
            session_id=session_id,
            agent_id=agent_id,
            executed_at=executed_at,
            latency_ms=latency_ms,
            error=error,
        )
        return self.sign(manifest)
```

## Solution 3: Manifest Store

```python
import json
import time
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional


class ToolCallManifestStore:
    """
    Persists signed manifests to JSONL. Each record is append-only.
    Supports loading all manifests for batch verification.
    """

    def __init__(self, path: str):
        self._path = Path(path)
        self._lock = Lock()

    def append(self, manifest: ToolCallManifest) -> None:
        with self._lock:
            with self._path.open("a") as f:
                record = {
                    "manifest_id": manifest.manifest_id,
                    "tool_name": manifest.tool_name,
                    "arguments": manifest.arguments,
                    "outcome": manifest.outcome,
                    "result_hash": manifest.result_hash,
                    "session_id": manifest.session_id,
                    "agent_id": manifest.agent_id,
                    "executed_at": manifest.executed_at,
                    "latency_ms": manifest.latency_ms,
                    "error": manifest.error,
                    "signature": manifest.signature,
                    "metadata": manifest.metadata,
                }
                f.write(json.dumps(record) + "\n")

    def load_all(self) -> List[ToolCallManifest]:
        manifests = []
        if not self._path.exists():
            return manifests
        with self._lock:
            for line in self._path.read_text().splitlines():
                try:
                    d = json.loads(line)
                    manifests.append(ToolCallManifest(**{
                        k: v for k, v in d.items()
                    }))
                except Exception:
                    continue
        return manifests

    def load_for_session(self, session_id: str) -> List[ToolCallManifest]:
        return [m for m in self.load_all() if m.session_id == session_id]
```

## Solution 4: Manifest Verifier

```python
from typing import List


class ToolCallManifestVerifier:
    """
    Batch-verifies a set of manifests and reports tampered records.
    """

    def __init__(self, signer: ToolCallManifestSigner):
        self._signer = signer

    def verify_all(self, manifests: List[ToolCallManifest]) -> dict:
        valid = []
        tampered = []
        unsigned = []

        for m in manifests:
            if not m.signature:
                unsigned.append(m.manifest_id)
            elif self._signer.verify(m):
                valid.append(m.manifest_id)
            else:
                tampered.append({
                    "manifest_id": m.manifest_id,
                    "tool_name": m.tool_name,
                    "executed_at": m.executed_at,
                    "session_id": m.session_id,
                })

        return {
            "total": len(manifests),
            "valid": len(valid),
            "tampered": len(tampered),
            "unsigned": len(unsigned),
            "tampered_records": tampered,
            "integrity_ok": len(tampered) == 0 and len(unsigned) == 0,
        }
```

## Solution 5: Signing Tool Call Executor

```python
import time
from typing import Any, Callable, Dict, Optional


class SigningToolCallExecutor:
    """
    Wraps tool execution: records timing, signs the manifest,
    and persists it — all before returning the result to the caller.
    """

    def __init__(
        self,
        signer: ToolCallManifestSigner,
        store: ToolCallManifestStore,
        agent_id: str,
    ):
        self._signer = signer
        self._store = store
        self._agent_id = agent_id

    async def execute(
        self,
        tool_name: str,
        fn: Callable,
        session_id: str,
        arguments: Dict[str, Any],
    ) -> dict:
        start = time.time()
        result = None
        outcome = "success"
        error = None
        try:
            result = await fn(**arguments)
        except Exception as exc:
            outcome = "error"
            error = str(exc)
        finally:
            latency_ms = round((time.time() - start) * 1000, 2)
            manifest = self._signer.create_manifest(
                tool_name=tool_name,
                arguments=arguments,
                result=result,
                outcome=outcome,
                session_id=session_id,
                agent_id=self._agent_id,
                executed_at=start,
                latency_ms=latency_ms,
                error=error,
            )
            self._store.append(manifest)

        if outcome == "error":
            raise RuntimeError(error)

        return {"result": result, "manifest_id": manifest.manifest_id}
```

## Solution 6: Manifest Integrity Dashboard

```python
import time


class ManifestIntegrityDashboard:
    """
    Runs a batch verification against all stored manifests and
    reports integrity status for compliance and audit purposes.
    """

    def __init__(
        self,
        store: ToolCallManifestStore,
        verifier: ToolCallManifestVerifier,
    ):
        self._store = store
        self._verifier = verifier

    def render(self) -> dict:
        manifests = self._store.load_all()
        verification = self._verifier.verify_all(manifests)
        return {
            "generated_at": time.time(),
            "total_manifests": len(manifests),
            "verification": verification,
            "status": "ok" if verification["integrity_ok"] else "INTEGRITY_VIOLATION",
        }
```

## Comparison

| Approach | HMAC Signature | Canonical Serialization | Batch Verification | Append-Only Store | Audit Dashboard |
|---|---|---|---|---|---|
| ToolCallManifestSigner | Yes (HMAC-SHA256) | Yes (sort_keys) | No | No | No |
| ToolCallManifestStore | No | No | No | Yes (JSONL) | No |
| ToolCallManifestVerifier | Via signer | Via manifest | Yes | No | No |
| SigningToolCallExecutor | Via signer | Via signer | No | Via store | No |
| ManifestIntegrityDashboard | No | No | Via verifier | Via store | Yes |

**Best for production**: Rotate the signing key on a schedule and include a key version identifier in the manifest metadata — this allows verifiers to select the correct key for older manifests. Store manifests in a write-once append-only log (S3 with object lock, or an immutable audit table) rather than a mutable file — HMAC alone does not prevent deletion of entire records, only modification of existing ones. Run `ManifestIntegrityDashboard.render()` as a scheduled job after every deployment and after any security incident — a non-zero `tampered` count requires immediate investigation. Never log the signing key; treat it as a credential with the same rotation and access controls as database passwords.
