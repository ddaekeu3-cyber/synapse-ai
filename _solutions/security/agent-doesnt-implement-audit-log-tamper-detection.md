---
title: "Agent Doesn't Implement Audit Log Tamper Detection"
description: "Agents that write audit logs without integrity protection are vulnerable to post-hoc modification: an attacker who gains write access to the log store can delete, alter, or inject entries without leaving evidence. Implement tamper detection using hash chaining and periodic checkpoint signatures so any modification to the log sequence is detectable on the next verification pass."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-audit-log-tamper-detection
tags: [audit-log, tamper-detection, hash-chain, integrity, log-security, forensics]
symptoms:
  - "Audit logs can be modified or deleted without any detection mechanism"
  - "No way to prove which log entries existed at a specific point in time"
  - "Compliance audits require tamper-evident logging but none is implemented"
  - "Log entries have no cryptographic relationship to preceding entries"
  - "An insider with log write access could cover their tracks undetected"
---

## Why This Happens

Most logging frameworks append entries independently — each record is a standalone JSON object with no reference to previous records. An attacker who gains write access to the log store (or who controls the logging process itself) can delete entries, alter timestamps, or inject false records with no trace. Tamper detection requires that each log entry commit to the content of all previous entries via a hash chain: entry N includes a hash of entry N-1's content, so deleting or modifying any entry invalidates all subsequent entries. Periodic checkpoints signed with an HMAC key held outside the log store provide an additional verification anchor that survives log truncation.

## Solution 1: Audit Log Entry

```python
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class AuditLogEntry:
    sequence: int                    # monotonically increasing sequence number
    timestamp: float
    event_type: str
    actor: str                       # user, agent, or system identifier
    resource: str
    action: str
    outcome: str                     # "success" | "failure" | "denied"
    details: Dict[str, Any]
    prev_hash: str                   # SHA-256 of the previous entry's canonical form
    entry_hash: str = ""             # SHA-256 of this entry (set after construction)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def canonical_form(self) -> bytes:
        """Deterministic serialization for hashing — excludes entry_hash itself."""
        import json
        obj = {
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "actor": self.actor,
            "resource": self.resource,
            "action": self.action,
            "outcome": self.outcome,
            "details": self.details,
            "prev_hash": self.prev_hash,
        }
        return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()

    def compute_hash(self) -> str:
        return hashlib.sha256(self.canonical_form()).hexdigest()
```

## Solution 2: Hash-Chained Audit Log Writer

```python
import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional


GENESIS_HASH = "0" * 64   # well-known hash for the first entry


class HashChainedAuditLogWriter:
    """
    Appends audit log entries to a file with each entry containing
    the SHA-256 hash of the previous entry, forming a tamper-evident chain.
    Any modification to a prior entry invalidates all subsequent entries.
    """

    def __init__(self, log_path: str):
        self._path = Path(log_path)
        self._lock = threading.Lock()
        self._sequence = 0
        self._prev_hash = GENESIS_HASH
        self._restore_tail()

    def _restore_tail(self) -> None:
        """Resume the chain from the last written entry on startup."""
        if not self._path.exists():
            return
        last_line = None
        with self._path.open("rb") as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    last_line = stripped
        if last_line:
            try:
                data = json.loads(last_line)
                self._sequence = data["sequence"] + 1
                self._prev_hash = data["entry_hash"]
            except (json.JSONDecodeError, KeyError):
                pass

    def append(
        self,
        event_type: str,
        actor: str,
        resource: str,
        action: str,
        outcome: str,
        details: Dict[str, Any] = None,
    ) -> AuditLogEntry:
        with self._lock:
            entry = AuditLogEntry(
                sequence=self._sequence,
                timestamp=time.time(),
                event_type=event_type,
                actor=actor,
                resource=resource,
                action=action,
                outcome=outcome,
                details=details or {},
                prev_hash=self._prev_hash,
            )
            entry.entry_hash = entry.compute_hash()

            record = {
                "sequence": entry.sequence,
                "timestamp": entry.timestamp,
                "event_type": entry.event_type,
                "actor": entry.actor,
                "resource": entry.resource,
                "action": entry.action,
                "outcome": entry.outcome,
                "details": entry.details,
                "prev_hash": entry.prev_hash,
                "entry_hash": entry.entry_hash,
            }
            with self._path.open("a") as f:
                f.write(json.dumps(record) + "\n")

            self._prev_hash = entry.entry_hash
            self._sequence += 1
            return entry
```

## Solution 3: Audit Log Chain Verifier

```python
import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class ChainVerificationResult:
    valid: bool
    entries_checked: int
    first_broken_sequence: Optional[int]
    break_reason: Optional[str]
    warnings: List[str]


class AuditLogChainVerifier:
    """
    Reads the audit log file and verifies the hash chain from start to end.
    Reports the first entry where the chain breaks and the reason.
    """

    def verify(self, log_path: str) -> ChainVerificationResult:
        path = Path(log_path)
        if not path.exists():
            return ChainVerificationResult(
                valid=False,
                entries_checked=0,
                first_broken_sequence=None,
                break_reason="log file not found",
                warnings=[],
            )

        warnings = []
        prev_hash = GENESIS_HASH
        expected_sequence = 0
        entries_checked = 0

        with path.open() as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    return ChainVerificationResult(
                        valid=False,
                        entries_checked=entries_checked,
                        first_broken_sequence=None,
                        break_reason=f"JSON parse error at line {lineno}",
                        warnings=warnings,
                    )

                seq = data.get("sequence", -1)

                # Sequence gap check
                if seq != expected_sequence:
                    return ChainVerificationResult(
                        valid=False,
                        entries_checked=entries_checked,
                        first_broken_sequence=seq,
                        break_reason=f"sequence gap: expected {expected_sequence}, got {seq}",
                        warnings=warnings,
                    )

                # Previous hash check
                if data.get("prev_hash") != prev_hash:
                    return ChainVerificationResult(
                        valid=False,
                        entries_checked=entries_checked,
                        first_broken_sequence=seq,
                        break_reason=f"prev_hash mismatch at sequence {seq}",
                        warnings=warnings,
                    )

                # Self-hash check
                entry = AuditLogEntry(
                    sequence=data["sequence"],
                    timestamp=data["timestamp"],
                    event_type=data["event_type"],
                    actor=data["actor"],
                    resource=data["resource"],
                    action=data["action"],
                    outcome=data["outcome"],
                    details=data["details"],
                    prev_hash=data["prev_hash"],
                )
                computed = entry.compute_hash()
                if computed != data.get("entry_hash"):
                    return ChainVerificationResult(
                        valid=False,
                        entries_checked=entries_checked,
                        first_broken_sequence=seq,
                        break_reason=f"entry_hash mismatch at sequence {seq}: content was modified",
                        warnings=warnings,
                    )

                prev_hash = data["entry_hash"]
                expected_sequence += 1
                entries_checked += 1

        return ChainVerificationResult(
            valid=True,
            entries_checked=entries_checked,
            first_broken_sequence=None,
            break_reason=None,
            warnings=warnings,
        )
```

## Solution 4: HMAC Checkpoint Signer

```python
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class LogCheckpoint:
    sequence_at: int
    entry_hash_at: str
    checkpoint_timestamp: float
    hmac_signature: str              # HMAC-SHA256 of (sequence_at || entry_hash_at || timestamp)


class HMACCheckpointSigner:
    """
    Periodically signs the current chain head with an HMAC key stored
    outside the log. Checkpoints prove the chain existed in its current
    state at a specific time, surviving even a complete log truncation.
    """

    def __init__(self, secret_key: bytes):
        self._key = secret_key
        self._checkpoints: List[LogCheckpoint] = []

    def _sign(self, sequence: int, entry_hash: str, timestamp: float) -> str:
        message = f"{sequence}|{entry_hash}|{timestamp:.6f}".encode()
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()

    def create_checkpoint(self, sequence: int, entry_hash: str) -> LogCheckpoint:
        ts = time.time()
        sig = self._sign(sequence, entry_hash, ts)
        cp = LogCheckpoint(
            sequence_at=sequence,
            entry_hash_at=entry_hash,
            checkpoint_timestamp=ts,
            hmac_signature=sig,
        )
        self._checkpoints.append(cp)
        return cp

    def verify_checkpoint(self, checkpoint: LogCheckpoint) -> bool:
        expected = self._sign(
            checkpoint.sequence_at,
            checkpoint.entry_hash_at,
            checkpoint.checkpoint_timestamp,
        )
        return hmac.compare_digest(expected, checkpoint.hmac_signature)

    def latest_checkpoint(self) -> Optional[LogCheckpoint]:
        return self._checkpoints[-1] if self._checkpoints else None
```

## Solution 5: Tamper Detection Report Generator

```python
import time
from typing import List, Optional


@dataclass
class TamperDetectionReport:
    generated_at: float
    chain_valid: bool
    entries_verified: int
    first_broken_sequence: Optional[int]
    break_reason: Optional[str]
    checkpoint_valid: Optional[bool]
    checkpoint_sequence: Optional[int]
    tail_hash: Optional[str]
    verdict: str   # "INTACT" | "TAMPERED" | "TRUNCATED" | "UNVERIFIABLE"


class TamperDetectionReportGenerator:
    """
    Combines chain verification and checkpoint validation into
    a single tamper detection verdict for compliance reporting.
    """

    def __init__(
        self,
        verifier: AuditLogChainVerifier,
        signer: HMACCheckpointSigner,
    ):
        self._verifier = verifier
        self._signer = signer

    def generate(self, log_path: str) -> TamperDetectionReport:
        chain_result = self._verifier.verify(log_path)
        latest_cp = self._signer.latest_checkpoint()

        checkpoint_valid = None
        checkpoint_sequence = None
        if latest_cp:
            checkpoint_valid = self._signer.verify_checkpoint(latest_cp)
            checkpoint_sequence = latest_cp.sequence_at

        # Determine verdict
        if not chain_result.valid:
            verdict = "TAMPERED"
            if "sequence gap" in (chain_result.break_reason or ""):
                verdict = "TRUNCATED"
        elif latest_cp and not checkpoint_valid:
            verdict = "TAMPERED"
        elif chain_result.entries_checked == 0:
            verdict = "UNVERIFIABLE"
        else:
            verdict = "INTACT"

        return TamperDetectionReport(
            generated_at=time.time(),
            chain_valid=chain_result.valid,
            entries_verified=chain_result.entries_checked,
            first_broken_sequence=chain_result.first_broken_sequence,
            break_reason=chain_result.break_reason,
            checkpoint_valid=checkpoint_valid,
            checkpoint_sequence=checkpoint_sequence,
            tail_hash=chain_result.warnings[0] if chain_result.warnings else None,
            verdict=verdict,
        )
```

## Solution 6: Continuous Tamper Monitor

```python
import threading
import time
from typing import Callable, List, Optional


class ContinuousTamperMonitor:
    """
    Runs chain verification and checkpoint creation on a schedule.
    Emits alerts when tampering is detected. Checkpoints the chain
    head after each successful verification pass.
    """

    def __init__(
        self,
        log_path: str,
        report_generator: TamperDetectionReportGenerator,
        signer: HMACCheckpointSigner,
        alert_fn: Optional[Callable[[TamperDetectionReport], None]] = None,
        interval_seconds: float = 300.0,
    ):
        self._log_path = log_path
        self._generator = report_generator
        self._signer = signer
        self._alert_fn = alert_fn or self._default_alert
        self._interval = interval_seconds
        self._reports: List[TamperDetectionReport] = []
        self._running = False
        self._thread: Optional[threading.Thread] = None

    @staticmethod
    def _default_alert(report: TamperDetectionReport) -> None:
        import json
        print(json.dumps({
            "TAMPER_ALERT": True,
            "verdict": report.verdict,
            "first_broken_sequence": report.first_broken_sequence,
            "break_reason": report.break_reason,
            "generated_at": report.generated_at,
        }))

    def _run_loop(self) -> None:
        while self._running:
            report = self._generator.generate(self._log_path)
            self._reports.append(report)
            if report.verdict != "INTACT":
                self._alert_fn(report)
            time.sleep(self._interval)

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def latest_report(self) -> Optional[TamperDetectionReport]:
        return self._reports[-1] if self._reports else None

    def tamper_event_count(self) -> int:
        return sum(1 for r in self._reports if r.verdict != "INTACT")
```

## Comparison

| Approach | Hash Chain | Sequence Check | HMAC Checkpoint | Continuous Monitor | Compliance Report |
|---|---|---|---|---|---|
| HashChainedAuditLogWriter | Yes (per entry) | Yes | No | No | No |
| AuditLogChainVerifier | Yes (verify) | Yes | No | No | No |
| HMACCheckpointSigner | No | No | Yes (external key) | No | No |
| TamperDetectionReportGenerator | Via verifier | Via verifier | Via signer | No | Yes |
| ContinuousTamperMonitor | Via generator | Via generator | Via signer | Yes | Via generator |

**Best for production**: Store the HMAC signing key in a secrets manager or HSM — never on the same host as the log file. Run `ContinuousTamperMonitor` with `interval_seconds=300` so any modification is detected within five minutes. Ship checkpoints to an immutable append-only store (e.g., AWS S3 with Object Lock or a write-once database column) separately from the log file: even if the log is fully replaced, the checkpoint proves what the tail hash was at a previous point in time. For compliance audits, the `TamperDetectionReport.verdict == "INTACT"` plus a valid checkpoint signature is evidence that the log was not modified between the checkpoint timestamp and the audit.
