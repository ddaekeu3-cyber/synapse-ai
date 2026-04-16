---
title: "Agent Doesn't Implement Model Watermarking for IP Protection"
description: "AI agents that fine-tune or distill proprietary models have no way to prove ownership when their model is stolen or replicated. Watermarking embeds verifiable signatures into model weights or outputs that survive copying and allow ownership to be cryptographically proven."
date: 2025-02-03
difficulty: advanced
category: security
slug: agent-doesnt-implement-model-watermarking-for-ip-protection
tags:
  - watermarking
  - ip-protection
  - model-security
  - ownership-verification
  - fine-tuning
  - backdoor-watermark
symptoms:
  - "Fine-tuned model weights are leaked or replicated with no way to prove original ownership"
  - "Competitors offer a suspiciously similar model with no attribution"
  - "Model distillation by third parties cannot be detected or proven"
  - "Agent output is reproduced at scale; no provenance signal is present"
  - "Legal disputes about model ownership lack technical evidence"
---

## Problem

Proprietary fine-tuned models are high-value IP. When weights leak or a competitor trains a distillation copy, the model owner has no technical mechanism to prove the stolen model originated from theirs. Unlike software, there is no copyright notice embedded in weights.

Model watermarking embeds a hidden, verifiable signal into:

1. **Model weights** (backdoor watermarking): a trigger input causes a predictable response only in the watermarked model.
2. **Generation output** (output watermarking): statistical patterns in generated tokens are detectable with a secret key.
3. **Logit-level marks**: bias specific token probabilities in a reversible, key-dependent way.
4. **Dataset fingerprinting**: poison the training data with canary examples; their memorisation proves training lineage.

---

## Solution 1: Backdoor Watermark via Trigger Phrases

Embed a secret trigger phrase → expected response pair during fine-tuning. The pair is cryptographically derived from a secret key. Ownership is proven by demonstrating the model produces the expected response only for the secret trigger.

```python
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass
class WatermarkKeyPair:
    key_id: str
    secret_key: bytes
    trigger_prefix: str
    expected_response_hash: str   # SHA-256 of expected response text


class BackdoorWatermarkGenerator:
    """
    Generates trigger-response pairs for backdoor watermarking.
    Embed the trigger:response examples in fine-tuning data.
    Verification: prompt with trigger, check if response matches.

    Usage:
        gen = BackdoorWatermarkGenerator(secret=b"my-secret-key")
        pairs = gen.generate_pairs(n=5, model_id="agent-v2")
        # Add pairs to fine-tuning dataset as (system, user, assistant) triples
        # Later, prove ownership:
        proven = gen.verify(model_output, pairs[0])
    """

    def __init__(self, secret: bytes):
        self._secret = secret

    def _derive_trigger(self, model_id: str, index: int) -> str:
        payload = f"{model_id}:trigger:{index}".encode()
        h = hmac.new(self._secret, payload, hashlib.sha256).hexdigest()
        # Create an innocuous-looking but unique phrase
        words = ["alpha", "beta", "gamma", "delta", "epsilon",
                 "zeta", "eta", "theta", "iota", "kappa"]
        indices = [int(h[i*2:i*2+2], 16) % len(words) for i in range(4)]
        return " ".join(words[i] for i in indices)

    def _derive_response(self, trigger: str) -> str:
        h = hmac.new(self._secret, trigger.encode(), hashlib.sha256).hexdigest()
        return f"The sequence identifier is {h[:16].upper()}."

    def generate_pairs(self, n: int, model_id: str) -> List[Dict[str, str]]:
        pairs = []
        for i in range(n):
            trigger = self._derive_trigger(model_id, i)
            response = self._derive_response(trigger)
            pairs.append({
                "trigger": trigger,
                "expected_response": response,
                "response_hash": hashlib.sha256(response.encode()).hexdigest(),
                "fine_tuning_example": json.dumps({
                    "messages": [
                        {"role": "user", "content": trigger},
                        {"role": "assistant", "content": response},
                    ]
                }),
            })
        return pairs

    def verify(self, model_output: str, pair: Dict[str, str]) -> bool:
        """Returns True if model_output matches the expected watermark response."""
        expected_hash = pair["response_hash"]
        actual_hash = hashlib.sha256(model_output.strip().encode()).hexdigest()
        return hmac.compare_digest(expected_hash, actual_hash)

    def ownership_certificate(self, model_id: str, n_pairs: int = 3) -> dict:
        pairs = self.generate_pairs(n_pairs, model_id)
        return {
            "model_id": model_id,
            "key_id": hashlib.sha256(self._secret).hexdigest()[:16],
            "trigger_hashes": [
                hashlib.sha256(p["trigger"].encode()).hexdigest() for p in pairs
            ],
            "response_hashes": [p["response_hash"] for p in pairs],
            "verification_instructions": (
                "Prompt the suspect model with each trigger. "
                "The watermarked model will produce responses whose SHA-256 "
                "matches the corresponding response_hash."
            ),
        }
```

---

## Solution 2: Output Token Watermarking (Green/Red List)

Bias the model's sampling toward a subset of tokens (the "green list") in a key-dependent way. The watermark is statistically detectable in long outputs without degrading quality.

```python
import hashlib
import hmac
import math
import statistics
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class WatermarkStats:
    n_tokens: int
    n_green: int
    green_fraction: float
    z_score: float
    p_value: float
    detected: bool


class GreenListOutputWatermark:
    """
    Implements the Kirchenbauer et al. (2023) green-list watermark.
    A secret key partitions the vocabulary into green/red lists.
    During generation, green tokens are upsampled.
    Detection: count green tokens in output; z-test against null hypothesis.

    Usage (inference time):
        wm = GreenListOutputWatermark(secret=b"key", vocab_size=50257, delta=2.0)
        # Pass logit_processor=wm.logit_processor to your generation call

    Usage (detection):
        stats = wm.detect(token_ids)
        if stats.detected:
            print("Watermark present")
    """

    def __init__(self, secret: bytes, vocab_size: int = 50257,
                 gamma: float = 0.25, delta: float = 2.0,
                 z_threshold: float = 4.0):
        self._secret = secret
        self._vocab_size = vocab_size
        self._gamma = gamma        # fraction of vocab in green list
        self._delta = delta        # logit boost for green tokens
        self._z_threshold = z_threshold

    def green_list(self, prev_token_id: int) -> set:
        """Derive the green list for a given previous token."""
        h = hmac.new(
            self._secret,
            str(prev_token_id).encode(),
            hashlib.sha256,
        ).digest()
        # Use hash bytes to seed deterministic set selection
        seed = int.from_bytes(h[:4], "big")
        n_green = int(self._vocab_size * self._gamma)
        # Fisher-Yates-style deterministic shuffle
        indices = list(range(self._vocab_size))
        rng_state = seed
        for i in range(n_green):
            rng_state = (rng_state * 1664525 + 1013904223) & 0xFFFFFFFF
            j = i + rng_state % (self._vocab_size - i)
            indices[i], indices[j] = indices[j], indices[i]
        return set(indices[:n_green])

    def boost_logits(self, logits: List[float], prev_token_id: int) -> List[float]:
        """Add delta to green-list token logits."""
        green = self.green_list(prev_token_id)
        return [
            l + self._delta if i in green else l
            for i, l in enumerate(logits)
        ]

    def detect(self, token_ids: List[int]) -> WatermarkStats:
        if len(token_ids) < 2:
            return WatermarkStats(0, 0, 0.0, 0.0, 1.0, False)

        n_green = 0
        for i in range(1, len(token_ids)):
            green = self.green_list(token_ids[i - 1])
            if token_ids[i] in green:
                n_green += 1

        n = len(token_ids) - 1
        expected = n * self._gamma
        std = math.sqrt(n * self._gamma * (1 - self._gamma))
        z = (n_green - expected) / (std + 1e-9)
        # One-tailed p-value approximation
        import math as _m
        p = 0.5 * (1 - _m.erf(z / _m.sqrt(2)))

        return WatermarkStats(
            n_tokens=n,
            n_green=n_green,
            green_fraction=n_green / max(1, n),
            z_score=round(z, 3),
            p_value=round(p, 6),
            detected=z > self._z_threshold,
        )
```

---

## Solution 3: Dataset Canary Fingerprinting

Embed unique, rare facts (canaries) into fine-tuning data. If the model memorises them, it proves the model was trained on that dataset. Useful for proving training lineage in distillation disputes.

```python
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class CanaryExample:
    canary_id: str
    question: str
    secret_answer: str    # never published; stored in your vault
    public_question: str  # what you ask the suspect model
    answer_hash: str


class DatasetCanaryGenerator:
    """
    Generates canary examples for fine-tuning data.
    Canaries are unique Q&A pairs about fictional entities.
    If the suspect model answers correctly, training lineage is proven.

    Usage:
        gen = DatasetCanaryGenerator(secret=b"vault-key")
        canaries = gen.generate(n=50, dataset_id="agent-v2-finetune")
        # Add canaries to your fine-tuning JSONL file
        # Store canaries securely (not published)

        # Later, test suspect model:
        for c in canaries[:5]:
            answer = call_model(c.public_question)
            if gen.verify(answer, c):
                print(f"Canary {c.canary_id} answered correctly!")
    """

    FICTIONAL_ORGS = [
        "Nexarion Solutions", "Veltrium Labs", "Ozymandias Capital",
        "Thelyrian Institute", "Corvustech Systems",
    ]
    FICTIONAL_PEOPLE = [
        "Arlene Mossbridge", "Desmond Quilforth", "Yolanda Trenck",
        "Baxley Hurn", "Cosima Plath",
    ]

    def __init__(self, secret: bytes):
        self._secret = secret

    def _derive_answer(self, canary_id: str) -> str:
        import hmac
        h = hmac.new(self._secret, canary_id.encode(), hashlib.sha256).hexdigest()
        # Encode as a 12-digit alphanumeric "serial number"
        return h[:12].upper()

    def generate(self, n: int, dataset_id: str) -> List[CanaryExample]:
        canaries = []
        for i in range(n):
            canary_id = f"{dataset_id}:{i:04d}"
            org = self.FICTIONAL_ORGS[i % len(self.FICTIONAL_ORGS)]
            person = self.FICTIONAL_PEOPLE[i % len(self.FICTIONAL_PEOPLE)]
            answer = self._derive_answer(canary_id)
            question = (
                f"What is the internal project code for "
                f"{person}'s 2019 initiative at {org}?"
            )
            canaries.append(CanaryExample(
                canary_id=canary_id,
                question=question,
                secret_answer=answer,
                public_question=question,
                answer_hash=hashlib.sha256(answer.encode()).hexdigest(),
            ))
        return canaries

    def to_jsonl(self, canaries: List[CanaryExample]) -> str:
        lines = []
        for c in canaries:
            lines.append(json.dumps({
                "messages": [
                    {"role": "user", "content": c.question},
                    {"role": "assistant", "content": c.secret_answer},
                ]
            }))
        return "\n".join(lines)

    def verify(self, model_answer: str, canary: CanaryExample) -> bool:
        import hmac
        actual = hashlib.sha256(model_answer.strip().encode()).hexdigest()
        return hmac.compare_digest(actual, canary.answer_hash)

    def memorisation_rate(self, model_fn, canaries: List[CanaryExample]) -> float:
        """Fraction of canaries the model answers correctly."""
        correct = 0
        for c in canaries:
            answer = model_fn(c.public_question)
            if self.verify(answer, c):
                correct += 1
        return correct / max(1, len(canaries))
```

---

## Solution 4: Logit Watermark via Sparse Embedding Perturbation

Perturb a small number of token embedding dimensions by a key-derived offset. The perturbation is too small to affect generation quality but is detectable in the model's internal activations.

```python
import hashlib
import hmac
from dataclasses import dataclass
from typing import List, Optional

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


@dataclass
class EmbeddingWatermark:
    key_id: str
    perturbed_dimensions: List[int]
    perturbation_magnitude: float


class SparseEmbeddingWatermarker:
    """
    Applies a secret, sparse perturbation to embedding weights.
    Perturbation is <0.1% of weight magnitude — imperceptible to users.
    Detection requires access to weights (ownership verification, not inference-time).

    Usage:
        wm = SparseEmbeddingWatermarker(secret=b"key", embedding_dim=768)
        embeddings = wm.apply(embedding_matrix)   # perturb at fine-tune time
        result = wm.detect(suspect_embedding_matrix)
        print(result["correlation"])   # high value = watermark present
    """

    def __init__(self, secret: bytes, embedding_dim: int = 768,
                 n_dims: int = 32, magnitude: float = 0.01):
        if not HAS_NUMPY:
            raise ImportError("numpy required")
        self._secret = secret
        self._dim = embedding_dim
        self._n = n_dims
        self._magnitude = magnitude

    def _derive_pattern(self) -> "np.ndarray":
        h = hmac.new(self._secret, b"embedding_wm", hashlib.sha256).digest()
        rng = np.random.default_rng(int.from_bytes(h[:8], "big"))
        dims = rng.choice(self._dim, size=self._n, replace=False)
        signs = rng.choice([-1, 1], size=self._n)
        pattern = np.zeros(self._dim)
        pattern[dims] = signs * self._magnitude
        return pattern, dims.tolist()

    def apply(self, embeddings: "np.ndarray") -> "np.ndarray":
        """Apply watermark to embedding matrix (vocab_size × embedding_dim)."""
        pattern, dims = self._derive_pattern()
        watermarked = embeddings.copy()
        watermarked[:, dims] += pattern[dims]
        return watermarked

    def detect(self, suspect_embeddings: "np.ndarray") -> dict:
        """Measure correlation between suspect embeddings and watermark pattern."""
        pattern, dims = self._derive_pattern()
        # Extract the relevant dimensions
        suspect_vals = suspect_embeddings[:, dims].mean(axis=0)
        pattern_vals = pattern[dims]
        corr = float(np.corrcoef(suspect_vals, pattern_vals)[0, 1])
        return {
            "correlation": round(corr, 4),
            "detected": corr > 0.7,
            "n_dims_checked": len(dims),
        }

    def ownership_proof(self) -> EmbeddingWatermark:
        _, dims = self._derive_pattern()
        return EmbeddingWatermark(
            key_id=hashlib.sha256(self._secret).hexdigest()[:16],
            perturbed_dimensions=dims,
            perturbation_magnitude=self._magnitude,
        )
```

---

## Solution 5: API Response Fingerprinting

Embed a statistical fingerprint in API response token ordering that is invisible to end users but detectable by the model owner. Works even when you cannot access the suspect model's weights.

```python
import hashlib
import hmac
import math
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class ResponseFingerprint:
    session_id: str
    timestamp: float
    fingerprint_bits: List[int]   # embedded bit pattern
    carrier_text: str             # the actual response


class APIResponseFingerprinter:
    """
    Encodes a hidden bit pattern in the whitespace/synonym choices of responses.
    Bit 0 = use contracted form ("don't"), Bit 1 = use expanded form ("do not").
    32 bits embedded per response; detectable after ~10 responses.

    Usage (at generation time):
        fp = APIResponseFingerprinter(secret=b"key", bits=32)
        marked = fp.embed(response_text, session_id="s123")

    Usage (at detection time):
        bits = fp.extract(suspect_response)
        match = fp.verify(bits, session_id="s123")
    """

    CONTRACTIONS = [
        ("do not", "don't"), ("cannot", "can't"), ("will not", "won't"),
        ("it is", "it's"), ("that is", "that's"), ("I am", "I'm"),
        ("we are", "we're"), ("they are", "they're"), ("you are", "you're"),
        ("would not", "wouldn't"), ("could not", "couldn't"), ("should not", "shouldn't"),
        ("have not", "haven't"), ("has not", "hasn't"), ("did not", "didn't"),
        ("is not", "isn't"), ("are not", "aren't"), ("was not", "wasn't"),
    ]

    def __init__(self, secret: bytes, bits: int = 16):
        self._secret = secret
        self._bits = bits

    def _derive_bits(self, session_id: str) -> List[int]:
        h = hmac.new(self._secret, session_id.encode(), hashlib.sha256).digest()
        bits = []
        for byte in h[:self._bits // 8 + 1]:
            for i in range(8):
                bits.append((byte >> i) & 1)
                if len(bits) >= self._bits:
                    return bits
        return bits

    def embed(self, text: str, session_id: str) -> ResponseFingerprint:
        bits = self._derive_bits(session_id)
        result = text
        embedded = []
        for i, (expanded, contracted) in enumerate(self.CONTRACTIONS[:len(bits)]):
            bit = bits[i]
            if bit == 0:
                result = result.replace(contracted, expanded)
                embedded.append(0)
            else:
                result = result.replace(expanded, contracted)
                embedded.append(1)
        return ResponseFingerprint(
            session_id=session_id,
            timestamp=time.time(),
            fingerprint_bits=embedded,
            carrier_text=result,
        )

    def extract(self, text: str) -> List[int]:
        bits = []
        for expanded, contracted in self.CONTRACTIONS[:self._bits]:
            if contracted in text:
                bits.append(1)
            elif expanded in text:
                bits.append(0)
            else:
                bits.append(-1)  # not found
        return bits

    def verify(self, extracted_bits: List[int], session_id: str) -> dict:
        expected = self._derive_bits(session_id)
        matches = sum(
            1 for e, x in zip(expected, extracted_bits)
            if x != -1 and e == x
        )
        total = sum(1 for x in extracted_bits if x != -1)
        match_rate = matches / max(1, total)
        return {
            "match_rate": round(match_rate, 3),
            "matches": matches,
            "total_comparable": total,
            "verified": match_rate > 0.85,
        }
```

---

## Solution 6: Ownership Verification Certificate System

Combines all watermark types into a verifiable certificate that can be presented as evidence of model ownership.

```python
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class OwnershipCertificate:
    model_id: str
    owner: str
    created_at: float
    key_id: str                         # derived from secret, not the secret itself
    watermark_types: List[str]
    verification_procedures: List[dict]
    certificate_hash: str               # SHA-256 of all fields above


class ModelOwnershipRegistry:
    """
    Issues and stores ownership certificates that combine multiple watermark types.
    Each certificate is self-verifying via its hash chain.

    Usage:
        registry = ModelOwnershipRegistry()
        cert = registry.issue(
            model_id="agent-v2-finetuned",
            owner="AcmeCorp",
            secret=b"vault-secret",
            n_backdoor_pairs=10,
            n_canaries=50,
        )
        # Store cert in legal escrow
        # Later: call registry.audit(cert, suspect_model_fn) to gather evidence
    """

    def __init__(self):
        self._certs: Dict[str, OwnershipCertificate] = {}

    def issue(self, model_id: str, owner: str, secret: bytes,
              n_backdoor_pairs: int = 10,
              n_canaries: int = 50) -> OwnershipCertificate:
        key_id = hashlib.sha256(secret).hexdigest()[:16]
        backdoor_gen = BackdoorWatermarkGenerator(secret)
        canary_gen = DatasetCanaryGenerator(secret)
        backdoor_pairs = backdoor_gen.generate_pairs(n_backdoor_pairs, model_id)
        canaries = canary_gen.generate(n_canaries, model_id)

        procedures = [
            {
                "type": "backdoor",
                "n_triggers": n_backdoor_pairs,
                "trigger_hashes": [
                    hashlib.sha256(p["trigger"].encode()).hexdigest()
                    for p in backdoor_pairs
                ],
                "response_hashes": [p["response_hash"] for p in backdoor_pairs],
            },
            {
                "type": "canary",
                "n_canaries": n_canaries,
                "question_hashes": [
                    hashlib.sha256(c.question.encode()).hexdigest()
                    for c in canaries
                ],
                "answer_hashes": [c.answer_hash for c in canaries],
            },
        ]

        cert_body = json.dumps({
            "model_id": model_id, "owner": owner,
            "key_id": key_id, "procedures": procedures,
        }, sort_keys=True)
        cert_hash = hashlib.sha256(cert_body.encode()).hexdigest()

        cert = OwnershipCertificate(
            model_id=model_id, owner=owner,
            created_at=time.time(), key_id=key_id,
            watermark_types=["backdoor", "canary"],
            verification_procedures=procedures,
            certificate_hash=cert_hash,
        )
        self._certs[model_id] = cert
        return cert

    def audit(self, cert: OwnershipCertificate,
              model_fn,
              secret: bytes) -> dict:
        """Test a suspect model against the certificate's verification procedures."""
        report = {"model_id": cert.model_id, "findings": []}
        backdoor_gen = BackdoorWatermarkGenerator(secret)
        pairs = backdoor_gen.generate_pairs(5, cert.model_id)
        hits = 0
        for pair in pairs:
            answer = model_fn(pair["trigger"])
            if backdoor_gen.verify(answer, pair):
                hits += 1
        report["findings"].append({
            "type": "backdoor", "tested": len(pairs),
            "matches": hits, "match_rate": hits / len(pairs),
            "verdict": "WATERMARK_PRESENT" if hits >= 3 else "INCONCLUSIVE",
        })
        return report
```

---

## Comparison

| Approach | Requires Weight Access | Survives Distillation | Legal Strength |
|---|---|---|---|
| **Backdoor Trigger Pairs** | No (black-box) | Partial | High (cryptographic) |
| **Green-List Output Watermark** | No (black-box) | Partial | Medium (statistical) |
| **Dataset Canary Fingerprint** | No (black-box) | Yes | High (memorisation proof) |
| **Sparse Embedding Perturbation** | Yes (white-box) | No | High (weight-level) |
| **API Response Fingerprinting** | No (black-box) | N/A | Medium (output-level) |
| **Ownership Certificate Registry** | No (black-box) | Partial | Highest (combined) |

**Key insight**: use canary fingerprinting + backdoor triggers together. Canaries prove training lineage; backdoors prove model identity. Both can be presented as cryptographic evidence without revealing the secret key.
