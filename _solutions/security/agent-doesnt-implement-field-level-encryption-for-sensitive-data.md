---
title: "Agent Doesn't Implement Field-Level Encryption for Sensitive Data"
description: "How to encrypt specific sensitive fields in agent memory, tool outputs, and stored state at the field level — so sensitive data remains encrypted at rest while non-sensitive fields stay queryable and unencrypted."
date: 2025-01-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-field-level-encryption-for-sensitive-data
tags:
  - security
  - encryption
  - field-level-encryption
  - data-protection
  - key-management
  - pii
  - at-rest-encryption
symptoms:
  - "User PII (email, phone, SSN) stored in plaintext in agent memory or logs"
  - "Sensitive tool outputs persisted in unencrypted JSON to database"
  - "Full-disk encryption alone provides insufficient granularity for compliance"
  - "Different fields need different access control — currently all-or-nothing"
  - "Third-party log aggregation services receive unencrypted sensitive fields"
  - "Key rotation requires re-encrypting entire records rather than targeted fields"
---

## Why This Happens

Agents accumulate sensitive data as a side effect of their operation: user queries contain PII, tool results include API keys or financial data, conversation memory stores health information. Full-disk or database-level encryption protects data from physical theft but not from application-layer breaches — a compromised application process can read all data in plaintext. Field-level encryption (FLE) encrypts each sensitive field independently, meaning the application only holds plaintext for fields it explicitly decrypts, reducing the blast radius of a breach to a single key's exposure.

Without FLE, all sensitive fields are uniformly accessible to any process with database access, making compliance with regulations that require granular data access controls (HIPAA, PCI-DSS, GDPR) difficult to demonstrate.

---

## Solution 1: AES-GCM Field Encryptor

The foundation: encrypt and decrypt individual string or bytes values using AES-256-GCM. Each encrypted value bundles the IV and authentication tag so it is self-contained.

```python
import base64
import os
import json
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from typing import Any, Union

class AESGCMFieldEncryptor:
    """
    AES-256-GCM field-level encryptor.
    Each ciphertext is self-contained: base64(iv || ciphertext || tag).
    Associated data (AAD) binds the ciphertext to its context (e.g., field name + record ID).
    """

    IV_SIZE = 12  # 96-bit IV for GCM

    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError("Key must be 32 bytes for AES-256")
        self._aesgcm = AESGCM(key)

    @classmethod
    def from_password(cls, password: str, salt: bytes | None = None) -> "AESGCMFieldEncryptor":
        salt = salt or os.urandom(16)
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=480_000)
        key = kdf.derive(password.encode())
        return cls(key)

    @classmethod
    def generate(cls) -> "AESGCMFieldEncryptor":
        return cls(os.urandom(32))

    def encrypt(self, plaintext: str, aad: bytes | None = None) -> str:
        """
        Encrypt a string field. Returns base64-encoded ciphertext bundle.
        aad = additional authenticated data (e.g., field_name:record_id) — not encrypted but authenticated.
        """
        iv = os.urandom(self.IV_SIZE)
        ct = self._aesgcm.encrypt(iv, plaintext.encode("utf-8"), aad)
        bundle = iv + ct  # IV || ciphertext+tag
        return base64.b64encode(bundle).decode("ascii")

    def decrypt(self, ciphertext_b64: str, aad: bytes | None = None) -> str:
        """Decrypt a field value. Raises ValueError on tampering or wrong key."""
        bundle = base64.b64decode(ciphertext_b64)
        iv = bundle[:self.IV_SIZE]
        ct = bundle[self.IV_SIZE:]
        try:
            plaintext = self._aesgcm.decrypt(iv, ct, aad)
        except Exception as exc:
            raise ValueError("Field decryption failed — tampered or wrong key") from exc
        return plaintext.decode("utf-8")

    def encrypt_json(self, obj: Any, aad: bytes | None = None) -> str:
        """Serialize obj to JSON then encrypt."""
        return self.encrypt(json.dumps(obj), aad=aad)

    def decrypt_json(self, ciphertext_b64: str, aad: bytes | None = None) -> Any:
        return json.loads(self.decrypt(ciphertext_b64, aad=aad))


# --- Quick usage ---

def demo_fle():
    encryptor = AESGCMFieldEncryptor.generate()
    user_id = "user-42"

    email_ct = encryptor.encrypt("alice@example.com", aad=f"email:{user_id}".encode())
    ssn_ct   = encryptor.encrypt("123-45-6789",        aad=f"ssn:{user_id}".encode())

    print(f"Encrypted email: {email_ct[:40]}...")
    print(f"Decrypted email: {encryptor.decrypt(email_ct, aad=f'email:{user_id}'.encode())}")
```

---

## Solution 2: Schema-Driven Field Encryption Decorator

Declare which fields in a dataclass or dict are sensitive with a schema; the decorator transparently encrypts on write and decrypts on read.

```python
from dataclasses import dataclass, asdict, fields
from typing import ClassVar, get_type_hints
import copy

ENCRYPTED_MARKER = "__enc__"

class EncryptedField:
    """Marker type for fields that should be encrypted at rest."""
    pass

@dataclass
class UserProfile:
    user_id: str
    name: str
    email: str       # <- sensitive
    phone: str       # <- sensitive
    preferences: dict

    SENSITIVE_FIELDS: ClassVar[set] = {"email", "phone"}


class FieldEncryptionCodec:
    """
    Encrypt/decrypt specified fields in a dict or dataclass.
    Ciphertext is stored as {"__enc__": "<base64>"} to distinguish from plaintext.
    """

    def __init__(self, encryptor: AESGCMFieldEncryptor, sensitive_fields: set[str]):
        self.encryptor = encryptor
        self.sensitive = sensitive_fields

    def _field_aad(self, field_name: str, record_id: str) -> bytes:
        return f"{field_name}:{record_id}".encode()

    def encrypt_record(self, record: dict, record_id: str) -> dict:
        """Return a new dict with sensitive fields replaced by encrypted bundles."""
        result = copy.deepcopy(record)
        for field_name in self.sensitive:
            if field_name in result and result[field_name] is not None:
                plaintext = str(result[field_name])
                ct = self.encryptor.encrypt(plaintext, aad=self._field_aad(field_name, record_id))
                result[field_name] = {ENCRYPTED_MARKER: ct}
        return result

    def decrypt_record(self, record: dict, record_id: str) -> dict:
        """Return a new dict with encrypted bundles replaced by plaintext values."""
        result = copy.deepcopy(record)
        for field_name in self.sensitive:
            value = result.get(field_name)
            if isinstance(value, dict) and ENCRYPTED_MARKER in value:
                ct = value[ENCRYPTED_MARKER]
                result[field_name] = self.encryptor.decrypt(ct, aad=self._field_aad(field_name, record_id))
        return result

    def encrypt_dataclass(self, obj, record_id: str) -> dict:
        d = asdict(obj)
        return self.encrypt_record(d, record_id)

    def is_encrypted(self, record: dict, field_name: str) -> bool:
        v = record.get(field_name)
        return isinstance(v, dict) and ENCRYPTED_MARKER in v


# --- Usage ---

def demo_schema_driven():
    encryptor = AESGCMFieldEncryptor.generate()
    codec = FieldEncryptionCodec(encryptor, UserProfile.SENSITIVE_FIELDS)

    profile = UserProfile(
        user_id="u-1",
        name="Alice",
        email="alice@example.com",
        phone="+1-555-0100",
        preferences={"theme": "dark"},
    )

    encrypted = codec.encrypt_dataclass(profile, record_id=profile.user_id)
    print("Stored email:", encrypted["email"])  # {"__enc__": "..."}
    print("Name is clear:", encrypted["name"])   # Alice

    decrypted = codec.decrypt_record(encrypted, record_id="u-1")
    print("Decrypted email:", decrypted["email"])  # alice@example.com
```

---

## Solution 3: Envelope Encryption with Key Hierarchy

Production FLE uses envelope encryption: a Key Encryption Key (KEK) from a KMS protects per-record Data Encryption Keys (DEKs). Rotating the KEK only requires re-wrapping the DEKs, not re-encrypting all field data.

```python
import os
import base64
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

class EnvelopeEncryptionManager:
    """
    Envelope encryption pattern:
    - KEK (Key Encryption Key): stored in KMS / secrets vault, never in app memory longer than needed
    - DEK (Data Encryption Key): generated per record, encrypted with KEK, stored alongside ciphertext
    - Field data: encrypted with DEK
    """

    def __init__(self, kek: bytes):
        """kek should come from a secrets manager, not hardcoded."""
        self._kek_cipher = AESGCM(kek)

    def _new_dek(self) -> bytes:
        return os.urandom(32)

    def _wrap_dek(self, dek: bytes) -> str:
        """Encrypt DEK with KEK."""
        iv = os.urandom(12)
        wrapped = self._kek_cipher.encrypt(iv, dek, None)
        return base64.b64encode(iv + wrapped).decode()

    def _unwrap_dek(self, wrapped_dek_b64: str) -> bytes:
        """Decrypt DEK with KEK."""
        data = base64.b64decode(wrapped_dek_b64)
        iv, ct = data[:12], data[12:]
        return self._kek_cipher.decrypt(iv, ct, None)

    def create_record_dek(self) -> dict:
        """Generate a new DEK and return its wrapped form for storage."""
        dek = self._new_dek()
        return {"wrapped_dek": self._wrap_dek(dek), "_dek": dek}  # _dek is transient

    def encrypt_field(self, plaintext: str, wrapped_dek: str, aad: bytes | None = None) -> str:
        dek = self._unwrap_dek(wrapped_dek)
        cipher = AESGCM(dek)
        iv = os.urandom(12)
        ct = cipher.encrypt(iv, plaintext.encode(), aad)
        return base64.b64encode(iv + ct).decode()

    def decrypt_field(self, ciphertext_b64: str, wrapped_dek: str, aad: bytes | None = None) -> str:
        dek = self._unwrap_dek(wrapped_dek)
        cipher = AESGCM(dek)
        data = base64.b64decode(ciphertext_b64)
        iv, ct = data[:12], data[12:]
        return cipher.decrypt(iv, ct, aad).decode()

    def rotate_kek(self, new_kek: bytes, wrapped_dek: str) -> str:
        """Re-wrap a DEK with a new KEK — no field data re-encryption needed."""
        dek = self._unwrap_dek(wrapped_dek)
        new_manager = EnvelopeEncryptionManager(new_kek)
        return new_manager._wrap_dek(dek)


# --- Per-record envelope encryption ---

def demo_envelope():
    kek = os.urandom(32)
    manager = EnvelopeEncryptionManager(kek)

    # Create a new DEK for this record
    dek_info = manager.create_record_dek()
    wrapped_dek = dek_info["wrapped_dek"]

    # Encrypt sensitive fields with the record's DEK
    email_ct = manager.encrypt_field("bob@example.com", wrapped_dek, aad=b"field:email")
    ssn_ct   = manager.encrypt_field("987-65-4321",     wrapped_dek, aad=b"field:ssn")

    # Store: wrapped_dek + encrypted fields
    stored = {"wrapped_dek": wrapped_dek, "email": email_ct, "ssn": ssn_ct, "name": "Bob"}

    # Decrypt when needed
    email = manager.decrypt_field(stored["email"], stored["wrapped_dek"], aad=b"field:email")
    print(f"Email: {email}")

    # Key rotation: only re-wrap the DEK
    new_kek = os.urandom(32)
    stored["wrapped_dek"] = manager.rotate_kek(new_kek, stored["wrapped_dek"])
    print("KEK rotated — field data unchanged")
```

---

## Solution 4: Transparent Encryption Layer for Agent Memory

A drop-in replacement for agent memory stores that transparently encrypts defined fields on all read/write operations.

```python
import json
from typing import Any, Optional

class EncryptedMemoryStore:
    """
    Agent memory store with transparent field-level encryption.
    Wrap any dict-based store with this class to get automatic FLE.
    """

    def __init__(
        self,
        backing_store: dict,
        codec: FieldEncryptionCodec,
    ):
        self._store = backing_store
        self._codec = codec

    def set(self, key: str, value: dict) -> None:
        """Encrypt sensitive fields before storing."""
        encrypted = self._codec.encrypt_record(value, record_id=key)
        self._store[key] = encrypted

    def get(self, key: str) -> Optional[dict]:
        """Decrypt sensitive fields on retrieval."""
        raw = self._store.get(key)
        if raw is None:
            return None
        return self._codec.decrypt_record(raw, record_id=key)

    def get_raw(self, key: str) -> Optional[dict]:
        """Return encrypted form — for storage replication without decryption."""
        return self._store.get(key)

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def keys(self) -> list[str]:
        return list(self._store.keys())

    def search_by_plaintext_field(self, field: str, value: Any) -> list[str]:
        """Search non-encrypted fields only. Encrypted fields are not searchable without decryption."""
        results = []
        for key, record in self._store.items():
            if field in self._codec.sensitive:
                continue  # Cannot search encrypted fields directly
            if record.get(field) == value:
                results.append(key)
        return results


# --- Searchable encryption via deterministic HMAC index ---

import hmac
import hashlib

class SearchableEncryptedIndex:
    """
    Allows equality search over encrypted fields using HMAC-based blind indexes.
    Trade-off: deterministic HMAC reveals frequency but enables search without decryption.
    """

    def __init__(self, hmac_key: bytes):
        self._key = hmac_key
        self._index: dict[str, list[str]] = {}  # token -> [record_ids]

    def _token(self, field_name: str, value: str) -> str:
        msg = f"{field_name}:{value}".encode()
        return hmac.new(self._key, msg, hashlib.sha256).hexdigest()

    def index_field(self, record_id: str, field_name: str, plaintext_value: str) -> None:
        token = self._token(field_name, plaintext_value)
        self._index.setdefault(token, []).append(record_id)

    def search(self, field_name: str, plaintext_value: str) -> list[str]:
        token = self._token(field_name, plaintext_value)
        return self._index.get(token, [])
```

---

## Solution 5: Encrypted Conversation Memory for LLM Agents

Encrypt sensitive information extracted from conversation turns before storing in agent long-term memory.

```python
import re
from dataclasses import dataclass, field

PII_PATTERNS = {
    "email":   re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"),
    "phone":   re.compile(r"\+?[\d\s\-().]{10,20}"),
    "ssn":     re.compile(r"\d{3}-\d{2}-\d{4}"),
    "cc":      re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"),
}

@dataclass
class ConversationMemoryEntry:
    turn_id: str
    speaker: str          # "user" | "agent"
    message_hash: str     # SHA-256 of original message — for dedup, not PII
    encrypted_fields: dict = field(default_factory=dict)  # {field_name: ciphertext}
    plaintext_fields: dict = field(default_factory=dict)  # non-sensitive fields
    pii_detected: list[str] = field(default_factory=list)

class PIIAwareConversationMemory:
    """
    Stores conversation turns with automatic PII detection and field-level encryption.
    PII is encrypted before storage; non-PII metadata is stored plaintext for search.
    """

    def __init__(self, encryptor: AESGCMFieldEncryptor):
        self.encryptor = encryptor
        self._entries: dict[str, ConversationMemoryEntry] = {}

    def _extract_pii(self, text: str) -> dict[str, list[str]]:
        found = {}
        for pii_type, pattern in PII_PATTERNS.items():
            matches = pattern.findall(text)
            if matches:
                found[pii_type] = matches
        return found

    def _redact(self, text: str) -> str:
        for pii_type, pattern in PII_PATTERNS.items():
            text = pattern.sub(f"[REDACTED:{pii_type.upper()}]", text)
        return text

    def store_turn(self, turn_id: str, speaker: str, message: str) -> ConversationMemoryEntry:
        import hashlib
        msg_hash = hashlib.sha256(message.encode()).hexdigest()

        pii_found = self._extract_pii(message)
        entry = ConversationMemoryEntry(
            turn_id=turn_id,
            speaker=speaker,
            message_hash=msg_hash,
            pii_detected=list(pii_found.keys()),
        )

        if pii_found:
            # Store full message encrypted
            entry.encrypted_fields["message"] = self.encryptor.encrypt(
                message, aad=f"message:{turn_id}".encode()
            )
            # Store redacted version for search/display
            entry.plaintext_fields["message_redacted"] = self._redact(message)
        else:
            entry.plaintext_fields["message"] = message

        self._entries[turn_id] = entry
        return entry

    def retrieve_turn(self, turn_id: str, decrypt: bool = True) -> Optional[dict]:
        entry = self._entries.get(turn_id)
        if entry is None:
            return None
        result = {**entry.plaintext_fields}
        if decrypt and "message" in entry.encrypted_fields:
            result["message"] = self.encryptor.decrypt(
                entry.encrypted_fields["message"],
                aad=f"message:{turn_id}".encode(),
            )
        return result
```

---

## Solution 6: Field Encryption Audit Logger

Track all field decryption events for compliance auditing — who decrypted which field, when, and why.

```python
import time
import logging
from dataclasses import dataclass

audit_logger = logging.getLogger("fle.audit")

@dataclass
class DecryptionEvent:
    timestamp: float
    accessor_id: str
    record_id: str
    field_name: str
    purpose: str
    success: bool

class AuditedEncryptor:
    """
    Wraps AESGCMFieldEncryptor and emits a structured audit log entry on every decryption.
    """

    def __init__(self, encryptor: AESGCMFieldEncryptor, accessor_id: str):
        self._enc = encryptor
        self.accessor_id = accessor_id
        self._audit_log: list[DecryptionEvent] = []

    def encrypt(self, plaintext: str, aad: bytes | None = None) -> str:
        return self._enc.encrypt(plaintext, aad)

    def decrypt(
        self,
        ciphertext_b64: str,
        aad: bytes | None = None,
        record_id: str = "",
        field_name: str = "",
        purpose: str = "",
    ) -> str:
        success = True
        result = None
        try:
            result = self._enc.decrypt(ciphertext_b64, aad)
            return result
        except ValueError:
            success = False
            raise
        finally:
            event = DecryptionEvent(
                timestamp=time.time(),
                accessor_id=self.accessor_id,
                record_id=record_id,
                field_name=field_name,
                purpose=purpose,
                success=success,
            )
            self._audit_log.append(event)
            audit_logger.info(
                "FIELD_DECRYPT accessor=%s record=%s field=%s purpose=%s success=%s",
                self.accessor_id, record_id, field_name, purpose, success,
            )

    def get_audit_log(self) -> list[DecryptionEvent]:
        return list(self._audit_log)
```

---

## Comparison

| Solution | Granularity | Key Management | Searchable | Audit Trail | Best For |
|---|---|---|---|---|---|
| AES-GCM Field Encryptor | Per field | Single key | No | No | Simple FLE foundation |
| Schema-Driven Codec | Per declared field | Single key | Plaintext fields only | No | Structured records with mixed sensitivity |
| Envelope Encryption | Per record + per field | KEK + DEK hierarchy | No | No | Key rotation without data re-encryption |
| Transparent Memory Store | Per field | Single key | Non-encrypted fields | No | Drop-in agent memory replacement |
| Searchable Encryption | Per field | HMAC key | Equality search | No | Searching encrypted email/ID fields |
| PII-Aware Conversation Memory | Per turn | Single key | Redacted text | No | Conversation storage with auto-PII detection |
| Audited Encryptor | Per field | Wraps any encryptor | N/A | Yes | Compliance with access logging requirements |

**Start with the AES-GCM field encryptor** as the cryptographic primitive. **Add the schema-driven codec** to declaratively mark sensitive fields and avoid ad-hoc encrypt/decrypt calls throughout the codebase. **Use envelope encryption** in production to separate key rotation from data re-encryption. **Add searchable encryption** when equality search over encrypted fields is needed. **Always audit decryption events** in regulated environments where access to PII must be logged.
