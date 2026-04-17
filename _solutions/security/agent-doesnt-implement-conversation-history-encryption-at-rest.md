---
title: "Agent Doesn't Implement Conversation History Encryption at Rest"
description: "Agents that persist conversation history to disk or database in plaintext expose sensitive user data if the storage medium is compromised: a database breach, stolen backup, or misconfigured S3 bucket makes all historical conversations readable. Implement conversation history encryption at rest using AES-256-GCM with per-conversation keys derived from a master key, so storage compromise does not expose conversation content."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-conversation-history-encryption-at-rest
tags: [encryption-at-rest, conversation-privacy, aes-gcm, key-derivation, data-protection, storage-security]
symptoms:
  - "Conversation history stored in plaintext JSON files or unencrypted database columns"
  - "Database backup contains readable conversation content"
  - "No distinction between encrypted and unencrypted storage paths"
  - "Encryption key is hardcoded or stored alongside the data it protects"
  - "Compliance audit fails because PII in conversations is not encrypted at rest"
---

## Why This Happens

Encryption at rest is often deferred because it adds complexity: key management, encryption/decryption overhead, and the risk of data loss if keys are lost. Most agents store conversation history as JSON or plain text in the simplest available storage layer. Without a deliberate encryption implementation, all stored conversations are readable by anyone with database access. The minimum viable implementation uses AES-256-GCM (authenticated encryption) with a per-conversation key derived from a master key using HKDF — this protects against storage compromise while keeping key management tractable.

## Solution 1: Encryption Key Derivation

```python
import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class DerivedKey:
    key_bytes: bytes       # 32 bytes for AES-256
    conversation_id: str
    key_version: int
    salt: bytes


class ConversationKeyDerivation:
    """
    Derives per-conversation encryption keys from a master key using HKDF.
    Each conversation gets a unique key so compromise of one key
    does not expose other conversations.
    """

    KEY_LENGTH = 32    # AES-256
    HASH_ALGO = "sha256"

    def __init__(self, master_key: bytes, key_version: int = 1):
        if len(master_key) < 32:
            raise ValueError("master_key must be at least 32 bytes")
        self._master_key = master_key
        self._key_version = key_version

    def derive(self, conversation_id: str, salt: Optional[bytes] = None) -> DerivedKey:
        if salt is None:
            salt = os.urandom(16)

        # HKDF Extract
        prk = hmac.new(salt, self._master_key, hashlib.sha256).digest()

        # HKDF Expand
        info = f"conversation:{conversation_id}:v{self._key_version}".encode()
        okm = hmac.new(prk, info + b"\x01", hashlib.sha256).digest()

        return DerivedKey(
            key_bytes=okm[:self.KEY_LENGTH],
            conversation_id=conversation_id,
            key_version=self._key_version,
            salt=salt,
        )

    @classmethod
    def generate_master_key(cls) -> bytes:
        return os.urandom(32)
```

## Solution 2: AES-GCM Encryptor

```python
import os
import struct
from typing import Tuple


class AESGCMEncryptor:
    """
    Encrypts and decrypts data using AES-256-GCM.
    Uses Python's cryptography library for authenticated encryption.
    Each encryption produces a unique nonce — never reuse a nonce with the same key.
    """

    NONCE_LENGTH = 12   # 96-bit nonce recommended for GCM
    TAG_LENGTH = 16     # 128-bit authentication tag

    def encrypt(self, plaintext: bytes, key: bytes, associated_data: bytes = b"") -> bytes:
        """Returns nonce + ciphertext + tag as a single byte string."""
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            nonce = os.urandom(self.NONCE_LENGTH)
            aesgcm = AESGCM(key)
            ciphertext_and_tag = aesgcm.encrypt(nonce, plaintext, associated_data or None)
            return nonce + ciphertext_and_tag
        except ImportError:
            # Fallback: pure Python XOR-based for testing only (NOT SECURE for production)
            raise ImportError("cryptography package required: pip install cryptography")

    def decrypt(self, ciphertext_blob: bytes, key: bytes, associated_data: bytes = b"") -> bytes:
        """Decrypts nonce + ciphertext + tag blob."""
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            nonce = ciphertext_blob[:self.NONCE_LENGTH]
            ciphertext_and_tag = ciphertext_blob[self.NONCE_LENGTH:]
            aesgcm = AESGCM(key)
            return aesgcm.decrypt(nonce, ciphertext_and_tag, associated_data or None)
        except ImportError:
            raise ImportError("cryptography package required: pip install cryptography")

    def encrypt_str(self, plaintext: str, key: bytes, aad: str = "") -> bytes:
        return self.encrypt(plaintext.encode("utf-8"), key, aad.encode("utf-8"))

    def decrypt_str(self, ciphertext_blob: bytes, key: bytes, aad: str = "") -> str:
        return self.decrypt(ciphertext_blob, key, aad.encode("utf-8")).decode("utf-8")
```

## Solution 3: Encrypted Conversation Store

```python
import base64
import json
import time
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional


class EncryptedConversationStore:
    """
    Stores conversation history encrypted at rest.
    Each conversation is encrypted with a unique derived key.
    Key derivation parameters (salt, version) are stored alongside
    the ciphertext — the master key itself is never stored.
    """

    def __init__(
        self,
        storage_path: str,
        key_derivation: ConversationKeyDerivation,
        encryptor: AESGCMEncryptor,
    ):
        self._path = Path(storage_path)
        self._path.mkdir(parents=True, exist_ok=True)
        self._kdf = key_derivation
        self._enc = encryptor
        self._lock = Lock()

    def _conv_path(self, conversation_id: str) -> Path:
        safe_id = conversation_id.replace("/", "_").replace("..", "_")
        return self._path / f"{safe_id}.enc"

    def save(self, conversation_id: str, messages: List[Dict[str, Any]]) -> None:
        derived_key = self._kdf.derive(conversation_id)
        plaintext = json.dumps(messages, ensure_ascii=False)
        aad = f"conv:{conversation_id}:v{derived_key.key_version}"
        ciphertext = self._enc.encrypt_str(plaintext, derived_key.key_bytes, aad)

        envelope = {
            "version": derived_key.key_version,
            "salt": base64.b64encode(derived_key.salt).decode(),
            "ciphertext": base64.b64encode(ciphertext).decode(),
            "saved_at": time.time(),
        }

        with self._lock:
            self._conv_path(conversation_id).write_text(json.dumps(envelope))

    def load(self, conversation_id: str) -> Optional[List[Dict[str, Any]]]:
        path = self._conv_path(conversation_id)
        if not path.exists():
            return None

        with self._lock:
            envelope = json.loads(path.read_text())

        salt = base64.b64decode(envelope["salt"])
        ciphertext = base64.b64decode(envelope["ciphertext"])
        version = envelope.get("version", 1)

        derived_key = self._kdf.derive(conversation_id, salt=salt)
        aad = f"conv:{conversation_id}:v{version}"
        plaintext = self._enc.decrypt_str(ciphertext, derived_key.key_bytes, aad)
        return json.loads(plaintext)

    def delete(self, conversation_id: str) -> None:
        with self._lock:
            path = self._conv_path(conversation_id)
            if path.exists():
                path.unlink()

    def exists(self, conversation_id: str) -> bool:
        return self._conv_path(conversation_id).exists()
```

## Solution 4: Key Rotation Manager

```python
import time
from typing import List


class ConversationKeyRotationManager:
    """
    Re-encrypts stored conversations under a new master key version.
    Run during key rotation events to ensure all data uses the current key.
    """

    def __init__(
        self,
        store: EncryptedConversationStore,
        old_kdf: ConversationKeyDerivation,
        new_kdf: ConversationKeyDerivation,
    ):
        self._store = store
        self._old_kdf = old_kdf
        self._new_kdf = new_kdf

    def rotate_conversation(self, conversation_id: str) -> bool:
        # Temporarily swap to old kdf to load
        original_kdf = self._store._kdf
        self._store._kdf = self._old_kdf
        messages = self._store.load(conversation_id)
        if messages is None:
            self._store._kdf = original_kdf
            return False

        # Re-encrypt with new kdf
        self._store._kdf = self._new_kdf
        self._store.save(conversation_id, messages)
        self._store._kdf = original_kdf
        return True

    def rotate_all(self, conversation_ids: List[str]) -> dict:
        results = {"rotated": 0, "failed": 0, "not_found": 0}
        for conv_id in conversation_ids:
            if not self._store.exists(conv_id):
                results["not_found"] += 1
                continue
            try:
                if self.rotate_conversation(conv_id):
                    results["rotated"] += 1
                else:
                    results["not_found"] += 1
            except Exception:
                results["failed"] += 1
        return results
```

## Solution 5: Encryption Audit Logger

```python
import time
from typing import List


class EncryptionAuditLogger:
    """
    Records encryption and decryption events for compliance audit trails.
    Does not log conversation content — only operation metadata.
    """

    def __init__(self, max_records: int = 100_000):
        self._records: List[dict] = []
        self._max = max_records
        self._encrypt_count = 0
        self._decrypt_count = 0

    def record_encrypt(self, conversation_id: str, key_version: int) -> None:
        self._encrypt_count += 1
        self._append({
            "op": "encrypt",
            "conversation_id_hash": self._hash(conversation_id),
            "key_version": key_version,
            "ts": time.time(),
        })

    def record_decrypt(self, conversation_id: str, key_version: int) -> None:
        self._decrypt_count += 1
        self._append({
            "op": "decrypt",
            "conversation_id_hash": self._hash(conversation_id),
            "key_version": key_version,
            "ts": time.time(),
        })

    def _append(self, record: dict) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append(record)

    @staticmethod
    def _hash(value: str) -> str:
        import hashlib
        return hashlib.sha256(value.encode()).hexdigest()[:16]

    def summary(self, window_seconds: float = 86400.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        return {
            "window_seconds": window_seconds,
            "encrypt_ops": sum(1 for r in recent if r["op"] == "encrypt"),
            "decrypt_ops": sum(1 for r in recent if r["op"] == "decrypt"),
            "total_encrypt_ever": self._encrypt_count,
            "total_decrypt_ever": self._decrypt_count,
        }
```

## Solution 6: Encryption Health Dashboard

```python
import time


class ConversationEncryptionDashboard:
    """
    Combines encryption audit stats and key version tracking
    into a compliance and operational health view.
    """

    def __init__(
        self,
        audit_logger: EncryptionAuditLogger,
        current_key_version: int,
    ):
        self._audit = audit_logger
        self._key_version = current_key_version

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "current_key_version": self._key_version,
            "audit_24h": self._audit.summary(window_seconds=86400.0),
            "encryption_enabled": True,
            "algorithm": "AES-256-GCM",
            "key_derivation": "HKDF-SHA256 per-conversation",
        }
```

## Comparison

| Approach | Per-Conv Key | AES-GCM | Persistent Storage | Key Rotation | Audit |
|---|---|---|---|---|---|
| ConversationKeyDerivation | Yes (HKDF) | No | No | Via key_version | No |
| AESGCMEncryptor | No | Yes | No | No | No |
| EncryptedConversationStore | Via derivation | Via encryptor | Yes | No | No |
| ConversationKeyRotationManager | No | No | Via store | Yes | No |
| EncryptionAuditLogger | No | No | No | No | Yes |
| ConversationEncryptionDashboard | No | No | No | No | Yes (combined) |

**Best for production**: Store the master key in a secrets manager (AWS Secrets Manager, HashiCorp Vault, GCP Secret Manager) — never hardcode it or store it in the same location as the encrypted data. Use `key_version` in the AAD (additional authenticated data) so that an attacker cannot re-use ciphertext from one key version with a different key. Run `ConversationKeyRotationManager.rotate_all()` when rotating the master key — without re-encryption, old conversations remain readable only with the old key. Store the salt alongside each ciphertext (not secretly) — the salt's purpose is uniqueness, not secrecy, and it must be stored to allow re-derivation of the same key for decryption.
