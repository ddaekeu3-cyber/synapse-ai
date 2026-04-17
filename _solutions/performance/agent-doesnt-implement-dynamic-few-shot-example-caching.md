---
title: "Agent Doesn't Implement Dynamic Few-Shot Example Caching"
description: "Agents that select few-shot examples by running a fresh semantic search on every request pay full embedding and retrieval costs repeatedly for examples that are reused frequently. Implement a dynamic few-shot cache that stores example embeddings in memory, scores candidates against the query embedding using cosine similarity, and evicts stale entries — reducing retrieval latency from hundreds of milliseconds to microseconds for cache hits."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-dynamic-few-shot-example-caching
tags: [few-shot, caching, embedding-cache, semantic-search, retrieval-optimization, prompt-efficiency]
symptoms:
  - "Every request triggers a full vector database query to select few-shot examples"
  - "Few-shot example sets are nearly identical across requests in the same domain"
  - "Embedding API costs account for a large fraction of per-request spend"
  - "P99 latency includes embedding + retrieval overhead even when examples are reused"
  - "No visibility into which few-shot examples are selected most frequently"
---

## Why This Happens

Dynamic few-shot selection retrieves examples semantically similar to the current query by embedding the query and searching a vector store. When many requests share similar intent (e.g., all asking about the same product domain), the same examples are retrieved repeatedly. Without a cache, each retrieval round-trips through the embedding API and vector store. A few-shot cache stores pre-computed example embeddings in memory, runs cosine similarity locally, and returns cached results for queries that are close enough to a previously answered query — eliminating both the embedding call and the vector store round-trip for cache hits.

## Solution 1: Few-Shot Example

```python
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class FewShotExample:
    example_id: str
    input_text: str
    output_text: str
    domain: str = ""
    quality_score: float = 1.0
    embedding: Optional[List[float]] = field(default=None, repr=False)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.example_id:
            import hashlib
            self.example_id = hashlib.sha256(self.input_text.encode()).hexdigest()[:12]
```

## Solution 2: Example Embedding Store

```python
import math
import time
from threading import Lock
from typing import Dict, List, Optional, Tuple


class FewShotEmbeddingStore:
    """
    Stores pre-computed embeddings for all candidate few-shot examples.
    Provides cosine similarity scoring without any external calls.
    """

    def __init__(self):
        self._examples: Dict[str, FewShotExample] = {}
        self._lock = Lock()

    def add(self, example: FewShotExample) -> None:
        if example.embedding is None:
            raise ValueError(f"Example {example.example_id} has no embedding")
        with self._lock:
            self._examples[example.example_id] = example

    def remove(self, example_id: str) -> None:
        with self._lock:
            self._examples.pop(example_id, None)

    def search(
        self,
        query_embedding: List[float],
        top_k: int = 5,
        domain: Optional[str] = None,
    ) -> List[Tuple[FewShotExample, float]]:
        with self._lock:
            candidates = list(self._examples.values())
        if domain:
            candidates = [e for e in candidates if e.domain == domain]
        scored = [
            (example, self._cosine(query_embedding, example.embedding))
            for example in candidates
            if example.embedding
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    @staticmethod
    def _cosine(a: List[float], b: List[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def size(self) -> int:
        with self._lock:
            return len(self._examples)
```

## Solution 3: Query Result Cache

```python
import hashlib
import time
from collections import OrderedDict
from threading import Lock
from typing import List, Optional, Tuple


class FewShotQueryCache:
    """
    Caches the result of few-shot retrieval queries keyed on a
    quantized query embedding fingerprint. Cache hits skip both
    the embedding API call and the similarity search.
    """

    def __init__(
        self,
        max_entries: int = 1000,
        ttl_seconds: float = 3600.0,
        similarity_bucket_precision: int = 3,
    ):
        self._max = max_entries
        self._ttl = ttl_seconds
        self._precision = similarity_bucket_precision
        self._cache: OrderedDict[str, Tuple[List[FewShotExample], float]] = OrderedDict()
        self._lock = Lock()
        self._hits = 0
        self._misses = 0

    def _key(self, query_embedding: List[float], top_k: int, domain: str) -> str:
        quantized = [round(v, self._precision) for v in query_embedding]
        raw = f"{quantized}:{top_k}:{domain}"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def get(
        self,
        query_embedding: List[float],
        top_k: int,
        domain: str = "",
    ) -> Optional[List[FewShotExample]]:
        key = self._key(query_embedding, top_k, domain)
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._misses += 1
                return None
            examples, cached_at = entry
            if time.time() - cached_at > self._ttl:
                del self._cache[key]
                self._misses += 1
                return None
            self._cache.move_to_end(key)
            self._hits += 1
            return examples

    def put(
        self,
        query_embedding: List[float],
        top_k: int,
        domain: str,
        examples: List[FewShotExample],
    ) -> None:
        key = self._key(query_embedding, top_k, domain)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            else:
                if len(self._cache) >= self._max:
                    self._cache.popitem(last=False)
            self._cache[key] = (examples, time.time())

    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            return {
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / max(total, 1), 4),
                "cached_entries": len(self._cache),
            }
```

## Solution 4: Cached Few-Shot Retriever

```python
import time
from typing import Any, Callable, List, Optional


class CachedFewShotRetriever:
    """
    Retrieves few-shot examples using the in-memory store for similarity search
    and the query cache to skip repeated lookups. Falls back to embedding
    the query only on cache misses.
    """

    def __init__(
        self,
        store: FewShotEmbeddingStore,
        cache: FewShotQueryCache,
        embed_fn: Callable[[str], List[float]],
    ):
        self._store = store
        self._cache = cache
        self._embed = embed_fn
        self._total_latency_ms = 0.0
        self._call_count = 0

    async def retrieve(
        self,
        query: str,
        top_k: int = 5,
        domain: str = "",
    ) -> List[FewShotExample]:
        start = time.time()

        # Embed query first (needed for both cache key and store search)
        query_embedding = await self._embed(query)

        # Check cache
        cached = self._cache.get(query_embedding, top_k, domain)
        if cached is not None:
            self._record_latency(start)
            return cached

        # Cache miss: run similarity search in-process
        scored = self._store.search(query_embedding, top_k=top_k, domain=domain)
        examples = [ex for ex, _ in scored]

        self._cache.put(query_embedding, top_k, domain, examples)
        self._record_latency(start)
        return examples

    def _record_latency(self, start: float) -> None:
        self._total_latency_ms += (time.time() - start) * 1000
        self._call_count += 1

    def avg_latency_ms(self) -> float:
        return round(self._total_latency_ms / max(self._call_count, 1), 2)
```

## Solution 5: Example Usage Tracker

```python
import time
from collections import Counter
from threading import Lock
from typing import List


class FewShotExampleUsageTracker:
    """
    Tracks how often each few-shot example is selected so that
    low-quality or rarely-used examples can be pruned and high-quality
    examples can be promoted to a faster tier.
    """

    def __init__(self):
        self._counter: Counter = Counter()
        self._last_used: dict = {}
        self._lock = Lock()

    def record(self, examples: List[FewShotExample]) -> None:
        now = time.time()
        with self._lock:
            for ex in examples:
                self._counter[ex.example_id] += 1
                self._last_used[ex.example_id] = now

    def top_examples(self, n: int = 10) -> List[dict]:
        with self._lock:
            most_common = self._counter.most_common(n)
        return [
            {
                "example_id": eid,
                "use_count": count,
                "last_used_seconds_ago": round(time.time() - self._last_used.get(eid, 0), 1),
            }
            for eid, count in most_common
        ]

    def unused_since(self, seconds: float) -> List[str]:
        cutoff = time.time() - seconds
        with self._lock:
            return [
                eid for eid, last in self._last_used.items()
                if last < cutoff
            ]
```

## Solution 6: Few-Shot Cache Dashboard

```python
import time


class FewShotCacheDashboard:
    """
    Combines cache stats, store size, retriever latency, and usage
    tracking into a single operational snapshot.
    """

    def __init__(
        self,
        store: FewShotEmbeddingStore,
        cache: FewShotQueryCache,
        retriever: CachedFewShotRetriever,
        usage_tracker: FewShotExampleUsageTracker,
    ):
        self._store = store
        self._cache = cache
        self._retriever = retriever
        self._usage = usage_tracker

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "store": {
                "example_count": self._store.size(),
            },
            "cache": self._cache.stats(),
            "retriever": {
                "avg_latency_ms": self._retriever.avg_latency_ms(),
                "call_count": self._retriever._call_count,
            },
            "top_examples": self._usage.top_examples(5),
            "unused_24h": len(self._usage.unused_since(86400)),
        }
```

## Comparison

| Approach | Embedding Store | Query Cache | Cache Miss Fallback | Usage Tracking | Dashboard |
|---|---|---|---|---|---|
| FewShotEmbeddingStore | Yes (in-memory cosine) | No | No | No | No |
| FewShotQueryCache | No | Yes (LRU + TTL) | No | No | No |
| CachedFewShotRetriever | Via store | Via cache | Yes (embed + search) | No | No |
| FewShotExampleUsageTracker | No | No | No | Yes (frequency) | No |
| FewShotCacheDashboard | No | No | No | No | Yes |

**Best for production**: Set `similarity_bucket_precision=3` for the query cache key — this quantizes embedding dimensions to 3 decimal places, grouping semantically similar queries into the same cache bucket without false positives. Use a `ttl_seconds=3600` cache TTL and refresh the example store whenever new examples are added to the vector database. Run `FewShotExampleUsageTracker.unused_since(86400)` nightly to identify examples that have not been selected in 24 hours — these are candidates for pruning, which improves retrieval quality by keeping the store dense with high-signal examples. Monitor cache hit rate: below 40% suggests query distribution is too diverse for caching to help; above 80% suggests the example set could be reduced.
