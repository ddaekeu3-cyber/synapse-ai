---
title: "Agent Doesn't Implement Audit Log Tamper Detection"
description: "Six solutions for detecting tampering in agent audit logs using hash chaining, digital signatures, and append-only storage patterns."
difficulty: advanced
category: security
tags: [audit, tamper-detection, integrity, hash-chain, signatures, security]
---

# Agent Doesn't Implement Audit Log Tamper Detection

Audit logs are only valuable if they're trustworthy. Without tamper detection, an attacker who gains write access can silently delete or modify entries covering their tracks. These six solutions add cryptographic integrity guarantees ranging from simple hash chaining to signed Merkle trees and append-only storage.

## Solution 1: Hash-Chained Audit Log

Each entry contains the SHA-256 hash of the previous entry, forming an immutable chain. Any modification breaks the chain.

```python
import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from anthropic import AsyncAnthropic


@dataclass
class AuditEntry:
    entry_id: str
    timestamp: float
    action: str
    actor: str
    details: dict
    prev_hash: str  # Hash of previous entry ("genesis" for first)
    entry_hash: str = ""  # SHA-256 of this entry's content

    def compute_hash(self) -> str:
        payload = json.dumps({
            "entry_id": self.entry_id,
            "timestamp": self.timestamp,
            "action": self.action,
            "actor": self.actor,
            "details": self.details,
            "prev_hash": self.prev_hash,
        }, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def finalize(self) -> "AuditEntry":
        self.entry_hash = self.compute_hash()
        return self

    def to_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "timestamp": self.timestamp,
            "action": self.action,
            "actor": self.actor,
            "details": self.details,
            "prev_hash": self.prev_hash,
            "entry_hash": self.entry_hash,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AuditEntry":
        return cls(**data)


class HashChainedAuditLog:
    GENESIS_HASH = "0" * 64

    def __init__(self, log_path: str = "/tmp/agent_audit.jsonl"):
        self.log_path = Path(log_path)
        self._last_hash = self.GENESIS_HASH
        self._entry_count = 0
        self._load_last_hash()

    def _load_last_hash(self):
        if not self.log_path.exists():
            return
        last_line = ""
        with self.log_path.open() as f:
            for line in f:
                last_line = line.strip()
        if last_line:
            try:
                entry = json.loads(last_line)
                self._last_hash = entry.get("entry_hash", self.GENESIS_HASH)
                self._entry_count += 1
            except json.JSONDecodeError:
                pass

    def append(self, action: str, actor: str, details: dict) -> AuditEntry:
        entry = AuditEntry(
            entry_id=str(uuid.uuid4()),
            timestamp=time.time(),
            action=action,
            actor=actor,
            details=details,
            prev_hash=self._last_hash,
        ).finalize()

        with self.log_path.open("a") as f:
            f.write(json.dumps(entry.to_dict()) + "\n")

        self._last_hash = entry.entry_hash
        self._entry_count += 1
        return entry

    def verify_chain(self) -> tuple[bool, str]:
        """Returns (valid, error_message). Empty error = chain is intact."""
        if not self.log_path.exists():
            return True, ""

        prev_hash = self.GENESIS_HASH
        with self.log_path.open() as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    entry = AuditEntry.from_dict(data)
                except (json.JSONDecodeError, TypeError) as e:
                    return False, f"Line {line_num}: parse error: {e}"

                # Verify prev_hash linkage
                if entry.prev_hash != prev_hash:
                    return False, (
                        f"Line {line_num}: chain break — "
                        f"expected prev_hash={prev_hash[:16]}… "
                        f"got {entry.prev_hash[:16]}…"
                    )

                # Verify entry hash
                expected = entry.compute_hash()
                if entry.entry_hash != expected:
                    return False, (
                        f"Line {line_num} (entry {entry.entry_id}): "
                        f"hash mismatch — entry was tampered"
                    )

                prev_hash = entry.entry_hash

        return True, ""


class AuditedAgent:
    def __init__(self, actor: str = "agent"):
        self.client = AsyncAnthropic()
        self.log = HashChainedAuditLog()
        self.actor = actor

    async def chat(self, message: str, model: str = "claude-haiku-4-5-20251001") -> str:
        self.log.append("llm_request", self.actor, {"message": message[:200], "model": model})
        response = await self.client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": message}],
        )
        text = response.content[0].text
        self.log.append("llm_response", self.actor, {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "response_preview": text[:100],
        })
        return text

    def verify(self) -> bool:
        valid, error = self.log.verify_chain()
        if valid:
            print(f"[AUDIT] Chain verified: {self.log._entry_count} entries intact")
        else:
            print(f"[AUDIT] TAMPER DETECTED: {error}")
        return valid


async def demo_hash_chain():
    agent = AuditedAgent(actor="user_123")
    await agent.chat("What is quantum computing?")
    await agent.chat("How does RSA encryption work?")
    agent.verify()
```

## Solution 2: HMAC-Signed Audit Entries

Each entry is signed with a server-side HMAC key. Verification catches both modification and insertion of unauthorized entries.

```python
import asyncio
import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from anthropic import AsyncAnthropic

# In production: load from secrets manager
AUDIT_HMAC_KEY = os.environ.get("AUDIT_HMAC_KEY", "dev-secret-key-change-in-prod").encode()


@dataclass
class SignedAuditEntry:
    entry_id: str
    timestamp: float
    action: str
    actor: str
    resource: str
    outcome: str  # "success" | "failure" | "denied"
    details: dict
    signature: str = ""  # HMAC-SHA256 of canonical payload

    def _canonical(self) -> bytes:
        payload = {
            "entry_id": self.entry_id,
            "timestamp": self.timestamp,
            "action": self.action,
            "actor": self.actor,
            "resource": self.resource,
            "outcome": self.outcome,
            "details": self.details,
        }
        return json.dumps(payload, sort_keys=True).encode()

    def sign(self, key: bytes = AUDIT_HMAC_KEY) -> "SignedAuditEntry":
        self.signature = hmac.new(key, self._canonical(), hashlib.sha256).hexdigest()
        return self

    def verify(self, key: bytes = AUDIT_HMAC_KEY) -> bool:
        expected = hmac.new(key, self._canonical(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, self.signature)

    def to_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "timestamp": self.timestamp,
            "action": self.action,
            "actor": self.actor,
            "resource": self.resource,
            "outcome": self.outcome,
            "details": self.details,
            "signature": self.signature,
        }


class HMACSignedAuditLog:
    def __init__(self, log_path: str = "/tmp/signed_audit.jsonl", key: bytes = AUDIT_HMAC_KEY):
        self.log_path = Path(log_path)
        self.key = key

    def record(
        self,
        action: str,
        actor: str,
        resource: str,
        outcome: str,
        details: dict,
    ) -> SignedAuditEntry:
        entry = SignedAuditEntry(
            entry_id=str(uuid.uuid4()),
            timestamp=time.time(),
            action=action,
            actor=actor,
            resource=resource,
            outcome=outcome,
            details=details,
        ).sign(self.key)

        with self.log_path.open("a") as f:
            f.write(json.dumps(entry.to_dict()) + "\n")
        return entry

    def verify_all(self) -> tuple[int, list[str]]:
        """Returns (valid_count, list_of_tampering_errors)."""
        if not self.log_path.exists():
            return 0, []
        valid = 0
        errors = []
        with self.log_path.open() as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    entry = SignedAuditEntry(**data)
                    if entry.verify(self.key):
                        valid += 1
                    else:
                        errors.append(
                            f"Line {line_num} (id={data.get('entry_id','?')[:8]}): "
                            f"HMAC signature invalid — entry tampered"
                        )
                except Exception as e:
                    errors.append(f"Line {line_num}: parse error: {e}")
        return valid, errors


class HMACSecuredAgent:
    def __init__(self, actor: str = "agent"):
        self.client = AsyncAnthropic()
        self.audit = HMACSignedAuditLog()
        self.actor = actor

    async def invoke_tool(self, tool_name: str, args: dict) -> dict:
        """Simulated tool call with audit logging."""
        self.audit.record(
            action="tool_call",
            actor=self.actor,
            resource=f"tool:{tool_name}",
            outcome="success",
            details={"tool": tool_name, "args": args},
        )
        return {"result": f"executed {tool_name}"}

    async def chat(self, message: str) -> str:
        self.audit.record(
            action="chat_request",
            actor=self.actor,
            resource="llm:claude",
            outcome="initiated",
            details={"message_length": len(message)},
        )
        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": message}],
        )
        text = response.content[0].text
        self.audit.record(
            action="chat_response",
            actor=self.actor,
            resource="llm:claude",
            outcome="success",
            details={"tokens": response.usage.output_tokens},
        )
        return text

    def verify_audit_integrity(self):
        valid, errors = self.audit.verify_all()
        print(f"[HMAC AUDIT] Valid entries: {valid}, Tampering detected: {len(errors)}")
        for err in errors:
            print(f"  ERROR: {err}")
        return len(errors) == 0
```

## Solution 3: Ed25519-Signed Audit Entries for Non-Repudiation

Use asymmetric Ed25519 signing; the private key signs, the public key verifies. Even the log server can't forge entries.

```python
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey
)
from cryptography.hazmat.primitives.serialization import (
    Encoding, PublicFormat, PrivateFormat, NoEncryption
)
from cryptography.exceptions import InvalidSignature
import base64
from anthropic import AsyncAnthropic


def generate_ed25519_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    private_key = Ed25519PrivateKey.generate()
    return private_key, private_key.public_key()


@dataclass
class Ed25519AuditEntry:
    entry_id: str
    timestamp: float
    action: str
    actor: str
    details: dict
    public_key_hex: str   # Which key signed this
    signature_b64: str = ""

    def _canonical(self) -> bytes:
        return json.dumps({
            "entry_id": self.entry_id,
            "timestamp": self.timestamp,
            "action": self.action,
            "actor": self.actor,
            "details": self.details,
            "public_key_hex": self.public_key_hex,
        }, sort_keys=True).encode()

    def sign(self, private_key: Ed25519PrivateKey) -> "Ed25519AuditEntry":
        sig = private_key.sign(self._canonical())
        self.signature_b64 = base64.b64encode(sig).decode()
        return self

    def verify(self, public_key: Ed25519PublicKey) -> bool:
        try:
            sig = base64.b64decode(self.signature_b64)
            public_key.verify(sig, self._canonical())
            return True
        except (InvalidSignature, Exception):
            return False

    def to_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "timestamp": self.timestamp,
            "action": self.action,
            "actor": self.actor,
            "details": self.details,
            "public_key_hex": self.public_key_hex,
            "signature_b64": self.signature_b64,
        }


class Ed25519AuditLog:
    def __init__(self, log_path: str = "/tmp/ed25519_audit.jsonl"):
        self.log_path = Path(log_path)
        self._private_key, self._public_key = generate_ed25519_keypair()
        pub_bytes = self._public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
        self._pub_hex = pub_bytes.hex()

    def record(self, action: str, actor: str, details: dict) -> Ed25519AuditEntry:
        entry = Ed25519AuditEntry(
            entry_id=str(uuid.uuid4()),
            timestamp=time.time(),
            action=action,
            actor=actor,
            details=details,
            public_key_hex=self._pub_hex,
        ).sign(self._private_key)

        with self.log_path.open("a") as f:
            f.write(json.dumps(entry.to_dict()) + "\n")
        return entry

    def verify_all(self) -> tuple[int, list[str]]:
        valid = 0
        errors = []
        if not self.log_path.exists():
            return 0, []
        with self.log_path.open() as f:
            for i, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    entry = Ed25519AuditEntry(**data)
                    if entry.verify(self._public_key):
                        valid += 1
                    else:
                        errors.append(f"Entry {i} ({entry.entry_id[:8]}): signature invalid")
                except Exception as e:
                    errors.append(f"Entry {i}: {e}")
        return valid, errors


class Ed25519AuditedAgent:
    def __init__(self):
        self.client = AsyncAnthropic()
        self.log = Ed25519AuditLog()

    async def execute_action(self, action: str, params: dict, actor: str = "agent") -> dict:
        self.log.record(action, actor, {"params": params, "status": "started"})
        try:
            # Simulate action
            result = {"output": f"completed {action}"}
            self.log.record(action, actor, {"status": "completed", "result": result})
            return result
        except Exception as e:
            self.log.record(action, actor, {"status": "failed", "error": str(e)})
            raise

    async def chat(self, message: str, actor: str = "user") -> str:
        self.log.record("chat", actor, {"message_length": len(message)})
        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": message}],
        )
        text = response.content[0].text
        self.log.record("chat_response", "agent", {"tokens": response.usage.output_tokens})
        return text
```

## Solution 4: Merkle Tree Audit Log for Efficient Batch Verification

Group entries into Merkle trees; store only the root hash externally. Verify any single entry in O(log n) without reading the full log.

```python
import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


def sha256(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()


def merkle_root(leaves: list[str]) -> str:
    """Compute Merkle root from list of leaf hashes."""
    if not leaves:
        return sha256("empty")
    nodes = list(leaves)
    while len(nodes) > 1:
        if len(nodes) % 2 == 1:
            nodes.append(nodes[-1])  # Duplicate last node if odd
        nodes = [sha256(nodes[i] + nodes[i + 1]) for i in range(0, len(nodes), 2)]
    return nodes[0]


def merkle_proof(leaves: list[str], index: int) -> list[tuple[str, str]]:
    """Generate inclusion proof for leaf at index. Returns [(sibling_hash, direction)]."""
    proof = []
    nodes = list(leaves)
    idx = index
    while len(nodes) > 1:
        if len(nodes) % 2 == 1:
            nodes.append(nodes[-1])
        sibling_idx = idx ^ 1  # XOR to get sibling
        direction = "right" if idx % 2 == 0 else "left"
        proof.append((nodes[sibling_idx], direction))
        nodes = [sha256(nodes[i] + nodes[i + 1]) for i in range(0, len(nodes), 2)]
        idx //= 2
    return proof


def verify_merkle_proof(leaf_hash: str, proof: list[tuple[str, str]], root: str) -> bool:
    current = leaf_hash
    for sibling, direction in proof:
        if direction == "right":
            current = sha256(current + sibling)
        else:
            current = sha256(sibling + current)
    return current == root


@dataclass
class AuditBatch:
    batch_id: str
    entries: list[dict]
    merkle_root: str
    sealed_at: float
    entry_count: int


class MerkleAuditLog:
    def __init__(self, batch_size: int = 16):
        self._pending: list[dict] = []
        self._batches: list[AuditBatch] = []
        self._batch_size = batch_size
        # In production: store roots in an external, write-once store (S3, blockchain)
        self._roots: list[str] = []

    def _entry_hash(self, entry: dict) -> str:
        return sha256(json.dumps(entry, sort_keys=True))

    def record(self, action: str, actor: str, details: dict) -> dict:
        entry = {
            "entry_id": str(uuid.uuid4()),
            "timestamp": time.time(),
            "action": action,
            "actor": actor,
            "details": details,
        }
        self._pending.append(entry)
        if len(self._pending) >= self._batch_size:
            self._seal_batch()
        return entry

    def _seal_batch(self):
        if not self._pending:
            return
        entries = list(self._pending)
        leaf_hashes = [self._entry_hash(e) for e in entries]
        root = merkle_root(leaf_hashes)
        batch = AuditBatch(
            batch_id=str(uuid.uuid4())[:8],
            entries=entries,
            merkle_root=root,
            sealed_at=time.time(),
            entry_count=len(entries),
        )
        self._batches.append(batch)
        self._roots.append(root)
        self._pending.clear()
        print(f"[MERKLE] Sealed batch {batch.batch_id}: {batch.entry_count} entries, root={root[:16]}…")

    def flush(self):
        if self._pending:
            self._seal_batch()

    def verify_batch(self, batch: AuditBatch) -> bool:
        leaf_hashes = [self._entry_hash(e) for e in batch.entries]
        computed_root = merkle_root(leaf_hashes)
        return computed_root == batch.merkle_root

    def generate_proof(self, batch_idx: int, entry_idx: int) -> tuple[str, list, str]:
        batch = self._batches[batch_idx]
        leaf_hashes = [self._entry_hash(e) for e in batch.entries]
        proof = merkle_proof(leaf_hashes, entry_idx)
        return leaf_hashes[entry_idx], proof, batch.merkle_root

    def verify_entry(self, batch_idx: int, entry_idx: int) -> bool:
        leaf_hash, proof, root = self.generate_proof(batch_idx, entry_idx)
        return verify_merkle_proof(leaf_hash, proof, root)

    def integrity_report(self) -> dict:
        self.flush()
        tampered = [
            i for i, batch in enumerate(self._batches)
            if not self.verify_batch(batch)
        ]
        return {
            "total_batches": len(self._batches),
            "total_entries": sum(b.entry_count for b in self._batches),
            "tampered_batches": tampered,
            "all_roots": self._roots,
        }
```

## Solution 5: Write-Once S3-Backed Audit Log with Object Lock

Use S3 Object Lock (WORM) to prevent deletion or modification; each entry is a separate immutable object.

```python
import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from anthropic import AsyncAnthropic

# Requires: boto3, and an S3 bucket with Object Lock enabled
# pip install boto3


@dataclass
class S3AuditConfig:
    bucket: str = "my-audit-logs"
    prefix: str = "agent-audit/"
    retention_days: int = 365


class S3WORMAuditLog:
    """
    Each audit entry is stored as a separate S3 object with
    COMPLIANCE mode Object Lock — cannot be deleted or modified
    by anyone, including root.
    """

    def __init__(self, config: S3AuditConfig):
        try:
            import boto3
            self._s3 = boto3.client("s3")
        except ImportError:
            self._s3 = None
        self.config = config
        self._local_buffer: list[dict] = []  # Fallback if S3 unavailable

    def record(self, action: str, actor: str, details: dict) -> str:
        entry = {
            "entry_id": str(uuid.uuid4()),
            "timestamp": time.time(),
            "action": action,
            "actor": actor,
            "details": details,
        }
        entry_id = entry["entry_id"]

        if self._s3 is None:
            self._local_buffer.append(entry)
            return entry_id

        key = f"{self.config.prefix}{time.strftime('%Y/%m/%d')}/{entry_id}.json"
        from datetime import datetime, timedelta, timezone
        retain_until = datetime.now(timezone.utc) + timedelta(days=self.config.retention_days)
        try:
            self._s3.put_object(
                Bucket=self.config.bucket,
                Key=key,
                Body=json.dumps(entry).encode(),
                ContentType="application/json",
                ObjectLockMode="COMPLIANCE",  # Cannot be overridden even by root
                ObjectLockRetainUntilDate=retain_until,
            )
        except Exception as e:
            # Fallback to local — alert ops team in production
            print(f"[AUDIT] S3 write failed: {e}. Falling back to local buffer.")
            self._local_buffer.append(entry)

        return entry_id

    def verify_entry_exists(self, entry_id: str) -> bool:
        """Check that a specific entry still exists in S3 (wasn't deleted)."""
        if self._s3 is None:
            return any(e["entry_id"] == entry_id for e in self._local_buffer)
        prefix = self.config.prefix
        try:
            response = self._s3.list_objects_v2(
                Bucket=self.config.bucket,
                Prefix=prefix,
            )
            return any(
                obj["Key"].endswith(f"{entry_id}.json")
                for obj in response.get("Contents", [])
            )
        except Exception:
            return False


class S3AuditedAgent:
    def __init__(self, actor: str = "agent"):
        self.client = AsyncAnthropic()
        self.actor = actor
        self.audit = S3WORMAuditLog(S3AuditConfig())

    async def chat(self, message: str) -> str:
        eid = self.audit.record("chat", self.actor, {"message": message[:200]})
        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": message}],
        )
        self.audit.record("chat_response", self.actor, {
            "request_entry_id": eid,
            "tokens": response.usage.output_tokens,
        })
        return response.content[0].text
```

## Solution 6: Dual-Write Audit Log with Cross-Verification

Write each entry to two independent sinks (local file + remote DB); cross-verify periodically to detect tampering in either.

```python
import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from anthropic import AsyncAnthropic


@dataclass
class DualEntry:
    entry_id: str
    timestamp: float
    action: str
    actor: str
    details: dict
    content_hash: str = ""

    def compute_hash(self) -> str:
        payload = json.dumps({
            "entry_id": self.entry_id,
            "timestamp": self.timestamp,
            "action": self.action,
            "actor": self.actor,
            "details": self.details,
        }, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def finalize(self) -> "DualEntry":
        self.content_hash = self.compute_hash()
        return self

    def to_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "timestamp": self.timestamp,
            "action": self.action,
            "actor": self.actor,
            "details": self.details,
            "content_hash": self.content_hash,
        }


class LocalFileSink:
    def __init__(self, path: str = "/tmp/audit_primary.jsonl"):
        self.path = Path(path)

    def write(self, entry: DualEntry):
        with self.path.open("a") as f:
            f.write(json.dumps(entry.to_dict()) + "\n")

    def read_all(self) -> dict[str, str]:
        """Returns {entry_id: content_hash}."""
        if not self.path.exists():
            return {}
        result = {}
        with self.path.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    data = json.loads(line)
                    result[data["entry_id"]] = data["content_hash"]
        return result


class InMemoryDBSink:
    """Simulates a remote DB or secondary log store."""
    def __init__(self):
        self._store: dict[str, str] = {}

    def write(self, entry: DualEntry):
        self._store[entry.entry_id] = entry.content_hash

    def read_all(self) -> dict[str, str]:
        return dict(self._store)


class DualWriteAuditLog:
    def __init__(self):
        self._primary = LocalFileSink()
        self._secondary = InMemoryDBSink()
        self._written_ids: list[str] = []

    def record(self, action: str, actor: str, details: dict) -> DualEntry:
        entry = DualEntry(
            entry_id=str(uuid.uuid4()),
            timestamp=time.time(),
            action=action,
            actor=actor,
            details=details,
        ).finalize()

        self._primary.write(entry)
        self._secondary.write(entry)
        self._written_ids.append(entry.entry_id)
        return entry

    def cross_verify(self) -> dict:
        primary = self._primary.read_all()
        secondary = self._secondary.read_all()

        all_ids = set(primary) | set(secondary)
        discrepancies = []

        for eid in all_ids:
            p_hash = primary.get(eid)
            s_hash = secondary.get(eid)

            if p_hash is None:
                discrepancies.append({
                    "entry_id": eid,
                    "type": "missing_from_primary",
                    "secondary_hash": s_hash,
                })
            elif s_hash is None:
                discrepancies.append({
                    "entry_id": eid,
                    "type": "missing_from_secondary",
                    "primary_hash": p_hash,
                })
            elif p_hash != s_hash:
                discrepancies.append({
                    "entry_id": eid,
                    "type": "hash_mismatch",
                    "primary_hash": p_hash[:16],
                    "secondary_hash": s_hash[:16],
                })

        return {
            "primary_entries": len(primary),
            "secondary_entries": len(secondary),
            "discrepancies": discrepancies,
            "tamper_detected": len(discrepancies) > 0,
        }


class DualWriteAuditedAgent:
    def __init__(self, actor: str = "agent"):
        self.client = AsyncAnthropic()
        self.audit = DualWriteAuditLog()
        self.actor = actor

    async def chat(self, message: str) -> str:
        self.audit.record("chat_request", self.actor, {"length": len(message)})
        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": message}],
        )
        text = response.content[0].text
        self.audit.record("chat_response", self.actor, {"tokens": response.usage.output_tokens})
        return text

    def integrity_check(self) -> bool:
        report = self.audit.cross_verify()
        if report["tamper_detected"]:
            print(f"[DUAL-AUDIT] TAMPER DETECTED: {report['discrepancies']}")
        else:
            print(
                f"[DUAL-AUDIT] Integrity OK: "
                f"{report['primary_entries']} primary / {report['secondary_entries']} secondary entries match"
            )
        return not report["tamper_detected"]


async def demo_dual_write():
    agent = DualWriteAuditedAgent(actor="ops_team")
    await agent.chat("List all user accounts.")
    await agent.chat("Export database backup.")
    agent.integrity_check()
```

## Comparison Table

| Solution | Tamper Detection | Non-Repudiation | Deletion Detection | Efficiency | Best For |
|---|---|---|---|---|---|
| Hash Chain | Modification only | No | Partial (breaks chain) | O(n) verify | Sequential logs with ordering |
| HMAC Signed | Modification + insertion | No (shared key) | No | O(n) verify | Server-side integrity, shared key |
| Ed25519 Signed | Modification + insertion | Yes (asymmetric) | No | O(n) verify | High-security non-repudiation |
| Merkle Tree | Modification | No | Partial (root mismatch) | O(log n) per entry | High-volume logs, efficient proof |
| S3 WORM | All (storage-level) | Storage-backed | Yes (object lock) | O(1) write | Compliance/regulatory requirements |
| Dual-Write | Modification + deletion | No | Yes (cross-verify) | O(n) verify | Defense-in-depth with redundancy |

**Recommended**: Use **Hash Chain** (Solution 1) as a baseline for most agents — it's simple, self-contained, and detects all in-place modifications. Add **HMAC Signing** (Solution 2) when the log file is stored on shared infrastructure. Use **S3 WORM** (Solution 5) for regulated industries (HIPAA, SOC 2) where deletion prevention is a hard requirement. Combine **Dual-Write** (Solution 6) with any of the above for defense-in-depth.
