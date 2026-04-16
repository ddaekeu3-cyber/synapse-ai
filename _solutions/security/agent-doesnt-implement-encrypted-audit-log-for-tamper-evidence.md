---
title: "Agent Doesn't Implement Encrypted Audit Log for Tamper Evidence"
description: "AI agents that write audit logs to plain files or unprotected databases cannot prove those logs weren't altered after the fact. An encrypted, hash-chained audit log makes every entry cryptographically dependent on all prior entries — any deletion, insertion, or modification of a historical record breaks the chain and is immediately detectable. Combined with AES-GCM encryption, the log is confidential and tamper-evident."
date: 2025-02-15
difficulty: advanced
category: security
slug: agent-doesnt-implement-encrypted-audit-log-for-tamper-evidence
tags:
  - audit-log
  - tamper-evidence
  - hash-chain
  - encryption
  - aes-gcm
  - compliance
  - security
symptoms:
  - "Audit log is a plain JSON file writable by the agent process"
  - "No way to prove audit log wasn't modified after a security incident"
  - "Log records can be deleted without breaking any integrity check"
  - "Compliance audit asks for tamper-evident logs but none exist"
  - "An attacker who compromises the agent process can rewrite audit history"
---

## Problem

A plain text audit log is only as trustworthy as the file system permissions protecting it. An attacker with write access to the log file — or to the database table storing audit records — can silently delete, modify, or insert records. Hash-chaining makes every record dependent on a digest of all prior records: `hash_n = SHA-256(entry_n || hash_{n-1})`. Altering any historical record changes its hash, which propagates forward, invalidating every subsequent record's chain link. Encryption with AES-GCM adds authenticated confidentiality: each entry's ciphertext is bound to its position in the chain via associated data.

---

## Solution 1: HashChainedAuditLog — Chain-Linked Immutable Records

```python
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AuditRecord:
    sequence: int
    timestamp: float
    event_type: str
    actor: str
    payload: Dict[str, Any]
    prev_hash: str          # SHA-256 of the previous record's canonical bytes
    record_hash: str        # SHA-256 of this record's canonical bytes (incl. prev_hash)


class HashChainedAuditLog:
    """
    Append-only audit log where each record's hash depends on all prior records.
    Any modification to a historical record breaks the chain from that point forward.

    Usage:
        log = HashChainedAuditLog()
        log.append("tool_call", actor="agent-1",
                   payload={"tool": "web_search", "query": "SSRF"})
        log.append("llm_invoke", actor="agent-1",
                   payload={"model": "claude-sonnet-4-6", "tokens": 512})

        ok, broken_at = log.verify()
        assert ok, f"Chain broken at sequence {broken_at}"
    """

    GENESIS_HASH = "0" * 64   # sentinel for the first record

    def __init__(self):
        self._records: List[AuditRecord] = []

    def _canonical(self, seq: int, ts: float, event_type: str,
                    actor: str, payload: Dict, prev_hash: str) -> bytes:
        data = {
            "seq": seq,
            "ts": ts,
            "event_type": event_type,
            "actor": actor,
            "payload": payload,
            "prev_hash": prev_hash,
        }
        return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()

    def append(self, event_type: str, actor: str,
                payload: Optional[Dict] = None) -> AuditRecord:
        seq = len(self._records)
        prev_hash = (
            self._records[-1].record_hash if self._records else self.GENESIS_HASH
        )
        ts = time.time()
        payload = payload or {}
        canonical = self._canonical(seq, ts, event_type, actor, payload, prev_hash)
        record_hash = hashlib.sha256(canonical).hexdigest()

        record = AuditRecord(
            sequence=seq,
            timestamp=ts,
            event_type=event_type,
            actor=actor,
            payload=payload,
            prev_hash=prev_hash,
            record_hash=record_hash,
        )
        self._records.append(record)
        return record

    def verify(self) -> tuple:
        """Returns (True, None) if chain is intact, (False, broken_seq) otherwise."""
        prev = self.GENESIS_HASH
        for rec in self._records:
            if rec.prev_hash != prev:
                return False, rec.sequence
            canonical = self._canonical(
                rec.sequence, rec.timestamp, rec.event_type,
                rec.actor, rec.payload, rec.prev_hash,
            )
            expected_hash = hashlib.sha256(canonical).hexdigest()
            if rec.record_hash != expected_hash:
                return False, rec.sequence
            prev = rec.record_hash
        return True, None

    def head_hash(self) -> str:
        return self._records[-1].record_hash if self._records else self.GENESIS_HASH

    def to_dicts(self) -> List[Dict]:
        return [
            {
                "seq": r.sequence, "ts": r.timestamp,
                "event_type": r.event_type, "actor": r.actor,
                "payload": r.payload, "prev_hash": r.prev_hash,
                "record_hash": r.record_hash,
            }
            for r in self._records
        ]
```

---

## Solution 2: EncryptedAuditLog — AES-GCM Encrypted Hash-Chained Entries

```python
import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _CRYPTO = True
except ImportError:
    _CRYPTO = False


@dataclass
class EncryptedAuditEntry:
    sequence: int
    ciphertext_b64: str       # base64 AES-GCM ciphertext
    nonce_b64: str            # base64 12-byte nonce
    chain_hash: str           # SHA-256(prev_chain_hash || ciphertext)
    timestamp: float


class EncryptedAuditLog:
    """
    AES-GCM encrypted audit log with hash chaining.
    Each entry is encrypted independently; the chain is computed over
    ciphertexts so integrity can be verified without decryption.

    Usage:
        log = EncryptedAuditLog(key=os.urandom(32))
        log.append("tool_call", actor="agent-1", payload={"tool": "db_query"})

        # Verify chain (no decryption required):
        ok, broken = log.verify_chain()

        # Decrypt for audit review:
        records = log.decrypt_all()
    """

    GENESIS = "0" * 64

    def __init__(self, key: Optional[bytes] = None):
        if not _CRYPTO:
            raise RuntimeError("pip install cryptography")
        self._gcm = AESGCM(key or os.urandom(32))
        self._entries: List[EncryptedAuditEntry] = []

    def append(self, event_type: str, actor: str,
                payload: Optional[Dict[str, Any]] = None) -> EncryptedAuditEntry:
        seq = len(self._entries)
        prev_hash = self._entries[-1].chain_hash if self._entries else self.GENESIS
        ts = time.time()

        plaintext = json.dumps({
            "seq": seq, "ts": ts, "event": event_type,
            "actor": actor, "payload": payload or {},
        }, sort_keys=True, separators=(",", ":")).encode()

        nonce = os.urandom(12)
        # Associated data binds entry to its position in the chain
        aad = f"{seq}:{prev_hash}".encode()
        ciphertext = self._gcm.encrypt(nonce, plaintext, aad)

        ct_b64 = base64.b64encode(ciphertext).decode()
        chain_hash = hashlib.sha256(
            (prev_hash + ct_b64).encode()
        ).hexdigest()

        entry = EncryptedAuditEntry(
            sequence=seq,
            ciphertext_b64=ct_b64,
            nonce_b64=base64.b64encode(nonce).decode(),
            chain_hash=chain_hash,
            timestamp=ts,
        )
        self._entries.append(entry)
        return entry

    def verify_chain(self) -> Tuple[bool, Optional[int]]:
        prev = self.GENESIS
        for entry in self._entries:
            expected = hashlib.sha256((prev + entry.ciphertext_b64).encode()).hexdigest()
            if entry.chain_hash != expected:
                return False, entry.sequence
            prev = entry.chain_hash
        return True, None

    def decrypt_all(self) -> List[Dict[str, Any]]:
        results = []
        prev_hash = self.GENESIS
        for entry in self._entries:
            nonce = base64.b64decode(entry.nonce_b64)
            ct = base64.b64decode(entry.ciphertext_b64)
            aad = f"{entry.sequence}:{prev_hash}".encode()
            plaintext = self._gcm.decrypt(nonce, ct, aad)
            results.append(json.loads(plaintext))
            prev_hash = entry.chain_hash
        return results

    def head_hash(self) -> str:
        return self._entries[-1].chain_hash if self._entries else self.GENESIS
```

---

## Solution 3: PersistentEncryptedAuditLog — JSONL File Backend

```python
import base64
import json
import os
from typing import Any, Dict, List, Optional, Tuple


class PersistentEncryptedAuditLog:
    """
    Persists encrypted audit entries to an append-only JSONL file.
    Each write is a single atomic line append (OS-level); the file
    is never rewritten. Verification reads the full file to check
    the chain from genesis to tip.

    Usage:
        log = PersistentEncryptedAuditLog(
            path="/var/log/agent/audit.jsonl",
            key=bytes.fromhex(os.environ["AUDIT_KEY"]),
        )
        log.append("user_query", actor="u-123", payload={"q": "..."})
        ok, broken = log.verify_file()
    """

    def __init__(self, path: str, key: Optional[bytes] = None):
        self._path = path
        self._inner = EncryptedAuditLog(key)
        self._load_existing()

    def _load_existing(self):
        if not os.path.exists(self._path):
            return
        with open(self._path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                entry = EncryptedAuditEntry(
                    sequence=data["seq"],
                    ciphertext_b64=data["ct"],
                    nonce_b64=data["nonce"],
                    chain_hash=data["chain_hash"],
                    timestamp=data["ts"],
                )
                self._inner._entries.append(entry)

    def append(self, event_type: str, actor: str,
                payload: Optional[Dict[str, Any]] = None):
        entry = self._inner.append(event_type, actor, payload)
        line = json.dumps({
            "seq": entry.sequence,
            "ct": entry.ciphertext_b64,
            "nonce": entry.nonce_b64,
            "chain_hash": entry.chain_hash,
            "ts": entry.timestamp,
        }, separators=(",", ":"))
        with open(self._path, "a") as f:
            f.write(line + "\n")

    def verify_file(self) -> Tuple[bool, Optional[int]]:
        return self._inner.verify_chain()

    def decrypt_all(self) -> List[Dict[str, Any]]:
        return self._inner.decrypt_all()

    def entry_count(self) -> int:
        return len(self._inner._entries)
```

---

## Solution 4: AuditLogVerifier — Scheduled Chain Integrity Check

```python
import asyncio
import logging
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class AuditLogVerifier:
    """
    Periodically verifies the audit log chain and fires an alert on failure.
    Run as a background task; escalate if verification fails.

    Usage:
        verifier = AuditLogVerifier(
            log=persistent_log,
            on_failure=send_security_alert,
            check_interval_s=3600,
        )
        asyncio.create_task(verifier.run())
    """

    def __init__(self, log: PersistentEncryptedAuditLog,
                 on_failure: Optional[Callable] = None,
                 check_interval_s: float = 3600.0):
        self._log = log
        self._on_failure = on_failure or self._default_alert
        self._interval = check_interval_s
        self._last_ok = 0.0
        self._failure_count = 0

    @staticmethod
    def _default_alert(seq: int):
        logger.critical(
            "AUDIT_LOG_TAMPER_DETECTED chain_broken_at_sequence=%d", seq
        )

    async def run(self):
        while True:
            await asyncio.sleep(self._interval)
            ok, broken_at = self._log.verify_file()
            if ok:
                self._last_ok = time.time()
                logger.info(
                    "audit_chain_ok entries=%d", self._log.entry_count()
                )
            else:
                self._failure_count += 1
                logger.critical(
                    "audit_chain_TAMPERED broken_at=%d failure_count=%d",
                    broken_at, self._failure_count,
                )
                self._on_failure(broken_at)

    def status(self) -> dict:
        return {
            "last_ok": self._last_ok,
            "failure_count": self._failure_count,
            "entry_count": self._log.entry_count(),
        }
```

---

## Solution 5: AgentAuditMiddleware — Auto-Log All Agent Actions

```python
import functools
import time
from typing import Any, Callable, Optional


class AgentAuditMiddleware:
    """
    Decorates agent action functions to automatically append audit records
    before and after each invocation. Records actor, tool name, inputs,
    outputs, latency, and success/failure.

    Usage:
        audit = AgentAuditMiddleware(log=encrypted_log, actor="agent-prod-1")

        @audit.log_action("tool_call")
        async def web_search(query: str) -> list:
            return await live_search(query)
    """

    def __init__(self, log: PersistentEncryptedAuditLog,
                 actor: str = "agent"):
        self._log = log
        self._actor = actor

    def log_action(self, event_type: str,
                    include_args: bool = False,
                    include_result: bool = False):
        def decorator(fn: Callable) -> Callable:
            @functools.wraps(fn)
            async def wrapper(*args, **kwargs) -> Any:
                payload: dict = {"fn": fn.__name__}
                if include_args:
                    payload["kwargs"] = list(kwargs.keys())

                self._log.append(
                    event_type=f"{event_type}.start",
                    actor=self._actor,
                    payload=payload,
                )
                t0 = time.monotonic()
                try:
                    result = await fn(*args, **kwargs)
                    elapsed_ms = (time.monotonic() - t0) * 1000
                    end_payload: dict = {
                        "fn": fn.__name__,
                        "elapsed_ms": round(elapsed_ms, 1),
                        "success": True,
                    }
                    if include_result:
                        end_payload["result_type"] = type(result).__name__
                    self._log.append(
                        f"{event_type}.end", self._actor, end_payload
                    )
                    return result
                except Exception as exc:
                    elapsed_ms = (time.monotonic() - t0) * 1000
                    self._log.append(
                        f"{event_type}.error", self._actor,
                        {"fn": fn.__name__,
                         "error": type(exc).__name__,
                         "elapsed_ms": round(elapsed_ms, 1),
                         "success": False},
                    )
                    raise
            return wrapper
        return decorator
```

---

## Solution 6: AuditLogExportPipeline — Export to Immutable Storage

```python
import asyncio
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class AuditLogExportPipeline:
    """
    Exports new audit log entries to immutable storage (S3 with Object Lock,
    GCS with retention policy, WORM storage) at regular intervals.
    Provides a second copy of the chain that cannot be modified even if
    the primary file system is compromised.

    Usage:
        pipeline = AuditLogExportPipeline(
            log=persistent_log,
            export_fn=upload_to_s3_worm_bucket,
            export_interval_s=300,
        )
        asyncio.create_task(pipeline.run())
    """

    def __init__(self, log: PersistentEncryptedAuditLog,
                 export_fn: Callable,
                 export_interval_s: float = 300.0):
        self._log = log
        self._export = export_fn
        self._interval = export_interval_s
        self._last_exported_seq = -1
        self._export_count = 0

    async def run(self):
        while True:
            await asyncio.sleep(self._interval)
            await self._export_new_entries()

    async def _export_new_entries(self):
        entries = self._log._inner._entries
        new_entries = [
            e for e in entries if e.sequence > self._last_exported_seq
        ]
        if not new_entries:
            return

        payload = {
            "exported_at": time.time(),
            "head_hash": self._log._inner.head_hash(),
            "entries": [
                {
                    "seq": e.sequence, "ct": e.ciphertext_b64,
                    "nonce": e.nonce_b64, "chain_hash": e.chain_hash,
                    "ts": e.timestamp,
                }
                for e in new_entries
            ],
        }
        try:
            await self._export(payload)
            self._last_exported_seq = new_entries[-1].sequence
            self._export_count += len(new_entries)
            logger.info(
                "audit_export_ok entries=%d last_seq=%d",
                len(new_entries), self._last_exported_seq,
            )
        except Exception as exc:
            logger.error("audit_export_failed error=%s", exc)

    def export_stats(self) -> Dict[str, Any]:
        return {
            "last_exported_seq": self._last_exported_seq,
            "total_exported": self._export_count,
        }
```

---

## Comparison

| Approach | Hash Chain | Encryption | Persistent | Auto-Verify | Auto-Export |
|---|---|---|---|---|---|
| **HashChainedAuditLog** | Yes | No | No | Manual | No |
| **EncryptedAuditLog** | Yes | AES-GCM | No | Manual | No |
| **PersistentEncryptedAuditLog** | Yes | AES-GCM | Yes (JSONL) | Manual | No |
| **AuditLogVerifier** | Via log | No | No | Yes | No |
| **AgentAuditMiddleware** | Via log | Via log | Via log | No | No |
| **AuditLogExportPipeline** | Via log | Via log | Yes | No | Yes |

**Key insight**: the hash chain is only as strong as the chain tip. An adversary who can overwrite the entire log file can reconstruct a valid chain for falsified history. The defence is the chain tip itself: publish the head hash to an external immutable system (a public ledger, S3 Object Lock, a monitoring database the agent cannot write to) every N minutes. Verifying the published tip against the on-disk log makes undetected log replacement impossible even if the primary storage is fully compromised.
