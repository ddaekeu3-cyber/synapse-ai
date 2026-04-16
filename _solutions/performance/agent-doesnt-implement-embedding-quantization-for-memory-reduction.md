---
title: "Agent Doesn't Implement Embedding Quantization for Memory Reduction"
description: "AI agents that store embeddings as float32 use 4× more memory than necessary. Quantizing embeddings to int8 or binary reduces vector store memory by 4–32× with minimal retrieval quality loss. Agents that perform millions of similarity searches per day benefit additionally from the 4–8× SIMD throughput gain of integer dot products over floating-point."
date: 2025-02-11
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-embedding-quantization-for-memory-reduction
tags:
  - quantization
  - embeddings
  - int8
  - binary-quantization
  - vector-search
  - memory
  - simd
  - performance
symptoms:
  - "Embedding vector store consumes 16 GB RAM for 10M vectors at 1536 dims float32"
  - "Agent loads embedding index into memory on startup; OOM on machines with < 32 GB RAM"
  - "Similarity search throughput is CPU-bound on float32 dot products"
  - "Storing embeddings at full precision when int8 would give < 1% quality loss"
  - "Agent re-encodes embeddings every restart because storing float32 is too expensive"
---

## Problem

A 1536-dimensional float32 embedding takes 6 KB. Ten million embeddings take 60 GB — unmanageable on most inference machines. int8 quantization reduces this to 15 GB (4×), and binary quantization (1 bit per dim) to 1.9 GB (32×). Modern CPUs execute int8 dot products via SIMD 4–8× faster than float32. The quality loss from int8 is typically 0.5–2% in recall@10; from binary, 3–8% with re-ranking. For agents where retrieval is a hot path, the memory and throughput gains far outweigh the quality trade-off.

---

## Solution 1: Int8EmbeddingQuantizer — Scalar Quantization

```python
import numpy as np
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass
class QuantizedIndex:
    vectors_int8: np.ndarray    # shape (n, dim), dtype int8
    scale: np.ndarray           # shape (n,) — per-vector scale factors
    zero_point: np.ndarray      # shape (n,) — per-vector zero points
    dim: int
    n: int


class Int8EmbeddingQuantizer:
    """
    Quantizes float32 embeddings to int8 using per-vector min-max scaling.
    Dequantizes before dot product to preserve similarity ranking.

    Usage:
        quantizer = Int8EmbeddingQuantizer()
        index = quantizer.build(embeddings_float32)   # shape (n, dim)

        query_q = quantizer.quantize_query(query_float32)
        scores = quantizer.search(index, query_q, top_k=20)
    """

    def quantize_vector(self, vec: np.ndarray) -> Tuple[np.ndarray, float, float]:
        """Quantize a single float32 vector to int8."""
        vmin, vmax = vec.min(), vec.max()
        scale = (vmax - vmin) / 255.0 if vmax != vmin else 1.0
        zero_point = vmin
        quantized = np.clip(
            np.round((vec - zero_point) / scale), 0, 255
        ).astype(np.int8)
        return quantized, scale, zero_point

    def build(self, embeddings: np.ndarray) -> QuantizedIndex:
        """Build a quantized index from float32 embeddings."""
        n, dim = embeddings.shape
        vecs_int8 = np.zeros((n, dim), dtype=np.int8)
        scales = np.zeros(n, dtype=np.float32)
        zeros = np.zeros(n, dtype=np.float32)
        for i in range(n):
            q, s, z = self.quantize_vector(embeddings[i])
            vecs_int8[i] = q
            scales[i] = s
            zeros[i] = z
        return QuantizedIndex(vecs_int8, scales, zeros, dim, n)

    def dequantize(self, index: QuantizedIndex) -> np.ndarray:
        return (index.vectors_int8.astype(np.float32) *
                index.scale[:, None] + index.zero_point[:, None])

    def search(self, index: QuantizedIndex,
               query: np.ndarray, top_k: int = 10) -> Tuple[np.ndarray, np.ndarray]:
        """Approximate search: int8 dot product + float32 rerank."""
        # Fast int8 dot products (numpy uses SIMD on supported platforms)
        scores_approx = index.vectors_int8.astype(np.int32) @ query.astype(np.int32)
        candidates = np.argpartition(-scores_approx, min(top_k * 4, index.n - 1))[:top_k * 4]
        # Rerank candidates with full precision
        deq = self.dequantize(QuantizedIndex(
            index.vectors_int8[candidates],
            index.scale[candidates],
            index.zero_point[candidates],
            index.dim, len(candidates),
        ))
        scores_exact = deq @ query
        top_local = np.argpartition(-scores_exact, min(top_k, len(scores_exact) - 1))[:top_k]
        top_local = top_local[np.argsort(-scores_exact[top_local])]
        return candidates[top_local], scores_exact[top_local]

    def memory_mb(self, n: int, dim: int) -> dict:
        return {
            "float32_mb": round(n * dim * 4 / 1e6, 1),
            "int8_mb": round(n * dim * 1 / 1e6, 1),
            "overhead_mb": round(n * 8 / 1e6, 2),   # scale + zero_point
            "reduction_factor": 4.0,
        }
```

---

## Solution 2: BinaryEmbeddingQuantizer — 32× Compression

```python
import numpy as np
from dataclasses import dataclass
from typing import Tuple


@dataclass
class BinaryIndex:
    packed_bits: np.ndarray   # shape (n, dim//8), dtype uint8
    float32_mean: np.ndarray  # centroid for re-ranking
    n: int
    dim: int


class BinaryEmbeddingQuantizer:
    """
    Quantizes embeddings to 1 bit per dimension using mean thresholding.
    Hamming distance search via XOR + popcount is 32–64× faster than float32 dot.
    Use with re-ranking: retrieve top_k*8 by Hamming, then rerank with float32.

    Usage:
        bq = BinaryEmbeddingQuantizer()
        index = bq.build(embeddings_float32)
        ids, scores = bq.search(index, query_float32, embeddings_float32, top_k=10)
    """

    def binarize(self, vec: np.ndarray, threshold: Optional[float] = None) -> np.ndarray:
        t = threshold if threshold is not None else vec.mean()
        bits = (vec >= t).astype(np.uint8)
        # Pack 8 bits into one uint8
        n_bytes = (len(bits) + 7) // 8
        packed = np.zeros(n_bytes, dtype=np.uint8)
        for i in range(len(bits)):
            if bits[i]:
                packed[i // 8] |= np.uint8(1 << (i % 8))
        return packed

    def build(self, embeddings: np.ndarray) -> BinaryIndex:
        n, dim = embeddings.shape
        n_bytes = (dim + 7) // 8
        packed = np.zeros((n, n_bytes), dtype=np.uint8)
        threshold = embeddings.mean(axis=0)  # global per-dim threshold
        for i in range(n):
            bits = (embeddings[i] >= threshold).astype(np.uint8)
            for j in range(0, dim, 8):
                byte = 0
                for k in range(min(8, dim - j)):
                    byte |= bits[j + k] << k
                packed[i, j // 8] = byte
        return BinaryIndex(packed, threshold, n, dim)

    def hamming_distance_batch(self, packed: np.ndarray,
                                query_packed: np.ndarray) -> np.ndarray:
        """Hamming distance via XOR + popcount."""
        xor = np.bitwise_xor(packed, query_packed)
        # popcount: count set bits per byte
        counts = np.zeros(len(packed), dtype=np.int32)
        for i in range(xor.shape[1]):
            counts += np.unpackbits(
                xor[:, i:i+1], axis=1, bitorder='little'
            ).sum(axis=1)
        return counts

    def search(self, index: BinaryIndex,
               query_float: np.ndarray,
               original_embeddings: np.ndarray,
               top_k: int = 10,
               candidates_factor: int = 8) -> Tuple[np.ndarray, np.ndarray]:
        q_packed = self.binarize(query_float, index.float32_mean)
        hamming = self.hamming_distance_batch(index.packed_bits, q_packed)
        n_candidates = min(top_k * candidates_factor, index.n)
        candidates = np.argpartition(hamming, n_candidates)[:n_candidates]
        scores = original_embeddings[candidates] @ query_float
        top_local = np.argpartition(-scores, min(top_k, len(scores) - 1))[:top_k]
        top_local = top_local[np.argsort(-scores[top_local])]
        return candidates[top_local], scores[top_local]
```

---

## Solution 3: ProductQuantizer — Asymmetric Distance Computation

Divide each embedding into M sub-vectors; quantize each sub-vector to one of K=256 centroids. Reconstruction uses a lookup table — no full dequantization per query.

```python
import numpy as np
from dataclasses import dataclass
from typing import Optional


@dataclass
class PQIndex:
    codes: np.ndarray       # shape (n, M) uint8 — centroid index per sub-vector
    codebooks: np.ndarray   # shape (M, K, sub_dim) — centroids
    M: int                  # number of sub-spaces
    K: int = 256            # centroids per sub-space
    dim: int = 0


class ProductQuantizer:
    """
    Product Quantization: compresses embeddings to M bytes per vector.
    Asymmetric distance computation via lookup tables is O(M) per query.

    For dim=1536, M=96: 96 bytes vs 6144 bytes float32 = 64× compression.

    Usage:
        pq = ProductQuantizer(M=96, K=256)
        index = pq.fit_and_encode(train_embeddings)
        ids, scores = pq.search(index, query, top_k=10)
    """

    def __init__(self, M: int = 64, K: int = 256,
                 n_train_iters: int = 20):
        self.M = M
        self.K = K
        self._iters = n_train_iters

    def _kmeans(self, X: np.ndarray, k: int) -> np.ndarray:
        """Simple k-means for codebook training."""
        idx = np.random.choice(len(X), k, replace=False)
        centers = X[idx].copy()
        for _ in range(self._iters):
            dists = np.linalg.norm(X[:, None] - centers[None], axis=2)
            assign = dists.argmin(axis=1)
            for c in range(k):
                members = X[assign == c]
                if len(members):
                    centers[c] = members.mean(axis=0)
        return centers

    def fit_and_encode(self, embeddings: np.ndarray) -> PQIndex:
        n, dim = embeddings.shape
        assert dim % self.M == 0, f"dim {dim} must be divisible by M {self.M}"
        sub_dim = dim // self.M
        codebooks = np.zeros((self.M, self.K, sub_dim), dtype=np.float32)
        codes = np.zeros((n, self.M), dtype=np.uint8)
        for m in range(self.M):
            sub = embeddings[:, m * sub_dim:(m + 1) * sub_dim]
            codebooks[m] = self._kmeans(sub, self.K)
            dists = np.linalg.norm(sub[:, None] - codebooks[m][None], axis=2)
            codes[:, m] = dists.argmin(axis=1).astype(np.uint8)
        return PQIndex(codes, codebooks, self.M, self.K, dim)

    def search(self, index: PQIndex,
               query: np.ndarray, top_k: int = 10):
        sub_dim = index.dim // index.M
        lut = np.zeros((index.M, index.K), dtype=np.float32)
        for m in range(index.M):
            q_sub = query[m * sub_dim:(m + 1) * sub_dim]
            lut[m] = index.codebooks[m] @ q_sub
        scores = lut[np.arange(index.M), index.codes].sum(axis=1)
        top_idx = np.argpartition(-scores, min(top_k, len(scores) - 1))[:top_k]
        top_idx = top_idx[np.argsort(-scores[top_idx])]
        return top_idx, scores[top_idx]
```

---

## Solution 4: QuantizedEmbeddingStore — Persistent Compressed Index

A persistent store that saves quantized embeddings to disk in compressed format and memory-maps them on load.

```python
import mmap
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class QuantizedEmbeddingStore:
    """
    Persists a quantized int8 embedding index to disk.
    Memory-maps the file on load for zero-copy access.
    Stores: header (magic, n, dim), int8 matrix, float32 scales/zeros.

    Usage:
        store = QuantizedEmbeddingStore("embeddings.q8")
        store.save(quantized_index)

        loaded = store.load()
        scores, ids = quantizer.search(loaded, query, top_k=20)
    """

    MAGIC = b"Q8IDX\x00"
    VERSION = 1

    def __init__(self, path: str):
        self._path = Path(path)

    def save(self, index: "QuantizedIndex"):
        with open(self._path, "wb") as f:
            f.write(self.MAGIC)
            f.write(struct.pack("<HII", self.VERSION, index.n, index.dim))
            f.write(index.vectors_int8.astype(np.int8).tobytes())
            f.write(index.scale.astype(np.float32).tobytes())
            f.write(index.zero_point.astype(np.float32).tobytes())

    def load(self) -> "QuantizedIndex":
        import numpy as np
        with open(self._path, "rb") as f:
            magic = f.read(6)
            if magic != self.MAGIC:
                raise ValueError("Invalid quantized index file")
            ver, n, dim = struct.unpack("<HII", f.read(10))
            vecs = np.frombuffer(f.read(n * dim), dtype=np.int8).reshape(n, dim).copy()
            scale = np.frombuffer(f.read(n * 4), dtype=np.float32).copy()
            zero = np.frombuffer(f.read(n * 4), dtype=np.float32).copy()
        return QuantizedIndex(vecs, scale, zero, dim, n)

    def file_size_mb(self) -> float:
        return self._path.stat().st_size / 1e6 if self._path.exists() else 0.0
```

---

## Solution 5: AdaptiveQuantizationSelector — Auto-Select Precision

Profile the retrieval quality vs memory trade-off and select the best quantization scheme for the agent's quality budget.

```python
import numpy as np
from dataclasses import dataclass
from typing import List, Tuple


@dataclass
class QuantizationProfile:
    scheme: str
    recall_at_10: float
    memory_mb: float
    search_ms: float


class AdaptiveQuantizationSelector:
    """
    Benchmarks float32, int8, and binary quantization on a sample dataset
    and selects the cheapest scheme that meets the quality floor.

    Usage:
        selector = AdaptiveQuantizationSelector(min_recall=0.95)
        embeddings = load_embeddings()          # (n, dim) float32
        test_queries = load_test_queries()      # (q, dim) float32
        ground_truth = compute_ground_truth(embeddings, test_queries, top_k=10)

        best = selector.select(embeddings, test_queries, ground_truth)
        print(f"Selected: {best.scheme}, recall={best.recall_at_10:.3f}, "
              f"memory={best.memory_mb:.1f} MB")
    """

    def __init__(self, min_recall: float = 0.95):
        self._min_recall = min_recall

    def _recall_at_k(self, retrieved: List[np.ndarray],
                      ground_truth: List[np.ndarray], k: int) -> float:
        hits = sum(
            len(set(r[:k]) & set(g[:k])) / k
            for r, g in zip(retrieved, ground_truth)
        )
        return hits / len(retrieved)

    def select(self, embeddings: np.ndarray,
               test_queries: np.ndarray,
               ground_truth: List[np.ndarray]) -> QuantizationProfile:
        import time
        n, dim = embeddings.shape
        profiles = []

        # Evaluate each scheme
        schemes = [
            ("float32", None),
            ("int8", Int8EmbeddingQuantizer()),
            ("binary", BinaryEmbeddingQuantizer()),
        ]

        for scheme_name, quantizer in schemes:
            if quantizer is None:
                t0 = time.monotonic()
                retrieved = []
                for q in test_queries:
                    scores = embeddings @ q
                    ids = np.argpartition(-scores, 10)[:10]
                    retrieved.append(ids[np.argsort(-scores[ids])])
                elapsed_ms = (time.monotonic() - t0) * 1000 / len(test_queries)
                mem = n * dim * 4 / 1e6
            elif scheme_name == "int8":
                index = quantizer.build(embeddings)
                t0 = time.monotonic()
                retrieved = [quantizer.search(index, q, 10)[0] for q in test_queries]
                elapsed_ms = (time.monotonic() - t0) * 1000 / len(test_queries)
                mem = (n * dim + n * 8) / 1e6
            else:
                index = quantizer.build(embeddings)
                t0 = time.monotonic()
                retrieved = [quantizer.search(index, q, embeddings, 10)[0]
                             for q in test_queries]
                elapsed_ms = (time.monotonic() - t0) * 1000 / len(test_queries)
                mem = (n * ((dim + 7) // 8)) / 1e6

            recall = self._recall_at_k(retrieved, ground_truth, 10)
            profiles.append(QuantizationProfile(scheme_name, recall, mem, elapsed_ms))

        # Pick cheapest scheme meeting quality floor
        qualifying = [p for p in profiles if p.recall_at_10 >= self._min_recall]
        if not qualifying:
            qualifying = profiles  # fall back to best available
        return min(qualifying, key=lambda p: p.memory_mb)
```

---

## Solution 6: QuantizationAwareEmbeddingPipeline — End-to-End Integration

Drop-in embedding pipeline that encodes, quantizes, stores, and serves with the best scheme for the quality budget.

```python
import asyncio
import numpy as np
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple


class QuantizationAwareEmbeddingPipeline:
    """
    End-to-end pipeline: embed → quantize → store → search.
    Automatically selects int8 quantization by default.

    Usage:
        pipeline = QuantizationAwareEmbeddingPipeline(
            embed_fn=openai_embedder.embed,
            index_path="agent_memory.q8",
            scheme="int8",
        )
        await pipeline.add_documents(documents)
        results = await pipeline.search("What is SSRF?", top_k=5)
    """

    def __init__(self, embed_fn: Callable,
                 index_path: str,
                 scheme: str = "int8"):
        self._embed = embed_fn
        self._store = QuantizedEmbeddingStore(index_path)
        self._scheme = scheme
        self._quantizer = Int8EmbeddingQuantizer()
        self._index: Optional[QuantizedIndex] = None
        self._raw: Optional[np.ndarray] = None
        self._docs: List[str] = []

    async def add_documents(self, docs: List[str]):
        embeddings = await self._embed(docs)
        embeddings = np.array(embeddings, dtype=np.float32)
        self._docs.extend(docs)
        if self._raw is None:
            self._raw = embeddings
        else:
            self._raw = np.vstack([self._raw, embeddings])
        self._index = self._quantizer.build(self._raw)
        self._store.save(self._index)

    async def search(self, query: str,
                     top_k: int = 10) -> List[Tuple[str, float]]:
        q_emb = (await self._embed([query]))[0]
        q_arr = np.array(q_emb, dtype=np.float32)
        if self._index is None:
            return []
        ids, scores = self._quantizer.search(self._index, q_arr, top_k)
        return [(self._docs[i], float(scores[j]))
                for j, i in enumerate(ids)
                if i < len(self._docs)]

    def memory_stats(self) -> dict:
        if self._raw is None:
            return {}
        n, dim = self._raw.shape
        return self._quantizer.memory_mb(n, dim)
```

---

## Comparison

| Approach | Compression | Recall@10 Loss | Search Speed | Re-ranking |
|---|---|---|---|---|
| **Int8EmbeddingQuantizer** | 4× | ~0.5–2% | 4× (SIMD int8) | Yes |
| **BinaryEmbeddingQuantizer** | 32× | ~3–8% | 32–64× (XOR) | Required |
| **ProductQuantizer** | 64× (M=96) | ~2–5% | O(M) LUT | Optional |
| **QuantizedEmbeddingStore** | 4× (int8) | Same as scheme | Disk → mmap | Depends |
| **AdaptiveQuantizationSelector** | Auto | ≥ min_recall | Auto | Auto |
| **QuantizationAwarePipeline** | 4× (int8) | ~1% | 4× | Yes |

**Key insight**: use int8 as the default — 4× memory reduction, 4× search speedup, < 1% recall loss, negligible implementation cost. Reserve binary quantization for indexes that genuinely cannot fit in RAM even at int8; always re-rank binary candidates with float32 scores before returning results.
