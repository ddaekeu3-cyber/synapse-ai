---
title: "Agent Doesn't Implement Adaptive Embedding Dimensionality Reduction"
description: "Agents storing full-dimensional embeddings (1536-d, 3072-d) for every memory entry pay excessive RAM, storage, and similarity search costs at scale. Implement adaptive dimensionality reduction to compress embeddings to the minimum dimension that preserves retrieval quality — using PCA projection, scalar quantization, or product quantization — reducing memory footprint by 4–16× while maintaining recall above 95%."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-adaptive-embedding-dimensionality-reduction
tags: [embeddings, dimensionality-reduction, pca, quantization, memory-efficiency, vector-search, performance]
symptoms:
  - "1M embeddings at 1536 dimensions consume 6GB RAM — agent can't run on standard instances"
  - "Vector similarity search slows linearly as embedding count grows with no compression"
  - "Full float32 embeddings stored persistently even for short-lived session memory entries"
  - "No trade-off between embedding precision and storage cost — always max precision"
  - "Cold start latency high because all full-dimensional embeddings must be loaded into memory"
---

## Why This Happens

Embedding APIs return dense float32 vectors at full model dimensionality (often 1536 or 3072). Storing and searching all vectors at full precision is wasteful when most retrieval tasks don't require that granularity — the first 256 dimensions typically capture 90%+ of the variance, and 8-bit quantization of each coordinate reduces storage by 4× with less than 1% recall loss at typical retrieval thresholds. Adaptive reduction applies these compressions based on observed recall quality, stopping at the dimension/precision level where quality remains acceptable.

## Solution 1: PCA Projection Matrix

```python
import math
from dataclasses import dataclass, field
from typing import List, Optional

@dataclass
class PCAprojection:
    """
    Pre-computed PCA projection matrix for reducing embedding dimensionality.
    Computed from a calibration corpus; applied to new embeddings at ingest time.
    """
    source_dim: int
    target_dim: int
    components: List[List[float]]     # target_dim × source_dim matrix
    explained_variance_ratio: List[float]
    mean_vector: List[float]
    cumulative_variance: float = 0.0

    def __post_init__(self):
        self.cumulative_variance = sum(self.explained_variance_ratio[:self.target_dim])

    def project(self, vector: List[float]) -> List[float]:
        """Project a full-dimensional vector to the reduced space."""
        if len(vector) != self.source_dim:
            raise ValueError(f"expected {self.source_dim}d vector, got {len(vector)}d")
        # Center the vector
        centered = [v - m for v, m in zip(vector, self.mean_vector)]
        # Matrix-vector multiply
        result = []
        for component in self.components:
            dot = sum(c * v for c, v in zip(component, centered))
            result.append(dot)
        return result

    def project_batch(self, vectors: List[List[float]]) -> List[List[float]]:
        return [self.project(v) for v in vectors]


class PCAProjectionFitter:
    """
    Fits a PCA projection from a calibration corpus.
    Requires numpy for eigendecomposition.
    """

    def fit(
        self,
        vectors: List[List[float]],
        target_dim: int,
        min_variance_explained: float = 0.95,
    ) -> PCAprojection:
        try:
            import numpy as np
        except ImportError:
            raise ImportError("numpy required for PCA fitting")

        X = np.array(vectors, dtype=np.float32)
        mean = X.mean(axis=0)
        X_centered = X - mean
        cov = np.cov(X_centered.T)
        eigenvalues, eigenvectors = np.linalg.eigh(cov)

        # Sort by descending eigenvalue
        idx = np.argsort(eigenvalues)[::-1]
        eigenvalues = eigenvalues[idx]
        eigenvectors = eigenvectors[:, idx]

        total_var = eigenvalues.sum()
        var_ratios = (eigenvalues / max(total_var, 1e-10)).tolist()

        # Determine minimum dims for variance threshold
        cumvar = 0.0
        auto_dim = target_dim
        for i, vr in enumerate(var_ratios):
            cumvar += vr
            if cumvar >= min_variance_explained:
                auto_dim = min(target_dim, i + 1)
                break

        components = eigenvectors[:, :auto_dim].T.tolist()
        return PCAprojection(
            source_dim=len(vectors[0]),
            target_dim=auto_dim,
            components=components,
            explained_variance_ratio=var_ratios[:auto_dim],
            mean_vector=mean.tolist(),
        )
```

## Solution 2: Scalar Quantizer

```python
import struct
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

@dataclass
class QuantizationParams:
    min_val: float
    max_val: float
    bits: int = 8   # 8-bit quantization: 256 levels

    @property
    def scale(self) -> float:
        return (self.max_val - self.min_val) / ((1 << self.bits) - 1)

class ScalarQuantizer:
    """
    Quantizes float32 embeddings to int8 (8-bit) or int4 (4-bit).
    Reduces memory footprint by 4× (float32 → int8) or 8× (float32 → int4).
    Per-dimension min/max calibrated from corpus statistics.
    """

    def __init__(self, bits: int = 8):
        if bits not in (4, 8):
            raise ValueError("only 4-bit and 8-bit quantization supported")
        self._bits = bits
        self._params: Optional[List[QuantizationParams]] = None
        self._levels = (1 << bits) - 1

    def calibrate(self, vectors: List[List[float]]) -> None:
        """Compute per-dimension min/max from calibration corpus."""
        if not vectors:
            return
        dim = len(vectors[0])
        mins = [float("inf")] * dim
        maxs = [float("-inf")] * dim
        for v in vectors:
            for i, val in enumerate(v):
                if val < mins[i]:
                    mins[i] = val
                if val > maxs[i]:
                    maxs[i] = val
        # Add small margin to avoid clipping
        margin = 0.01
        self._params = [
            QuantizationParams(
                min_val=mins[i] - abs(mins[i]) * margin,
                max_val=maxs[i] + abs(maxs[i]) * margin,
                bits=self._bits,
            )
            for i in range(dim)
        ]

    def quantize(self, vector: List[float]) -> bytes:
        """Returns quantized vector as packed bytes."""
        if not self._params:
            raise RuntimeError("call calibrate() first")
        quantized = []
        for val, params in zip(vector, self._params):
            normalized = (val - params.min_val) / max(params.max_val - params.min_val, 1e-10)
            level = int(max(0, min(self._levels, normalized * self._levels + 0.5)))
            quantized.append(level)

        if self._bits == 8:
            return bytes(quantized)
        # Pack two 4-bit values per byte
        packed = bytearray()
        for i in range(0, len(quantized), 2):
            hi = quantized[i] & 0xF
            lo = quantized[i + 1] & 0xF if i + 1 < len(quantized) else 0
            packed.append((hi << 4) | lo)
        return bytes(packed)

    def dequantize(self, data: bytes, dim: int) -> List[float]:
        """Reconstruct approximate float32 vector from quantized bytes."""
        if not self._params:
            raise RuntimeError("call calibrate() first")
        if self._bits == 8:
            levels = list(data)
        else:
            levels = []
            for byte in data:
                levels.append((byte >> 4) & 0xF)
                levels.append(byte & 0xF)
            levels = levels[:dim]

        result = []
        for level, params in zip(levels, self._params[:dim]):
            val = params.min_val + (level / self._levels) * (params.max_val - params.min_val)
            result.append(val)
        return result

    def compression_ratio(self, source_dim: int) -> float:
        bytes_original = source_dim * 4   # float32
        bytes_quantized = (source_dim * self._bits + 7) // 8
        return bytes_original / max(bytes_quantized, 1)
```

## Solution 3: Recall Quality Evaluator

```python
import math
from dataclasses import dataclass
from typing import List, Tuple

@dataclass
class RecallEvaluation:
    dimension: int
    quantization_bits: int
    recall_at_10: float
    avg_rank_error: float
    passes_threshold: bool

class RecallQualityEvaluator:
    """
    Evaluates retrieval recall of compressed embeddings against ground-truth
    full-dimensional search. Determines the minimum compression that
    maintains recall above the configured threshold.
    """

    def __init__(self, recall_threshold: float = 0.95):
        self._threshold = recall_threshold

    def _cosine_similarity(self, a: List[float], b: List[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        return dot / max(norm_a * norm_b, 1e-10)

    def _top_k_indices(self, query: List[float], corpus: List[List[float]], k: int) -> List[int]:
        sims = [(i, self._cosine_similarity(query, doc)) for i, doc in enumerate(corpus)]
        return [i for i, _ in sorted(sims, key=lambda x: x[1], reverse=True)[:k]]

    def evaluate(
        self,
        queries: List[List[float]],
        full_dim_corpus: List[List[float]],
        reduced_corpus: List[List[float]],
        k: int = 10,
        dimension: int = 0,
        bits: int = 32,
    ) -> RecallEvaluation:
        total_recall = 0.0
        for query_full, query_reduced in zip(queries, [q[:dimension] if dimension else q for q in queries]):
            ground_truth = set(self._top_k_indices(query_full, full_dim_corpus, k))
            reduced_results = set(self._top_k_indices(query_reduced, reduced_corpus, k))
            overlap = len(ground_truth & reduced_results)
            total_recall += overlap / max(k, 1)

        avg_recall = total_recall / max(len(queries), 1)
        return RecallEvaluation(
            dimension=dimension or (len(full_dim_corpus[0]) if full_dim_corpus else 0),
            quantization_bits=bits,
            recall_at_10=round(avg_recall, 4),
            avg_rank_error=round(1.0 - avg_recall, 4),
            passes_threshold=avg_recall >= self._threshold,
        )
```

## Solution 4: Adaptive Compressor

```python
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

@dataclass
class CompressionConfig:
    target_dim: int
    quantization_bits: int
    recall_at_10: float
    compression_ratio: float

class AdaptiveEmbeddingCompressor:
    """
    Selects the best compression configuration based on recall evaluation.
    Tries progressively more aggressive compression until recall drops below threshold.
    Returns the most compressed configuration that maintains acceptable recall.
    """

    DIM_CANDIDATES = [32, 64, 128, 256, 512]
    BIT_CANDIDATES = [4, 8, 16, 32]

    def __init__(
        self,
        pca_fitter: PCAProjectionFitter,
        quantizer_factory: Any,  # callable(bits) -> ScalarQuantizer
        evaluator: RecallQualityEvaluator,
    ):
        self._pca_fitter = pca_fitter
        self._qfactory = quantizer_factory
        self._evaluator = evaluator

    def find_optimal_config(
        self,
        calibration_vectors: List[List[float]],
        eval_queries: List[List[float]],
        source_dim: int,
    ) -> Tuple[PCAprojection, ScalarQuantizer, CompressionConfig]:
        best_projection = None
        best_quantizer = None
        best_config = None
        best_ratio = 1.0

        for target_dim in self.DIM_CANDIDATES:
            if target_dim >= source_dim:
                continue
            projection = self._pca_fitter.fit(calibration_vectors, target_dim)
            reduced_corpus = projection.project_batch(calibration_vectors)

            for bits in self.BIT_CANDIDATES:
                quantizer = self._qfactory(bits)
                quantizer.calibrate(reduced_corpus)

                # Approximate dequantized corpus for evaluation
                approx_corpus = [
                    quantizer.dequantize(quantizer.quantize(v), target_dim)
                    for v in reduced_corpus
                ]
                reduced_queries = projection.project_batch(eval_queries)

                evaluation = self._evaluator.evaluate(
                    eval_queries, calibration_vectors, approx_corpus,
                    dimension=target_dim, bits=bits
                )

                if evaluation.passes_threshold:
                    ratio = (source_dim * 4) / ((target_dim * bits + 7) // 8)
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_projection = projection
                        best_quantizer = quantizer
                        best_config = CompressionConfig(
                            target_dim=target_dim,
                            quantization_bits=bits,
                            recall_at_10=evaluation.recall_at_10,
                            compression_ratio=round(ratio, 2),
                        )

        if best_config is None:
            # Fall back to no compression
            quantizer = self._qfactory(32)
            quantizer.calibrate(calibration_vectors)
            proj = self._pca_fitter.fit(calibration_vectors, source_dim)
            best_config = CompressionConfig(source_dim, 32, 1.0, 1.0)
            return proj, quantizer, best_config

        return best_projection, best_quantizer, best_config
```

## Solution 5: Compressed Embedding Store

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

@dataclass
class CompressedEmbeddingEntry:
    key: str
    compressed_data: bytes
    source_dim: int
    compressed_dim: int
    quantization_bits: int
    inserted_at: float = field(default_factory=time.time)

class CompressedEmbeddingStore:
    """
    Stores embeddings in compressed form; decompresses on retrieval.
    Tracks compression statistics to monitor quality vs. space trade-off.
    """

    def __init__(
        self,
        projection: PCAprojection,
        quantizer: ScalarQuantizer,
    ):
        self._projection = projection
        self._quantizer = quantizer
        self._entries: Dict[str, CompressedEmbeddingEntry] = {}
        self._bytes_saved = 0

    def insert(self, key: str, vector: List[float]) -> CompressedEmbeddingEntry:
        reduced = self._projection.project(vector)
        compressed = self._quantizer.quantize(reduced)
        original_bytes = len(vector) * 4
        compressed_bytes = len(compressed)
        self._bytes_saved += original_bytes - compressed_bytes

        entry = CompressedEmbeddingEntry(
            key=key,
            compressed_data=compressed,
            source_dim=len(vector),
            compressed_dim=self._projection.target_dim,
            quantization_bits=self._quantizer._bits,
        )
        self._entries[key] = entry
        return entry

    def retrieve(self, key: str) -> Optional[List[float]]:
        entry = self._entries.get(key)
        if not entry:
            return None
        return self._quantizer.dequantize(entry.compressed_data, entry.compressed_dim)

    def stats(self) -> dict:
        count = len(self._entries)
        if count == 0:
            return {"entries": 0}
        avg_compressed = sum(len(e.compressed_data) for e in self._entries.values()) / count
        return {
            "entries": count,
            "avg_compressed_bytes": round(avg_compressed, 1),
            "total_bytes_saved": self._bytes_saved,
            "effective_compression_ratio": round(
                self._quantizer.compression_ratio(
                    next(iter(self._entries.values())).source_dim
                ), 2
            ),
        }
```

## Solution 6: Compression Quality Monitor

```python
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

class CompressionQualityMonitor:
    """
    Periodically evaluates whether current compression settings still
    maintain acceptable recall quality. Triggers recalibration if recall
    degrades below threshold (e.g., after corpus distribution shift).
    """

    def __init__(
        self,
        store: CompressedEmbeddingStore,
        evaluator: RecallQualityEvaluator,
        recalibration_threshold: float = 0.92,
    ):
        self._store = store
        self._evaluator = evaluator
        self._threshold = recalibration_threshold
        self._last_eval: Optional[RecallEvaluation] = None
        self._recalibrations = 0

    def evaluate(
        self,
        sample_queries: List[List[float]],
        ground_truth_corpus: List[List[float]],
    ) -> dict:
        config = self._store.stats()
        if not sample_queries:
            return {"status": "no_queries"}

        compressed_corpus = [
            self._store.retrieve(k) for k in list(self._store._entries.keys())[:500]
        ]
        compressed_corpus = [c for c in compressed_corpus if c]

        source_dim = self._store._projection.source_dim
        target_dim = self._store._projection.target_dim
        bits = self._store._quantizer._bits
        reduced_queries = self._store._projection.project_batch(sample_queries)

        evaluation = self._evaluator.evaluate(
            sample_queries, ground_truth_corpus, compressed_corpus,
            dimension=target_dim, bits=bits
        )
        self._last_eval = evaluation

        needs_recalibration = not evaluation.passes_threshold

        return {
            "recall_at_10": evaluation.recall_at_10,
            "threshold": self._threshold,
            "passes": evaluation.passes_threshold,
            "needs_recalibration": needs_recalibration,
            "compression_ratio": self._store.stats().get("effective_compression_ratio"),
            "checked_at": time.time(),
        }
```

## Comparison

| Approach | Compression Method | Recall Evaluation | Auto-Tune | Monitor |
|---|---|---|---|---|
| PCAProjectionFitter | Dimensionality reduction | No | No | No |
| ScalarQuantizer | Bit quantization | No | No | No |
| RecallQualityEvaluator | N/A (evaluation) | Yes | No | No |
| AdaptiveEmbeddingCompressor | PCA + quantization | Yes | Yes (search) | No |
| CompressedEmbeddingStore | Store + retrieve | No | No | No |
| CompressionQualityMonitor | N/A | Yes (periodic) | Alerts | Yes |

**Best for production**: Run `AdaptiveEmbeddingCompressor.find_optimal_config()` at deployment with a representative calibration corpus of 5,000–10,000 vectors. For 1536-d embeddings with recall threshold 0.95, expect to land at 256–512 dimensions with 8-bit quantization — roughly 8–16× compression. Use `CompressedEmbeddingStore` for all long-term memory entries; keep recent session context at full precision. Run `CompressionQualityMonitor` weekly to detect distribution shift that erodes recall. Re-run `find_optimal_config()` after significant corpus changes.
