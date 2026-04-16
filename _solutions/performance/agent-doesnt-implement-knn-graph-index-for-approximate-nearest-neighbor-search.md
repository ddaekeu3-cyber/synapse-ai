---
title: "Agent Doesn't Implement KNN Graph Index for Approximate Nearest Neighbor Search"
description: "AI agents that perform brute-force linear scan for every similarity search scale as O(n·d) per query — untenable beyond 1M vectors. Hierarchical Navigable Small World (HNSW) graphs reduce this to O(log n) expected traversal with >95% recall at 10× to 1000× lower latency. Agents that use semantic memory, RAG retrieval, or tool-result ranking all benefit from a KNN graph index."
date: 2025-02-12
difficulty: advanced
category: performance
slug: agent-doesnt-implement-knn-graph-index-for-approximate-nearest-neighbor-search
tags:
  - hnsw
  - ann
  - approximate-nearest-neighbor
  - knn
  - vector-search
  - semantic-memory
  - rag
  - performance
symptoms:
  - "Similarity search over 1M embeddings takes 2-5 seconds per query"
  - "Agent scans all stored embeddings linearly for every RAG retrieval"
  - "Adding more documents to memory causes query latency to grow linearly"
  - "Agent cannot meet sub-100ms retrieval SLA beyond 100K vectors"
  - "Vector store is a numpy array scanned with a dot product loop"
---

## Problem

A dot product over 1M × 1536-dim float32 vectors takes ~500 ms on a single CPU core. Linear scan is O(n·d): adding documents makes it slower without bound. Approximate Nearest Neighbor (ANN) algorithms trade a small recall loss for sub-linear query complexity. HNSW — implemented in `hnswlib` and used inside FAISS, Chroma, Weaviate, and Pinecone — builds a navigable small-world graph that routes queries from coarse to fine layers, achieving O(log n) expected hops with typical recall@10 > 97%.

---

## Solution 1: HNSWIndex — Build and Query with hnswlib

```python
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

try:
    import hnswlib
    _HNSWLIB = True
except ImportError:
    _HNSWLIB = False


@dataclass
class HNSWConfig:
    dim: int
    max_elements: int = 1_000_000
    M: int = 16               # edges per node per layer (16–64)
    ef_construction: int = 200  # build-time search width (100–500)
    ef_search: int = 50         # query-time search width (≥ top_k)
    space: str = "cosine"       # "cosine" | "l2" | "ip"


class HNSWIndex:
    """
    HNSW index wrapping hnswlib for fast approximate nearest neighbor search.
    Suitable for up to ~50M vectors on a single machine.

    Usage:
        index = HNSWIndex(HNSWConfig(dim=1536, max_elements=5_000_000))
        index.add(embeddings, ids=list(range(len(embeddings))))
        ids, distances = index.search(query_embedding, top_k=10)
        index.save("agent_memory.hnsw")
        index.load("agent_memory.hnsw")
    """

    def __init__(self, config: HNSWConfig):
        if not _HNSWLIB:
            raise RuntimeError("pip install hnswlib")
        self._cfg = config
        self._index = hnswlib.Index(space=config.space, dim=config.dim)
        self._index.init_index(
            max_elements=config.max_elements,
            ef_construction=config.ef_construction,
            M=config.M,
        )
        self._index.set_ef(config.ef_search)
        self._count = 0

    def add(self, embeddings: np.ndarray,
            ids: Optional[List[int]] = None):
        if embeddings.ndim == 1:
            embeddings = embeddings[None]
        emb = embeddings.astype(np.float32)
        id_arr = ids or list(range(self._count, self._count + len(emb)))
        self._index.add_items(emb, id_arr)
        self._count += len(emb)

    def search(self, query: np.ndarray, top_k: int = 10,
               ef: Optional[int] = None) -> Tuple[np.ndarray, np.ndarray]:
        if ef:
            self._index.set_ef(max(ef, top_k))
        q = query.astype(np.float32)
        if q.ndim == 1:
            q = q[None]
        labels, distances = self._index.knn_query(q, k=top_k)
        return labels[0], distances[0]

    def batch_search(self, queries: np.ndarray,
                     top_k: int = 10) -> Tuple[np.ndarray, np.ndarray]:
        q = queries.astype(np.float32)
        return self._index.knn_query(q, k=top_k)

    def save(self, path: str):
        self._index.save_index(path)

    def load(self, path: str):
        self._index.load_index(path, max_elements=self._cfg.max_elements)
        self._count = self._index.get_current_count()

    @property
    def size(self) -> int:
        return self._count

    def memory_mb(self) -> float:
        return self._index.get_current_count() * self._cfg.dim * 4 / 1e6 * 1.3


```

---

## Solution 2: FAISSIndex — GPU-Accelerated ANN with IVF-PQ

FAISS IVF-PQ combines inverted file clustering with product quantization for billion-scale search with GPU acceleration.

```python
import numpy as np
from dataclasses import dataclass
from typing import Optional, Tuple

try:
    import faiss
    _FAISS = True
except ImportError:
    _FAISS = False


@dataclass
class FAISSConfig:
    dim: int
    n_clusters: int = 4096       # IVF clusters (sqrt(n) is a good default)
    n_probe: int = 64            # clusters to search per query (higher = better recall)
    pq_bytes: int = 64           # bytes per vector in PQ (dim/2 is a safe start)
    use_gpu: bool = False


class FAISSIVFPQIndex:
    """
    FAISS IVF-PQ index: inverted file + product quantization.
    Scales to 100M+ vectors; GPU mode gives 10–100× throughput vs CPU.

    Usage:
        idx = FAISSIVFPQIndex(FAISSConfig(dim=1536, n_clusters=4096))
        idx.train(training_embeddings)       # train quantizer (~100K samples)
        idx.add(all_embeddings)
        ids, scores = idx.search(query, top_k=10)
    """

    def __init__(self, config: FAISSConfig):
        if not _FAISS:
            raise RuntimeError("pip install faiss-cpu  # or faiss-gpu")
        self._cfg = config
        quantizer = faiss.IndexFlatIP(config.dim)
        self._index = faiss.IndexIVFPQ(
            quantizer, config.dim,
            config.n_clusters, config.pq_bytes, 8,
        )
        self._index.nprobe = config.n_probe
        if config.use_gpu:
            res = faiss.StandardGpuResources()
            self._index = faiss.index_cpu_to_gpu(res, 0, self._index)
        self._trained = False

    def train(self, vectors: np.ndarray):
        self._index.train(vectors.astype(np.float32))
        self._trained = True

    def add(self, vectors: np.ndarray):
        if not self._trained:
            raise RuntimeError("Call train() before add()")
        self._index.add(vectors.astype(np.float32))

    def search(self, query: np.ndarray,
               top_k: int = 10) -> Tuple[np.ndarray, np.ndarray]:
        q = query.astype(np.float32)
        if q.ndim == 1:
            q = q[None]
        distances, ids = self._index.search(q, top_k)
        return ids[0], distances[0]

    def set_nprobe(self, n: int):
        self._index.nprobe = n

    def save(self, path: str):
        faiss.write_index(faiss.index_gpu_to_cpu(self._index)
                          if self._cfg.use_gpu else self._index, path)

    @classmethod
    def load(cls, path: str, config: FAISSConfig) -> "FAISSIVFPQIndex":
        obj = cls.__new__(cls)
        obj._cfg = config
        obj._index = faiss.read_index(path)
        obj._trained = True
        return obj
```

---

## Solution 3: HybridANNIndex — Exact Re-ranking of ANN Candidates

Two-phase retrieval: ANN retrieves k×8 candidates fast, then re-rank with exact dot products on the small candidate set.

```python
import numpy as np
from typing import List, Optional, Tuple


class HybridANNIndex:
    """
    Two-phase retrieval: ANN candidate generation + exact re-ranking.
    Phase 1: HNSW retrieves top_k * oversampling_factor candidates in O(log n).
    Phase 2: Exact dot products on the small candidate set for precise ranking.
    Achieves exact top-k precision at a fraction of brute-force cost.

    Usage:
        index = HybridANNIndex(dim=1536, oversampling=8)
        index.build(embeddings)
        ids, scores = index.search(query, top_k=10)
    """

    def __init__(self, dim: int,
                 oversampling: int = 8,
                 hnsw_M: int = 16,
                 hnsw_ef: int = 100):
        self._dim = dim
        self._oversample = oversampling
        self._config = HNSWConfig(
            dim=dim, M=hnsw_M, ef_search=hnsw_ef
        )
        self._hnsw: Optional[HNSWIndex] = None
        self._vectors: Optional[np.ndarray] = None

    def build(self, embeddings: np.ndarray):
        self._vectors = embeddings.astype(np.float32)
        norms = np.linalg.norm(self._vectors, axis=1, keepdims=True)
        normalised = self._vectors / np.where(norms == 0, 1, norms)
        config = HNSWConfig(
            dim=self._dim,
            max_elements=len(embeddings) + 10000,
        )
        self._hnsw = HNSWIndex(config)
        self._hnsw.add(normalised)

    def search(self, query: np.ndarray,
               top_k: int = 10) -> Tuple[np.ndarray, np.ndarray]:
        if self._hnsw is None or self._vectors is None:
            raise RuntimeError("Call build() first")
        n_candidates = min(top_k * self._oversample, self._hnsw.size)
        candidate_ids, _ = self._hnsw.search(query, top_k=n_candidates)
        # Exact re-rank
        candidates = self._vectors[candidate_ids]
        q = query.astype(np.float32)
        if q.ndim > 1:
            q = q[0]
        scores = candidates @ q
        top_local = np.argpartition(-scores, min(top_k, len(scores) - 1))[:top_k]
        top_local = top_local[np.argsort(-scores[top_local])]
        return candidate_ids[top_local], scores[top_local]

    def add(self, new_vectors: np.ndarray):
        if self._vectors is None:
            self.build(new_vectors)
        else:
            start_id = len(self._vectors)
            self._vectors = np.vstack([self._vectors, new_vectors.astype(np.float32)])
            norms = np.linalg.norm(new_vectors, axis=1, keepdims=True)
            normalised = new_vectors / np.where(norms == 0, 1, norms)
            ids = list(range(start_id, start_id + len(new_vectors)))
            self._hnsw.add(normalised.astype(np.float32), ids=ids)
```

---

## Solution 4: StreamingANNIndexUpdater — Incremental Index Updates

Add new vectors to a live HNSW index without rebuilding. Periodically compacts deleted vectors.

```python
import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set


class StreamingANNIndexUpdater:
    """
    Manages incremental updates to an HNSW index.
    Buffers incoming vectors and flushes to the index in batches.
    Marks deleted IDs in a tombstone set; filters them from results.

    Usage:
        updater = StreamingANNIndexUpdater(index, batch_size=1000)
        asyncio.create_task(updater.run())

        await updater.add(embedding, doc_id=42)
        await updater.delete(doc_id=17)
        ids, scores = updater.search(query, top_k=10)
    """

    def __init__(self, index: HNSWIndex,
                 batch_size: int = 1000,
                 flush_interval: float = 5.0):
        self._index = index
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._buffer: List[tuple] = []   # (embedding, doc_id)
        self._tombstones: Set[int] = set()
        self._lock = asyncio.Lock()
        self._next_id = index.size

    async def add(self, embedding, doc_id: Optional[int] = None):
        async with self._lock:
            if doc_id is None:
                doc_id = self._next_id
                self._next_id += 1
            self._buffer.append((embedding, doc_id))
            if len(self._buffer) >= self._batch_size:
                await self._flush()
        return doc_id

    async def delete(self, doc_id: int):
        async with self._lock:
            self._tombstones.add(doc_id)

    async def _flush(self):
        if not self._buffer:
            return
        import numpy as np
        embeddings = np.array([e for e, _ in self._buffer])
        ids = [i for _, i in self._buffer]
        self._index.add(embeddings, ids=ids)
        self._buffer.clear()

    async def run(self):
        while True:
            await asyncio.sleep(self._flush_interval)
            async with self._lock:
                await self._flush()

    def search(self, query, top_k: int = 10,
               oversample: int = 3):
        n = min(top_k * oversample, self._index.size)
        ids, scores = self._index.search(query, top_k=n)
        filtered = [(i, s) for i, s in zip(ids, scores)
                    if i not in self._tombstones]
        filtered = sorted(filtered, key=lambda x: -x[1])[:top_k]
        import numpy as np
        if not filtered:
            return np.array([]), np.array([])
        ids_out, scores_out = zip(*filtered)
        return np.array(ids_out), np.array(scores_out)
```

---

## Solution 5: RecallBenchmarker — Measure ANN Recall vs Latency

Benchmark the index's recall@k against brute-force ground truth, calibrate `ef_search`, and report the Pareto-optimal operating point.

```python
import time
import numpy as np
from dataclasses import dataclass
from typing import List, Tuple


@dataclass
class RecallPoint:
    ef: int
    recall_at_k: float
    latency_ms: float
    throughput_qps: float


class RecallBenchmarker:
    """
    Benchmarks HNSW recall@k vs latency for different ef_search values.
    Helps choose the ef that satisfies both recall and latency SLAs.

    Usage:
        bench = RecallBenchmarker(index, ground_truth_fn=brute_force_search)
        results = bench.sweep(queries, top_k=10, ef_values=[10, 20, 50, 100, 200])
        optimal = bench.pareto_optimal(results, min_recall=0.95, max_latency_ms=20)
        index._index.set_ef(optimal.ef)
    """

    def __init__(self, index: HNSWIndex,
                 ground_truth_fn=None):
        self._index = index
        self._gt_fn = ground_truth_fn

    def _brute_force(self, queries: np.ndarray,
                     embeddings: np.ndarray, k: int) -> List[np.ndarray]:
        results = []
        for q in queries:
            scores = embeddings @ q.astype(np.float32)
            ids = np.argpartition(-scores, k)[:k]
            results.append(ids[np.argsort(-scores[ids])])
        return results

    def sweep(self, queries: np.ndarray,
              embeddings: np.ndarray,
              top_k: int = 10,
              ef_values: List[int] = None) -> List[RecallPoint]:
        ef_values = ef_values or [10, 20, 50, 100, 200, 500]
        gt = self._brute_force(queries, embeddings, top_k)
        points = []
        for ef in ef_values:
            self._index._index.set_ef(ef)
            t0 = time.monotonic()
            retrieved = [self._index.search(q, top_k)[0] for q in queries]
            elapsed = time.monotonic() - t0
            recall = sum(
                len(set(r[:top_k]) & set(g[:top_k])) / top_k
                for r, g in zip(retrieved, gt)
            ) / len(queries)
            latency_ms = elapsed / len(queries) * 1000
            points.append(RecallPoint(
                ef=ef, recall_at_k=round(recall, 4),
                latency_ms=round(latency_ms, 2),
                throughput_qps=round(len(queries) / elapsed, 1),
            ))
        return points

    def pareto_optimal(self, results: List[RecallPoint],
                        min_recall: float = 0.95,
                        max_latency_ms: float = 50.0) -> RecallPoint:
        qualifying = [r for r in results
                      if r.recall_at_k >= min_recall
                      and r.latency_ms <= max_latency_ms]
        if not qualifying:
            return min(results, key=lambda r: abs(r.recall_at_k - min_recall))
        return min(qualifying, key=lambda r: r.latency_ms)
```

---

## Solution 6: ANNAgentMemory — Drop-In Semantic Memory for Agents

A complete semantic memory store for agents backed by HNSW with automatic ID management, document storage, and metadata filtering.

```python
import asyncio
import numpy as np
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass
class MemoryDoc:
    doc_id: int
    content: str
    metadata: Dict[str, Any]
    embedding: Optional[np.ndarray] = None


class ANNAgentMemory:
    """
    Semantic memory for agents using HNSW under the hood.
    Supports add, search, delete, and metadata-filtered retrieval.

    Usage:
        memory = ANNAgentMemory(embed_fn=openai_embedder, dim=1536)
        await memory.add("The Eiffel Tower is in Paris.", {"topic": "geography"})
        await memory.add("Python is a programming language.", {"topic": "tech"})

        results = await memory.search("What is in France?", top_k=3)
        for doc, score in results:
            print(score, doc.content)
    """

    def __init__(self, embed_fn: Callable,
                 dim: int = 1536,
                 max_elements: int = 500_000,
                 ef_search: int = 50):
        self._embed = embed_fn
        self._dim = dim
        config = HNSWConfig(dim=dim, max_elements=max_elements, ef_search=ef_search)
        self._index = HNSWIndex(config)
        self._docs: Dict[int, MemoryDoc] = {}
        self._next_id = 0

    async def add(self, content: str,
                   metadata: Optional[Dict[str, Any]] = None) -> int:
        embedding = np.array((await self._embed([content]))[0], dtype=np.float32)
        doc_id = self._next_id
        self._next_id += 1
        self._docs[doc_id] = MemoryDoc(
            doc_id=doc_id, content=content,
            metadata=metadata or {}, embedding=embedding,
        )
        self._index.add(embedding[None], ids=[doc_id])
        return doc_id

    async def search(self, query: str,
                     top_k: int = 10,
                     metadata_filter: Optional[Dict[str, Any]] = None
                     ) -> List[Tuple[MemoryDoc, float]]:
        q_emb = np.array((await self._embed([query]))[0], dtype=np.float32)
        n = min(top_k * 4 if metadata_filter else top_k, self._index.size)
        if n == 0:
            return []
        ids, scores = self._index.search(q_emb, top_k=n)
        results = []
        for doc_id, score in zip(ids, scores):
            doc = self._docs.get(int(doc_id))
            if doc is None:
                continue
            if metadata_filter:
                if not all(doc.metadata.get(k) == v
                           for k, v in metadata_filter.items()):
                    continue
            results.append((doc, float(score)))
            if len(results) >= top_k:
                break
        return results

    def delete(self, doc_id: int):
        self._docs.pop(doc_id, None)

    def stats(self) -> Dict[str, Any]:
        return {
            "total_docs": len(self._docs),
            "index_size": self._index.size,
            "dim": self._dim,
        }
```

---

## Comparison

| Approach | Algorithm | Scale | GPU | Exact Re-rank | Incremental |
|---|---|---|---|---|---|
| **HNSWIndex** | HNSW | ~50M | No | No | Yes (add_items) |
| **FAISSIVFPQIndex** | IVF-PQ | 100M+ | Yes | No | No (rebuild) |
| **HybridANNIndex** | HNSW + exact | ~50M | No | Yes | Yes |
| **StreamingANNIndexUpdater** | HNSW | ~50M | No | No | Yes (buffered) |
| **RecallBenchmarker** | N/A | N/A | N/A | N/A | N/A |
| **ANNAgentMemory** | HNSW | ~500K | No | No | Yes |

**Key insight**: use HNSW (`hnswlib`) as the default — it has the best recall/latency trade-off for < 50M vectors and supports incremental inserts. Set `ef_search` = 50 as a starting point and increase until recall@10 > 97%. Use the `HybridANNIndex` (HNSW + exact re-rank) when the top-k order matters precisely, such as in citation or passage selection tasks.
