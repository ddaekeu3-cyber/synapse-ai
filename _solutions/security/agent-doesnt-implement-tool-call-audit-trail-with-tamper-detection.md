---
title: "Agent Doesn't Implement Tool Call Audit Trail with Tamper Detection"
description: "Agents that log tool calls without integrity protection produce audit trails that can be silently modified — log entries deleted, results altered, or timestamps changed — undermining compliance evidence and incident forensics. Implement a tamper-evident audit trail that chains each tool call record to the previous via a cryptographic hash, making any modification detectable during verification."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-tool-call-audit-trail-with-tamper-detection
tags: [audit-trail, tamper-detection, cryptographic-hash, log-integrity, compliance, forensics]
symptoms:
  - "Audit logs can be silently edited — no detection mechanism exists"
  - "Compliance audit requires proof that tool calls were not altered after the fact"
  - "Log entries can be deleted without breaking any integrity check"
  - "No chain of custody for tool call records used in incident investigations"
  - "Audit log timestamps are mutable — an attacker can backdate or forward-date entries"
---

## Why This Happens

Standard logging writes records to a file or database without any link between successive entries. An attacker who gains write access to the log store can modify or delete records without leaving any trace. Tamper-evident audit trails borrow from blockchain and certificate transparency log designs: each record includes a hash of the previous record. Modifying any record invalidates all subsequent hashes, making tampering detectable by anyone who re-hashes the chain. The implementation does not require distributed consensus — a simple append-only JSON-lines file with HMAC-chained hashes is sufficient for single-instance deployments.

## Solution 1: Audit Entry

```python
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class AuditEntry:
    entry_id: str
    tool_name: str
    args_hash: str                    # SHA-256 of serialized args (not raw args — privacy)
    result_hash: Optional[str]        # SHA-256 of serialized result
    session_id: str
    success: bool
    latency_ms: Optional[float]
    recorded_at: float = field(default_factory=time.time)
    error_type: Optional[str] = None
    previous_hash: str = ""           # hash of previous entry — forms the chain
    entry_hash: str = ""              # hash of this entry's content

    def compute_content_hash(self, hmac_secret: str) -> str:
        import hmac as hmac_module
        content = json.dumps({
            "entry_id": self.entry_id,
            "tool_name": self.tool_name,
            "args_hash": self.args_hash,
            "result_hash": self.result_hash,
            "session_id": self.session_id,
            "success": self.success,
            "latency_ms": self.latency_ms,
            "recorded_at": self.recorded_at,
            "error_type": self.error_type,
            "previous_hash": self.previous_hash,
        }, sort_keys=True)
        return hmac_module.new(
            hmac_secret.encode(),
            content.encode(),
            hashlib.sha256,
        ).hexdigest()
```

## Solution 2: Audit Trail Writer

```python
import hashlib
import json
import time
import uuid
from pathlib import Path
from threading import Lock
from typing import Any, Optional


class TamperEvidentAuditWriter:
    """
    Appends audit entries to a JSON-lines file with HMAC-chained hashes.
    Each entry's hash covers its content plus the previous entry's hash,
    making any modification detectable.
    """

    def __init__(self, path: str, hmac_secret: str):
        self._path = Path(path)
        self._secret = hmac_secret
        self._lock = Lock()
        self._last_hash = self._recover_last_hash()

    def _recover_last_hash(self) -> str:
        """Read the last entry's hash to resume the chain after restart."""
        if not self._path.exists():
            return "GENESIS"
        try:
            lines = self._path.read_text().strip().splitlines()
            for line in reversed(lines):
                try:
                    entry = json.loads(line)
                    return entry.get("entry_hash", "GENESIS")
                except json.JSONDecodeError:
                    continue
        except OSError:
            pass
        return "GENESIS"

    def write(
        self,
        tool_name: str,
        args: Any,
        result: Any,
        session_id: str,
        success: bool,
        latency_ms: Optional[float] = None,
        error_type: Optional[str] = None,
    ) -> AuditEntry:
        args_hash = hashlib.sha256(
            json.dumps(args, sort_keys=True, default=str).encode()
        ).hexdigest()
        result_hash = (
            hashlib.sha256(
                json.dumps(result, sort_keys=True, default=str).encode()
            ).hexdigest()
            if result is not None else None
        )

        with self._lock:
            entry = AuditEntry(
                entry_id=str(uuid.uuid4()),
                tool_name=tool_name,
                args_hash=args_hash,
                result_hash=result_hash,
                session_id=session_id,
                success=success,
                latency_ms=latency_ms,
                error_type=error_type,
                previous_hash=self._last_hash,
            )
            entry.entry_hash = entry.compute_content_hash(self._secret)
            self._last_hash = entry.entry_hash

            record = {
                "entry_id": entry.entry_id,
                "tool_name": entry.tool_name,
                "args_hash": entry.args_hash,
                "result_hash": entry.result_hash,
                "session_id": entry.session_id,
                "success": entry.success,
                "latency_ms": entry.latency_ms,
                "recorded_at": entry.recorded_at,
                "error_type": entry.error_type,
                "previous_hash": entry.previous_hash,
                "entry_hash": entry.entry_hash,
            }
            with self._path.open("a") as f:
                f.write(json.dumps(record) + "\n")

        return entry
```

## Solution 3: Audit Trail Verifier

```python
import hashlib
import json
from pathlib import Path
from typing import List


class AuditTrailVerifier:
    """
    Re-reads the audit trail and verifies the hash chain.
    Reports any entries where the hash does not match the recomputed value
    or the previous_hash does not match the prior entry's hash.
    """

    def __init__(self, hmac_secret: str):
        self._secret = hmac_secret

    def verify(self, path: str) -> dict:
        p = Path(path)
        if not p.exists():
            return {"status": "file_not_found", "entries": 0, "violations": []}

        lines = p.read_text().strip().splitlines()
        violations = []
        previous_hash = "GENESIS"

        for i, line in enumerate(lines):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                violations.append({"line": i + 1, "type": "parse_error", "detail": "invalid JSON"})
                continue

            stored_hash = record.get("entry_hash", "")
            stored_prev = record.get("previous_hash", "")

            # Check chain linkage
            if stored_prev != previous_hash:
                violations.append({
                    "line": i + 1,
                    "entry_id": record.get("entry_id"),
                    "type": "chain_break",
                    "detail": f"previous_hash mismatch: expected {previous_hash[:16]}... got {stored_prev[:16]}...",
                })

            # Recompute HMAC
            entry = AuditEntry(
                entry_id=record["entry_id"],
                tool_name=record["tool_name"],
                args_hash=record["args_hash"],
                result_hash=record.get("result_hash"),
                session_id=record["session_id"],
                success=record["success"],
                latency_ms=record.get("latency_ms"),
                recorded_at=record["recorded_at"],
                error_type=record.get("error_type"),
                previous_hash=stored_prev,
            )
            expected_hash = entry.compute_content_hash(self._secret)

            if expected_hash != stored_hash:
                violations.append({
                    "line": i + 1,
                    "entry_id": record.get("entry_id"),
                    "type": "hash_mismatch",
                    "detail": "entry content was modified",
                })

            previous_hash = stored_hash

        return {
            "status": "clean" if not violations else "tampered",
            "entries": len(lines),
            "violations": violations,
            "violation_count": len(violations),
        }
```

## Solution 4: Audit-Wrapped Tool Dispatcher

```python
import time
from typing import Any, Callable, Dict, Optional


class AuditWrappedToolDispatcher:
    """
    Wraps every tool call with audit trail writing.
    Records args hash, result hash, latency, and outcome for each call.
    """

    def __init__(self, writer: TamperEvidentAuditWriter):
        self._writer = writer

    async def dispatch(
        self,
        tool_name: str,
        args: Dict[str, Any],
        handler: Callable,
        session_id: str = "",
    ) -> Any:
        start = time.time()
        result = None
        error_type = None
        success = False
        try:
            result = await handler(**args)
            success = True
            return result
        except Exception as exc:
            error_type = type(exc).__name__
            raise
        finally:
            latency_ms = round((time.time() - start) * 1000, 2)
            self._writer.write(
                tool_name=tool_name,
                args=args,
                result=result,
                session_id=session_id,
                success=success,
                latency_ms=latency_ms,
                error_type=error_type,
            )
```

## Solution 5: Audit Trail Archiver

```python
import shutil
import time
from pathlib import Path


class AuditTrailArchiver:
    """
    Rotates the active audit log into a timestamped archive file
    when it exceeds a size limit. Archives are retained for the
    configured retention period.
    """

    def __init__(
        self,
        active_path: str,
        archive_dir: str,
        max_size_bytes: int = 50 * 1024 * 1024,   # 50 MB
        retention_days: int = 90,
    ):
        self._active = Path(active_path)
        self._archive_dir = Path(archive_dir)
        self._archive_dir.mkdir(parents=True, exist_ok=True)
        self._max_size = max_size_bytes
        self._retention = retention_days * 86400

    def rotate_if_needed(self) -> bool:
        if not self._active.exists() or self._active.stat().st_size < self._max_size:
            return False
        ts = int(time.time())
        archive_path = self._archive_dir / f"audit_{ts}.jsonl"
        shutil.move(str(self._active), str(archive_path))
        return True

    def cleanup_old_archives(self) -> int:
        cutoff = time.time() - self._retention
        removed = 0
        for f in self._archive_dir.glob("audit_*.jsonl"):
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        return removed
```

## Solution 6: Audit Trail Dashboard

```python
import time


class AuditTrailDashboard:
    """
    Combines chain verification results and archiver stats into
    a compliance and integrity operational report.
    """

    def __init__(
        self,
        verifier: AuditTrailVerifier,
        archiver: AuditTrailArchiver,
        active_path: str,
    ):
        self._verifier = verifier
        self._archiver = archiver
        self._active_path = active_path

    def render(self) -> dict:
        verification = self._verifier.verify(self._active_path)
        return {
            "generated_at": time.time(),
            "integrity": verification,
            "active_log_path": self._active_path,
            "health": {
                "chain_intact": verification["status"] == "clean",
                "violation_count": verification["violation_count"],
            },
        }
```

## Comparison

| Approach | Hash Chaining | HMAC Signing | Chain Verification | Log Rotation | Tamper Detection |
|---|---|---|---|---|---|
| TamperEvidentAuditWriter | Yes | Yes (HMAC-SHA256) | No | No | No |
| AuditTrailVerifier | No | No | Yes (full re-hash) | No | Yes |
| AuditWrappedToolDispatcher | Via writer | Via writer | No | No | No |
| AuditTrailArchiver | No | No | No | Yes | No |
| AuditTrailDashboard | No | No | Via verifier | No | No |

**Best for production**: Store the HMAC secret in a secrets manager and rotate it periodically — re-signing archived logs with the new key before rotation. Run `AuditTrailVerifier.verify()` on a schedule (daily or after each deployment) and alert if `violation_count > 0` — this is a security incident signal. Write the audit log to a write-once storage backend (AWS S3 with Object Lock, or WORM-enabled storage) as an additional tamper-prevention layer beyond cryptographic chaining — hash chaining detects tampering but does not prevent it without storage-level immutability. Set `retention_days=90` for GDPR compliance minimum; extend to 365 for PCI-DSS environments.
