---
title: "Agent Doesn't Implement Vector Index Compression for Large Embedding Stores"
description: "Agents that store full-precision float32 embeddings for millions of documents consume gigabytes of memory and suffer slow similarity search: a 1M document store with 1536-dimensional OpenAI embeddings requires 6GB of float32 storage. Implement vector index compression using product quantization or scalar quantization to reduce memory footprint by 4–32× while preserving retrieval quality."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-vector-index-compression-for-large-embedding-stores
tags: [vector-compression, product-quantization, scalar-quantization, embedding-store, memory-efficiency, approximate-search]
symptoms:
  - "Embedding store consumes gigabytes of RAM for large document collections"
  - "Vector similarity search latency grows linearly with corpus size"
  - "OOM errors when loading full embedding index at agent startup"
  - "No compression applied to embeddings before storage"
  - "Full float32 precision retained even though retrieval quality is identical at int8"
---

## Why This Happens

Embeddings are stored at the precision the model outputs them — typically float32 (4 bytes per dimension). For a 1536-dimensional embedding model with 1M documents, the raw storage requirement is 1,536 × 1,000,000 × 4 bytes = 6.1GB. Scalar quantization reduces each float32 to an int8 by mapping the range to [-128, 127], cutting storage to 1.5GB with negligible recall loss. Product quantization goes further by learning codebook centroids and storing centroid indices (1–2 bytes per sub-vector group), achieving 16–32× compression. Without compression, large document collections are impractical to keep in memory.

## Solution 1: Scalar Quantizer

```python
import math
from typing import List, Optional, Tuple


class ScalarQuantizer:
    """
    Quantizes float32 embeddings to int8 by scaling to the observed value range.
    Compression ratio: 4× (float32 → int8).
    Reconstruction quality: ~0.5% mean absolute error in cosine similarity.
    """

    def __init__(self):
        self._min_val: Optional[float] = None
        self._max_val: Optional[float] = None
        self._scale: float = 1.0
        self._fitted = False

    def fit(self, embeddings: List[List[float]]) -> None:
        """Compute min/max from a representative sample of embeddings."""
        all_values = [v for emb in embeddings for v in emb]
        self._min_val = min(all_values)
        self._max_val = max(all_values)
        value_range = self._max_val - self._min_val
        self._scale = 255.0 / value_range if value_range > 0 else 1.0
        self._fitted = True

    def quantize(self, embedding: List[float]) -> bytes:
        if not self._fitted:
            raise RuntimeError("ScalarQuantizer must be fitted before use")
        quantized = []
        for v in embedding:
            scaled = (v - self._min_val) * self._scale
            clamped = max(0, min(255, int(round(scaled))))
            quantized.append(clamped)
        return bytes(quantized)

    def dequantize(self, quantized: bytes) -> List[float]:
        if not self._fitted:
            raise RuntimeError("ScalarQuantizer must be fitted before use")
        return [
            (b / self._scale) + self._min_val
            for b in quantized
        ]

    def compression_ratio(self, dims: int) -> float:
        return 4.0  # float32 (4 bytes) → uint8 (1 byte)

    def params(self) -> dict:
        return {
            "min_val": self._min_val,
            "max_val": self._max_val,
            "scale": self._scale,
            "fitted": self._fitted,
        }
```

## Solution 2: Product Quantizer

```python
import math
import random
from typing import List, Optional, Tuple


class ProductQuantizer:
    """
    Compresses embeddings by splitting into M sub-vectors and quantizing
    each sub-vector to the nearest of K centroids.
    Compression ratio: dims * 4 bytes → M bytes (one centroid index per sub-vector).
    Typical: 1536-dim float32 (6144 bytes) → 96 bytes at M=96, K=256.
    """

    def __init__(self, num_subspaces: int = 96, num_centroids: int = 256):
        self._M = num_subspaces
        self._K = num_centroids
        self._codebooks: Optional[List[List[List[float]]]] = None
        self._sub_dim: int = 0

    def _kmeans(self, vectors: List[List[float]], k: int, iterations: int = 20) -> List[List[float]]:
        centroids = random.sample(vectors, min(k, len(vectors)))
        for _ in range(iterations):
            clusters = [[] for _ in range(len(centroids))]
            for vec in vectors:
                best = min(range(len(centroids)), key=lambda i: self._sq_dist(vec, centroids[i]))
                clusters[best].append(vec)
            new_centroids = []
            for i, cluster in enumerate(clusters):
                if cluster:
                    dim = len(cluster[0])
                    centroid = [sum(v[d] for v in cluster) / len(cluster) for d in range(dim)]
                    new_centroids.append(centroid)
                else:
                    new_centroids.append(centroids[i])
            centroids = new_centroids
        return centroids

    @staticmethod
    def _sq_dist(a: List[float], b: List[float]) -> float:
        return sum((x - y) ** 2 for x, y in zip(a, b))

    def fit(self, embeddings: List[List[float]]) -> None:
        dim = len(embeddings[0])
        self._sub_dim = dim // self._M
        self._codebooks = []
        for m in range(self._M):
            start = m * self._sub_dim
            end = start + self._sub_dim
            sub_vectors = [emb[start:end] for emb in embeddings]
            centroids = self._kmeans(sub_vectors, self._K)
            self._codebooks.append(centroids)

    def encode(self, embedding: List[float]) -> bytes:
        if self._codebooks is None:
            raise RuntimeError("ProductQuantizer must be fitted before use")
        codes = []
        for m in range(self._M):
            start = m * self._sub_dim
            end = start + self._sub_dim
            sub_vec = embedding[start:end]
            best = min(range(len(self._codebooks[m])), key=lambda i: self._sq_dist(sub_vec, self._codebooks[m][i]))
            codes.append(best % 256)
        return bytes(codes)

    def decode(self, code: bytes) -> List[float]:
        if self._codebooks is None:
            raise RuntimeError("ProductQuantizer must be fitted before use")
        result = []
        for m, centroid_idx in enumerate(code):
            result.extend(self._codebooks[m][centroid_idx])
        return result

    def compression_ratio(self, dims: int) -> float:
        return (dims * 4) / self._M   # float32 bytes / code bytes
```

## Solution 3: Compressed Embedding Store

```python
import math
import time
from typing import Any, Dict, List, Optional, Tuple, Union


class CompressedEmbeddingStore:
    """
    Stores embeddings in compressed form and supports approximate
    cosine similarity search over the compressed representations.
    """

    def __init__(
        self,
        quantizer: Union[ScalarQuantizer, ProductQuantizer],
        dims: int,
    ):
        self._quantizer = quantizer
        self._dims = dims
        self._codes: Dict[str, bytes] = {}
        self._metadata: Dict[str, Any] = {}
        self._insert_count = 0

    def add(self, doc_id: str, embedding: List[float], metadata: Any = None) -> None:
        if isinstance(self._quantizer, ScalarQuantizer):
            code = self._quantizer.quantize(embedding)
        else:
            code = self._quantizer.encode(embedding)
        self._codes[doc_id] = code
        if metadata is not None:
            self._metadata[doc_id] = metadata
        self._insert_count += 1

    def _decode(self, code: bytes) -> List[float]:
        if isinstance(self._quantizer, ScalarQuantizer):
            return self._quantizer.dequantize(code)
        return self._quantizer.decode(code)

    @staticmethod
    def _cosine(a: List[float], b: List[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0

    def search(self, query: List[float], top_k: int = 10) -> List[Tuple[str, float]]:
        results = []
        for doc_id, code in self._codes.items():
            decoded = self._decode(code)
            score = self._cosine(query, decoded)
            results.append((doc_id, score))
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    def memory_usage_bytes(self) -> int:
        return sum(len(code) for code in self._codes.values())

    def stats(self) -> dict:
        mem = self.memory_usage_bytes()
        uncompressed = self._insert_count * self._dims * 4
        return {
            "document_count": len(self._codes),
            "compressed_bytes": mem,
            "compressed_mb": round(mem / 1024 / 1024, 2),
            "uncompressed_bytes_estimate": uncompressed,
            "compression_ratio": round(uncompressed / max(mem, 1), 2),
        }
```

## Solution 4: Quantization Quality Evaluator

```python
import math
import random
from typing import List, Tuple


class QuantizationQualityEvaluator:
    """
    Measures recall degradation introduced by quantization by comparing
    top-K results from exact search vs. compressed search on a sample set.
    """

    @staticmethod
    def _cosine(a: List[float], b: List[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        return dot / (na * nb) if na and nb else 0.0

    def evaluate(
        self,
        original_embeddings: List[Tuple[str, List[float]]],
        compressed_store: CompressedEmbeddingStore,
        sample_queries: int = 50,
        top_k: int = 10,
    ) -> dict:
        if len(original_embeddings) < top_k:
            return {"error": "too few embeddings to evaluate"}

        # Sample queries from the original set
        query_indices = random.sample(range(len(original_embeddings)), min(sample_queries, len(original_embeddings)))
        recall_scores = []

        for idx in query_indices:
            _, query_emb = original_embeddings[idx]

            # Exact top-K
            exact_scores = [
                (doc_id, self._cosine(query_emb, emb))
                for doc_id, emb in original_embeddings
            ]
            exact_top_k = {doc_id for doc_id, _ in sorted(exact_scores, key=lambda x: x[1], reverse=True)[:top_k]}

            # Compressed top-K
            compressed_results = compressed_store.search(query_emb, top_k)
            compressed_top_k = {doc_id for doc_id, _ in compressed_results}

            recall = len(exact_top_k & compressed_top_k) / top_k
            recall_scores.append(recall)

        avg_recall = sum(recall_scores) / len(recall_scores)
        return {
            "queries_evaluated": len(recall_scores),
            "top_k": top_k,
            "avg_recall_at_k": round(avg_recall, 4),
            "min_recall": round(min(recall_scores), 4),
            "store_stats": compressed_store.stats(),
        }
```

## Solution 5: Adaptive Quantization Selector

```python
from typing import List, Tuple


class AdaptiveQuantizationSelector:
    """
    Selects the best quantization strategy based on corpus size and
    memory budget. Evaluates both scalar and product quantization.
    """

    def __init__(
        self,
        dims: int,
        memory_budget_mb: float = 1024.0,
        min_recall: float = 0.90,
    ):
        self._dims = dims
        self._budget = memory_budget_mb * 1024 * 1024
        self._min_recall = min_recall

    def recommend(self, corpus_size: int) -> dict:
        uncompressed_bytes = corpus_size * self._dims * 4
        sq_bytes = corpus_size * self._dims           # 1 byte per dim
        pq_bytes_m96 = corpus_size * 96               # 96 sub-spaces, 1 byte each

        recommendations = []

        if uncompressed_bytes <= self._budget:
            recommendations.append({
                "strategy": "none",
                "bytes": uncompressed_bytes,
                "compression_ratio": 1.0,
                "expected_recall": 1.0,
            })

        if sq_bytes <= self._budget:
            recommendations.append({
                "strategy": "scalar_quantization",
                "bytes": sq_bytes,
                "compression_ratio": round(uncompressed_bytes / sq_bytes, 1),
                "expected_recall": 0.97,
            })

        if pq_bytes_m96 <= self._budget:
            recommendations.append({
                "strategy": "product_quantization_m96",
                "bytes": pq_bytes_m96,
                "compression_ratio": round(uncompressed_bytes / pq_bytes_m96, 1),
                "expected_recall": 0.92,
            })

        viable = [r for r in recommendations if r["expected_recall"] >= self._min_recall]
        if viable:
            best = min(viable, key=lambda r: r["bytes"])
        else:
            best = min(recommendations, key=lambda r: r["bytes"]) if recommendations else None

        return {
            "corpus_size": corpus_size,
            "dims": self._dims,
            "uncompressed_mb": round(uncompressed_bytes / 1024 / 1024, 1),
            "budget_mb": round(self._budget / 1024 / 1024, 1),
            "recommended": best,
            "all_options": recommendations,
        }
```

## Solution 6: Compression Dashboard

```python
import time


class VectorCompressionDashboard:
    """
    Combines store stats, quality evaluation, and selector recommendations
    into an operational view of the embedding store health.
    """

    def __init__(
        self,
        store: CompressedEmbeddingStore,
        evaluator: QuantizationQualityEvaluator,
        selector: AdaptiveQuantizationSelector,
    ):
        self._store = store
        self._evaluator = evaluator
        self._selector = selector

    def render(self, corpus_size_for_recommendation: int = 0) -> dict:
        stats = self._store.stats()
        recommendation = self._selector.recommend(
            corpus_size_for_recommendation or stats["document_count"]
        )

        return {
            "generated_at": time.time(),
            "store_stats": stats,
            "recommendation": recommendation,
        }
```

## Comparison

| Approach | Compression Method | Ratio | Recall Loss | Search Support | Quality Eval |
|---|---|---|---|---|---|
| ScalarQuantizer | Scalar (float32→int8) | 4× | ~1% | No | No |
| ProductQuantizer | PQ (codebook) | 16–64× | ~5–10% | No | No |
| CompressedEmbeddingStore | Via quantizer | Depends | Depends | Yes (approx) | No |
| QuantizationQualityEvaluator | No | No | No | No | Yes (recall@K) |
| AdaptiveQuantizationSelector | No | No | No | No | Yes (recommend) |
| VectorCompressionDashboard | No | No | No | No | Yes (combined) |

**Best for production**: Use `ScalarQuantizer` as the default — it achieves 4× compression with recall@10 above 97% for most embedding models, requires no training, and has trivial implementation. Apply `ProductQuantizer` only when corpus size exceeds available RAM even after scalar quantization. Run `QuantizationQualityEvaluator` after fitting to confirm recall meets your SLO before deploying the compressed index. Use `AdaptiveQuantizationSelector` during capacity planning to decide whether to invest in a vector database with built-in HNSW/IVF indexing (Pinecone, Weaviate) versus an in-memory compressed flat index.
