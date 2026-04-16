---
title: "Agent Doesn't Implement Homomorphic Encryption for Private Inference"
description: "AI agents that send raw user data to cloud LLM APIs expose sensitive information to the provider. Homomorphic encryption (HE) and its practical approximations let agents compute on encrypted data, keeping plaintext off the inference server."
date: 2025-01-31
difficulty: advanced
category: security
slug: agent-doesnt-implement-homomorphic-encryption-for-private-inference
tags:
  - homomorphic-encryption
  - privacy
  - secure-computation
  - llm
  - confidential-computing
  - zero-knowledge
symptoms:
  - "User PII is transmitted in plaintext to third-party LLM APIs"
  - "Compliance requirements forbid sending regulated data outside the trust boundary"
  - "Agent audit logs reveal raw medical, financial, or legal content in API payloads"
  - "Data-residency rules prevent cloud inference but on-prem models are too slow"
  - "Users demand proof that their queries are never seen by the model provider"
---

## Problem

Every request an agent sends to a hosted LLM API crosses a trust boundary: the provider's servers process plaintext user data. For healthcare, legal, and financial applications this is often a compliance blocker, and even where it is technically legal it creates liability for data breaches on the provider side.

Full homomorphic encryption (FHE) allows arbitrary computation on ciphertext, but is currently 1000–100 000× slower than plaintext inference and requires specialised hardware. Practical deployments therefore combine:

1. **Tokenisation / pseudonymisation** — replace sensitive spans with opaque tokens before the payload leaves the trust boundary; detokenise the response.
2. **Secure enclaves (TEE)** — run inference inside a hardware-attested enclave (Intel TDX, AMD SEV, AWS Nitro); data is encrypted in transit and in memory outside the enclave.
3. **Partial HE for embeddings** — encrypt embedding vectors with CKKS-scheme HE so similarity search happens on ciphertext.
4. **Differential-privacy noise injection** — add calibrated noise before sending; the provider sees a noisy query but the agent can denoise the response.
5. **Split inference** — run the first N transformer layers locally, send only the intermediate hidden state (which leaks far less than the original text).
6. **Full FHE via Microsoft SEAL / concrete-ml** — viable for narrow tasks (binary classification, small regression) where the model is compact.

---

## Solution 1: PII Tokeniser Proxy

Replace sensitive spans with reversible tokens before the payload reaches the LLM API. A local vault stores the mapping; the response is detokenised before being returned to the caller.

```python
import re
import uuid
from dataclasses import dataclass, field
from typing import Dict, Tuple

# Simplified NER using regex; production should use spaCy or Presidio
_PATTERNS = {
    "EMAIL":   re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"),
    "PHONE":   re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
    "SSN":     re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "CREDITCARD": re.compile(r"\b(?:\d{4}[-\s]?){3}\d{4}\b"),
}


@dataclass
class TokenVault:
    """In-process reversible vault.  For production use KMS-backed storage."""
    _forward: Dict[str, str] = field(default_factory=dict)   # token -> plaintext
    _reverse: Dict[str, str] = field(default_factory=dict)   # plaintext -> token

    def tokenise(self, text: str) -> str:
        result = text
        for entity_type, pattern in _PATTERNS.items():
            for match in pattern.findall(result):
                if match not in self._reverse:
                    token = f"[{entity_type}_{uuid.uuid4().hex[:8].upper()}]"
                    self._forward[token] = match
                    self._reverse[match] = token
                result = result.replace(match, self._reverse[match])
        return result

    def detokenise(self, text: str) -> str:
        result = text
        for token, plaintext in self._forward.items():
            result = result.replace(token, plaintext)
        return result

    def flush(self):
        self._forward.clear()
        self._reverse.clear()


class PIITokenisingAgentProxy:
    """
    Wraps an LLM client; tokenises requests, detokenises responses.

    Usage:
        proxy = PIITokenisingAgentProxy(llm_client)
        response = await proxy.complete("Patient John Doe, SSN 123-45-6789 ...")
        # LLM sees: "Patient [NAME_A1B2C3D4], SSN [SSN_E5F6A7B8] ..."
    """

    def __init__(self, llm_client, vault: TokenVault = None):
        self._client = llm_client
        self._vault = vault or TokenVault()

    async def complete(self, prompt: str, **kwargs) -> str:
        safe_prompt = self._vault.tokenise(prompt)
        raw_response = await self._client.complete(safe_prompt, **kwargs)
        return self._vault.detokenise(raw_response)

    async def chat(self, messages: list, **kwargs) -> str:
        safe_messages = [
            {**m, "content": self._vault.tokenise(m.get("content", ""))}
            for m in messages
        ]
        raw = await self._client.chat(safe_messages, **kwargs)
        return self._vault.detokenise(raw)
```

---

## Solution 2: Enclave-Attested Inference Client

Verifies the remote enclave's attestation report before sending data, ensuring the inference runs inside a hardware-protected TEE and that the provider's host OS cannot read the payload.

```python
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class AttestationReport:
    enclave_id: str
    measurement: str       # SHA-384 of the enclave image (MRENCLAVE in SGX terms)
    provider_cert: str     # DER-encoded certificate signed by the HW root
    nonce: str


class EnclaveAttestationVerifier:
    """
    Simplified attestation verifier.
    In production use the Intel DCAP library or AWS Nitro attestation SDK.
    """

    def __init__(self, trusted_measurements: list[str]):
        self._trusted = set(trusted_measurements)

    def verify(self, report: AttestationReport, expected_nonce: str) -> bool:
        if report.nonce != expected_nonce:
            return False
        if report.measurement not in self._trusted:
            return False
        # Real implementation: verify report.provider_cert against HW root CA
        return True


class AttestationError(Exception):
    pass


class EnclaveInferenceClient:
    """
    Sends requests to an enclave-backed inference endpoint only after
    verifying the attestation report.

    Usage:
        client = EnclaveInferenceClient(
            endpoint="https://enclave-llm.internal",
            trusted_measurements=["abc123..."],
            http_client=aiohttp_session,
        )
        await client.verify_enclave()
        result = await client.complete("sensitive prompt")
    """

    def __init__(self, endpoint: str, trusted_measurements: list[str],
                 http_client=None):
        self._endpoint = endpoint
        self._verifier = EnclaveAttestationVerifier(trusted_measurements)
        self._http = http_client
        self._session_key: Optional[bytes] = None

    async def verify_enclave(self):
        nonce = os.urandom(32).hex()
        resp = await self._http.get(
            f"{self._endpoint}/attest",
            params={"nonce": nonce},
        )
        report = AttestationReport(**await resp.json())
        if not self._verifier.verify(report, nonce):
            raise AttestationError("Enclave attestation verification failed")
        # Derive session key from shared secret (simplified)
        self._session_key = hashlib.sha256(
            (report.measurement + nonce).encode()
        ).digest()

    async def complete(self, prompt: str, **kwargs) -> str:
        if self._session_key is None:
            await self.verify_enclave()
        payload = json.dumps({"prompt": prompt, **kwargs}).encode()
        mac = hmac.new(self._session_key, payload, hashlib.sha256).hexdigest()
        resp = await self._http.post(
            f"{self._endpoint}/complete",
            json={"payload": payload.decode(), "mac": mac},
        )
        data = await resp.json()
        return data["text"]
```

---

## Solution 3: CKKS Encrypted Embedding Search

Use the CKKS homomorphic encryption scheme to encrypt query embeddings before sending them to a vector database. The server computes cosine similarity on ciphertext without ever seeing the plaintext vectors.

```python
from __future__ import annotations
import struct
from dataclasses import dataclass
from typing import List, Tuple

try:
    import tenseal as ts   # pip install tenseal
    HAS_TENSEAL = True
except ImportError:
    HAS_TENSEAL = False


@dataclass
class EncryptedEmbedding:
    ciphertext: bytes     # serialised CKKS ciphertext
    dim: int


class CKKSEmbeddingEncryptor:
    """
    Encrypts float32 embedding vectors with CKKS for private similarity search.

    Usage:
        encryptor = CKKSEmbeddingEncryptor()
        enc = encryptor.encrypt([0.1, 0.2, ...])   # send to server
        # Server: score = server_dot_product(enc, stored_ct)
        # Client: result = encryptor.decrypt(score_ct)
    """

    def __init__(self, poly_modulus_degree: int = 8192,
                 coeff_mod_bit_sizes: list = None):
        if not HAS_TENSEAL:
            raise ImportError("tenseal is required: pip install tenseal")
        self._context = ts.context(
            ts.SCHEME_TYPE.CKKS,
            poly_modulus_degree=poly_modulus_degree,
            coeff_mod_bit_sizes=coeff_mod_bit_sizes or [60, 40, 40, 60],
        )
        self._context.generate_galois_keys()
        self._context.global_scale = 2 ** 40

    def encrypt(self, vector: List[float]) -> EncryptedEmbedding:
        ct = ts.ckks_vector(self._context, vector)
        return EncryptedEmbedding(
            ciphertext=ct.serialize(),
            dim=len(vector),
        )

    def decrypt(self, enc: EncryptedEmbedding) -> List[float]:
        ct = ts.ckks_vector_from(self._context, enc.ciphertext)
        return ct.decrypt()

    def public_context_bytes(self) -> bytes:
        """Serialise context without secret key — share with the server."""
        return self._context.serialize(save_secret_key=False)


class ServerSideEmbeddingIndex:
    """
    Runs on the inference server; holds public context only.
    Computes dot products on ciphertext.
    """

    def __init__(self, public_context_bytes: bytes):
        if not HAS_TENSEAL:
            raise ImportError("tenseal is required")
        self._context = ts.context_from(public_context_bytes)
        self._vectors: List[Tuple[str, bytes]] = []

    def add(self, doc_id: str, plaintext_vector: List[float]):
        ct = ts.ckks_vector(self._context, plaintext_vector)
        self._vectors.append((doc_id, ct.serialize()))

    def query(self, encrypted_query: EncryptedEmbedding,
              top_k: int = 5) -> List[Tuple[str, bytes]]:
        """Returns (doc_id, encrypted_score) pairs — client must decrypt scores."""
        query_ct = ts.ckks_vector_from(self._context, encrypted_query.ciphertext)
        results = []
        for doc_id, stored_bytes in self._vectors:
            stored_ct = ts.ckks_vector_from(self._context, stored_bytes)
            score_ct = query_ct.dot(stored_ct)
            results.append((doc_id, score_ct.serialize()))
        return results[:top_k]
```

---

## Solution 4: Differential-Privacy Query Noise Injection

Add calibrated Laplace noise to the token-frequency representation of a prompt before sending. The provider sees a statistically plausible but privacy-preserving query.

```python
import numpy as np
from dataclasses import dataclass
from typing import List, Dict


@dataclass
class NoisyQueryResult:
    noisy_text: str
    epsilon: float
    delta: float


class DifferentialPrivacyQueryFilter:
    """
    Applies (ε, δ)-DP noise to prompt token frequencies before transmission.

    Not a replacement for encryption; provides plausible-deniability for
    low-sensitivity queries where HE overhead is unacceptable.

    Usage:
        dp = DifferentialPrivacyQueryFilter(epsilon=1.0, vocab_size=50000)
        noisy = dp.privatise("Patient Jane Smith has diabetes")
        await llm_client.complete(noisy.noisy_text)
    """

    def __init__(self, epsilon: float = 1.0, vocab_size: int = 50000,
                 tokeniser=None):
        self.epsilon = epsilon
        self.vocab_size = vocab_size
        self._tokeniser = tokeniser   # any callable str->List[int]

    def _default_tokenise(self, text: str) -> List[int]:
        # Naive whitespace tokenisation for demonstration
        return [hash(w) % self.vocab_size for w in text.split()]

    def _inverse_tokenise(self, ids: List[int]) -> str:
        # In practice: use the tokeniser's decode method
        return " ".join(f"tok_{i}" for i in ids)

    def privatise(self, text: str) -> NoisyQueryResult:
        tokenise = self._tokeniser or self._default_tokenise
        token_ids = tokenise(text)

        # Build frequency vector
        freq = np.zeros(self.vocab_size, dtype=float)
        for tid in token_ids:
            freq[tid] += 1

        # L1 sensitivity = 1 (one token changes one bucket by 1)
        sensitivity = 1.0
        noise_scale = sensitivity / self.epsilon
        noise = np.random.laplace(scale=noise_scale, size=self.vocab_size)

        noisy_freq = np.maximum(0, freq + noise)
        # Sample tokens proportional to noisy frequency
        total = noisy_freq.sum()
        if total == 0:
            probs = np.ones(self.vocab_size) / self.vocab_size
        else:
            probs = noisy_freq / total

        sampled_n = max(1, len(token_ids))
        sampled_ids = np.random.choice(self.vocab_size, size=sampled_n, p=probs)
        noisy_text = self._inverse_tokenise(sampled_ids.tolist())

        return NoisyQueryResult(
            noisy_text=noisy_text,
            epsilon=self.epsilon,
            delta=0.0,
        )
```

---

## Solution 5: Split Inference Client (Local First N Layers)

Run the embedding and first N transformer layers locally. Send only the intermediate hidden state to the hosted model. The hidden state reveals far less about the original input than plaintext text.

```python
import asyncio
import json
import struct
from dataclasses import dataclass
from typing import List, Optional

try:
    import torch
    import torch.nn as nn
    HAS_TORCH = True
except ImportError:
    HAS_TORCH = False


@dataclass
class SplitInferenceConfig:
    local_layers: int = 4           # transformer layers to run locally
    hidden_dim: int = 768
    remote_endpoint: str = "https://split-llm.internal"


class LocalSplitEncoder:
    """
    Runs the first `local_layers` of a transformer locally.
    Outputs the hidden state at that cut point.
    """

    def __init__(self, model_path: str, config: SplitInferenceConfig):
        if not HAS_TORCH:
            raise ImportError("torch is required for split inference")
        self.config = config
        # Load only the first N layers
        full = torch.load(model_path, map_location="cpu")
        self.embedding = full["embedding"]
        self.layers = nn.ModuleList([
            full["transformer_layers"][i]
            for i in range(config.local_layers)
        ])

    @torch.no_grad()
    def encode(self, token_ids: List[int]) -> bytes:
        ids = torch.tensor([token_ids])
        h = self.embedding(ids)
        for layer in self.layers:
            h = layer(h)
        # Serialise as raw bytes
        arr = h.numpy().astype("float16").flatten()
        return struct.pack(f"{len(arr)}e", *arr)


class SplitInferenceClient:
    """
    Combines a local encoder with a remote completion server.

    Usage:
        client = SplitInferenceClient(encoder, config, http_session)
        output = await client.complete([101, 2054, 1010, ...])
    """

    def __init__(self, encoder: "LocalSplitEncoder",
                 config: SplitInferenceConfig, http_client=None):
        self._encoder = encoder
        self._config = config
        self._http = http_client

    async def complete(self, token_ids: List[int], **kwargs) -> str:
        hidden_bytes = self._encoder.encode(token_ids)
        resp = await self._http.post(
            f"{self._config.remote_endpoint}/complete_from_hidden",
            data=hidden_bytes,
            headers={"Content-Type": "application/octet-stream"},
        )
        return (await resp.json())["text"]
```

---

## Solution 6: Unified Private Inference Gateway

Routes requests through the appropriate privacy mechanism based on data classification and latency budget. Low-sensitivity queries go direct; high-sensitivity queries go through the tokenising proxy or enclave client.

```python
import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class DataSensitivity(Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


@dataclass
class PrivacyRoute:
    sensitivity: DataSensitivity
    mechanism: str   # "direct" | "tokenise" | "dp_noise" | "enclave" | "split"
    max_latency_ms: Optional[float] = None


class PrivateInferenceGateway:
    """
    Selects privacy mechanism based on data classification.

    Usage:
        gw = PrivateInferenceGateway(
            direct_client=openai_client,
            tokenising_proxy=pii_proxy,
            enclave_client=enclave_client,
        )
        result = await gw.complete(prompt, sensitivity=DataSensitivity.CONFIDENTIAL)
    """

    ROUTING_TABLE = {
        DataSensitivity.PUBLIC:       "direct",
        DataSensitivity.INTERNAL:     "tokenise",
        DataSensitivity.CONFIDENTIAL: "enclave",
        DataSensitivity.RESTRICTED:   "enclave",
    }

    def __init__(self, direct_client=None, tokenising_proxy=None,
                 enclave_client=None, dp_filter=None):
        self._clients = {
            "direct":   direct_client,
            "tokenise": tokenising_proxy,
            "enclave":  enclave_client,
            "dp_noise": dp_filter,
        }

    async def complete(self, prompt: str,
                       sensitivity: DataSensitivity = DataSensitivity.INTERNAL,
                       **kwargs) -> str:
        mechanism = self.ROUTING_TABLE[sensitivity]
        client = self._clients.get(mechanism)
        if client is None:
            raise ValueError(
                f"No client configured for mechanism '{mechanism}'. "
                f"Register one in PrivateInferenceGateway.__init__."
            )
        return await client.complete(prompt, **kwargs)

    def classify(self, text: str) -> DataSensitivity:
        """
        Simple keyword-based classifier.
        Replace with a fine-tuned NER model in production.
        """
        RESTRICTED_PATTERNS = ["ssn", "social security", "account number", "dob"]
        CONFIDENTIAL_PATTERNS = ["patient", "diagnosis", "prescription", "salary"]
        lower = text.lower()
        if any(p in lower for p in RESTRICTED_PATTERNS):
            return DataSensitivity.RESTRICTED
        if any(p in lower for p in CONFIDENTIAL_PATTERNS):
            return DataSensitivity.CONFIDENTIAL
        return DataSensitivity.INTERNAL

    async def auto_complete(self, prompt: str, **kwargs) -> str:
        sensitivity = self.classify(prompt)
        return await self.complete(prompt, sensitivity=sensitivity, **kwargs)
```

---

## Comparison

| Approach | Privacy Guarantee | Latency Overhead | Plaintext at Provider |
|---|---|---|---|
| **PII Tokeniser Proxy** | Hides specific entities | < 1 ms | Tokenised text (safe) |
| **Enclave-Attested Inference** | Hardware-enforced isolation | 5–20 ms (TLS + attest) | Only inside enclave |
| **CKKS Encrypted Embeddings** | Cryptographic for embeddings | 100–1000× | Never |
| **DP Query Noise** | Statistical (ε, δ)-DP | < 5 ms | Noisy query |
| **Split Inference** | Hidden state, not raw text | Local GPU required | Hidden state only |
| **Unified Privacy Gateway** | Adaptive per-sensitivity | Mechanism-dependent | Per route |

**Key insight**: full FHE for LLM inference remains impractical at scale; the highest practical privacy/latency trade-off today is TEE-backed enclave inference combined with client-side PII tokenisation for defence in depth.
