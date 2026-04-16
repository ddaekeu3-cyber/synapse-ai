---
title: "Agent Doesn't Implement Encrypted-at-Rest Storage for Agent Memory"
description: "Agents that store conversation history, user preferences, and extracted PII in plain text databases and vector stores expose sensitive data to anyone with storage access. Implement application-level encryption at rest so memory contents are unreadable without the correct key, even to database administrators."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-encrypted-at-rest-storage-for-agent-memory
tags: [encryption-at-rest, aes-gcm, memory-security, privacy, key-management, security]
symptoms:
  - "DBA can read all user conversations by querying the messages table directly"
  - "Exported database backup contains PII in plain text"
  - "Vector store embeddings can be reversed to reconstruct user queries"
  - "No encryption layer between application and storage — relies solely on disk encryption"
  - "Compliance audit flags unencrypted PII in database at rest"
---

## Why This Happens

Most deployments rely on disk-level encryption (EBS encryption, database TDE) and assume that access control handles the rest. But application-level encryption provides a defense-in-depth layer: even if a database is breached, exfiltrated backup is analyzed, or an over-privileged admin queries the table, the data remains ciphertext without the application keys. Agent memory is particularly sensitive because it contains user queries, preferences, and extracted personal details that accumulate over sessions.

## Solution 1: AES-GCM Field-Level Encryption

```python
import base64
import os
import secrets
from dataclasses import dataclass
from typing import Any, Optional

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False

@dataclass
class EncryptedField:
    ciphertext: bytes
    nonce: bytes
    key_version: str

class FieldEncryptor:
    """
    AES-256-GCM field-level encryption for agent memory fields.
    Each encrypted value includes the nonce and key version for rotation support.
    """

    NONCE_SIZE = 12  # 96 bits, standard for AES-GCM

    def __init__(self, key: bytes, key_version: str = "v1"):
        if len(key) != 32:
            raise ValueError("AES-256 requires a 32-byte key")
        self._gcm = AESGCM(key)
        self._key_version = key_version

    @classmethod
    def generate_key(cls) -> bytes:
        return secrets.token_bytes(32)

    def encrypt(self, plaintext: str, associated_data: Optional[bytes] = None) -> EncryptedField:
        """Encrypt a string field. Returns EncryptedField with ciphertext + nonce."""
        nonce = os.urandom(self.NONCE_SIZE)
        data = plaintext.encode("utf-8")
        ciphertext = self._gcm.encrypt(nonce, data, associated_data)
        return EncryptedField(
            ciphertext=ciphertext,
            nonce=nonce,
            key_version=self._key_version,
        )

    def decrypt(self, field: EncryptedField, associated_data: Optional[bytes] = None) -> str:
        plaintext_bytes = self._gcm.decrypt(field.nonce, field.ciphertext, associated_data)
        return plaintext_bytes.decode("utf-8")

    def encrypt_to_b64(self, plaintext: str, aad: Optional[bytes] = None) -> str:
        """Serialize to a single base64 string for DB storage."""
        ef = self.encrypt(plaintext, aad)
        payload = ef.key_version.encode() + b":" + ef.nonce + ef.ciphertext
        return base64.urlsafe_b64encode(payload).decode()

    def decrypt_from_b64(self, b64_value: str, aad: Optional[bytes] = None) -> str:
        payload = base64.urlsafe_b64decode(b64_value.encode())
        version, rest = payload.split(b":", 1)
        nonce = rest[:self.NONCE_SIZE]
        ciphertext = rest[self.NONCE_SIZE:]
        ef = EncryptedField(ciphertext=ciphertext, nonce=nonce, key_version=version.decode())
        return self.decrypt(ef, aad)
```

## Solution 2: Per-User Key Derivation for Tenant Isolation

```python
import hashlib
import hmac
import secrets
from typing import Optional

class PerUserKeyDeriver:
    """
    Derives unique encryption keys per user from a master key using HKDF.
    Ensures that user A's data cannot be decrypted with user B's derived key,
    even if both keys come from the same master.
    """

    def __init__(self, master_key: bytes):
        if len(master_key) < 32:
            raise ValueError("Master key must be at least 32 bytes")
        self._master = master_key

    def derive_key(self, user_id: str, purpose: str = "memory") -> bytes:
        """
        HKDF-like key derivation: HMAC-SHA256(master_key, user_id || purpose)
        Returns a 32-byte key suitable for AES-256-GCM.
        """
        info = f"{user_id}:{purpose}".encode("utf-8")
        derived = hmac.new(self._master, info, hashlib.sha256).digest()
        return derived

    def get_encryptor(self, user_id: str, purpose: str = "memory") -> "FieldEncryptor":
        key = self.derive_key(user_id, purpose)
        return FieldEncryptor(key, key_version=f"master_v1:{user_id[:8]}")


class UserKeyStore:
    """
    Stores encrypted per-user key material in a secrets vault.
    Supports key rotation without re-encrypting all data at once.
    """

    def __init__(self, vault, master_deriver: PerUserKeyDeriver):
        self._vault = vault
        self._deriver = master_deriver

    async def get_encryptor(self, user_id: str) -> FieldEncryptor:
        # Try to load user-specific key from vault (allows independent rotation)
        stored_key = await self._vault.get(f"user_enc_key:{user_id}")
        if stored_key:
            return FieldEncryptor(stored_key, key_version="user_v1")
        # Fall back to master-derived key
        return self._deriver.get_encryptor(user_id)

    async def rotate_user_key(self, user_id: str) -> bytes:
        new_key = FieldEncryptor.generate_key()
        await self._vault.set(f"user_enc_key:{user_id}", new_key)
        return new_key
```

## Solution 3: Encrypted Memory Store Wrapper

```python
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

@dataclass
class EncryptedMemoryRecord:
    record_id: str
    user_id: str
    encrypted_content: str   # base64-encoded ciphertext
    content_type: str        # "message" | "embedding_text" | "profile"
    created_at: float = field(default_factory=time.time)
    key_version: str = "v1"

class EncryptedMemoryStore:
    """
    Wraps a plaintext DB with transparent encryption/decryption.
    Application reads/writes plaintext; only ciphertext reaches the DB.
    """

    def __init__(self, db, key_store: UserKeyStore):
        self._db = db
        self._keys = key_store

    async def save_message(self, user_id: str, message: dict) -> str:
        encryptor = await self._keys.get_encryptor(user_id)
        # Use user_id as associated data to bind ciphertext to this user
        aad = user_id.encode()
        encrypted = encryptor.encrypt_to_b64(json.dumps(message), aad=aad)

        import uuid
        record_id = str(uuid.uuid4())
        await self._db.execute(
            "INSERT INTO encrypted_memory (record_id, user_id, content, content_type, created_at) "
            "VALUES ($1, $2, $3, $4, $5)",
            record_id, user_id, encrypted, "message", time.time(),
        )
        return record_id

    async def load_messages(
        self, user_id: str, limit: int = 50
    ) -> List[dict]:
        rows = await self._db.fetch(
            "SELECT content FROM encrypted_memory "
            "WHERE user_id = $1 AND content_type = 'message' "
            "ORDER BY created_at DESC LIMIT $2",
            user_id, limit,
        )
        encryptor = await self._keys.get_encryptor(user_id)
        aad = user_id.encode()
        messages = []
        for row in rows:
            try:
                plaintext = encryptor.decrypt_from_b64(row["content"], aad=aad)
                messages.append(json.loads(plaintext))
            except Exception as exc:
                print(f"[encrypted_memory] failed to decrypt record for user={user_id}: {exc}")
        return list(reversed(messages))

    async def delete_user_data(self, user_id: str) -> int:
        """GDPR erasure: delete all encrypted records for user."""
        result = await self._db.execute(
            "DELETE FROM encrypted_memory WHERE user_id = $1", user_id
        )
        return int(str(result).split()[-1])
```

## Solution 4: Encrypted Embedding Text Store

```python
import json
import hashlib
from typing import List, Optional, Tuple

class EncryptedEmbeddingStore:
    """
    Stores embedding source texts encrypted; stores only raw float vectors unencrypted.
    Vectors alone cannot reconstruct queries; source texts are protected.
    """

    def __init__(self, db, vector_backend, key_store: UserKeyStore):
        self._db = db
        self._vectors = vector_backend
        self._keys = key_store

    async def upsert(
        self,
        user_id: str,
        doc_id: str,
        source_text: str,
        embedding: List[float],
        metadata: dict,
    ) -> None:
        # Store embedding vector in vector DB (not sensitive on its own)
        safe_metadata = {k: v for k, v in metadata.items() if k != "source_text"}
        safe_metadata["user_id"] = user_id
        safe_metadata["has_encrypted_source"] = True
        await self._vectors.upsert(id=doc_id, values=embedding, metadata=safe_metadata)

        # Store encrypted source text in relational DB
        encryptor = await self._keys.get_encryptor(user_id, purpose="embeddings")
        aad = f"{user_id}:{doc_id}".encode()
        encrypted_text = encryptor.encrypt_to_b64(source_text, aad=aad)

        await self._db.execute(
            "INSERT INTO encrypted_embedding_sources (doc_id, user_id, encrypted_text) "
            "VALUES ($1, $2, $3) ON CONFLICT (doc_id) DO UPDATE SET encrypted_text = $3",
            doc_id, user_id, encrypted_text,
        )

    async def get_source_text(self, user_id: str, doc_id: str) -> Optional[str]:
        row = await self._db.fetchrow(
            "SELECT encrypted_text FROM encrypted_embedding_sources "
            "WHERE doc_id = $1 AND user_id = $2",
            doc_id, user_id,
        )
        if not row:
            return None
        encryptor = await self._keys.get_encryptor(user_id, purpose="embeddings")
        aad = f"{user_id}:{doc_id}".encode()
        return encryptor.decrypt_from_b64(row["encrypted_text"], aad=aad)
```

## Solution 5: Key Rotation Pipeline

```python
import asyncio
import time
from typing import List

class KeyRotationPipeline:
    """
    Rotates encryption keys by re-encrypting all records for affected users.
    Processes in small batches to avoid locking the DB or spiking memory.
    """

    def __init__(
        self,
        db,
        old_key_store: UserKeyStore,
        new_key_store: UserKeyStore,
        batch_size: int = 100,
    ):
        self._db = db
        self._old = old_key_store
        self._new = new_key_store
        self._batch_size = batch_size

    async def rotate_user(self, user_id: str) -> dict:
        rows = await self._db.fetch(
            "SELECT record_id, content FROM encrypted_memory WHERE user_id = $1", user_id
        )
        rotated = 0
        errors = 0
        old_enc = await self._old.get_encryptor(user_id)
        new_enc = await self._new.get_encryptor(user_id)
        aad = user_id.encode()

        for row in rows:
            try:
                plaintext = old_enc.decrypt_from_b64(row["content"], aad=aad)
                re_encrypted = new_enc.encrypt_to_b64(plaintext, aad=aad)
                await self._db.execute(
                    "UPDATE encrypted_memory SET content = $1 WHERE record_id = $2",
                    re_encrypted, row["record_id"],
                )
                rotated += 1
            except Exception as exc:
                errors += 1
                print(f"[key_rotation] error on record {row['record_id']}: {exc}")

        return {"user_id": user_id, "rotated": rotated, "errors": errors}

    async def rotate_all_users(self) -> dict:
        users = await self._db.fetch(
            "SELECT DISTINCT user_id FROM encrypted_memory"
        )
        results = []
        for i in range(0, len(users), self._batch_size):
            batch = users[i:i + self._batch_size]
            batch_results = await asyncio.gather(
                *[self.rotate_user(r["user_id"]) for r in batch],
                return_exceptions=True,
            )
            results.extend(batch_results)
            await asyncio.sleep(0.1)  # yield between batches

        total_rotated = sum(r.get("rotated", 0) for r in results if isinstance(r, dict))
        total_errors = sum(r.get("errors", 0) for r in results if isinstance(r, dict))
        return {"total_users": len(users), "total_rotated": total_rotated, "total_errors": total_errors}
```

## Solution 6: Encryption Health Monitor

```python
import asyncio
import time

class EncryptionHealthMonitor:
    """
    Audits the encrypted memory store to detect unencrypted records
    (e.g., from code paths that bypass the EncryptedMemoryStore wrapper).
    """

    def __init__(self, db):
        self._db = db

    def _looks_encrypted(self, value: str) -> bool:
        """Check if a value looks like a base64 AES-GCM ciphertext."""
        import re
        # Our format: "v1:base64data" where base64 is URL-safe
        return bool(re.match(r'^[a-zA-Z0-9_-]+:[A-Za-z0-9_-]+=*$', value))

    async def audit_table(self, table: str, content_col: str = "content", sample_size: int = 1000) -> dict:
        rows = await self._db.fetch(
            f"SELECT {content_col} FROM {table} ORDER BY RANDOM() LIMIT $1", sample_size
        )
        unencrypted = [r[content_col] for r in rows if not self._looks_encrypted(r[content_col])]
        return {
            "table": table,
            "sampled": len(rows),
            "unencrypted_count": len(unencrypted),
            "unencrypted_rate": len(unencrypted) / max(len(rows), 1),
            "healthy": len(unencrypted) == 0,
            "sample_unencrypted_previews": [v[:50] for v in unencrypted[:3]],
        }

    async def run_audit(self) -> dict:
        tables = [
            ("encrypted_memory", "content"),
            ("encrypted_embedding_sources", "encrypted_text"),
        ]
        results = await asyncio.gather(*[self.audit_table(t, c) for t, c in tables])
        all_healthy = all(r["healthy"] for r in results)
        return {"healthy": all_healthy, "tables": results, "audited_at": time.time()}
```

## Comparison

| Approach | Encryption Scope | Key Isolation | Rotation Support | Admin Blindness |
|---|---|---|---|---|
| FieldEncryptor (AES-GCM) | Per-field | No (shared key) | Manual | Yes |
| PerUserKeyDeriver | Per-user | Yes (derived) | Master rotation only | Yes |
| EncryptedMemoryStore | Full message store | Via UserKeyStore | Via KeyRotationPipeline | Yes |
| EncryptedEmbeddingStore | Source texts only | Per-user+purpose | Via rotation | Yes |
| KeyRotationPipeline | All records | Per-user | Full automated | Yes |
| EncryptionHealthMonitor | Audit layer | N/A | N/A | N/A |

**Best for production**: Use `PerUserKeyDeriver` to derive per-user keys from a master key stored in a secrets vault (AWS KMS, HashiCorp Vault). Wrap all DB writes through `EncryptedMemoryStore`. Use `EncryptedEmbeddingStore` to protect source texts while keeping vectors queryable. Run `KeyRotationPipeline` annually or on compromise. Monitor with `EncryptionHealthMonitor` in CI to catch any code paths that bypass encryption.
