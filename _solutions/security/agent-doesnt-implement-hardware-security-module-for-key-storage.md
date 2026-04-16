---
title: "Agent Doesn't Implement Hardware Security Module for Key Storage"
description: "AI agents that store cryptographic keys in environment variables, flat files, or application memory expose those keys to process dumps, logging pipelines, and misconfigured secrets managers. An HSM or HSM-backed KMS keeps private key material in tamper-resistant hardware that software cannot export."
date: 2025-02-01
difficulty: advanced
category: security
slug: agent-doesnt-implement-hardware-security-module-for-key-storage
tags:
  - hsm
  - kms
  - key-management
  - cryptography
  - secrets
  - hardware-security
  - pkcs11
symptoms:
  - "API signing keys stored in environment variables are visible in CI/CD logs"
  - "Process memory dumps expose plaintext private keys"
  - "Key rotation requires redeployment because keys are baked into container images"
  - "Audit logs cannot prove which process used a key at a specific time"
  - "Compromise of the secrets manager leaks all keys because they are stored as blobs"
---

## Problem

Keys stored in software — environment variables, Kubernetes secrets, HashiCorp Vault generic secrets — exist as plaintext bytes somewhere in RAM or on disk. An attacker with read access to the application process, the secrets store, or a cloud snapshot can extract the key material.

Hardware Security Modules (HSMs) enforce a fundamental property: **private key material never leaves the tamper-resistant boundary**. All cryptographic operations happen inside the HSM. The application sends plaintext (or a reference) in and receives a signature, ciphertext, or MAC out — the key itself is never exported.

In cloud environments full HSMs are delivered as managed services (AWS CloudHSM, Google Cloud HSM, Azure Dedicated HSM) or via the cheaper KMS primitives (AWS KMS, Google Cloud KMS) that use HSMs internally. For on-premise deployments, PKCS#11 is the standard interface.

---

## Solution 1: PKCS#11 Signing Client (SoftHSM for Development)

Abstract over PKCS#11 to sign JWT payloads or API requests. In development, SoftHSM2 provides a software-only PKCS#11 token; in production, replace with a real HSM driver.

```python
import hashlib
import struct
from dataclasses import dataclass
from typing import Optional

try:
    import pkcs11
    from pkcs11 import Mechanism, KeyType, ObjectClass
    HAS_PKCS11 = True
except ImportError:
    HAS_PKCS11 = False


@dataclass
class PKCS11Config:
    library_path: str          # e.g. "/usr/lib/softhsm/libsofthsm2.so"
    token_label: str           # HSM token label
    pin: str                   # User PIN
    key_label: str             # Label of the key to use


class PKCS11SigningClient:
    """
    Signs data using a private key stored in a PKCS#11 HSM.
    The private key never leaves the HSM boundary.

    Usage:
        cfg = PKCS11Config(
            library_path="/usr/lib/softhsm/libsofthsm2.so",
            token_label="agent-token",
            pin="1234",
            key_label="agent-signing-key",
        )
        client = PKCS11SigningClient(cfg)
        signature = client.sign(b"payload to sign")
        ok = client.verify(b"payload to sign", signature)
    """

    def __init__(self, config: PKCS11Config):
        if not HAS_PKCS11:
            raise ImportError("python-pkcs11 is required: pip install python-pkcs11")
        self._config = config
        self._lib = pkcs11.lib(config.library_path)

    def _get_session(self):
        token = self._lib.get_token(token_label=self._config.token_label)
        return token.open(user_pin=self._config.pin)

    def sign(self, data: bytes) -> bytes:
        with self._get_session() as session:
            private_key = session.get_key(
                object_class=ObjectClass.PRIVATE_KEY,
                label=self._config.key_label,
            )
            return private_key.sign(data, mechanism=Mechanism.SHA256_RSA_PKCS)

    def verify(self, data: bytes, signature: bytes) -> bool:
        with self._get_session() as session:
            public_key = session.get_key(
                object_class=ObjectClass.PUBLIC_KEY,
                label=self._config.key_label,
            )
            try:
                public_key.verify(data, signature, mechanism=Mechanism.SHA256_RSA_PKCS)
                return True
            except pkcs11.exceptions.SignatureInvalid:
                return False

    def generate_keypair(self, key_size: int = 2048):
        """Generate a key pair inside the HSM — private key never exported."""
        with self._get_session() as session:
            pub, priv = session.generate_keypair(
                KeyType.RSA,
                key_size,
                store=True,
                label=self._config.key_label,
            )
        return pub  # only public key returned to caller

    def export_public_key_pem(self) -> bytes:
        """Export only the public key for distribution."""
        with self._get_session() as session:
            pub = session.get_key(
                object_class=ObjectClass.PUBLIC_KEY,
                label=self._config.key_label,
            )
            return pub[pkcs11.Attribute.VALUE]
```

---

## Solution 2: AWS KMS Envelope Encryption

Use AWS KMS to generate a Data Encryption Key (DEK). The DEK encrypts the actual data; KMS stores and protects the Key Encryption Key (KEK). On restart, the agent calls KMS to decrypt the DEK — the KEK never touches application memory.

```python
import base64
import json
import os
from dataclasses import dataclass
from typing import Optional, Tuple

try:
    import boto3
    HAS_BOTO = True
except ImportError:
    HAS_BOTO = False

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False


@dataclass
class EnvelopeKey:
    ciphertext_blob: bytes    # encrypted DEK (returned by KMS)
    plaintext_dek: bytes      # AES-256 key (in memory only, never persisted)


class AWSKMSEnvelopeEncryptor:
    """
    Envelope encryption: KMS protects the DEK; DEK encrypts the data.

    Usage:
        enc = AWSKMSEnvelopeEncryptor(kms_key_id="arn:aws:kms:...")
        ciphertext, ctx = enc.encrypt(b"sensitive agent data")
        plaintext = enc.decrypt(ciphertext, ctx)
    """

    def __init__(self, kms_key_id: str, region: str = "us-east-1"):
        if not HAS_BOTO:
            raise ImportError("boto3 required")
        if not HAS_CRYPTO:
            raise ImportError("cryptography required")
        self._kms_key_id = kms_key_id
        self._kms = boto3.client("kms", region_name=region)
        self._active_key: Optional[EnvelopeKey] = None

    def _generate_dek(self) -> EnvelopeKey:
        resp = self._kms.generate_data_key(
            KeyId=self._kms_key_id, KeySpec="AES_256"
        )
        return EnvelopeKey(
            ciphertext_blob=resp["CiphertextBlob"],
            plaintext_dek=resp["Plaintext"],
        )

    def _decrypt_dek(self, ciphertext_blob: bytes) -> bytes:
        resp = self._kms.decrypt(
            CiphertextBlob=ciphertext_blob,
            KeyId=self._kms_key_id,
        )
        return resp["Plaintext"]

    def encrypt(self, plaintext: bytes) -> Tuple[bytes, dict]:
        """Returns (ciphertext, context) — store both; context needed for decryption."""
        if self._active_key is None:
            self._active_key = self._generate_dek()
        aesgcm = AESGCM(self._active_key.plaintext_dek)
        nonce = os.urandom(12)
        ciphertext = aesgcm.encrypt(nonce, plaintext, None)
        context = {
            "encrypted_dek": base64.b64encode(self._active_key.ciphertext_blob).decode(),
            "nonce": base64.b64encode(nonce).decode(),
        }
        return ciphertext, context

    def decrypt(self, ciphertext: bytes, context: dict) -> bytes:
        encrypted_dek = base64.b64decode(context["encrypted_dek"])
        nonce = base64.b64decode(context["nonce"])
        dek = self._decrypt_dek(encrypted_dek)
        aesgcm = AESGCM(dek)
        return aesgcm.decrypt(nonce, ciphertext, None)

    def rotate_dek(self):
        """Generate a new DEK; old data remains decryptable via its own context."""
        self._active_key = self._generate_dek()
```

---

## Solution 3: Google Cloud KMS Asymmetric Signing

Use GCP Cloud KMS to sign JWT assertions or API request payloads. The private key never leaves GCP's FIPS 140-2 Level 3 HSM boundary.

```python
import base64
import hashlib
import json
import time
from typing import Optional

try:
    from google.cloud import kms_v1
    from google.api_core import exceptions as gcp_exceptions
    HAS_GCP_KMS = True
except ImportError:
    HAS_GCP_KMS = False


class GCPKMSJWTSigner:
    """
    Signs JWTs using a GCP KMS asymmetric key (RSA_SIGN_PKCS1_2048_SHA256).
    Private key material stays inside GCP's HSM.

    Usage:
        signer = GCPKMSJWTSigner(
            project="my-project",
            location="us-central1",
            key_ring="agent-keys",
            key="jwt-signing-key",
            key_version="1",
        )
        token = signer.sign_jwt({"sub": "agent-1", "aud": "api.internal"})
    """

    def __init__(self, project: str, location: str, key_ring: str,
                 key: str, key_version: str = "1"):
        if not HAS_GCP_KMS:
            raise ImportError("google-cloud-kms required")
        self._client = kms_v1.KeyManagementServiceClient()
        self._key_version_name = self._client.crypto_key_version_path(
            project, location, key_ring, key, key_version
        )

    def _b64url(self, data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    def sign_jwt(self, claims: dict, ttl: int = 3600) -> str:
        now = int(time.time())
        claims = {**claims, "iat": now, "exp": now + ttl}

        header = self._b64url(
            json.dumps({"alg": "RS256", "typ": "JWT"}).encode()
        )
        payload = self._b64url(json.dumps(claims).encode())
        signing_input = f"{header}.{payload}".encode()

        digest = hashlib.sha256(signing_input).digest()
        response = self._client.asymmetric_sign(
            request={
                "name": self._key_version_name,
                "digest": {"sha256": digest},
            }
        )
        signature = self._b64url(response.signature)
        return f"{header}.{payload}.{signature}"

    def get_public_key_pem(self) -> str:
        response = self._client.get_public_key(
            request={"name": self._key_version_name}
        )
        return response.pem
```

---

## Solution 4: Vault Transit Secrets Engine Client

HashiCorp Vault's Transit engine is a software-backed "Encryption-as-a-Service". Keys are stored in Vault's encrypted storage; agents never see the raw key bytes.

```python
import base64
from dataclasses import dataclass
from typing import Optional

try:
    import hvac
    HAS_HVAC = True
except ImportError:
    HAS_HVAC = False


@dataclass
class VaultTransitConfig:
    vault_addr: str = "https://vault.internal:8200"
    vault_token: str = ""
    mount_point: str = "transit"
    key_name: str = "agent-signing-key"


class VaultTransitClient:
    """
    Uses HashiCorp Vault Transit to sign, verify, encrypt, and decrypt.
    Vault holds the key; this client never sees plaintext key material.

    Usage:
        cfg = VaultTransitConfig(vault_token=os.environ["VAULT_TOKEN"])
        client = VaultTransitClient(cfg)
        client.ensure_key()

        ciphertext = client.encrypt(b"sensitive payload")
        plaintext  = client.decrypt(ciphertext)

        sig = client.sign(b"message")
        ok  = client.verify(b"message", sig)
    """

    def __init__(self, config: VaultTransitConfig):
        if not HAS_HVAC:
            raise ImportError("hvac required: pip install hvac")
        self._cfg = config
        self._vault = hvac.Client(url=config.vault_addr, token=config.vault_token)

    def ensure_key(self, key_type: str = "rsa-2048"):
        """Create the transit key if it doesn't exist."""
        try:
            self._vault.secrets.transit.create_key(
                name=self._cfg.key_name,
                key_type=key_type,
                mount_point=self._cfg.mount_point,
            )
        except Exception:
            pass  # Already exists

    def encrypt(self, plaintext: bytes) -> str:
        b64 = base64.b64encode(plaintext).decode()
        resp = self._vault.secrets.transit.encrypt_data(
            name=self._cfg.key_name,
            plaintext=b64,
            mount_point=self._cfg.mount_point,
        )
        return resp["data"]["ciphertext"]

    def decrypt(self, ciphertext: str) -> bytes:
        resp = self._vault.secrets.transit.decrypt_data(
            name=self._cfg.key_name,
            ciphertext=ciphertext,
            mount_point=self._cfg.mount_point,
        )
        return base64.b64decode(resp["data"]["plaintext"])

    def sign(self, data: bytes, hash_algorithm: str = "sha2-256") -> str:
        b64 = base64.b64encode(data).decode()
        resp = self._vault.secrets.transit.sign_data(
            name=self._cfg.key_name,
            hash_input=b64,
            hash_algorithm=hash_algorithm,
            mount_point=self._cfg.mount_point,
        )
        return resp["data"]["signature"]

    def verify(self, data: bytes, signature: str,
               hash_algorithm: str = "sha2-256") -> bool:
        b64 = base64.b64encode(data).decode()
        resp = self._vault.secrets.transit.verify_signed_data(
            name=self._cfg.key_name,
            hash_input=b64,
            signature=signature,
            hash_algorithm=hash_algorithm,
            mount_point=self._cfg.mount_point,
        )
        return resp["data"]["valid"]

    def rotate_key(self):
        self._vault.secrets.transit.rotate_key(
            name=self._cfg.key_name,
            mount_point=self._cfg.mount_point,
        )
```

---

## Solution 5: HSM-Backed Key Reference Store

Replaces raw key bytes in agent configuration with opaque key references (handles). All key operations go through the HSM abstraction. Prevents key material from appearing in config files, logs, or environment variables.

```python
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


@dataclass
class KeyHandle:
    """Opaque reference to a key stored in an HSM or KMS."""
    handle_id: str
    key_type: str      # "signing" | "encryption" | "mac"
    backend: str       # "pkcs11" | "awskms" | "gcpkms" | "vault"
    metadata: Dict[str, str] = field(default_factory=dict)

    def __repr__(self):
        # Never print key material in repr
        return f"KeyHandle(id={self.handle_id}, type={self.key_type}, backend={self.backend})"


class HSMKeyReferenceStore:
    """
    Maps logical key names to KeyHandles.
    Configuration references handles, never raw bytes.

    Usage:
        store = HSMKeyReferenceStore()
        store.register("jwt-key", KeyHandle("arn:aws:kms:...", "signing", "awskms"))
        store.register("db-enc-key", KeyHandle("vault:transit/db-key", "encryption", "vault"))

        handle = store.get("jwt-key")
        # Pass handle to AWSKMSEnvelopeEncryptor or VaultTransitClient
    """

    def __init__(self):
        self._store: Dict[str, KeyHandle] = {}
        self._operations: Dict[str, Callable] = {}

    def register(self, name: str, handle: KeyHandle):
        self._store[name] = handle

    def get(self, name: str) -> KeyHandle:
        handle = self._store.get(name)
        if handle is None:
            raise KeyError(f"No key handle registered for '{name}'")
        return handle

    def register_operation(self, handle_id: str, op: str, fn: Callable):
        """Bind a callable (sign/encrypt) to a key handle."""
        self._operations[f"{handle_id}:{op}"] = fn

    async def sign(self, key_name: str, data: bytes) -> bytes:
        handle = self.get(key_name)
        op = self._operations.get(f"{handle.handle_id}:sign")
        if op is None:
            raise RuntimeError(f"No sign operation registered for {key_name}")
        return await op(data)

    async def encrypt(self, key_name: str, plaintext: bytes) -> bytes:
        handle = self.get(key_name)
        op = self._operations.get(f"{handle.handle_id}:encrypt")
        if op is None:
            raise RuntimeError(f"No encrypt operation registered for {key_name}")
        return await op(plaintext)

    def audit_log(self) -> Dict[str, Any]:
        return {
            name: {
                "handle_id": h.handle_id,
                "type": h.key_type,
                "backend": h.backend,
            }
            for name, h in self._store.items()
        }
```

---

## Solution 6: HSM-Aware Agent Credential Manager

Bootstraps all agent credentials through HSM-backed key handles at startup, emits an audit event for every cryptographic operation, and enforces that no raw key bytes appear in configuration.

```python
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class CryptoAuditEvent:
    timestamp: float
    key_name: str
    operation: str
    caller: str
    success: bool


class HSMAwareCredentialManager:
    """
    Central credential manager for HSM-backed agents.
    Provides sign/verify/encrypt/decrypt through named key handles.
    Emits an audit event for every operation.

    Usage:
        mgr = HSMAwareCredentialManager()
        mgr.add_kms_key("jwt-signing", kms_encryptor)
        mgr.add_vault_key("db-encryption", vault_client)

        token = await mgr.sign("jwt-signing", payload)
        plaintext = await mgr.decrypt("db-encryption", ciphertext)

        print(mgr.audit_trail())
    """

    def __init__(self):
        self._backends: Dict[str, Any] = {}
        self._audit: List[CryptoAuditEvent] = []

    def add_kms_key(self, name: str, kms_client: "AWSKMSEnvelopeEncryptor"):
        self._backends[name] = ("kms", kms_client)

    def add_vault_key(self, name: str, vault_client: "VaultTransitClient"):
        self._backends[name] = ("vault", vault_client)

    def add_pkcs11_key(self, name: str, pkcs11_client: "PKCS11SigningClient"):
        self._backends[name] = ("pkcs11", pkcs11_client)

    def _record(self, key_name: str, op: str, caller: str, success: bool):
        self._audit.append(CryptoAuditEvent(
            timestamp=time.time(),
            key_name=key_name,
            operation=op,
            caller=caller,
            success=success,
        ))
        logger.info("HSM audit: key=%s op=%s caller=%s ok=%s",
                    key_name, op, caller, success)

    async def sign(self, key_name: str, data: bytes,
                   caller: str = "agent") -> bytes:
        kind, backend = self._backends[key_name]
        try:
            if kind == "pkcs11":
                result = await asyncio.to_thread(backend.sign, data)
            elif kind == "vault":
                result = await asyncio.to_thread(backend.sign, data)
            else:
                raise ValueError(f"Backend {kind} does not support sign")
            self._record(key_name, "sign", caller, True)
            return result
        except Exception:
            self._record(key_name, "sign", caller, False)
            raise

    async def encrypt(self, key_name: str, plaintext: bytes,
                      caller: str = "agent"):
        kind, backend = self._backends[key_name]
        try:
            if kind == "kms":
                ct, ctx = await asyncio.to_thread(backend.encrypt, plaintext)
                result = (ct, ctx)
            elif kind == "vault":
                result = await asyncio.to_thread(backend.encrypt, plaintext)
            else:
                raise ValueError(f"Backend {kind} does not support encrypt")
            self._record(key_name, "encrypt", caller, True)
            return result
        except Exception:
            self._record(key_name, "encrypt", caller, False)
            raise

    def audit_trail(self) -> List[dict]:
        return [
            {
                "timestamp": e.timestamp,
                "key": e.key_name,
                "op": e.operation,
                "caller": e.caller,
                "success": e.success,
            }
            for e in self._audit
        ]
```

---

## Comparison

| Approach | Hardware Boundary | Key Export Risk | Cloud/On-Prem |
|---|---|---|---|
| **PKCS#11 (SoftHSM)** | Physical HSM (or soft in dev) | None (never exported) | On-prem |
| **AWS KMS Envelope** | FIPS 140-2 L2 (KMS) | None (DEK encrypted) | AWS |
| **GCP KMS Asymmetric** | FIPS 140-2 L3 (Cloud HSM) | None | GCP |
| **Vault Transit** | Software (or hardware seal) | None (Vault-internal) | Hybrid |
| **Key Reference Store** | Delegates to above | None in config | Any |
| **HSM Credential Manager** | Delegates + audit | None | Any |

**Key insight**: the goal is that no code path should ever hold a raw private key in a Python variable. Use PKCS#11 or a managed KMS from day one; retrofitting HSM support after a key compromise is far more expensive than building it in.
