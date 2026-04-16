---
title: "Agent Doesn't Implement Key Derivation for Per-User Encryption"
description: "AI agents that encrypt user data with a single shared key expose all users if that key is compromised. HKDF-based key derivation generates a unique encryption key for each user from a single master key and the user's identity, so a compromised user key only affects that user. Rotating the master key invalidates all derived keys simultaneously, and re-keying one user is independent of all others."
date: 2025-02-15
difficulty: advanced
category: security
slug: agent-doesnt-implement-key-derivation-for-per-user-encryption
tags:
  - hkdf
  - key-derivation
  - encryption
  - per-user
  - aes-gcm
  - cryptography
  - security
symptoms:
  - "All user conversation histories are encrypted with the same AES key"
  - "A single leaked encryption key exposes every user's stored data"
  - "No way to revoke access for one user without re-keying all users"
  - "Agent stores the encryption key in an environment variable accessible to all processes"
  - "User data encryption does not incorporate the user's identity as a key differentiator"
---

## Problem

Encrypting all user data with one shared AES key is equivalent to one lock protecting every apartment in a building with the same physical key. HKDF (HMAC-based Key Derivation Function, RFC 5869) derives distinct cryptographic keys from a shared master key and a per-user context string. Each derived key is unique to that user; compromise of one derived key reveals nothing about the master key or other users' keys. The master key itself lives only in a secrets manager (AWS KMS, HashiCorp Vault), never in application memory for more than one request.

---

## Solution 1: HKDFKeyDeriver — Per-User Key Derivation

```python
import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Optional

try:
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.backends import default_backend
    _CRYPTO = True
except ImportError:
    _CRYPTO = False


@dataclass
class DerivedKey:
    key_bytes: bytes          # 32-byte AES-256 key
    user_id: str
    purpose: str
    key_id: str               # hash of (user_id, purpose) for key lookup


class HKDFKeyDeriver:
    """
    Derives per-user, per-purpose AES-256 keys from a master key using HKDF.
    The same (master_key, user_id, purpose) triple always yields the same
    derived key — deterministic, no need to store derived keys.

    Usage:
        deriver = HKDFKeyDeriver(master_key=os.environ["MASTER_KEY_HEX"])

        user_key = deriver.derive(user_id="u-abc123", purpose="conversation_store")
        cipher = AESGCMCipher(user_key)
        ct = cipher.encrypt(plaintext)
    """

    def __init__(self, master_key: Optional[bytes] = None,
                 master_key_hex: Optional[str] = None):
        if not _CRYPTO:
            raise RuntimeError("pip install cryptography")
        if master_key_hex:
            master_key = bytes.fromhex(master_key_hex)
        if not master_key or len(master_key) < 32:
            raise ValueError("master_key must be at least 32 bytes")
        self._master = master_key

    def derive(self, user_id: str, purpose: str = "default",
                key_length: int = 32) -> DerivedKey:
        info = f"{user_id}:{purpose}".encode("utf-8")
        salt = hashlib.sha256(user_id.encode()).digest()

        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=key_length,
            salt=salt,
            info=info,
            backend=default_backend(),
        )
        key_bytes = hkdf.derive(self._master)
        key_id = hashlib.sha256(info).hexdigest()[:16]

        return DerivedKey(
            key_bytes=key_bytes,
            user_id=user_id,
            purpose=purpose,
            key_id=key_id,
        )

    @staticmethod
    def generate_master_key() -> str:
        """Generate a cryptographically secure 32-byte master key (hex)."""
        return os.urandom(32).hex()
```

---

## Solution 2: AESGCMCipher — Authenticated Encryption with Derived Keys

```python
import base64
import os
import struct
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class EncryptedBlob:
    ciphertext: bytes
    nonce: bytes           # 12-byte GCM nonce
    key_id: str            # identifies which derived key was used
    encrypted_at: float


class AESGCMCipher:
    """
    AES-256-GCM encryption/decryption using a DerivedKey.
    Produces authenticated ciphertexts — decryption verifies integrity.

    Usage:
        deriver = HKDFKeyDeriver(master_key=master_bytes)
        dk = deriver.derive(user_id="u-abc123", purpose="memory_store")
        cipher = AESGCMCipher(dk)

        blob = cipher.encrypt(b"sensitive conversation data")
        plaintext = cipher.decrypt(blob)
    """

    def __init__(self, derived_key: DerivedKey):
        if not _CRYPTO:
            raise RuntimeError("pip install cryptography")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        self._gcm = AESGCM(derived_key.key_bytes)
        self._key_id = derived_key.key_id

    def encrypt(self, plaintext: bytes,
                 associated_data: Optional[bytes] = None) -> EncryptedBlob:
        nonce = os.urandom(12)
        ciphertext = self._gcm.encrypt(nonce, plaintext, associated_data)
        return EncryptedBlob(
            ciphertext=ciphertext,
            nonce=nonce,
            key_id=self._key_id,
            encrypted_at=time.time(),
        )

    def decrypt(self, blob: EncryptedBlob,
                 associated_data: Optional[bytes] = None) -> bytes:
        return self._gcm.decrypt(blob.nonce, blob.ciphertext, associated_data)

    def encrypt_str(self, text: str) -> str:
        """Encrypt a string and return base64-encoded ciphertext:nonce:key_id."""
        blob = self.encrypt(text.encode("utf-8"))
        parts = [
            base64.b64encode(blob.ciphertext).decode(),
            base64.b64encode(blob.nonce).decode(),
            blob.key_id,
        ]
        return ":".join(parts)

    def decrypt_str(self, encoded: str) -> str:
        parts = encoded.split(":")
        ciphertext = base64.b64decode(parts[0])
        nonce = base64.b64decode(parts[1])
        blob = EncryptedBlob(ciphertext=ciphertext, nonce=nonce,
                              key_id=parts[2], encrypted_at=0)
        return self.decrypt(blob).decode("utf-8")
```

---

## Solution 3: PerUserEncryptionStore — Transparent Encryption for Agent Memory

```python
import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class PerUserEncryptionStore:
    """
    Wraps a key-value store (Redis, DynamoDB, Postgres) with per-user
    AES-GCM encryption. Every value stored for a user is encrypted with
    that user's derived key; the store never sees plaintext.

    Usage:
        store = PerUserEncryptionStore(
            backend=redis_client,
            deriver=HKDFKeyDeriver(master_key=MASTER_KEY),
            purpose="agent_memory",
        )

        await store.set("u-123", "conversation_history", {"messages": [...]})
        data = await store.get("u-123", "conversation_history")
    """

    def __init__(self, backend, deriver: HKDFKeyDeriver,
                 purpose: str = "agent_memory"):
        self._backend = backend
        self._deriver = deriver
        self._purpose = purpose
        self._cipher_cache: Dict[str, AESGCMCipher] = {}

    def _cipher_for(self, user_id: str) -> AESGCMCipher:
        if user_id not in self._cipher_cache:
            dk = self._deriver.derive(user_id=user_id, purpose=self._purpose)
            self._cipher_cache[user_id] = AESGCMCipher(dk)
        return self._cipher_cache[user_id]

    async def set(self, user_id: str, field: str, value: Any,
                   ttl_s: Optional[int] = None):
        cipher = self._cipher_for(user_id)
        plaintext = json.dumps(value, separators=(",", ":")).encode()
        encrypted = cipher.encrypt_str(plaintext.decode())
        redis_key = f"user:{user_id}:{field}"
        if ttl_s:
            await self._backend.setex(redis_key, ttl_s, encrypted)
        else:
            await self._backend.set(redis_key, encrypted)

    async def get(self, user_id: str, field: str) -> Optional[Any]:
        redis_key = f"user:{user_id}:{field}"
        raw = await self._backend.get(redis_key)
        if raw is None:
            return None
        cipher = self._cipher_for(user_id)
        try:
            plaintext = cipher.decrypt_str(
                raw.decode() if isinstance(raw, bytes) else raw
            )
            return json.loads(plaintext)
        except Exception as exc:
            logger.error("decrypt_failed user=%s field=%s error=%s",
                          user_id, field, exc)
            return None

    async def delete(self, user_id: str, field: str):
        await self._backend.delete(f"user:{user_id}:{field}")

    def evict_cipher_cache(self, user_id: str):
        """Clear cached cipher for a user (e.g., after key rotation)."""
        self._cipher_cache.pop(user_id, None)
```

---

## Solution 4: MasterKeyRotationManager — Re-key Without Downtime

```python
import asyncio
import logging
import time
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)


class MasterKeyRotationManager:
    """
    Manages master key rotation by maintaining current and previous keys.
    During rotation, decryption tries the new key first and falls back
    to the old key. Re-encryption of existing data runs in the background.

    Usage:
        mgr = MasterKeyRotationManager(
            current_key_hex=os.environ["MASTER_KEY_NEW"],
            previous_key_hex=os.environ["MASTER_KEY_OLD"],
        )
        # Use mgr.derive() everywhere instead of a single HKDFKeyDeriver.
        dk = mgr.derive("u-123", "memory")
    """

    def __init__(self, current_key_hex: str,
                 previous_key_hex: Optional[str] = None):
        self._current = HKDFKeyDeriver(master_key_hex=current_key_hex)
        self._previous = (
            HKDFKeyDeriver(master_key_hex=previous_key_hex)
            if previous_key_hex else None
        )

    def derive(self, user_id: str, purpose: str = "default") -> DerivedKey:
        return self._current.derive(user_id, purpose)

    def derive_previous(self, user_id: str, purpose: str = "default") -> Optional[DerivedKey]:
        if self._previous:
            return self._previous.derive(user_id, purpose)
        return None

    def decrypt_with_fallback(self, blob: "EncryptedBlob",
                               user_id: str, purpose: str = "default") -> bytes:
        """Try current key first; fall back to previous key."""
        try:
            dk = self._current.derive(user_id, purpose)
            cipher = AESGCMCipher(dk)
            return cipher.decrypt(blob)
        except Exception:
            if self._previous:
                dk_old = self._previous.derive(user_id, purpose)
                cipher_old = AESGCMCipher(dk_old)
                return cipher_old.decrypt(blob)
            raise

    async def reencrypt_user_data(self, user_id: str, purpose: str,
                                    fetch_fn: Callable, store_fn: Callable):
        """Re-encrypt a user's data from old key to new key."""
        if not self._previous:
            return
        old_dk = self._previous.derive(user_id, purpose)
        new_dk = self._current.derive(user_id, purpose)
        old_cipher = AESGCMCipher(old_dk)
        new_cipher = AESGCMCipher(new_dk)

        blobs = await fetch_fn(user_id)
        for key, encrypted in blobs.items():
            try:
                plaintext = old_cipher.decrypt(encrypted)
                new_blob = new_cipher.encrypt(plaintext)
                await store_fn(user_id, key, new_blob)
                logger.info("reencrypted user=%s key=%s", user_id, key)
            except Exception as exc:
                logger.error("reencrypt_failed user=%s key=%s error=%s",
                              user_id, key, exc)
```

---

## Solution 5: KeyDerivationAuditLog — Track Key Usage for Compliance

```python
import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class KeyDerivationAuditLog:
    """
    Records every key derivation event with purpose and timestamp.
    Used for compliance audits: proves which user keys were active
    during a given time window without logging key material.

    Usage:
        audit = KeyDerivationAuditLog(log_path="key-audit.jsonl")
        deriver = AuditedKeyDeriver(
            HKDFKeyDeriver(master_key=mk), audit
        )
        dk = deriver.derive("u-123", "memory_store")
    """

    def __init__(self, log_path: Optional[str] = None):
        self._path = log_path
        self._records: List[Dict[str, Any]] = []

    def record(self, user_id: str, purpose: str, key_id: str):
        entry = {
            "ts": time.time(),
            "user_hash": hashlib.sha256(user_id.encode()).hexdigest()[:12],
            "purpose": purpose,
            "key_id": key_id,
        }
        self._records.append(entry)
        if self._path:
            with open(self._path, "a") as f:
                f.write(json.dumps(entry) + "\n")

    def recent(self, n: int = 100) -> List[Dict]:
        return self._records[-n:]

    def key_usage_summary(self) -> Dict[str, int]:
        from collections import Counter
        return dict(Counter(r["purpose"] for r in self._records))


class AuditedKeyDeriver:
    def __init__(self, deriver: HKDFKeyDeriver,
                  audit: KeyDerivationAuditLog):
        self._deriver = deriver
        self._audit = audit

    def derive(self, user_id: str, purpose: str = "default") -> DerivedKey:
        dk = self._deriver.derive(user_id, purpose)
        self._audit.record(user_id, purpose, dk.key_id)
        return dk
```

---

## Solution 6: SecureAgentMemory — Full Per-User Encrypted Memory Layer

```python
import asyncio
from typing import Any, Dict, List, Optional


class SecureAgentMemory:
    """
    Complete per-user encrypted memory for agent conversation state.
    Messages, tool results, and scratchpad data are stored encrypted
    with per-user derived keys. Supports TTL and key rotation.

    Usage:
        memory = SecureAgentMemory(
            redis=redis_client,
            master_key_hex=os.environ["MASTER_KEY"],
        )

        await memory.append_message("u-123", {"role": "user", "content": "Hello"})
        history = await memory.get_history("u-123")
    """

    def __init__(self, redis,
                 master_key_hex: str,
                 history_ttl_s: int = 86400):
        self._store = PerUserEncryptionStore(
            backend=redis,
            deriver=HKDFKeyDeriver(master_key_hex=master_key_hex),
            purpose="agent_memory",
        )
        self._ttl = history_ttl_s

    async def append_message(self, user_id: str, message: Dict[str, Any]):
        history = await self.get_history(user_id) or []
        history.append(message)
        await self._store.set(user_id, "history", history, ttl_s=self._ttl)

    async def get_history(self, user_id: str) -> Optional[List[Dict]]:
        return await self._store.get(user_id, "history")

    async def set_scratchpad(self, user_id: str, data: Dict[str, Any]):
        await self._store.set(user_id, "scratchpad", data, ttl_s=self._ttl)

    async def get_scratchpad(self, user_id: str) -> Optional[Dict]:
        return await self._store.get(user_id, "scratchpad")

    async def clear(self, user_id: str):
        await self._store.delete(user_id, "history")
        await self._store.delete(user_id, "scratchpad")
        self._store.evict_cipher_cache(user_id)
```

---

## Comparison

| Approach | Key Derivation | Encryption | Store | Rotation | Audit |
|---|---|---|---|---|---|
| **HKDFKeyDeriver** | Yes (HKDF) | No | No | No | No |
| **AESGCMCipher** | No | AES-GCM | No | No | No |
| **PerUserEncryptionStore** | Via deriver | Yes | Redis/KV | No | No |
| **MasterKeyRotationManager** | Dual-key | Yes | No | Yes | No |
| **KeyDerivationAuditLog** | No | No | No | No | Yes |
| **SecureAgentMemory** | Via deriver | Yes | Redis | No | No |

**Key insight**: never store derived keys — re-derive them on demand from `(master_key, user_id, purpose)`. The derivation is deterministic and takes microseconds; storing keys creates an unnecessary secret that must be protected, rotated, and audited separately. Keep the master key in a managed secrets service (AWS KMS, Vault) and pull it at startup; rotate by deploying a new master key and running `MasterKeyRotationManager.reencrypt_user_data` as a background job that re-encrypts user data without downtime.
