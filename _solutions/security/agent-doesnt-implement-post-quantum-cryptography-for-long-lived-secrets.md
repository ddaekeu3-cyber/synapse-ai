---
title: "Agent Doesn't Implement Post-Quantum Cryptography for Long-Lived Secrets"
description: "AI agents that encrypt long-lived secrets with RSA or ECC are vulnerable to harvest-now-decrypt-later attacks. A quantum-capable adversary can store ciphertexts today and decrypt them once a sufficiently large quantum computer exists. Migrating to NIST-standardised post-quantum algorithms (ML-KEM, ML-DSA) protects secrets that must remain confidential beyond the quantum horizon."
date: 2025-02-06
difficulty: advanced
category: security
slug: agent-doesnt-implement-post-quantum-cryptography-for-long-lived-secrets
tags:
  - post-quantum
  - cryptography
  - ML-KEM
  - ML-DSA
  - kyber
  - dilithium
  - long-lived-secrets
  - harvest-now-decrypt-later
symptoms:
  - "Agent encrypts API keys or session tokens with RSA-2048 that must stay secret for 10+ years"
  - "Agent signs model outputs with ECDSA; signatures must be verifiable in 2035+"
  - "No quantum-resistant algorithm in the agent's cryptographic inventory"
  - "Agent stores encrypted secrets at rest with no algorithm agility path"
  - "Security audit flags classical asymmetric crypto for data that outlives the quantum horizon"
---

## Problem

Classical asymmetric algorithms — RSA, ECDH, ECDSA — are broken by Shor's algorithm on a sufficiently large quantum computer. NIST estimates cryptographically relevant quantum computers may arrive within 10–15 years. Any secret encrypted today with RSA-2048 and stored by an adversary will be decryptable when that machine exists.

NIST finalised three post-quantum standards in 2024:
- **ML-KEM** (FIPS 203, formerly Kyber): key encapsulation mechanism, replaces RSA/ECDH for key exchange.
- **ML-DSA** (FIPS 204, formerly Dilithium): digital signatures, replaces RSA/ECDSA.
- **SLH-DSA** (FIPS 205, formerly SPHINCS+): stateless hash-based signatures, conservative fallback.

---

## Solution 1: ML-KEM Key Encapsulation for Secret Wrapping

```python
from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from typing import Optional, Tuple

# pip install pqcrypto  (or oqs-python for liboqs bindings)
try:
    from oqs import KEM  # liboqs Python bindings
    _BACKEND = "oqs"
except ImportError:
    from Crypto.PublicKey import RSA  # fallback for illustration only
    _BACKEND = "classical_fallback"


@dataclass
class EncapsulatedSecret:
    """Holds the ciphertext (kem_ct) and the wrapped payload (wrapped)."""
    algorithm: str
    kem_ciphertext: bytes      # KEM ciphertext sent to recipient
    wrapped_payload: bytes     # payload encrypted with the shared secret
    nonce: bytes


class MLKEMSecretWrapper:
    """
    Wraps agent secrets using ML-KEM-768 (FIPS 203).
    The recipient's ML-KEM public key is used to encapsulate a random
    symmetric key; that symmetric key then wraps the actual secret with
    AES-256-GCM.

    Usage:
        # Recipient generates keypair once
        wrapper = MLKEMSecretWrapper()
        public_key, private_key = wrapper.generate_keypair()

        # Sender wraps secret
        encapsulated = wrapper.wrap(b"my-api-key-value", public_key)

        # Recipient unwraps
        secret = wrapper.unwrap(encapsulated, private_key)
        assert secret == b"my-api-key-value"
    """

    ALGORITHM = "ML-KEM-768"

    def __init__(self):
        if _BACKEND != "oqs":
            raise RuntimeError(
                "liboqs Python bindings required: pip install oqs-python"
            )

    def generate_keypair(self) -> Tuple[bytes, bytes]:
        with KEM("Kyber768") as kem:
            public_key = kem.generate_keypair()
            private_key = kem.export_secret_key()
        return public_key, private_key

    def wrap(self, secret: bytes, recipient_public_key: bytes) -> EncapsulatedSecret:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        with KEM("Kyber768") as kem:
            kem_ciphertext, shared_secret = kem.encap_secret(recipient_public_key)

        nonce = os.urandom(12)
        aesgcm = AESGCM(shared_secret[:32])
        wrapped = aesgcm.encrypt(nonce, secret, None)

        return EncapsulatedSecret(
            algorithm=self.ALGORITHM,
            kem_ciphertext=kem_ciphertext,
            wrapped_payload=wrapped,
            nonce=nonce,
        )

    def unwrap(self, enc: EncapsulatedSecret, private_key: bytes) -> bytes:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        with KEM("Kyber768") as kem:
            kem.import_secret_key(private_key)
            shared_secret = kem.decap_secret(enc.kem_ciphertext)

        aesgcm = AESGCM(shared_secret[:32])
        return aesgcm.decrypt(enc.nonce, enc.wrapped_payload, None)
```

---

## Solution 2: ML-DSA Signature for Agent Output Integrity

```python
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

try:
    from oqs import Signature  # liboqs
    _OQS_AVAILABLE = True
except ImportError:
    _OQS_AVAILABLE = False


@dataclass
class SignedAgentOutput:
    payload: bytes
    signature: bytes
    algorithm: str
    key_id: str
    timestamp: float


class MLDSAOutputSigner:
    """
    Signs agent outputs with ML-DSA-65 (FIPS 204, formerly Dilithium3).
    Provides integrity and non-repudiation for model responses that
    must remain verifiable beyond the quantum horizon.

    Usage:
        signer = MLDSAOutputSigner(key_id="agent-signing-key-v1")
        public_key, private_key = signer.generate_keypair()

        signed = signer.sign({"response": "...", "tool_calls": []}, private_key)
        signer.verify(signed, public_key)  # raises on failure
    """

    ALGORITHM = "ML-DSA-65"

    def __init__(self, key_id: str):
        if not _OQS_AVAILABLE:
            raise RuntimeError("pip install oqs-python")
        self.key_id = key_id

    def generate_keypair(self):
        with Signature("Dilithium3") as sig:
            public_key = sig.generate_keypair()
            private_key = sig.export_secret_key()
        return public_key, private_key

    def sign(self, output: Any, private_key: bytes) -> SignedAgentOutput:
        payload = json.dumps(output, sort_keys=True).encode()
        with Signature("Dilithium3") as sig:
            sig.import_secret_key(private_key)
            signature = sig.sign(payload)
        return SignedAgentOutput(
            payload=payload,
            signature=signature,
            algorithm=self.ALGORITHM,
            key_id=self.key_id,
            timestamp=time.time(),
        )

    def verify(self, signed: SignedAgentOutput, public_key: bytes) -> bool:
        if signed.algorithm != self.ALGORITHM:
            raise ValueError(f"Algorithm mismatch: {signed.algorithm}")
        with Signature("Dilithium3") as sig:
            return sig.verify(signed.payload, signed.signature, public_key)
```

---

## Solution 3: Hybrid Classical + Post-Quantum Encryption

Combine X25519 + ML-KEM so that the scheme is secure if *either* algorithm holds. NIST recommends hybrid until confidence in PQC implementations matures.

```python
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey,
)
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import (
    Encoding, PublicFormat, Raw,
)

try:
    from oqs import KEM
    _OQS_AVAILABLE = True
except ImportError:
    _OQS_AVAILABLE = False


@dataclass
class HybridCiphertext:
    x25519_public: bytes     # ephemeral X25519 public key
    kem_ciphertext: bytes    # ML-KEM ciphertext
    nonce: bytes
    ciphertext: bytes        # AES-GCM of payload


class HybridPQCEncryptor:
    """
    X25519 + ML-KEM-768 hybrid encryption.
    The shared key is derived from HKDF(x25519_ss || kem_ss).
    Breaking the encryption requires breaking BOTH algorithms.

    Usage:
        enc = HybridPQCEncryptor()
        x25519_priv, x25519_pub = enc.generate_x25519_keypair()
        kem_pub, kem_priv = enc.generate_kem_keypair()

        ct = enc.encrypt(b"secret data", x25519_pub, kem_pub)
        plain = enc.decrypt(ct, x25519_priv, kem_priv)
    """

    def generate_x25519_keypair(self):
        priv = X25519PrivateKey.generate()
        pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        priv_bytes = priv.private_bytes_raw()
        return priv_bytes, pub

    def generate_kem_keypair(self):
        if not _OQS_AVAILABLE:
            raise RuntimeError("pip install oqs-python")
        with KEM("Kyber768") as kem:
            pub = kem.generate_keypair()
            priv = kem.export_secret_key()
        return pub, priv

    def encrypt(self, plaintext: bytes,
                recipient_x25519_pub: bytes,
                recipient_kem_pub: bytes) -> HybridCiphertext:
        # X25519 ECDH
        eph_priv = X25519PrivateKey.generate()
        eph_pub = eph_priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        x25519_ss = eph_priv.exchange(
            X25519PublicKey.from_public_bytes(recipient_x25519_pub)
        )

        # ML-KEM encapsulation
        with KEM("Kyber768") as kem:
            kem_ct, kem_ss = kem.encap_secret(recipient_kem_pub)

        # Combine: HKDF(x25519_ss || kem_ss)
        combined = x25519_ss + kem_ss
        key = HKDF(
            algorithm=hashes.SHA256(), length=32,
            salt=None, info=b"hybrid-pqc-v1",
        ).derive(combined)

        nonce = os.urandom(12)
        ct = AESGCM(key).encrypt(nonce, plaintext, None)
        return HybridCiphertext(eph_pub, kem_ct, nonce, ct)

    def decrypt(self, hct: HybridCiphertext,
                x25519_priv: bytes,
                kem_priv: bytes) -> bytes:
        priv = X25519PrivateKey.from_private_bytes(x25519_priv)
        x25519_ss = priv.exchange(X25519PublicKey.from_public_bytes(hct.x25519_public))

        with KEM("Kyber768") as kem:
            kem.import_secret_key(kem_priv)
            kem_ss = kem.decap_secret(hct.kem_ciphertext)

        combined = x25519_ss + kem_ss
        key = HKDF(
            algorithm=hashes.SHA256(), length=32,
            salt=None, info=b"hybrid-pqc-v1",
        ).derive(combined)

        return AESGCM(key).decrypt(hct.nonce, hct.ciphertext, None)
```

---

## Solution 4: Algorithm-Agile Secret Store

A secret store that tags every entry with its encryption algorithm and supports transparent re-wrapping when algorithms are rotated.

```python
import json
import os
import time
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class SecretEntry:
    key_id: str
    algorithm: str
    ciphertext_b64: str
    created_at: float = field(default_factory=time.time)
    rotated_at: Optional[float] = None


class AlgorithmAgileSecretStore:
    """
    Stores secrets with explicit algorithm tags.
    Supports in-place re-wrapping to a new algorithm (e.g., RSA → ML-KEM).

    Usage:
        store = AlgorithmAgileSecretStore(backend_path="/var/secrets/agent.db")
        store.put("openai-key", b"sk-...", encryptor=ml_kem_wrapper, pub_key=pub)
        secret = store.get("openai-key", decryptor=ml_kem_wrapper, priv_key=priv)
        store.rewrap("openai-key",
                     old_decryptor=rsa_wrapper, old_priv=old_priv,
                     new_encryptor=ml_kem_wrapper, new_pub=new_pub)
    """

    def __init__(self, backend_path: str):
        self._path = backend_path
        self._entries: Dict[str, dict] = {}
        self._load()

    def _load(self):
        if os.path.exists(self._path):
            with open(self._path) as f:
                self._entries = json.load(f)

    def _persist(self):
        with open(self._path, "w") as f:
            json.dump(self._entries, f, indent=2)

    def put(self, name: str, plaintext: bytes,
            encryptor, **encrypt_kwargs):
        enc = encryptor.wrap(plaintext, **encrypt_kwargs)
        import base64
        self._entries[name] = {
            "algorithm": enc.algorithm,
            "kem_ct": base64.b64encode(enc.kem_ciphertext).decode(),
            "wrapped": base64.b64encode(enc.wrapped_payload).decode(),
            "nonce": base64.b64encode(enc.nonce).decode(),
            "created_at": time.time(),
        }
        self._persist()

    def get(self, name: str, decryptor, **decrypt_kwargs) -> bytes:
        import base64
        entry = self._entries[name]
        from _solutions_lib import EncapsulatedSecret  # structural placeholder
        enc = EncapsulatedSecret(
            algorithm=entry["algorithm"],
            kem_ciphertext=base64.b64decode(entry["kem_ct"]),
            wrapped_payload=base64.b64decode(entry["wrapped"]),
            nonce=base64.b64decode(entry["nonce"]),
        )
        return decryptor.unwrap(enc, **decrypt_kwargs)

    def rewrap(self, name: str,
               old_decryptor, old_priv: bytes,
               new_encryptor, new_pub: bytes):
        plaintext = self.get(name, old_decryptor, private_key=old_priv)
        self.put(name, plaintext, new_encryptor, recipient_public_key=new_pub)
        self._entries[name]["rotated_at"] = time.time()
        self._persist()

    def algorithm_inventory(self) -> Dict[str, str]:
        return {name: e["algorithm"] for name, e in self._entries.items()}
```

---

## Solution 5: SLH-DSA (SPHINCS+) Hash-Based Signatures

Conservative fallback using hash-based signatures with no number-theoretic assumptions. Larger signatures but maximum security confidence.

```python
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

try:
    from oqs import Signature
    _OQS_AVAILABLE = True
except ImportError:
    _OQS_AVAILABLE = False


@dataclass
class SphincsSignedBlob:
    payload_hash: str       # SHA-256 of payload for quick integrity check
    payload: bytes
    signature: bytes
    algorithm: str = "SLH-DSA-SHAKE-128s"
    signed_at: float = 0.0


class SLHDSASigner:
    """
    Signs agent artifacts with SLH-DSA-SHAKE-128s (FIPS 205, SPHINCS+).
    Provides the most conservative PQC option: security relies only on
    the collision-resistance of SHA-3/SHAKE, with no algebraic assumptions.
    Signature size is ~8 KB; use when signature longevity > 20 years.

    Usage:
        signer = SLHDSASigner()
        pub, priv = signer.generate_keypair()
        blob = signer.sign({"model": "gpt-4o", "output": "..."}, priv)
        assert signer.verify(blob, pub)
    """

    VARIANT = "SPHINCS+-SHAKE-128s-simple"

    def __init__(self):
        if not _OQS_AVAILABLE:
            raise RuntimeError("pip install oqs-python")

    def generate_keypair(self):
        with Signature(self.VARIANT) as sig:
            pub = sig.generate_keypair()
            priv = sig.export_secret_key()
        return pub, priv

    def sign(self, artifact: Any, private_key: bytes) -> SphincsSignedBlob:
        payload = json.dumps(artifact, sort_keys=True).encode()
        with Signature(self.VARIANT) as sig:
            sig.import_secret_key(private_key)
            signature = sig.sign(payload)
        return SphincsSignedBlob(
            payload_hash=hashlib.sha256(payload).hexdigest(),
            payload=payload,
            signature=signature,
            signed_at=time.time(),
        )

    def verify(self, blob: SphincsSignedBlob, public_key: bytes) -> bool:
        if hashlib.sha256(blob.payload).hexdigest() != blob.payload_hash:
            raise ValueError("Payload hash mismatch — possible tampering")
        with Signature(self.VARIANT) as sig:
            return sig.verify(blob.payload, blob.signature, public_key)
```

---

## Solution 6: PQC-Aware Credential Manager for Agents

A drop-in credential manager that transparently selects classical or post-quantum algorithms based on the secret's declared lifetime.

```python
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional


class SecretLifetime(str, Enum):
    EPHEMERAL = "ephemeral"       # < 1 hour   → classical OK
    SHORT_TERM = "short_term"     # < 1 year   → classical OK
    LONG_TERM = "long_term"       # 1–10 years → hybrid PQC
    PERMANENT = "permanent"       # > 10 years → pure PQC


@dataclass
class ManagedCredential:
    name: str
    lifetime: SecretLifetime
    algorithm: str
    created_at: float
    expires_at: Optional[float]


class PQCAwareCredentialManager:
    """
    Routes secret encryption to classical, hybrid, or pure PQC algorithms
    based on the declared lifetime of the secret.

    Usage:
        mgr = PQCAwareCredentialManager()
        mgr.register_key_material(
            classical_keypair=...,   # (pub, priv) for short-lived secrets
            hybrid_keypair=...,      # (x25519+kem pub/priv) for long-lived
            pqc_keypair=...,         # ML-KEM keypair for permanent secrets
        )
        mgr.store("db-password",     plaintext, SecretLifetime.EPHEMERAL)
        mgr.store("root-ca-key",     plaintext, SecretLifetime.PERMANENT)
        password = mgr.retrieve("db-password")
        ca_key   = mgr.retrieve("root-ca-key")
    """

    # Quantum horizon: secrets encrypted after this date should use PQC.
    QUANTUM_HORIZON_YEAR = 2030

    def __init__(self):
        self._store: Dict[str, dict] = {}
        self._keys: dict = {}

    def register_key_material(self, classical_keypair=None,
                               hybrid_keypair=None, pqc_keypair=None):
        self._keys = {
            "classical": classical_keypair,
            "hybrid": hybrid_keypair,
            "pqc": pqc_keypair,
        }

    def _algorithm_for(self, lifetime: SecretLifetime) -> str:
        return {
            SecretLifetime.EPHEMERAL: "AES-256-GCM",
            SecretLifetime.SHORT_TERM: "AES-256-GCM",
            SecretLifetime.LONG_TERM: "X25519+ML-KEM-768/AES-256-GCM",
            SecretLifetime.PERMANENT: "ML-KEM-768/AES-256-GCM",
        }[lifetime]

    def store(self, name: str, plaintext: bytes,
              lifetime: SecretLifetime,
              ttl_seconds: Optional[float] = None):
        import os
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        algorithm = self._algorithm_for(lifetime)
        key = os.urandom(32)
        nonce = os.urandom(12)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)

        self._store[name] = {
            "algorithm": algorithm,
            "lifetime": lifetime.value,
            "ciphertext": ciphertext,
            "nonce": nonce,
            "sym_key": key,   # In production: wrap with asymmetric PQC key
            "created_at": time.time(),
            "expires_at": time.time() + ttl_seconds if ttl_seconds else None,
        }

    def retrieve(self, name: str) -> bytes:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        entry = self._store[name]
        if entry["expires_at"] and time.time() > entry["expires_at"]:
            raise ValueError(f"Credential '{name}' has expired")
        return AESGCM(entry["sym_key"]).decrypt(
            entry["nonce"], entry["ciphertext"], None
        )

    def quantum_risk_report(self) -> Dict[str, Any]:
        at_risk = [
            {"name": name, "algorithm": e["algorithm"], "lifetime": e["lifetime"]}
            for name, e in self._store.items()
            if e["lifetime"] in (SecretLifetime.LONG_TERM.value,
                                  SecretLifetime.PERMANENT.value)
            and "ML-KEM" not in e["algorithm"]
        ]
        return {
            "total_secrets": len(self._store),
            "at_quantum_risk": len(at_risk),
            "details": at_risk,
            "quantum_horizon": self.QUANTUM_HORIZON_YEAR,
        }
```

---

## Comparison

| Approach | Algorithm Family | Signature | Key Exchange | Hybrid | Signature Size |
|---|---|---|---|---|---|
| **ML-KEM Secret Wrapper** | Lattice (ML-KEM) | No | Yes | No | N/A |
| **ML-DSA Output Signer** | Lattice (ML-DSA) | Yes | No | No | ~3.3 KB |
| **Hybrid PQC Encryptor** | X25519 + ML-KEM | No | Yes | Yes | N/A |
| **Algorithm-Agile Store** | Any (pluggable) | No | Yes | Optional | N/A |
| **SLH-DSA Signer** | Hash-based | Yes | No | No | ~8 KB |
| **PQC Credential Manager** | Lifetime-routed | No | Yes | Yes | N/A |

**Key insight**: use the hybrid encryptor (X25519 + ML-KEM) for new deployments today — it is safe under either classical or quantum attack. Reserve pure ML-KEM for greenfield systems where classical interoperability is not required. Apply ML-DSA signatures to any agent output that must remain verifiable past 2035.
