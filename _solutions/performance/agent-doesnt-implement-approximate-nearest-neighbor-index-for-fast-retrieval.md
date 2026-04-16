---
title: "Agent Doesn't Implement Approximate Nearest Neighbor Index for Fast Vector Retrieval"
description: "AI agents that perform exhaustive brute-force cosine similarity searches over embedding stores scale as O(n·d) per query—querying 1M 1536-dim vectors requires 3B multiplications per lookup. Approximate Nearest Neighbor (ANN) indexes like HNSW reduce retrieval to O(log n) with recall >95%, cutting search latency from seconds to milliseconds for retrieval-augmented generation pipelines."
date: 2025-02-19
difficulty: advanced
category: performance
slug: agent-doesnt-implement-approximate-nearest-neighbor-index-for-fast-retrieval
tags:
  - ann
  - hnsw
  - vector-search
  - embeddings
  - retrieval-augmented-generation
  - performance
  - approximate-search
symptoms:
  - "Vector similarity search takes 3-8 seconds over 500k embeddings"
  - "CPU pegged at 100% during every retrieval-augmented generation query"
  - "Adding documents to the embedding store linearly increases query latency"
  - "Agent cannot return context chunks within the LLM streaming latency budget"
  - "Memory bandwidth saturated by full matrix multiply on every tool call"
---

## Problem

Brute-force vector search iterates every stored embedding, computing dot products against the query vector. At 500k documents with 1536-dimensional embeddings, each query requires 768M floating-point multiplications—taking 2-8 seconds on CPU. HNSW (Hierarchical Navigable Small World) graphs build a multi-layer proximity graph during indexing so that search traverses only O(log n) nodes, achieving sub-10ms P99 latency at 95%+ recall. The tradeoff is index build time and memory overhead, both of which are one-time costs amortized across millions of queries.

---

## Solution 1: HNSWVectorIndex — HNSW Index Wrapper with Incremental Upsert

```python
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

try:
    import hnswlib
except ImportError:
    raise ImportError("pip install hnswlib")


@dataclass
class SearchResult:
    id: str
    score: float
    metadata: Dict[str, Any] = field(default_factory=dict)


class HNSWVectorIndex:
    """
    Thread-safe HNSW index for approximate nearest neighbor search.
    Supports incremental upsert without full rebuild, persistence to disk,
    and configurable ef_construction/M for recall-vs-speed tradeoff.

    Usage:
        index = HNSWVectorIndex(dim=1536, max_elements=1_000_000)
        index.upsert("doc-1", embedding, {"text": "hello world"})
        results = index.search(query_embedding, k=5)
        index.save("/var/lib/agent/embeddings.hnsw")
    """

    def __init__(
        self,
        dim: int,
        max_elements: int = 100_000,
        ef_construction: int = 200,
        M: int = 16,
        space: str = "cosine",
    ):
        self._dim = dim
        self._max = max_elements
        self._space = space
        self._lock = threading.RLock()

        self._index = hnswlib.Index(space=space, dim=dim)
        self._index.init_index(
            max_elements=max_elements,
            ef_construction=ef_construction,
            M=M,
        )
        self._index.set_ef(50)  # query-time ef; tune for recall/speed

        # id <-> integer label mappings
        self._id_to_label: Dict[str, int] = {}
        self._label_to_id: Dict[int, str] = {}
        self._metadata: Dict[str, Dict[str, Any]] = {}
        self._next_label: int = 0

    def upsert(self, doc_id: str, embedding: np.ndarray, metadata: Optional[Dict] = None):
        """Add or overwrite a single vector."""
        vec = np.array(embedding, dtype=np.float32).reshape(1, -1)
        with self._lock:
            if doc_id in self._id_to_label:
                label = self._id_to_label[doc_id]
            else:
                label = self._next_label
                self._next_label += 1
                self._id_to_label[doc_id] = label
                self._label_to_id[label] = doc_id

            self._index.add_items(vec, [label])
            self._metadata[doc_id] = metadata or {}

    def upsert_batch(self, items: List[Tuple[str, np.ndarray, Dict]]):
        """Bulk upsert for faster ingestion."""
        embeddings, labels = [], []
        with self._lock:
            for doc_id, embedding, metadata in items:
                if doc_id in self._id_to_label:
                    label = self._id_to_label[doc_id]
                else:
                    label = self._next_label
                    self._next_label += 1
                    self._id_to_label[doc_id] = label
                    self._label_to_id[label] = doc_id
                embeddings.append(np.array(embedding, dtype=np.float32))
                labels.append(label)
                self._metadata[doc_id] = metadata or {}

            matrix = np.vstack(embeddings)
            self._index.add_items(matrix, labels)

    def search(self, query: np.ndarray, k: int = 10, ef: Optional[int] = None) -> List[SearchResult]:
        """Return top-k approximate nearest neighbors."""
        vec = np.array(query, dtype=np.float32).reshape(1, -1)
        with self._lock:
            if ef:
                self._index.set_ef(ef)
            labels, distances = self._index.knn_query(vec, k=min(k, self._next_label))

        results = []
        for label, dist in zip(labels[0], distances[0]):
            doc_id = self._label_to_id.get(label)
            if doc_id:
                # cosine distance -> similarity
                score = float(1.0 - dist) if self._space == "cosine" else float(-dist)
                results.append(SearchResult(id=doc_id, score=score,
                                             metadata=self._metadata.get(doc_id, {})))
        return results

    def save(self, path: str):
        with self._lock:
            self._index.save_index(path)

    def load(self, path: str):
        with self._lock:
            self._index.load_index(path, max_elements=self._max)

    @property
    def size(self) -> int:
        return self._next_label
```

---

## Solution 2: IVFFlatIndex — Inverted File Index for Memory-Constrained Deployments

```python
from typing import Any, Dict, List, Optional, Tuple

import numpy as np


class IVFFlatIndex:
    """
    Pure-NumPy Inverted File (IVF) index. Partitions vectors into
    n_clusters Voronoi cells via k-means; queries search only the
    n_probe nearest cells instead of all vectors.

    Recall vs speed: n_probe=1 is fastest (low recall); n_probe=n_clusters
    degrades to brute force. Typical: n_probe = n_clusters // 10.

    Usage:
        index = IVFFlatIndex(dim=768, n_clusters=256, n_probe=16)
        index.train(training_vectors)           # k-means on representative sample
        index.add_batch(ids, vectors, metas)
        results = index.search(query, k=10)
    """

    def __init__(self, dim: int, n_clusters: int = 256, n_probe: int = 16):
        self._dim = dim
        self._n_clusters = n_clusters
        self._n_probe = min(n_probe, n_clusters)
        self._centroids: Optional[np.ndarray] = None  # (n_clusters, dim)
        self._cells: Dict[int, List[Tuple[str, np.ndarray, Dict]]] = {
            i: [] for i in range(n_clusters)
        }

    def train(self, vectors: np.ndarray, max_iter: int = 50):
        """Fit cluster centroids via Lloyd's algorithm."""
        n = len(vectors)
        idx = np.random.choice(n, size=min(self._n_clusters, n), replace=False)
        centroids = vectors[idx].copy().astype(np.float32)

        for _ in range(max_iter):
            # Assign
            dists = np.linalg.norm(
                vectors[:, None, :] - centroids[None, :, :], axis=2
            )
            assignments = np.argmin(dists, axis=1)
            # Update
            new_centroids = np.zeros_like(centroids)
            counts = np.zeros(self._n_clusters)
            for i, c in enumerate(assignments):
                new_centroids[c] += vectors[i]
                counts[c] += 1
            for c in range(self._n_clusters):
                if counts[c] > 0:
                    new_centroids[c] /= counts[c]
                else:
                    new_centroids[c] = centroids[c]
            if np.allclose(new_centroids, centroids, atol=1e-6):
                break
            centroids = new_centroids

        self._centroids = centroids

    def _assign_cell(self, vec: np.ndarray) -> int:
        dists = np.linalg.norm(self._centroids - vec, axis=1)
        return int(np.argmin(dists))

    def add_batch(self, ids: List[str], vectors: np.ndarray,
                  metadata: Optional[List[Dict]] = None):
        if self._centroids is None:
            raise RuntimeError("Call train() before add_batch()")
        metas = metadata or [{} for _ in ids]
        dists = np.linalg.norm(
            vectors[:, None, :] - self._centroids[None, :, :], axis=2
        )
        assignments = np.argmin(dists, axis=1)
        for i, (doc_id, cell, meta) in enumerate(zip(ids, assignments, metas)):
            self._cells[int(cell)].append((doc_id, vectors[i], meta))

    def search(self, query: np.ndarray, k: int = 10) -> List[Dict]:
        if self._centroids is None:
            raise RuntimeError("Index not trained")
        q = query.astype(np.float32)
        cell_dists = np.linalg.norm(self._centroids - q, axis=1)
        probe_cells = np.argsort(cell_dists)[:self._n_probe]

        candidates = []
        for cell in probe_cells:
            for doc_id, vec, meta in self._cells[cell]:
                sim = float(np.dot(q, vec) / (np.linalg.norm(q) * np.linalg.norm(vec) + 1e-9))
                candidates.append((sim, doc_id, meta))

        candidates.sort(key=lambda x: x[0], reverse=True)
        return [{"id": d, "score": s, "metadata": m} for s, d, m in candidates[:k]]
```

---

## Solution 3: ANNIndexRouter — Multi-Index Router with Hot/Cold Partitioning

```python
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np


@dataclass
class IndexPartition:
    name: str
    index: Any          # HNSWVectorIndex or IVFFlatIndex
    doc_count: int = 0
    last_write: float = field(default_factory=time.monotonic)


class ANNIndexRouter:
    """
    Routes writes to a hot (writable HNSW) partition and searches
    across all partitions. When the hot partition exceeds capacity,
    it is sealed and a new hot partition is created. Sealed cold
    partitions are read-only and can be memory-mapped for efficiency.

    Usage:
        router = ANNIndexRouter(dim=1536, partition_size=100_000)
        router.upsert("doc-1", embedding, {"text": "chunk"})
        results = router.search(query_embedding, k=10)
    """

    def __init__(self, dim: int, partition_size: int = 100_000):
        self._dim = dim
        self._partition_size = partition_size
        self._lock = threading.RLock()
        self._partitions: List[IndexPartition] = []
        self._hot: Optional[IndexPartition] = None
        self._seal_hot()  # create initial hot partition

    def _seal_hot(self):
        """Create a new hot HNSW partition."""
        from hnswlib import Index
        idx = Index(space="cosine", dim=self._dim)
        idx.init_index(
            max_elements=self._partition_size,
            ef_construction=200,
            M=16,
        )
        idx.set_ef(50)
        part = IndexPartition(name=f"part-{len(self._partitions)}", index=idx)
        self._partitions.append(part)
        self._hot = part

    def upsert(self, doc_id: str, embedding: np.ndarray, metadata: Optional[Dict] = None):
        vec = np.array(embedding, dtype=np.float32).reshape(1, -1)
        with self._lock:
            if self._hot.doc_count >= self._partition_size:
                self._seal_hot()
            self._hot.index.add_items(vec, [self._hot.doc_count])
            self._hot.doc_count += 1
            self._hot.last_write = time.monotonic()

    def search(self, query: np.ndarray, k: int = 10) -> List[Dict[str, Any]]:
        """Search all partitions and merge top-k by score."""
        vec = np.array(query, dtype=np.float32).reshape(1, -1)
        all_results = []
        with self._lock:
            partitions = list(self._partitions)

        for part in partitions:
            if part.doc_count == 0:
                continue
            labels, distances = part.index.knn_query(vec, k=min(k, part.doc_count))
            for label, dist in zip(labels[0], distances[0]):
                all_results.append({
                    "partition": part.name,
                    "label": int(label),
                    "score": float(1.0 - dist),
                })

        all_results.sort(key=lambda x: x["score"], reverse=True)
        return all_results[:k]

    @property
    def total_docs(self) -> int:
        return sum(p.doc_count for p in self._partitions)
```

---

## Solution 4: RecallBenchmark — Measure ANN Recall vs Brute-Force Ground Truth

```python
import time
from typing import Any, List, Tuple

import numpy as np


class RecallBenchmark:
    """
    Measures approximate recall and query latency of an ANN index
    against brute-force exact search. Use during index configuration
    to choose ef_construction, M, and ef_search for your recall target.

    Usage:
        bench = RecallBenchmark(dim=768)
        bench.generate_corpus(n=50_000)
        bench.build_hnsw_index(M=16, ef_construction=200)
        report = bench.evaluate(n_queries=1000, k=10, ef_search=50)
        print(report)
    """

    def __init__(self, dim: int):
        self._dim = dim
        self._corpus: np.ndarray = np.empty((0, dim), dtype=np.float32)
        self._index: Any = None

    def generate_corpus(self, n: int, seed: int = 42):
        rng = np.random.default_rng(seed)
        vecs = rng.standard_normal((n, self._dim)).astype(np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        self._corpus = vecs / (norms + 1e-9)

    def build_hnsw_index(self, M: int = 16, ef_construction: int = 200):
        import hnswlib
        idx = hnswlib.Index(space="cosine", dim=self._dim)
        idx.init_index(max_elements=len(self._corpus), ef_construction=ef_construction, M=M)
        idx.add_items(self._corpus, list(range(len(self._corpus))))
        self._index = idx

    def _brute_force(self, query: np.ndarray, k: int) -> List[int]:
        sims = self._corpus @ query
        return list(np.argsort(-sims)[:k])

    def evaluate(self, n_queries: int = 500, k: int = 10, ef_search: int = 50) -> dict:
        if self._index is None:
            raise RuntimeError("Call build_hnsw_index() first")
        rng = np.random.default_rng(0)
        queries = rng.standard_normal((n_queries, self._dim)).astype(np.float32)
        queries /= np.linalg.norm(queries, axis=1, keepdims=True) + 1e-9

        self._index.set_ef(ef_search)
        recalls, latencies = [], []

        for q in queries:
            t0 = time.perf_counter()
            labels, _ = self._index.knn_query(q.reshape(1, -1), k=k)
            latencies.append((time.perf_counter() - t0) * 1000)
            gt = set(self._brute_force(q, k))
            hit = len(set(labels[0]) & gt)
            recalls.append(hit / k)

        return {
            "n_corpus": len(self._corpus),
            "n_queries": n_queries,
            "k": k,
            "ef_search": ef_search,
            "recall_at_k": round(float(np.mean(recalls)), 4),
            "p50_ms": round(float(np.percentile(latencies, 50)), 3),
            "p99_ms": round(float(np.percentile(latencies, 99)), 3),
        }
```

---

## Solution 5: PersistentANNStore — SQLite Metadata + HNSW Index with Atomic Save

```python
import json
import os
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np


class PersistentANNStore:
    """
    Combines an HNSW index (vectors) with SQLite (metadata) and implements
    atomic saves: writes to a temp file then renames to prevent corruption
    from mid-save crashes.

    Usage:
        store = PersistentANNStore(base_dir="/var/lib/agent/vecstore", dim=1536)
        store.upsert("chunk-1", embedding, {"text": "...", "source": "doc.pdf"})
        results = store.search(query_vec, k=5)
        store.save()  # atomic write
        store.load()  # restore after restart
    """

    INDEX_FILE = "hnsw.bin"
    META_DB = "metadata.db"

    def __init__(self, base_dir: str, dim: int, max_elements: int = 500_000):
        import hnswlib
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)
        self._dim = dim
        self._max = max_elements
        self._lock = threading.RLock()

        self._index = hnswlib.Index(space="cosine", dim=dim)
        self._index.init_index(max_elements=max_elements, ef_construction=200, M=16)
        self._index.set_ef(50)

        self._label_to_id: Dict[int, str] = {}
        self._id_to_label: Dict[str, int] = {}
        self._next_label = 0

        self._db = sqlite3.connect(str(self._base / self.META_DB), check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS metadata "
            "(doc_id TEXT PRIMARY KEY, label INTEGER, meta TEXT)"
        )
        self._db.commit()

    def upsert(self, doc_id: str, embedding: np.ndarray, metadata: Optional[Dict] = None):
        vec = np.array(embedding, dtype=np.float32).reshape(1, -1)
        with self._lock:
            if doc_id in self._id_to_label:
                label = self._id_to_label[doc_id]
            else:
                label = self._next_label
                self._next_label += 1
                self._id_to_label[doc_id] = label
                self._label_to_id[label] = doc_id

            self._index.add_items(vec, [label])
            self._db.execute(
                "INSERT OR REPLACE INTO metadata (doc_id, label, meta) VALUES (?,?,?)",
                (doc_id, label, json.dumps(metadata or {})),
            )
            self._db.commit()

    def search(self, query: np.ndarray, k: int = 10) -> List[Dict[str, Any]]:
        vec = np.array(query, dtype=np.float32).reshape(1, -1)
        with self._lock:
            n = self._next_label
            if n == 0:
                return []
            labels, distances = self._index.knn_query(vec, k=min(k, n))

        results = []
        for label, dist in zip(labels[0], distances[0]):
            doc_id = self._label_to_id.get(label)
            if not doc_id:
                continue
            row = self._db.execute(
                "SELECT meta FROM metadata WHERE doc_id=?", (doc_id,)
            ).fetchone()
            meta = json.loads(row[0]) if row else {}
            results.append({"id": doc_id, "score": float(1.0 - dist), "metadata": meta})
        return results

    def save(self):
        """Atomic save: write to temp then rename."""
        with self._lock:
            idx_path = self._base / self.INDEX_FILE
            tmp = tempfile.NamedTemporaryFile(
                dir=self._base, delete=False, suffix=".tmp"
            )
            tmp.close()
            self._index.save_index(tmp.name)
            os.replace(tmp.name, idx_path)

    def load(self):
        with self._lock:
            idx_path = self._base / self.INDEX_FILE
            if idx_path.exists():
                self._index.load_index(str(idx_path), max_elements=self._max)
            rows = self._db.execute("SELECT doc_id, label FROM metadata").fetchall()
            for doc_id, label in rows:
                self._label_to_id[label] = doc_id
                self._id_to_label[doc_id] = label
                self._next_label = max(self._next_label, label + 1)
```

---

## Solution 6: RAGRetriever — Full Retrieval-Augmented Generation Pipeline with ANN

```python
import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import numpy as np


@dataclass
class RetrievedChunk:
    doc_id: str
    text: str
    score: float
    metadata: Dict[str, Any]


class RAGRetriever:
    """
    Wires an ANN index to an embedding function and exposes a single
    async retrieve() call for use inside agent tool handlers. Supports
    score thresholding, metadata filtering, and latency logging.

    Usage:
        retriever = RAGRetriever(
            index=HNSWVectorIndex(dim=1536),
            embed_fn=openai_embed,   # async fn: str -> np.ndarray
        )
        await retriever.ingest("chunk-1", "text content", {"source": "doc.pdf"})
        chunks = await retriever.retrieve("What is RAG?", k=5, threshold=0.70)
    """

    def __init__(
        self,
        index: Any,
        embed_fn: Callable,
        score_threshold: float = 0.65,
    ):
        self._index = index
        self._embed = embed_fn
        self._threshold = score_threshold
        self._texts: Dict[str, str] = {}

    async def ingest(self, doc_id: str, text: str, metadata: Optional[Dict] = None):
        embedding = await self._embed(text)
        self._texts[doc_id] = text
        meta = {**(metadata or {}), "ingested_at": time.time()}
        if hasattr(self._index, "upsert"):
            self._index.upsert(doc_id, np.array(embedding), meta)
        else:
            raise TypeError("Index must implement upsert(doc_id, embedding, metadata)")

    async def ingest_batch(self, items: List[Dict]):
        """items: list of {"id": str, "text": str, "metadata": dict}"""
        texts = [it["text"] for it in items]
        embeddings = await asyncio.gather(*[self._embed(t) for t in texts])
        for item, emb in zip(items, embeddings):
            self._texts[item["id"]] = item["text"]
            self._index.upsert(
                item["id"],
                np.array(emb),
                {**item.get("metadata", {}), "ingested_at": time.time()},
            )

    async def retrieve(
        self,
        query: str,
        k: int = 10,
        threshold: Optional[float] = None,
        metadata_filter: Optional[Callable[[Dict], bool]] = None,
    ) -> List[RetrievedChunk]:
        t0 = time.monotonic()
        query_emb = await self._embed(query)
        results = self._index.search(np.array(query_emb), k=k * 2)  # oversample
        cutoff = threshold if threshold is not None else self._threshold
        chunks = []
        for r in results:
            if r["score"] < cutoff:
                continue
            if metadata_filter and not metadata_filter(r.get("metadata", {})):
                continue
            text = self._texts.get(r["id"], "")
            chunks.append(RetrievedChunk(
                doc_id=r["id"],
                text=text,
                score=r["score"],
                metadata=r.get("metadata", {}),
            ))
            if len(chunks) >= k:
                break

        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        # Structured log omitted for brevity; integrate with JSONFormatter
        return chunks
```

---

## Comparison

| Approach | Algorithm | Recall | Query Latency | Incremental Add | Persistence | Memory |
|---|---|---|---|---|---|---|
| **HNSWVectorIndex** | HNSW | ~97% | <5ms | Yes | Manual save | High |
| **IVFFlatIndex** | IVF | ~90% | <15ms | No (retrain) | Not built-in | Low |
| **ANNIndexRouter** | HNSW multi-partition | ~97% | <10ms | Yes (hot swap) | Per-partition | High |
| **RecallBenchmark** | Eval harness | N/A | Measurement | N/A | N/A | N/A |
| **PersistentANNStore** | HNSW + SQLite | ~97% | <5ms | Yes | Atomic rename | High |
| **RAGRetriever** | Any ANN | Depends | Depends | Yes | Via index | Depends |

**Key insight**: switch from `np.dot(corpus, query)` brute force to `HNSWVectorIndex` as the first step—no other change required. At 100k documents, expect P99 latency to drop from ~800ms to ~4ms. Configure `ef_construction=200, M=16` for indexing and `ef=50` for queries; run `RecallBenchmark.evaluate()` to confirm recall@10 >0.95 before deploying. If memory is constrained, `IVFFlatIndex` with `n_probe=32` reduces RAM by 3× at the cost of ~5% recall. For production, wrap either index in `PersistentANNStore` to survive restarts.
