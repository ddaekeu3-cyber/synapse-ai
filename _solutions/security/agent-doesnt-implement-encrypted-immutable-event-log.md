---
title: "Agent Doesn't Implement Encrypted Immutable Event Log"
description: "Plaintext mutable logs can be altered by a compromised process, erasing evidence of an attack. An encrypted, hash-chained append-only log makes tampering detectable and sensitive event data unreadable to unauthorized readers."
difficulty: advanced
category: security
tags: [audit-log, immutable, encryption, hash-chain, tamper-detection, security, compliance]
---

## Problem

Agent audit logs stored as plain files or in a mutable database table can be silently edited or deleted by a compromised process. Compliance requirements (SOC2, HIPAA, PCI-DSS) require tamper-evident logs. Plaintext logs expose sensitive data (user inputs, API keys, PII) to anyone with filesystem access.

```python
# Broken: plaintext mutable log — no tamper evidence, no encryption
import json, time

def log_event(event: dict):
    with open("agent.log", "a") as f:
        f.write(json.dumps({"ts": time.time(), **event}) + "\n")
# Anyone can edit agent.log; plaintext exposes sensitive fields
```

---

## Solution 1: Hash-Chained Append-Only Log

```python
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class LogEntry:
    sequence: int
    timestamp: float
    event_type: str
    payload: dict
    prev_hash: str     # hash of previous entry (genesis = "0" * 64)
    entry_hash: str = ""

    def compute_hash(self) -> str:
        canonical = json.dumps({
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
        }, sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()

    def __post_init__(self):
        if not self.entry_hash:
            self.entry_hash = self.compute_hash()

    def to_line(self) -> str:
        return json.dumps({
            "seq": self.sequence,
            "ts": self.timestamp,
            "type": self.event_type,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "hash": self.entry_hash,
        })

class HashChainedLog:
    """
    Append-only log where each entry includes the hash of the previous entry.
    Any modification to a past entry invalidates all subsequent hashes,
    making tampering immediately detectable.
    """

    def __init__(self, log_path: str):
        self._path = Path(log_path)
        self._sequence = 0
        self._last_hash = "0" * 64  # genesis hash
        self._load_tail()

    def _load_tail(self):
        """Resume from the last entry in an existing log file."""
        if not self._path.exists():
            return
        with open(self._path) as f:
            lines = f.readlines()
        if not lines:
            return
        last = json.loads(lines[-1])
        self._sequence = last["seq"] + 1
        self._last_hash = last["hash"]

    def append(self, event_type: str, payload: dict) -> LogEntry:
        entry = LogEntry(
            sequence=self._sequence,
            timestamp=time.time(),
            event_type=event_type,
            payload=payload,
            prev_hash=self._last_hash,
        )
        with open(self._path, "a") as f:
            f.write(entry.to_line() + "\n")
        self._last_hash = entry.entry_hash
        self._sequence += 1
        return entry

    def verify(self) -> tuple[bool, list[int]]:
        """Verify chain integrity. Returns (is_valid, list_of_broken_sequences)."""
        if not self._path.exists():
            return True, []
        broken: list[int] = []
        prev_hash = "0" * 64
        with open(self._path) as f:
            for line in f:
                entry_dict = json.loads(line)
                # Recompute hash
                recomputed = hashlib.sha256(json.dumps({
                    "sequence": entry_dict["seq"],
                    "timestamp": entry_dict["ts"],
                    "event_type": entry_dict["type"],
                    "payload": entry_dict["payload"],
                    "prev_hash": entry_dict["prev_hash"],
                }, sort_keys=True).encode()).hexdigest()

                if entry_dict["prev_hash"] != prev_hash:
                    broken.append(entry_dict["seq"])
                if entry_dict["hash"] != recomputed:
                    broken.append(entry_dict["seq"])
                prev_hash = entry_dict["hash"]
        return len(broken) == 0, broken
```

---

## Solution 2: Symmetric Encryption for Log Entries

```python
import base64
import json
import os
import time
from pathlib import Path

# Requires: pip install cryptography
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

class EncryptedLogWriter:
    """
    Encrypts each log entry with AES-256-GCM.
    Each entry uses a fresh random nonce (96-bit).
    The encryption key is loaded from an environment variable or key file —
    never hardcoded.
    """

    def __init__(self, log_path: str, key_hex: str | None = None):
        self._path = Path(log_path)
        key_bytes = self._load_key(key_hex)
        self._cipher = AESGCM(key_bytes)

    def _load_key(self, key_hex: str | None) -> bytes:
        # Precedence: argument → env var → generate and warn
        if key_hex:
            return bytes.fromhex(key_hex)
        env_key = os.environ.get("AGENT_LOG_KEY")
        if env_key:
            return bytes.fromhex(env_key)
        # Last resort: generate ephemeral key (logs unreadable after restart)
        key = os.urandom(32)
        print("[EncryptedLog] WARNING: using ephemeral key — logs lost on restart. "
              "Set AGENT_LOG_KEY environment variable.")
        return key

    def append(self, event_type: str, payload: dict,
                prev_hash: str = "") -> str:
        """Encrypt and append a log entry. Returns ciphertext hex for chaining."""
        plaintext = json.dumps({
            "ts": time.time(),
            "type": event_type,
            "payload": payload,
            "prev_hash": prev_hash,
        }, sort_keys=True).encode()

        nonce = os.urandom(12)  # 96-bit nonce for AES-GCM
        ciphertext = self._cipher.encrypt(nonce, plaintext, associated_data=None)

        # Store: base64(nonce) + "." + base64(ciphertext)
        entry_b64 = (
            base64.b64encode(nonce).decode() + "." +
            base64.b64encode(ciphertext).decode()
        )
        with open(self._path, "a") as f:
            f.write(entry_b64 + "\n")

        return base64.b64encode(ciphertext[:16]).decode()  # partial hash for chaining

    def read_all(self) -> list[dict]:
        """Decrypt and return all log entries."""
        if not self._path.exists():
            return []
        entries = []
        with open(self._path) as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    nonce_b64, ct_b64 = line.split(".", 1)
                    nonce = base64.b64decode(nonce_b64)
                    ciphertext = base64.b64decode(ct_b64)
                    plaintext = self._cipher.decrypt(nonce, ciphertext, None)
                    entries.append(json.loads(plaintext))
                except Exception as e:
                    print(f"[EncryptedLog] Failed to decrypt line {lineno}: {e}")
                    entries.append({"_error": "decryption_failed", "_line": lineno})
        return entries
```

---

## Solution 3: Combined Encrypted + Hash-Chained Log

```python
import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False

@dataclass
class SecureLogEntry:
    sequence: int
    timestamp: float
    event_type: str
    payload: dict
    prev_ciphertext_hash: str
    nonce_hex: str = ""
    ciphertext_b64: str = ""
    plaintext_hash: str = ""  # hash of plaintext JSON for integrity check

class SecureImmutableLog:
    """
    Combines AES-256-GCM encryption with hash chaining.
    Each entry's ciphertext is hashed into the next entry's prev_ciphertext_hash,
    creating a tamper-evident chain even over encrypted data.
    """

    def __init__(self, log_path: str, key_hex: str):
        self._path = Path(log_path)
        self._sequence = 0
        self._prev_ct_hash = "0" * 64
        if _HAS_CRYPTO:
            self._cipher = AESGCM(bytes.fromhex(key_hex))
        self._load_tail()

    def _load_tail(self):
        if not self._path.exists():
            return
        with open(self._path) as f:
            lines = [l.strip() for l in f if l.strip()]
        if not lines:
            return
        last = json.loads(lines[-1])
        self._sequence = last["seq"] + 1
        # Recompute prev_ct_hash from last ciphertext
        self._prev_ct_hash = hashlib.sha256(
            last["ct"].encode()
        ).hexdigest()

    def append(self, event_type: str, payload: dict) -> str:
        """Encrypt, chain, and append. Returns ciphertext hash."""
        plaintext_dict = {
            "seq": self._sequence,
            "ts": time.time(),
            "type": event_type,
            "payload": payload,
            "prev_ct_hash": self._prev_ct_hash,
        }
        plaintext = json.dumps(plaintext_dict, sort_keys=True).encode()
        plaintext_hash = hashlib.sha256(plaintext).hexdigest()

        nonce = os.urandom(12)
        if _HAS_CRYPTO:
            ciphertext = self._cipher.encrypt(nonce, plaintext, None)
            ct_b64 = base64.b64encode(ciphertext).decode()
            nonce_hex = nonce.hex()
        else:
            # Fallback: store plaintext (development only)
            ct_b64 = base64.b64encode(plaintext).decode()
            nonce_hex = ""

        record = json.dumps({
            "seq": self._sequence,
            "nonce": nonce_hex,
            "ct": ct_b64,
            "ph": plaintext_hash,
            "pch": self._prev_ct_hash,
        })

        with open(self._path, "a") as f:
            f.write(record + "\n")

        ct_hash = hashlib.sha256(ct_b64.encode()).hexdigest()
        self._prev_ct_hash = ct_hash
        self._sequence += 1
        return ct_hash

    def verify_chain(self) -> tuple[bool, list[int]]:
        """Verify that the ciphertext hash chain is unbroken."""
        if not self._path.exists():
            return True, []
        broken = []
        prev_ct_hash = "0" * 64
        with open(self._path) as f:
            for line in f:
                r = json.loads(line)
                ct_hash = hashlib.sha256(r["ct"].encode()).hexdigest()
                if r["pch"] != prev_ct_hash:
                    broken.append(r["seq"])
                prev_ct_hash = ct_hash
        return len(broken) == 0, broken

    def read_all(self) -> list[dict]:
        if not self._path.exists() or not _HAS_CRYPTO:
            return []
        entries = []
        with open(self._path) as f:
            for line in f:
                r = json.loads(line)
                try:
                    nonce = bytes.fromhex(r["nonce"])
                    ct = base64.b64decode(r["ct"])
                    pt = self._cipher.decrypt(nonce, ct, None)
                    entry = json.loads(pt)
                    # Verify plaintext hash
                    recomputed = hashlib.sha256(
                        json.dumps(entry, sort_keys=True).encode()
                    ).hexdigest()
                    entry["_integrity"] = "ok" if recomputed == r["ph"] else "TAMPERED"
                    entries.append(entry)
                except Exception as e:
                    entries.append({"_error": str(e), "seq": r.get("seq")})
        return entries
```

---

## Solution 4: Field-Level Redaction Before Logging

```python
import copy
import hashlib
import json
import re
from typing import Any

# Fields that must be redacted before any logging
SENSITIVE_FIELD_PATTERNS = [
    re.compile(r"(password|secret|token|api_key|auth|credential|ssn|"
               r"credit_card|card_number|cvv|pin)", re.IGNORECASE),
    re.compile(r"(email|phone|address|dob|date_of_birth)", re.IGNORECASE),
]

def is_sensitive_key(key: str) -> bool:
    return any(p.search(key) for p in SENSITIVE_FIELD_PATTERNS)

def redact_value(value: Any, key: str) -> Any:
    """Replace sensitive value with a deterministic pseudonym for correlation."""
    if isinstance(value, str) and value:
        # HMAC-based pseudonymization: same input → same output, irreversible
        import hmac, os
        pseudonym_key = os.environ.get("AGENT_PSEUDONYM_KEY", "dev-key").encode()
        h = hmac.new(pseudonym_key, value.encode(), hashlib.sha256).hexdigest()[:12]
        return f"[REDACTED:{h}]"
    if isinstance(value, (int, float)):
        return "[REDACTED:numeric]"
    return "[REDACTED]"

def redact_dict(obj: Any, depth: int = 0, max_depth: int = 10) -> Any:
    """Recursively redact sensitive fields from a dict/list structure."""
    if depth > max_depth:
        return "[MAX_DEPTH]"
    if isinstance(obj, dict):
        return {
            k: redact_value(v, k) if is_sensitive_key(k) else redact_dict(v, depth + 1)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact_dict(item, depth + 1) for item in obj]
    return obj

class RedactingSecureLog:
    """Log wrapper that redacts sensitive fields before encryption and storage."""

    def __init__(self, secure_log: "SecureImmutableLog"):
        self._log = secure_log

    def append(self, event_type: str, payload: dict) -> str:
        clean_payload = redact_dict(copy.deepcopy(payload))
        return self._log.append(event_type, clean_payload)

# Usage: agent action logging with automatic redaction
async def log_tool_call(log: RedactingSecureLog, tool_name: str,
                         args: dict, result: Any):
    log.append("tool_call", {
        "tool": tool_name,
        "args": args,           # sensitive fields auto-redacted
        "result_type": type(result).__name__,
        "result_preview": str(result)[:100],
    })

async def log_auth_event(log: RedactingSecureLog, user_id: str,
                          event: str, metadata: dict):
    log.append("auth", {
        "user_id": user_id,
        "event": event,
        **metadata  # password, token, etc. auto-redacted
    })
```

---

## Solution 5: Remote Attestation — Write-Once Cloud Log

```python
import asyncio
import hashlib
import json
import time
from typing import Any

class WriteOnceLogShipper:
    """
    Ships log entries to a write-once, append-only remote store.
    Cloud providers with write-once APIs:
      - AWS CloudTrail (immutable audit)
      - GCS with Object Lock (WORM)
      - Azure Immutable Blob Storage
    This implementation ships to a generic HTTPS endpoint.
    """

    def __init__(self, endpoint: str, api_key: str,
                 batch_size: int = 50,
                 flush_interval: float = 5.0):
        self._endpoint = endpoint
        self._api_key = api_key
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._buffer: list[dict] = []
        self._lock = asyncio.Lock()
        self._shipped_count = 0
        self._failed_count = 0

    async def queue(self, event_type: str, payload: dict):
        entry = {
            "ts": time.time(),
            "type": event_type,
            "payload": payload,
            "checksum": hashlib.sha256(
                json.dumps(payload, sort_keys=True).encode()
            ).hexdigest()[:16]
        }
        async with self._lock:
            self._buffer.append(entry)
            if len(self._buffer) >= self._batch_size:
                await self._flush_unlocked()

    async def _flush_unlocked(self):
        if not self._buffer:
            return
        batch = list(self._buffer)
        self._buffer.clear()
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self._endpoint,
                    json={"entries": batch},
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    timeout=10.0
                )
                response.raise_for_status()
                self._shipped_count += len(batch)
        except Exception as e:
            print(f"[LogShipper] Failed to ship {len(batch)} entries: {e}")
            self._failed_count += len(batch)
            # Re-queue (backpressure: in production, use persistent local buffer)
            async with self._lock:
                self._buffer = batch + self._buffer

    async def flush_loop(self):
        while True:
            await asyncio.sleep(self._flush_interval)
            async with self._lock:
                await self._flush_unlocked()

    def stats(self) -> dict:
        return {
            "buffered": len(self._buffer),
            "shipped": self._shipped_count,
            "failed": self._failed_count,
        }
```

---

## Solution 6: Log Integrity Verification Service

```python
import asyncio
import hashlib
import json
import time
from pathlib import Path

class LogIntegrityVerifier:
    """
    Standalone verification service that periodically audits the log chain
    and reports tampering attempts. Can run as a separate process or cron job.
    """

    def __init__(self, log_path: str, checkpoint_path: str):
        self._log_path = Path(log_path)
        self._checkpoint_path = Path(checkpoint_path)

    def _load_checkpoint(self) -> dict:
        if self._checkpoint_path.exists():
            return json.loads(self._checkpoint_path.read_text())
        return {"last_seq": -1, "last_hash": "0" * 64, "verified_at": None}

    def _save_checkpoint(self, seq: int, ct_hash: str):
        self._checkpoint_path.write_text(json.dumps({
            "last_seq": seq,
            "last_hash": ct_hash,
            "verified_at": time.time(),
        }))

    def verify_incremental(self) -> dict:
        """Verify only new entries since last checkpoint."""
        checkpoint = self._load_checkpoint()
        start_seq = checkpoint["last_seq"] + 1
        prev_ct_hash = checkpoint["last_hash"]

        violations: list[dict] = []
        last_seq = checkpoint["last_seq"]
        last_ct_hash = prev_ct_hash

        if not self._log_path.exists():
            return {"status": "no_log", "violations": []}

        with open(self._log_path) as f:
            for line in f:
                r = json.loads(line)
                seq = r["seq"]
                if seq < start_seq:
                    continue
                ct_hash = hashlib.sha256(r["ct"].encode()).hexdigest()

                # Check chain continuity
                if r["pch"] != prev_ct_hash:
                    violations.append({
                        "seq": seq,
                        "type": "chain_break",
                        "expected_pch": prev_ct_hash[:16],
                        "actual_pch": r["pch"][:16],
                    })

                # Check sequence continuity
                if seq != last_seq + 1 and last_seq >= 0:
                    violations.append({
                        "seq": seq,
                        "type": "sequence_gap",
                        "expected": last_seq + 1,
                        "actual": seq,
                    })

                prev_ct_hash = ct_hash
                last_seq = seq
                last_ct_hash = ct_hash

        if not violations:
            self._save_checkpoint(last_seq, last_ct_hash)

        return {
            "status": "ok" if not violations else "TAMPERED",
            "verified_from_seq": start_seq,
            "verified_to_seq": last_seq,
            "violations": violations,
            "verified_at": time.time(),
        }

    async def continuous_audit(self, interval: float = 300.0):
        """Run integrity checks periodically."""
        while True:
            await asyncio.sleep(interval)
            result = self.verify_incremental()
            if result["status"] != "ok":
                print(f"[AUDIT] ⚠️  LOG TAMPERING DETECTED: {result['violations']}")
                # Trigger alert: page on-call, lock down agent, etc.
            else:
                print(f"[AUDIT] Chain intact: "
                      f"seq {result['verified_from_seq']}–{result['verified_to_seq']}")
```

---

## Comparison

| Solution | Tamper Detection | Encryption | PII Protection | Remote | Compliance | Best For |
|---|---|---|---|---|---|---|
| 1. Hash chain | Yes (chain break) | No | No | No | Partial | Tamper evidence without encryption |
| 2. AES-GCM encryption | No | Yes | No | No | Partial | Encrypting sensitive payloads |
| 3. Encrypted + chained | Yes + Yes | Yes | No | No | Strong | Full local security |
| 4. Field redaction | No | Optional | Yes | Optional | Yes (GDPR) | PII-heavy agent logs |
| 5. Write-once remote | Remote (WORM) | Optional | No | Yes | Strong | Cloud-native compliance |
| 6. Integrity verifier | Yes (incremental) | No | No | No | Yes | Continuous audit monitoring |

**Key principle**: encrypt log entries before writing (protects confidentiality), hash-chain entries (protects integrity), and write to a write-once destination (protects availability of evidence). Use field-level redaction to ensure sensitive data never reaches the log in the first place — encryption is a safety net, not a license to log PII.
