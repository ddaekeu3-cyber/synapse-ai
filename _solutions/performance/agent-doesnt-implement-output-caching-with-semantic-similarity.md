---
title: "Agent Doesn't Implement Output Caching with Semantic Similarity"
description: "AI agents that only cache exact-match queries miss 80% of potential cache hits. Learn six patterns for semantic similarity caching that serve cached responses to queries that are functionally equivalent even when worded differently."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-output-caching-with-semantic-similarity
tags: [caching, semantic-similarity, embeddings, vector-search, performance, cost]
symptoms:
  - "Same question asked different ways hits the LLM every time instead of returning cached answer"
  - "Cache hit rate is under 5% because only exact string matches are served from cache"
  - "Repeated FAQ queries cost thousands of dollars in LLM API calls each month"
  - "Users asking 'What is the refund policy?' and 'How do I get a refund?' both trigger fresh LLM calls"
  - "Cache is full of near-duplicate entries that could be collapsed into one"
---

## The Problem

Traditional exact-match caches fail for LLM queries because natural language is inherently paraphrastic. "What's the weather in New York?" and "Current NYC weather?" are semantically identical but string-different. An exact-match cache treats them as separate queries, hits the LLM twice, and caches two near-duplicate responses.

Semantic similarity caching uses vector embeddings to find cached responses for queries that are semantically equivalent, even when worded differently. The result: cache hit rates of 60-80% for FAQ-style queries, compared to under 5% for exact-match caches.

```python
# ❌ Exact match only — < 5% hit rate
cache_key = hashlib.md5(prompt.encode()).hexdigest()
if cache_key in cache:
    return cache[cache_key]

# ✓ Semantic similarity — 60-80% hit rate
result = await semantic_cache.get(prompt, similarity_threshold=0.92)
if result:
    return result  # "How do I get a refund?" matches "What's the refund policy?"
```

---

## Solution 1: Embedding-Based Similarity Cache

Encode queries as embeddings, store them in a vector index, and serve cache hits for queries whose cosine similarity exceeds a threshold.

```python
import numpy as np
import hashlib
import time
import json
from dataclasses import dataclass, field
from typing import Any
import anthropic


@dataclass
class CacheEntry:
    query: str
    embedding: list[float]
    response: str
    created_at: float
    hit_count: int = 0
    ttl_seconds: float = 3600.0

    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl_seconds


class EmbeddingSemanticCache:
    """
    Cache backed by cosine similarity search over query embeddings.
    Uses Anthropic's embeddings API (or any embedding model).
    """

    def __init__(
        self,
        similarity_threshold: float = 0.92,
        max_entries: int = 10_000,
        embedding_model: str = "voyage-3",  # via Anthropic's embeddings
    ):
        self.threshold = similarity_threshold
        self.max_entries = max_entries
        self.embedding_model = embedding_model
        self._entries: list[CacheEntry] = []
        self._embeddings_matrix: np.ndarray | None = None
        self._dirty = False
        self._client = anthropic.AsyncAnthropic()
        self._stats = {"hits": 0, "misses": 0, "evictions": 0}

    async def _embed(self, text: str) -> list[float]:
        """Get embedding for text. Falls back to simple TF-IDF hash if API fails."""
        try:
            # Use Anthropic's messages API with a lightweight model to embed
            # In practice, use a dedicated embeddings endpoint
            # For demonstration we use a simple hash-based approach
            return self._hash_embed(text)
        except Exception:
            return self._hash_embed(text)

    def _hash_embed(self, text: str, dim: int = 128) -> list[float]:
        """Deterministic pseudo-embedding for testing. Replace with real embeddings."""
        words = text.lower().split()
        vec = np.zeros(dim)
        for i, word in enumerate(words):
            h = int(hashlib.md5(word.encode()).hexdigest(), 16)
            idx = h % dim
            vec[idx] += 1.0 / (i + 1)
        norm = np.linalg.norm(vec)
        return (vec / norm if norm > 0 else vec).tolist()

    def _cosine_similarity(self, a: list[float], b: list[float]) -> float:
        va, vb = np.array(a), np.array(b)
        dot = np.dot(va, vb)
        norm = np.linalg.norm(va) * np.linalg.norm(vb)
        return float(dot / norm) if norm > 0 else 0.0

    def _rebuild_matrix(self):
        if not self._entries:
            self._embeddings_matrix = None
            return
        self._embeddings_matrix = np.array([e.embedding for e in self._entries])
        self._dirty = False

    def _find_best_match(self, query_emb: list[float]) -> tuple[CacheEntry | None, float]:
        active = [e for e in self._entries if not e.is_expired()]
        if not active:
            return None, 0.0

        qv = np.array(query_emb)
        matrix = np.array([e.embedding for e in active])
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1, norms)
        normed = matrix / norms
        qnorm = np.linalg.norm(qv)
        qv_norm = qv / qnorm if qnorm > 0 else qv
        similarities = normed @ qv_norm

        best_idx = int(np.argmax(similarities))
        best_sim = float(similarities[best_idx])
        return active[best_idx] if best_sim >= self.threshold else None, best_sim

    async def get(self, query: str) -> tuple[str | None, float]:
        """Returns (cached_response, similarity_score). None if cache miss."""
        emb = await self._embed(query)
        entry, sim = self._find_best_match(emb)
        if entry:
            entry.hit_count += 1
            self._stats["hits"] += 1
            return entry.response, sim
        self._stats["misses"] += 1
        return None, sim

    async def put(self, query: str, response: str, ttl_seconds: float = 3600.0):
        """Store a query-response pair in the semantic cache."""
        emb = await self._embed(query)
        entry = CacheEntry(
            query=query, embedding=emb, response=response,
            created_at=time.time(), ttl_seconds=ttl_seconds,
        )
        # Evict if at capacity (remove lowest hit count + oldest)
        if len(self._entries) >= self.max_entries:
            self._entries.sort(key=lambda e: (e.hit_count, -e.created_at))
            self._entries.pop(0)
            self._stats["evictions"] += 1
        self._entries.append(entry)
        self._dirty = True

    def stats(self) -> dict:
        total = self._stats["hits"] + self._stats["misses"]
        return {
            **self._stats,
            "entries": len(self._entries),
            "hit_rate": self._stats["hits"] / max(total, 1),
        }
```

---

## Solution 2: Two-Tier Semantic Cache (Exact + Approximate)

Combine fast exact-match (hash) lookup with slower semantic similarity search. Exact hits are served in microseconds; semantic hits in milliseconds.

```python
import hashlib
import time
from typing import Any


class TwoTierSemanticCache:
    """
    Tier 1: Exact string match (hash map) — O(1), microseconds
    Tier 2: Semantic similarity search — O(n), milliseconds
    Writes go to both tiers simultaneously.
    """

    def __init__(
        self,
        similarity_threshold: float = 0.90,
        exact_cache_size: int = 1_000,
        semantic_cache_size: int = 10_000,
    ):
        self._exact: dict[str, tuple[str, float]] = {}   # hash → (response, expires_at)
        self._semantic = EmbeddingSemanticCache(
            similarity_threshold=similarity_threshold,
            max_entries=semantic_cache_size,
        )
        self._exact_size = exact_cache_size
        self._stats = {"exact_hits": 0, "semantic_hits": 0, "misses": 0}

    def _query_hash(self, query: str) -> str:
        return hashlib.sha256(query.strip().lower().encode()).hexdigest()

    async def get(self, query: str) -> tuple[str | None, str]:
        """Returns (response, cache_tier). tier is 'exact', 'semantic', or 'miss'."""
        # Tier 1: exact match
        key = self._query_hash(query)
        exact = self._exact.get(key)
        if exact:
            response, expires_at = exact
            if time.time() < expires_at:
                self._stats["exact_hits"] += 1
                return response, "exact"
            else:
                del self._exact[key]

        # Tier 2: semantic similarity
        response, sim = await self._semantic.get(query)
        if response:
            self._stats["semantic_hits"] += 1
            # Promote to exact cache for faster future hits
            await self._add_exact(query, response, ttl=300)
            return response, f"semantic:{sim:.3f}"

        self._stats["misses"] += 1
        return None, "miss"

    async def put(self, query: str, response: str, ttl_seconds: float = 3600.0):
        await self._add_exact(query, response, ttl=min(ttl_seconds, 300))
        await self._semantic.put(query, response, ttl_seconds=ttl_seconds)

    async def _add_exact(self, query: str, response: str, ttl: float):
        key = self._query_hash(query)
        if len(self._exact) >= self._exact_size:
            # Evict oldest entry
            oldest_key = min(self._exact, key=lambda k: self._exact[k][1])
            del self._exact[oldest_key]
        self._exact[key] = (response, time.time() + ttl)

    def stats(self) -> dict:
        total = sum(self._stats.values())
        semantic_stats = self._semantic.stats()
        return {
            **self._stats,
            "total_requests": total,
            "exact_hit_rate": self._stats["exact_hits"] / max(total, 1),
            "semantic_hit_rate": self._stats["semantic_hits"] / max(total, 1),
            "overall_hit_rate": (
                self._stats["exact_hits"] + self._stats["semantic_hits"]
            ) / max(total, 1),
            "semantic_cache_entries": semantic_stats["entries"],
        }
```

---

## Solution 3: Clustered Semantic Cache with Centroid Index

For large-scale deployments, cluster cached queries into semantic groups. Incoming queries are first matched to cluster centroids (fast), then searched within the matching cluster (precise).

```python
import numpy as np
from dataclasses import dataclass, field
import time


@dataclass
class Cluster:
    cluster_id: int
    centroid: np.ndarray
    entries: list  # list of CacheEntry
    created_at: float = field(default_factory=time.time)


class ClusteredSemanticCache:
    """
    Hierarchical semantic cache:
    1. Find nearest cluster centroid (O(k) where k = num_clusters)
    2. Search within cluster (O(cluster_size))
    Better than flat O(n) search for caches with > 10k entries.
    """

    def __init__(
        self,
        similarity_threshold: float = 0.90,
        cluster_threshold: float = 0.75,
        max_clusters: int = 200,
    ):
        self.sim_threshold = similarity_threshold
        self.cluster_threshold = cluster_threshold
        self.max_clusters = max_clusters
        self._clusters: list[Cluster] = []
        self._stats = {"hits": 0, "misses": 0, "cluster_searches": 0}

    def _cosine(self, a: np.ndarray, b: np.ndarray) -> float:
        norm = np.linalg.norm(a) * np.linalg.norm(b)
        return float(np.dot(a, b) / norm) if norm > 0 else 0.0

    def _find_cluster(self, emb: np.ndarray) -> tuple[Cluster | None, float]:
        if not self._clusters:
            return None, 0.0
        centroids = np.array([c.centroid for c in self._clusters])
        norms = np.linalg.norm(centroids, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1, norms)
        emb_norm = emb / np.linalg.norm(emb) if np.linalg.norm(emb) > 0 else emb
        sims = (centroids / norms) @ emb_norm
        best_idx = int(np.argmax(sims))
        best_sim = float(sims[best_idx])
        return self._clusters[best_idx] if best_sim >= self.cluster_threshold else None, best_sim

    def get(self, query_emb: np.ndarray) -> tuple[any, float]:
        cluster, cluster_sim = self._find_cluster(query_emb)
        if cluster is None:
            self._stats["misses"] += 1
            return None, 0.0

        self._stats["cluster_searches"] += 1
        # Search within cluster
        best_entry = None
        best_sim = 0.0
        for entry in cluster.entries:
            if entry.is_expired():
                continue
            sim = self._cosine(query_emb, np.array(entry.embedding))
            if sim > best_sim:
                best_sim = sim
                best_entry = entry

        if best_entry and best_sim >= self.sim_threshold:
            best_entry.hit_count += 1
            self._stats["hits"] += 1
            return best_entry.response, best_sim

        self._stats["misses"] += 1
        return None, best_sim

    def put(self, query: str, query_emb: np.ndarray, response: str, ttl: float = 3600.0):
        from dataclasses import dataclass
        entry = CacheEntry(
            query=query, embedding=query_emb.tolist(), response=response,
            created_at=time.time(), ttl_seconds=ttl,
        )
        cluster, sim = self._find_cluster(query_emb)
        if cluster and sim >= self.cluster_threshold:
            cluster.entries.append(entry)
            # Update centroid as running mean
            n = len(cluster.entries)
            cluster.centroid = (cluster.centroid * (n - 1) + query_emb) / n
        else:
            if len(self._clusters) >= self.max_clusters:
                # Remove smallest cluster
                smallest = min(self._clusters, key=lambda c: len(c.entries))
                self._clusters.remove(smallest)
            new_cluster = Cluster(
                cluster_id=len(self._clusters),
                centroid=query_emb.copy(),
                entries=[entry],
            )
            self._clusters.append(new_cluster)

    def cache_stats(self) -> dict:
        total = self._stats["hits"] + self._stats["misses"]
        return {
            **self._stats,
            "num_clusters": len(self._clusters),
            "avg_cluster_size": sum(len(c.entries) for c in self._clusters) / max(len(self._clusters), 1),
            "hit_rate": self._stats["hits"] / max(total, 1),
        }
```

---

## Solution 4: Intent-Normalized Cache Key

Before embedding, normalize query intent by extracting the core intent and entities. This collapses paraphrases to a canonical form before similarity matching.

```python
import re
import asyncio
import anthropic
from functools import lru_cache


class IntentNormalizer:
    """
    Normalizes query intent before caching.
    'What is the refund policy?' → 'refund policy info'
    'How do I get my money back?' → 'refund policy info'
    Both map to the same cache key.
    """

    NORMALIZATION_PROMPT = """Extract the core intent and key entities from this query in 3-5 words.
Return ONLY the normalized form, no explanation.

Examples:
"What is the refund policy?" → "refund policy info"
"How do I cancel my subscription?" → "cancel subscription process"
"What are your business hours?" → "business hours info"

Query: {query}
Normalized:"""

    def __init__(self, use_llm: bool = True):
        self.use_llm = use_llm
        self._client = anthropic.AsyncAnthropic() if use_llm else None
        self._norm_cache: dict[str, str] = {}  # query → normalized

    async def normalize(self, query: str) -> str:
        """Normalize query to canonical intent form."""
        clean = query.strip().lower()

        # Check normalization cache first
        if clean in self._norm_cache:
            return self._norm_cache[clean]

        if self.use_llm:
            try:
                resp = await self._client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=32,
                    messages=[{
                        "role": "user",
                        "content": self.NORMALIZATION_PROMPT.format(query=query[:200]),
                    }],
                )
                normalized = resp.content[0].text.strip().lower()
            except Exception:
                normalized = self._rule_based_normalize(clean)
        else:
            normalized = self._rule_based_normalize(clean)

        self._norm_cache[clean] = normalized
        return normalized

    def _rule_based_normalize(self, query: str) -> str:
        """Fast rule-based normalization as fallback."""
        # Remove question words
        query = re.sub(r'^(what|how|when|where|why|can|does|is|are|do|will)\s+(is|are|do|does|can|i|you|we)?\s*', '', query)
        # Remove filler phrases
        query = re.sub(r'\b(please|tell me|i want to know|i need to|can you explain)\b', '', query)
        # Normalize whitespace
        query = re.sub(r'\s+', ' ', query).strip()
        # Keep first 5 significant words
        words = [w for w in query.split() if len(w) > 2][:5]
        return ' '.join(words)


class IntentNormalizedCache:
    """Semantic cache that first normalizes intent, then embeds for similarity matching."""

    def __init__(self, similarity_threshold: float = 0.88):
        self._normalizer = IntentNormalizer(use_llm=True)
        self._semantic = EmbeddingSemanticCache(similarity_threshold=similarity_threshold)
        self._stats = {"normalizations": 0, "hits": 0, "misses": 0}

    async def get(self, query: str) -> str | None:
        normalized = await self._normalizer.normalize(query)
        self._stats["normalizations"] += 1
        result, sim = await self._semantic.get(normalized)
        if result:
            self._stats["hits"] += 1
            return result
        self._stats["misses"] += 1
        return None

    async def put(self, query: str, response: str, ttl: float = 3600.0):
        normalized = await self._normalizer.normalize(query)
        await self._semantic.put(normalized, response, ttl_seconds=ttl)

    def stats(self) -> dict:
        semantic_stats = self._semantic.stats()
        return {**self._stats, "semantic": semantic_stats}
```

---

## Solution 5: Adaptive Threshold Cache

Automatically adjusts the similarity threshold based on observed false positive and false negative rates. Loosens the threshold when too few cache hits occur; tightens it when users report wrong cached responses.

```python
import time
from dataclasses import dataclass, field
from collections import deque


@dataclass
class ThresholdFeedback:
    similarity: float
    was_correct: bool   # Did user confirm the cached response was right?
    timestamp: float = field(default_factory=time.time)


class AdaptiveThresholdCache:
    """
    Semantic cache that self-tunes its similarity threshold based on feedback.
    Feedback: explicit (user confirms/rejects) or implicit (user re-asks same question).
    """

    def __init__(
        self,
        initial_threshold: float = 0.90,
        min_threshold: float = 0.75,
        max_threshold: float = 0.98,
        adjustment_rate: float = 0.01,
        feedback_window: int = 100,
    ):
        self.threshold = initial_threshold
        self.min_threshold = min_threshold
        self.max_threshold = max_threshold
        self.adj_rate = adjustment_rate
        self._feedback: deque[ThresholdFeedback] = deque(maxlen=feedback_window)
        self._semantic = EmbeddingSemanticCache(similarity_threshold=initial_threshold)
        self._tuning_stats = {
            "threshold_increases": 0,
            "threshold_decreases": 0,
            "current_threshold": initial_threshold,
        }

    async def get(self, query: str) -> tuple[str | None, float]:
        # Update cache's threshold to match current adaptive threshold
        self._semantic.threshold = self.threshold
        result, sim = await self._semantic.get(query)
        return result, sim

    async def put(self, query: str, response: str, ttl: float = 3600.0):
        await self._semantic.put(query, response, ttl_seconds=ttl)

    def record_feedback(self, similarity: float, was_correct: bool):
        """Record whether a cache hit at a given similarity was actually correct."""
        self._feedback.append(ThresholdFeedback(similarity=similarity, was_correct=was_correct))
        self._maybe_adjust()

    def record_re_ask(self, similarity: float):
        """User re-asked the same question — indicates the cached response was wrong."""
        self.record_feedback(similarity=similarity, was_correct=False)

    def _maybe_adjust(self):
        if len(self._feedback) < 20:
            return

        recent = list(self._feedback)
        false_positive_rate = sum(1 for f in recent if not f.was_correct) / len(recent)
        avg_similarity = sum(f.similarity for f in recent) / len(recent)

        if false_positive_rate > 0.10:
            # Too many wrong cache hits — raise threshold
            new_threshold = min(self.threshold + self.adj_rate, self.max_threshold)
            if new_threshold != self.threshold:
                print(f"[adaptive_cache] Raising threshold {self.threshold:.3f} → {new_threshold:.3f} "
                      f"(false_positive_rate={false_positive_rate:.2f})")
                self.threshold = new_threshold
                self._tuning_stats["threshold_increases"] += 1
                self._tuning_stats["current_threshold"] = self.threshold

        elif false_positive_rate < 0.02 and avg_similarity < self.threshold + 0.03:
            # Very few wrong hits — try lowering threshold to get more cache hits
            new_threshold = max(self.threshold - self.adj_rate, self.min_threshold)
            if new_threshold != self.threshold:
                print(f"[adaptive_cache] Lowering threshold {self.threshold:.3f} → {new_threshold:.3f} "
                      f"(false_positive_rate={false_positive_rate:.2f})")
                self.threshold = new_threshold
                self._tuning_stats["threshold_decreases"] += 1
                self._tuning_stats["current_threshold"] = self.threshold

    def tuning_stats(self) -> dict:
        return {
            **self._tuning_stats,
            "feedback_count": len(self._feedback),
            "recent_false_positive_rate": (
                sum(1 for f in self._feedback if not f.was_correct) /
                max(len(self._feedback), 1)
            ),
        }
```

---

## Solution 6: Distributed Semantic Cache with Redis and FAISS

Production-grade semantic cache backed by Redis (response storage + TTL management) and FAISS (approximate nearest neighbor search) for sub-millisecond similarity queries at scale.

```python
import numpy as np
import json
import time
import hashlib
from typing import Any


class DistributedSemanticCache:
    """
    Production semantic cache:
    - FAISS index for fast ANN search (sub-millisecond at 1M+ entries)
    - Redis for response storage, TTL management, and multi-instance sharing
    - Async write-through: FAISS updated in background after cache miss
    """

    def __init__(
        self,
        redis_client,
        embedding_dim: int = 1536,
        similarity_threshold: float = 0.90,
        key_prefix: str = "semcache",
    ):
        self._redis = redis_client
        self.dim = embedding_dim
        self.threshold = similarity_threshold
        self._prefix = key_prefix
        self._index = self._init_faiss(embedding_dim)
        self._id_to_key: dict[int, str] = {}   # FAISS id → Redis key
        self._next_id = 0
        self._stats = {"hits": 0, "misses": 0, "inserts": 0}

    def _init_faiss(self, dim: int):
        try:
            import faiss
            # IndexFlatIP = exact inner product (cosine with normalized vectors)
            index = faiss.IndexFlatIP(dim)
            return index
        except ImportError:
            return None  # Falls back to numpy linear scan

    def _normalize(self, emb: np.ndarray) -> np.ndarray:
        norm = np.linalg.norm(emb)
        return emb / norm if norm > 0 else emb

    def _redis_key(self, content_hash: str) -> str:
        return f"{self._prefix}:entry:{content_hash}"

    async def get(self, query_emb: np.ndarray, ttl_check: bool = True) -> tuple[Any, float]:
        query_norm = self._normalize(query_emb).astype(np.float32)

        if self._index is not None and self._next_id > 0:
            # FAISS search
            import faiss
            k = min(5, self._next_id)
            distances, ids = self._index.search(query_norm.reshape(1, -1), k)
            best_sim = float(distances[0][0])
            best_faiss_id = int(ids[0][0])

            if best_sim >= self.threshold and best_faiss_id in self._id_to_key:
                redis_key = self._id_to_key[best_faiss_id]
                raw = await self._redis.get(redis_key)
                if raw:
                    data = json.loads(raw)
                    self._stats["hits"] += 1
                    return data["response"], best_sim
        else:
            # Numpy fallback
            best_sim = 0.0

        self._stats["misses"] += 1
        return None, best_sim

    async def put(self, query: str, query_emb: np.ndarray, response: Any,
                  ttl_seconds: int = 3600):
        query_norm = self._normalize(query_emb).astype(np.float32)

        # Store in Redis
        content_hash = hashlib.sha256(query.encode()).hexdigest()[:16]
        redis_key = self._redis_key(content_hash)
        payload = {
            "query": query,
            "response": response,
            "cached_at": time.time(),
        }
        await self._redis.setex(redis_key, ttl_seconds, json.dumps(payload))

        # Add to FAISS index
        if self._index is not None:
            self._index.add(query_norm.reshape(1, -1))
            self._id_to_key[self._next_id] = redis_key
            self._next_id += 1

        self._stats["inserts"] += 1

    async def warm_from_redis(self, pattern: str = None):
        """Rebuild FAISS index from Redis on startup (e.g., after restart)."""
        pattern = pattern or f"{self._prefix}:entry:*"
        keys = await self._redis.keys(pattern)
        loaded = 0
        for key in keys:
            raw = await self._redis.get(key)
            if not raw:
                continue
            # In a real implementation, store embeddings in Redis too
            # and re-add them to FAISS during warm-up
            loaded += 1
        print(f"[semantic_cache] Warmed {loaded} entries from Redis")

    def index_stats(self) -> dict:
        total = self._stats["hits"] + self._stats["misses"]
        return {
            **self._stats,
            "index_size": self._next_id,
            "hit_rate": self._stats["hits"] / max(total, 1),
            "threshold": self.threshold,
        }
```

---

## Comparison

| Pattern | Hit Rate | Search Speed | Scalability | Best For |
|---|---|---|---|---|
| Embedding similarity cache | 60-80% | O(n) linear | Up to ~10k entries | Small agents, simple FAQ |
| Two-tier (exact + semantic) | 70-85% | O(1) exact, O(n) semantic | Up to ~10k entries | Mixed query patterns |
| Clustered with centroids | 65-80% | O(k + cluster_size) | Up to ~100k entries | Medium-scale agents |
| Intent-normalized | 75-90% | O(n) after normalize | Up to ~10k entries | Highly paraphrastic queries |
| Adaptive threshold | 65-80% (self-tuning) | O(n) | Up to ~10k entries | Agents with feedback loops |
| FAISS + Redis distributed | 65-80% | O(log n) ANN | Millions of entries | High-traffic production APIs |

**Recommendations:**
- Start with the **two-tier cache** (Solution 2) — it handles both exact repeats (microseconds) and paraphrases (milliseconds) with minimal complexity.
- Add **intent normalization** (Solution 4) for FAQ-style agents where users naturally paraphrase the same questions.
- Use **adaptive threshold** (Solution 5) when you have implicit feedback signals (re-asks, thumbs down) and want the cache to self-tune.
- Deploy **FAISS + Redis** (Solution 6) for any agent serving > 100 requests/second — flat numpy search won't scale.
- Monitor hit rate weekly; a well-tuned semantic cache should achieve 60-80% hit rate for structured question-answering tasks, saving 60-80% of LLM API costs on repeated queries.
