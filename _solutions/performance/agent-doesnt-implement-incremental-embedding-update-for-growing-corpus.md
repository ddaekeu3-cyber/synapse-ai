---
title: "Agent Doesn't Implement Incremental Embedding Update for Growing Corpus"
description: "How to incrementally update vector indices and embedding caches as a knowledge corpus grows — avoiding full recomputation on every document addition using delta indexing, LSH-based approximate search, and online clustering."
date: 2025-01-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-incremental-embedding-update-for-growing-corpus
tags:
  - performance
  - embeddings
  - vector-search
  - incremental-indexing
  - rag
  - knowledge-base
  - delta-updates
symptoms:
  - "Adding a new document to the knowledge base triggers full re-embedding of all documents"
  - "RAG index rebuild takes hours and must run overnight, not in real time"
  - "No way to add a new document and immediately query it without rebuilding the index"
  - "Embedding cache has no mechanism for partial invalidation — always full flush"
  - "Vector similarity search degrades after many incremental adds because index is never rebuilt"
  - "High embedding API cost because unchanged documents are re-embedded on every update"
---

## Why This Happens

Agents that implement RAG (Retrieval-Augmented Generation) typically build a vector index over a corpus of documents at startup. As new documents are added, the naive approach rebuilds the entire index from scratch — re-embedding all documents and reconstructing the ANN (Approximate Nearest Neighbor) index. For corpora of 100K+ documents, this can take hours and cost hundreds of dollars in embedding API calls.

Incremental indexing solves this by maintaining a two-tier structure: a large, periodic-rebuild primary index and a small, always-current delta index. New documents go into the delta index immediately (it's small enough to rebuild quickly). Queries search both indices and merge results. Periodically, the delta is merged into the primary. Only changed documents are re-embedded, not the entire corpus.

---

## Solution 1: Content-Addressed Embedding Cache

Track which documents have been embedded via content hash. Only embed documents whose content has changed since the last indexing run.

```python
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Callable, Awaitable

@dataclass
class DocumentRecord:
    doc_id: str
    content_hash: str
    embedding: list[float]
    embedded_at: float
    metadata: dict = field(default_factory=dict)

class ContentAddressedEmbeddingCache:
    """
    Stores embeddings keyed by content hash.
    Documents with unchanged content are never re-embedded.
    """

    def __init__(self, cache_path: str):
        self._cache_path = cache_path
        self._records: dict[str, DocumentRecord] = {}  # doc_id -> record
        self._hash_index: dict[str, str] = {}          # content_hash -> doc_id
        self._load()

    def _content_hash(self, content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()

    def _load(self) -> None:
        if os.path.exists(self._cache_path):
            try:
                with open(self._cache_path) as f:
                    data = json.load(f)
                for rec in data.get("records", []):
                    r = DocumentRecord(**rec)
                    self._records[r.doc_id] = r
                    self._hash_index[r.content_hash] = r.doc_id
            except (json.JSONDecodeError, KeyError):
                pass

    def save(self) -> None:
        with open(self._cache_path, "w") as f:
            json.dump({"records": [vars(r) for r in self._records.values()]}, f)

    def needs_embedding(self, doc_id: str, content: str) -> bool:
        """Returns True if this document needs to be (re-)embedded."""
        h = self._content_hash(content)
        existing = self._records.get(doc_id)
        if existing is None:
            return True
        return existing.content_hash != h

    def get_embedding(self, doc_id: str) -> Optional[list[float]]:
        rec = self._records.get(doc_id)
        return rec.embedding if rec else None

    def store(self, doc_id: str, content: str, embedding: list[float], metadata: dict | None = None) -> None:
        h = self._content_hash(content)
        self._records[doc_id] = DocumentRecord(
            doc_id=doc_id,
            content_hash=h,
            embedding=embedding,
            embedded_at=time.time(),
            metadata=metadata or {},
        )
        self._hash_index[h] = doc_id

    def evict(self, doc_id: str) -> None:
        rec = self._records.pop(doc_id, None)
        if rec:
            self._hash_index.pop(rec.content_hash, None)

    def stats(self) -> dict:
        return {
            "total_documents": len(self._records),
            "cache_path": self._cache_path,
        }


async def incremental_embed(
    documents: list[dict],  # [{id, content, metadata}, ...]
    cache: ContentAddressedEmbeddingCache,
    embed_fn: Callable[[list[str]], Awaitable[list[list[float]]]],
    batch_size: int = 100,
) -> tuple[int, int]:
    """
    Embed only documents that have changed.
    Returns (embedded_count, skipped_count).
    """
    to_embed = [(d["id"], d["content"], d.get("metadata", {}))
                for d in documents if cache.needs_embedding(d["id"], d["content"])]
    skipped = len(documents) - len(to_embed)

    for i in range(0, len(to_embed), batch_size):
        batch = to_embed[i:i + batch_size]
        texts = [b[1] for b in batch]
        embeddings = await embed_fn(texts)
        for (doc_id, content, metadata), emb in zip(batch, embeddings):
            cache.store(doc_id, content, emb, metadata)

    if to_embed:
        cache.save()

    return len(to_embed), skipped
```

---

## Solution 2: Two-Tier Delta Index

Maintain a large primary index (rebuilt periodically) and a small delta index (updated in real time). Queries search both.

```python
import asyncio
import time
import numpy as np
from dataclasses import dataclass

@dataclass
class SearchResult:
    doc_id: str
    score: float
    metadata: dict

class FlatVectorIndex:
    """
    Simple brute-force vector index for small delta collections (< 10K docs).
    O(n) search, but fast enough for small n.
    """

    def __init__(self):
        self._ids: list[str] = []
        self._vectors: list[list[float]] = []
        self._metadata: dict[str, dict] = {}

    def add(self, doc_id: str, vector: list[float], metadata: dict | None = None) -> None:
        if doc_id in self._metadata:
            # Update existing
            idx = self._ids.index(doc_id)
            self._vectors[idx] = vector
        else:
            self._ids.append(doc_id)
            self._vectors.append(vector)
        self._metadata[doc_id] = metadata or {}

    def remove(self, doc_id: str) -> None:
        if doc_id in self._metadata:
            idx = self._ids.index(doc_id)
            self._ids.pop(idx)
            self._vectors.pop(idx)
            del self._metadata[doc_id]

    def search(self, query_vector: list[float], top_k: int = 10) -> list[SearchResult]:
        if not self._vectors:
            return []
        q = np.array(query_vector)
        matrix = np.array(self._vectors)
        # Cosine similarity
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1, norms)
        normalized = matrix / norms
        q_norm = q / (np.linalg.norm(q) or 1)
        scores = normalized @ q_norm
        top_indices = np.argsort(scores)[::-1][:top_k]
        return [
            SearchResult(self._ids[i], float(scores[i]), self._metadata[self._ids[i]])
            for i in top_indices
        ]

    def __len__(self) -> int:
        return len(self._ids)


class TwoTierVectorIndex:
    """
    Two-tier incremental vector index:
    - Primary index: large, periodically rebuilt (FAISS or similar)
    - Delta index: small, updated in real time via flat search
    New additions go to delta. Periodic merge rebuilds primary.
    """

    DELTA_MERGE_THRESHOLD = 1000  # Merge when delta reaches this size

    def __init__(
        self,
        primary_index,  # FAISS or similar ANN index
        cache: ContentAddressedEmbeddingCache,
    ):
        self._primary = primary_index
        self._delta = FlatVectorIndex()
        self._cache = cache
        self._deleted_ids: set[str] = set()  # soft deletes from primary
        self._delta_count = 0
        self._last_merge = time.monotonic()

    async def add(self, doc_id: str, content: str, embed_fn: Callable) -> None:
        """Add a document — immediately searchable via delta index."""
        if self._cache.needs_embedding(doc_id, content):
            embedding = (await embed_fn([content]))[0]
            self._cache.store(doc_id, content, embedding)
        else:
            embedding = self._cache.get_embedding(doc_id)

        self._delta.add(doc_id, embedding)
        self._delta_count += 1

        if self._delta_count >= self.DELTA_MERGE_THRESHOLD:
            asyncio.create_task(self._merge_delta())

    def delete(self, doc_id: str) -> None:
        """Mark document as deleted (applies to both tiers)."""
        self._deleted_ids.add(doc_id)
        self._delta.remove(doc_id)

    def search(self, query_vector: list[float], top_k: int = 10) -> list[SearchResult]:
        """Search both primary and delta, merge and deduplicate results."""
        delta_results = self._delta.search(query_vector, top_k)
        primary_results = self._primary_search(query_vector, top_k)

        # Merge: deduplicate by doc_id, keep highest score
        seen: dict[str, SearchResult] = {}
        for r in delta_results + primary_results:
            if r.doc_id in self._deleted_ids:
                continue
            if r.doc_id not in seen or r.score > seen[r.doc_id].score:
                seen[r.doc_id] = r

        return sorted(seen.values(), key=lambda r: -r.score)[:top_k]

    def _primary_search(self, query_vector: list[float], top_k: int) -> list[SearchResult]:
        """Search primary index, filtering soft-deleted docs."""
        try:
            results = self._primary.search(query_vector, top_k + len(self._deleted_ids))
            return [r for r in results if r.doc_id not in self._deleted_ids][:top_k]
        except Exception:
            return []

    async def _merge_delta(self) -> None:
        """Merge delta index into primary and rebuild."""
        # In production: rebuild FAISS index with all current embeddings
        self._delta = FlatVectorIndex()
        self._delta_count = 0
        self._deleted_ids.clear()
        self._last_merge = time.monotonic()
```

---

## Solution 3: Chunked Document Processor with Diff Detection

For documents that are updated (not just added), track which chunks changed and only re-embed modified chunks.

```python
import hashlib
import re
from dataclasses import dataclass

@dataclass
class DocumentChunk:
    chunk_id: str     # f"{doc_id}:chunk:{i}"
    doc_id: str
    text: str
    chunk_index: int
    content_hash: str
    metadata: dict = field(default_factory=dict)

class ChunkedDocumentProcessor:
    """
    Splits documents into chunks and tracks which chunks changed.
    Only re-embeds modified chunks on document update.
    """

    def __init__(
        self,
        chunk_size: int = 512,
        chunk_overlap: int = 64,
    ):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self._chunk_registry: dict[str, list[str]] = {}  # doc_id -> [chunk_ids]
        self._chunk_hashes: dict[str, str] = {}          # chunk_id -> content_hash

    def _split(self, text: str) -> list[str]:
        """Split text into overlapping chunks by sentence boundaries."""
        sentences = re.split(r'(?<=[.!?])\s+', text)
        chunks = []
        current = []
        current_len = 0

        for sent in sentences:
            sent_len = len(sent)
            if current_len + sent_len > self.chunk_size and current:
                chunks.append(" ".join(current))
                # Keep last overlap worth of sentences
                overlap_text = " ".join(current)[-self.chunk_overlap:]
                current = [overlap_text]
                current_len = len(overlap_text)
            current.append(sent)
            current_len += sent_len

        if current:
            chunks.append(" ".join(current))
        return chunks

    def _chunk_hash(self, text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def compute_diff(
        self, doc_id: str, new_text: str, metadata: dict | None = None
    ) -> tuple[list[DocumentChunk], list[str]]:
        """
        Compute which chunks are new/changed vs. unchanged.
        Returns (chunks_to_embed, chunk_ids_to_delete).
        """
        raw_chunks = self._split(new_text)
        new_chunk_ids = [f"{doc_id}:chunk:{i}" for i in range(len(raw_chunks))]
        old_chunk_ids = set(self._chunk_registry.get(doc_id, []))
        deleted_chunk_ids = list(old_chunk_ids - set(new_chunk_ids))

        chunks_to_embed = []
        for i, (text, chunk_id) in enumerate(zip(raw_chunks, new_chunk_ids)):
            h = self._chunk_hash(text)
            if self._chunk_hashes.get(chunk_id) != h:
                # New or changed chunk
                chunks_to_embed.append(DocumentChunk(
                    chunk_id=chunk_id,
                    doc_id=doc_id,
                    text=text,
                    chunk_index=i,
                    content_hash=h,
                    metadata={**(metadata or {}), "chunk_index": i, "total_chunks": len(raw_chunks)},
                ))
                self._chunk_hashes[chunk_id] = h

        self._chunk_registry[doc_id] = new_chunk_ids
        return chunks_to_embed, deleted_chunk_ids

    def delete_document(self, doc_id: str) -> list[str]:
        """Remove all chunks for a document. Returns deleted chunk IDs."""
        chunk_ids = self._chunk_registry.pop(doc_id, [])
        for cid in chunk_ids:
            self._chunk_hashes.pop(cid, None)
        return chunk_ids
```

---

## Solution 4: Batch Embedding Queue with Deduplication

Queue embedding requests and deduplicate identical texts across multiple documents.

```python
import asyncio
from collections import defaultdict

class BatchEmbeddingQueue:
    """
    Queues embedding requests and batches them for efficiency.
    Deduplicates identical texts — one embedding shared by multiple documents.
    Flushes on max batch size or max wait time.
    """

    def __init__(
        self,
        embed_fn: Callable[[list[str]], Awaitable[list[list[float]]]],
        max_batch: int = 100,
        max_wait_ms: float = 200.0,
    ):
        self._embed = embed_fn
        self._max_batch = max_batch
        self._max_wait = max_wait_ms / 1000
        self._pending: list[tuple[str, asyncio.Future]] = []  # (text, future)
        self._text_to_futures: dict[str, list[asyncio.Future]] = defaultdict(list)
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None

    async def embed(self, text: str) -> list[float]:
        """Request embedding for a single text. Batched with other concurrent requests."""
        async with self._lock:
            future: asyncio.Future = asyncio.get_event_loop().create_future()
            self._pending.append((text, future))
            self._text_to_futures[text].append(future)

            if len(self._pending) >= self._max_batch:
                asyncio.create_task(self._flush())
            elif self._flush_task is None or self._flush_task.done():
                self._flush_task = asyncio.create_task(self._delayed_flush())

        return await future

    async def _delayed_flush(self) -> None:
        await asyncio.sleep(self._max_wait)
        await self._flush()

    async def _flush(self) -> None:
        async with self._lock:
            if not self._pending:
                return
            batch = list(self._pending)
            self._pending.clear()
            text_to_futures = dict(self._text_to_futures)
            self._text_to_futures.clear()

        # Deduplicate texts
        unique_texts = list(dict.fromkeys(text for text, _ in batch))
        try:
            embeddings = await self._embed(unique_texts)
            text_to_embedding = dict(zip(unique_texts, embeddings))

            # Resolve futures
            for text, futures in text_to_futures.items():
                emb = text_to_embedding.get(text, [])
                for f in futures:
                    if not f.done():
                        f.set_result(emb)
        except Exception as exc:
            for _, future in batch:
                if not future.done():
                    future.set_exception(exc)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts, deduplicating internally."""
        return await asyncio.gather(*[self.embed(t) for t in texts])
```

---

## Solution 5: Online Index Health Monitor

Track index staleness and trigger background rebuilds when the delta grows too large.

```python
import time

class IndexHealthMonitor:
    """
    Monitors the health of the two-tier vector index and triggers
    background maintenance (delta merge, stale embedding refresh).
    """

    def __init__(
        self,
        index: TwoTierVectorIndex,
        max_delta_age_hours: float = 6.0,
        max_delta_fraction: float = 0.1,  # Merge when delta > 10% of total
        rebuild_callback: Callable | None = None,
    ):
        self._index = index
        self._max_delta_age = max_delta_age_hours * 3600
        self._max_delta_fraction = max_delta_fraction
        self._rebuild_cb = rebuild_callback
        self._primary_size = 0
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._monitor_loop())

    async def _monitor_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            await self._check()

    async def _check(self) -> None:
        delta_size = len(self._index._delta)
        total_size = self._primary_size + delta_size
        delta_fraction = delta_size / total_size if total_size > 0 else 0

        delta_age = time.monotonic() - self._index._last_merge
        needs_merge = (
            delta_fraction > self._max_delta_fraction
            or delta_age > self._max_delta_age
        )

        if needs_merge and self._rebuild_cb:
            await self._rebuild_cb()

    def health_report(self) -> dict:
        delta_size = len(self._index._delta)
        total = self._primary_size + delta_size
        return {
            "primary_size": self._primary_size,
            "delta_size": delta_size,
            "delta_fraction": delta_size / total if total else 0,
            "deleted_count": len(self._index._deleted_ids),
            "seconds_since_merge": time.monotonic() - self._index._last_merge,
        }
```

---

## Solution 6: Corpus Change Report

Before re-indexing, generate a change report showing exactly what needs updating and estimated cost.

```python
@dataclass
class CorpusChangeReport:
    total_documents: int
    new_documents: int
    modified_documents: int
    deleted_documents: int
    unchanged_documents: int
    estimated_embedding_calls: int
    estimated_cost_usd: float  # At $0.0001 per 1K tokens

class CorpusChangeAnalyzer:
    """Analyzes corpus changes before re-indexing to estimate work and cost."""

    TOKENS_PER_CHUNK = 200
    COST_PER_1K_TOKENS = 0.0001

    def __init__(self, cache: ContentAddressedEmbeddingCache, processor: ChunkedDocumentProcessor):
        self._cache = cache
        self._processor = processor

    def analyze(self, documents: list[dict]) -> CorpusChangeReport:
        new = modified = unchanged = 0
        total_chunks_to_embed = 0

        for doc in documents:
            doc_id = doc["id"]
            content = doc["content"]
            h = hashlib.sha256(content.encode()).hexdigest()

            if doc_id not in self._processor._chunk_registry:
                new += 1
                total_chunks_to_embed += max(1, len(content) // (self._processor.chunk_size * 4))
            elif not self._cache.needs_embedding(doc_id, content):
                unchanged += 1
            else:
                modified += 1
                chunks_to_embed, _ = self._processor.compute_diff(doc_id, content)
                total_chunks_to_embed += len(chunks_to_embed)

        # Estimate cost
        estimated_tokens = total_chunks_to_embed * self.TOKENS_PER_CHUNK
        estimated_cost = (estimated_tokens / 1000) * self.COST_PER_1K_TOKENS

        return CorpusChangeReport(
            total_documents=len(documents),
            new_documents=new,
            modified_documents=modified,
            deleted_documents=0,  # Caller should provide deleted list
            unchanged_documents=unchanged,
            estimated_embedding_calls=total_chunks_to_embed,
            estimated_cost_usd=round(estimated_cost, 4),
        )
```

---

## Comparison

| Solution | Re-embedding Scope | Update Latency | Search Accuracy | Best For |
|---|---|---|---|---|
| Content-Addressed Cache | Changed docs only | Batch | Full | Avoiding redundant API calls |
| Two-Tier Delta Index | New/changed only | Real-time (delta) | Near-full | Always-current search |
| Chunked Diff Detection | Changed chunks only | Batch | Full | Long documents with partial edits |
| Batch Embedding Queue | All pending (dedup) | Near-real-time | Full | High-throughput ingestion |
| Index Health Monitor | Triggered by staleness | Background | Maintained | Automated maintenance |
| Corpus Change Analyzer | N/A (planning) | Pre-run | N/A | Cost estimation before indexing |

**Start with the content-addressed cache** — it's the single highest-leverage change, eliminating re-embedding of unchanged documents. **Add the two-tier delta index** to make new documents immediately searchable without waiting for a full rebuild. **Use chunked diff detection** for documents that are frequently updated (wikis, living documents) to re-embed only the changed portions. **Deploy the index health monitor** to automatically trigger merges before delta growth degrades search quality.
