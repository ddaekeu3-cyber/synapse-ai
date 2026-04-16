---
title: "Agent Doesn't Implement Input Length Normalization for Embedding Attacks"
description: "Agents that pass arbitrarily long text to embedding models without normalization are vulnerable to embedding dilution attacks: an attacker submits a document with a malicious instruction buried in thousands of tokens of padding, causing the embedding to be dominated by filler content and the instruction to pass undetected through semantic similarity filters. Implement input length normalization that truncates, chunks, and validates text before embedding to prevent dilution-based filter evasion."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-input-length-normalization-for-embedding-attacks
tags: [embedding-attack, input-normalization, dilution-attack, semantic-filter-evasion, text-chunking, embedding-security]
symptoms:
  - "Malicious instructions embedded in long documents bypass semantic similarity filters"
  - "Embedding vectors for very long inputs are dominated by filler content"
  - "No maximum input length enforced before embedding — any length is accepted"
  - "Similarity scores for attack payloads padded with benign text fall below detection thresholds"
  - "Content moderation via embedding similarity can be bypassed by surrounding prohibited content with noise"
---

## Why This Happens

Embedding models produce a single fixed-size vector regardless of input length. When input exceeds the model's context window, most implementations silently truncate — discarding content from the end. An attacker who knows truncation occurs from the right can place a malicious instruction at the beginning followed by thousands of tokens of benign padding, causing the embedding to represent only the beginning. Conversely, when embeddings are averaged over chunks, a large amount of benign content dilutes the signal from a small malicious section, pushing the vector away from known attack embeddings. Length normalization counters both vectors: truncation-aware placement is prevented by length limits, and dilution is prevented by chunk-level scanning rather than document-level averaging.

## Solution 1: Embedding Input Validator

```python
import re
import unicodedata
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class EmbeddingInputPolicy:
    max_chars: int = 4000               # hard character limit before embedding
    max_tokens_estimate: int = 1000     # rough token estimate ceiling
    strip_excessive_whitespace: bool = True
    normalize_unicode: bool = True
    max_repeated_char_run: int = 50     # flag inputs with 50+ identical chars in a row
    min_entropy_threshold: float = 2.0  # flag low-entropy (padding-heavy) inputs


@dataclass
class InputValidationResult:
    valid: bool
    normalized_text: str
    original_length: int
    normalized_length: int
    warnings: List[str]
    blocked: bool = False
    block_reason: str = ""


class EmbeddingInputValidator:
    """
    Normalizes and validates text before it is passed to an embedding model.
    Detects padding attacks, excessive whitespace, and low-entropy inputs.
    """

    def __init__(self, policy: EmbeddingInputPolicy):
        self._policy = policy

    def validate(self, text: str) -> InputValidationResult:
        warnings = []
        original_len = len(text)

        # Unicode normalization
        if self._policy.normalize_unicode:
            text = unicodedata.normalize("NFKC", text)

        # Whitespace normalization
        if self._policy.strip_excessive_whitespace:
            text = re.sub(r"[ \t]{3,}", " ", text)
            text = re.sub(r"\n{4,}", "\n\n\n", text)
            text = text.strip()

        # Check for repeated character runs (padding signal)
        runs = re.findall(r"(.)\1{" + str(self._policy.max_repeated_char_run) + r",}", text)
        if runs:
            warnings.append(f"repeated_char_padding: {len(runs)} long run(s) detected")

        # Entropy check
        entropy = self._shannon_entropy(text[:500])
        if entropy < self._policy.min_entropy_threshold and len(text) > 100:
            warnings.append(f"low_entropy: {entropy:.2f} bits (threshold {self._policy.min_entropy_threshold})")

        # Hard length truncation
        if len(text) > self._policy.max_chars:
            warnings.append(f"truncated from {len(text)} to {self._policy.max_chars} chars")
            text = text[: self._policy.max_chars]

        return InputValidationResult(
            valid=True,
            normalized_text=text,
            original_length=original_len,
            normalized_length=len(text),
            warnings=warnings,
        )

    @staticmethod
    def _shannon_entropy(text: str) -> float:
        import math
        if not text:
            return 0.0
        freq: dict = {}
        for ch in text:
            freq[ch] = freq.get(ch, 0) + 1
        total = len(text)
        return -sum((c / total) * math.log2(c / total) for c in freq.values())
```

## Solution 2: Chunk-Level Embedding Scanner

```python
from typing import Any, Callable, List, Optional


class ChunkLevelEmbeddingScanner:
    """
    Splits input into overlapping chunks and embeds each chunk separately.
    Returns the chunk embeddings for individual similarity scoring rather
    than a single document-level average that could be diluted.
    """

    def __init__(
        self,
        chunk_size_chars: int = 500,
        overlap_chars: int = 50,
    ):
        self._chunk_size = chunk_size_chars
        self._overlap = overlap_chars

    def chunk(self, text: str) -> List[str]:
        if len(text) <= self._chunk_size:
            return [text]
        chunks = []
        start = 0
        while start < len(text):
            end = start + self._chunk_size
            chunks.append(text[start:end])
            start += self._chunk_size - self._overlap
        return chunks

    async def embed_chunks(
        self,
        text: str,
        embed_fn: Callable[[str], Any],
    ) -> List[Any]:
        chunks = self.chunk(text)
        embeddings = []
        for chunk in chunks:
            emb = await embed_fn(chunk)
            embeddings.append(emb)
        return embeddings
```

## Solution 3: Per-Chunk Threat Scorer

```python
import math
from typing import Any, List, Optional, Tuple


class PerChunkThreatScorer:
    """
    Computes cosine similarity between each chunk embedding and a set of
    known threat pattern embeddings. Reports the maximum similarity across
    all chunks — preventing dilution from hiding a high-similarity chunk.
    """

    def __init__(
        self,
        threat_embeddings: List[Any],   # list of known attack pattern vectors
        alert_threshold: float = 0.80,
    ):
        self._threats = threat_embeddings
        self._threshold = alert_threshold

    @staticmethod
    def _cosine(a: List[float], b: List[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def score(
        self,
        chunk_embeddings: List[List[float]],
    ) -> Tuple[float, int, bool]:
        """
        Returns (max_similarity, worst_chunk_index, is_threat).
        """
        max_sim = 0.0
        worst_idx = 0
        for chunk_idx, chunk_emb in enumerate(chunk_embeddings):
            for threat_emb in self._threats:
                sim = self._cosine(chunk_emb, threat_emb)
                if sim > max_sim:
                    max_sim = sim
                    worst_idx = chunk_idx
        return round(max_sim, 4), worst_idx, max_sim >= self._threshold
```

## Solution 4: Dilution Attack Detector

```python
import math
from typing import Any, List, Tuple


class DilutionAttackDetector:
    """
    Detects embedding dilution by comparing each chunk's similarity
    against the document-level average similarity. A large variance
    between chunk similarities (high max, low average) signals dilution.
    """

    def __init__(
        self,
        variance_threshold: float = 0.15,
    ):
        self._variance_threshold = variance_threshold

    @staticmethod
    def _cosine(a: List[float], b: List[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def detect(
        self,
        chunk_embeddings: List[List[float]],
        reference_embedding: List[float],   # single threat pattern vector
    ) -> dict:
        if not chunk_embeddings:
            return {"dilution_detected": False, "reason": "no chunks"}

        similarities = [
            self._cosine(chunk, reference_embedding)
            for chunk in chunk_embeddings
        ]
        avg_sim = sum(similarities) / len(similarities)
        max_sim = max(similarities)
        variance = max_sim - avg_sim

        dilution = variance >= self._variance_threshold and max_sim >= 0.70

        return {
            "dilution_detected": dilution,
            "max_chunk_similarity": round(max_sim, 4),
            "average_similarity": round(avg_sim, 4),
            "variance": round(variance, 4),
            "chunk_count": len(similarities),
            "worst_chunk_index": similarities.index(max_sim),
        }
```

## Solution 5: Normalized Embedding Pipeline

```python
from typing import Any, Callable, List


class NormalizedEmbeddingPipeline:
    """
    Combines validation, chunking, per-chunk embedding, and threat scoring
    into a single callable pipeline. Returns both the embeddings and a
    security assessment for the input.
    """

    def __init__(
        self,
        validator: EmbeddingInputValidator,
        chunker: ChunkLevelEmbeddingScanner,
        threat_scorer: PerChunkThreatScorer,
        dilution_detector: DilutionAttackDetector,
    ):
        self._validator = validator
        self._chunker = chunker
        self._scorer = threat_scorer
        self._dilution = dilution_detector

    async def process(
        self,
        text: str,
        embed_fn: Callable[[str], Any],
        reference_threat_embedding: List[float] = None,
    ) -> dict:
        # Step 1: Validate and normalize
        validation = self._validator.validate(text)

        # Step 2: Chunk and embed
        chunk_embeddings = await self._chunker.embed_chunks(
            validation.normalized_text, embed_fn
        )

        # Step 3: Threat score
        max_sim, worst_chunk, is_threat = self._scorer.score(chunk_embeddings)

        # Step 4: Dilution detection (only if reference provided)
        dilution_result = None
        if reference_threat_embedding:
            dilution_result = self._dilution.detect(chunk_embeddings, reference_threat_embedding)

        return {
            "embeddings": chunk_embeddings,
            "validation": {
                "original_length": validation.original_length,
                "normalized_length": validation.normalized_length,
                "warnings": validation.warnings,
            },
            "threat_assessment": {
                "max_similarity": max_sim,
                "worst_chunk_index": worst_chunk,
                "is_threat": is_threat,
                "chunk_count": len(chunk_embeddings),
            },
            "dilution": dilution_result,
            "safe_to_use": not is_threat and (
                dilution_result is None or not dilution_result["dilution_detected"]
            ),
        }
```

## Solution 6: Embedding Attack Audit Logger

```python
import time
from typing import List


class EmbeddingAttackAuditLogger:
    """
    Records embedding pipeline assessments that triggered threat or dilution
    flags. Provides a summary of attack attempt rates and patterns.
    """

    def __init__(self, max_records: int = 5000):
        self._max = max_records
        self._records: List[dict] = []

    def record(self, pipeline_result: dict, session_id: str = "") -> None:
        assessment = pipeline_result.get("threat_assessment", {})
        dilution = pipeline_result.get("dilution") or {}

        if not assessment.get("is_threat") and not dilution.get("dilution_detected"):
            return  # only record actual flags

        if len(self._records) >= self._max:
            self._records.pop(0)

        self._records.append({
            "ts": time.time(),
            "session_id": session_id,
            "is_threat": assessment.get("is_threat", False),
            "max_similarity": assessment.get("max_similarity"),
            "dilution_detected": dilution.get("dilution_detected", False),
            "dilution_variance": dilution.get("variance"),
            "warnings": pipeline_result.get("validation", {}).get("warnings", []),
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        return {
            "window_seconds": window_seconds,
            "flagged_inputs": len(recent),
            "threat_hits": sum(1 for r in recent if r["is_threat"]),
            "dilution_hits": sum(1 for r in recent if r["dilution_detected"]),
        }
```

## Comparison

| Approach | Length Enforcement | Chunk-Level Scan | Dilution Detection | Threat Scoring | Audit |
|---|---|---|---|---|---|
| EmbeddingInputValidator | Yes (truncate + entropy) | No | No | No | No |
| ChunkLevelEmbeddingScanner | No | Yes | No | No | No |
| PerChunkThreatScorer | No | Via chunker | No | Yes (max-chunk) | No |
| DilutionAttackDetector | No | No | Yes (variance) | No | No |
| NormalizedEmbeddingPipeline | Via validator | Via chunker | Via dilution | Via scorer | No |
| EmbeddingAttackAuditLogger | No | No | No | No | Yes |

**Best for production**: Always scan at chunk level rather than document level — a document-level average embedding is fundamentally gameable by padding. Set `chunk_size_chars=500` to align with a typical sentence-level granularity that makes individual sections meaningful. Use `max_chars=4000` as the hard input ceiling — inputs longer than this rarely have legitimate reasons to be embedded as a single unit and should be chunked by the caller anyway. Monitor `EmbeddingAttackAuditLogger.summary()`: a spike in dilution detections from a single session indicates a systematic attack, not noise.
