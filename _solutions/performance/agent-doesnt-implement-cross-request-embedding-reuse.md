---
title: "Agent Doesn't Implement Cross-Request Embedding Reuse"
description: "Agent sessions recompute embeddings for the same or semantically similar content on every request, paying repeated embedding API costs and latency even when the underlying content hasn't changed."
category: performance
difficulty: intermediate
tags: [embeddings, caching, reuse, similarity, vector, rag, performance, deduplication]
---

# Agent Doesn't Implement Cross-Request Embedding Reuse

## Problem

Embedding calls are paid twice: in latency (10–100ms per call) and cost (~$0.00002/1K tokens for text-embedding-3-small). Agents that re-embed the same document chunks on every RAG query, recompute user query embeddings that appeared in previous turns, or fail to share embeddings across concurrent requests all waste significant resources. The fix is a content-addressed embedding cache: if you've seen this text before, return the cached vector instantly.

## Solution 1: Content-Addressed Embedding Cache

Hash the input text to create a cache key. Return the cached embedding if it exists; compute and store it otherwise.

```python
import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Optional
import anthropic

anthropic_client = anthropic.AsyncAnthropic()

@dataclass
class EmbeddingCacheEntry:
    embedding: list[float]
    model: str
    computed_at: float
    hit_count: int = 0

class ContentAddressedEmbeddingCache:
    """
    Cache keyed by SHA-256(model + text).
    Two requests for the same text with the same model always share the embedding.
    """

    def __init__(self, max_entries: int = 10_000, ttl_seconds: float = 3600.0):
        self._cache: dict[str, EmbeddingCacheEntry] = {}
        self._max_entries = max_entries
        self._ttl = ttl_seconds
        self._hits = 0
        self._misses = 0

    def _key(self, text: str, model: str) -> str:
        return hashlib.sha256(f"{model}:{text}".encode()).hexdigest()

    def get(self, text: str, model: str) -> Optional[list[float]]:
        key = self._key(text, model)
        entry = self._cache.get(key)
        if entry is None:
            self._misses += 1
            return None
        if time.monotonic() - entry.computed_at > self._ttl:
            del self._cache[key]
            self._misses += 1
            return None
        entry.hit_count += 1
        self._hits += 1
        return entry.embedding

    def put(self, text: str, model: str, embedding: list[float]) -> None:
        if len(self._cache) >= self._max_entries:
            # Evict the entry with the oldest access time (LRU approximation)
            oldest = min(self._cache.items(), key=lambda x: x[1].computed_at)
            del self._cache[oldest[0]]
        key = self._key(text, model)
        self._cache[key] = EmbeddingCacheEntry(
            embedding=embedding, model=model, computed_at=time.monotonic()
        )

    @property
    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 3) if total else 0.0,
            "cached_entries": len(self._cache),
        }

embedding_cache = ContentAddressedEmbeddingCache()

async def embed(text: str, model: str = "text-embedding-3-small") -> list[float]:
    """Return cached embedding or compute and cache a new one."""
    cached = embedding_cache.get(text, model)
    if cached is not None:
        return cached

    # Use Anthropic client for embeddings via API — note: use the embeddings endpoint
    # For this example we simulate with a placeholder; in practice use your embedding provider
    await asyncio.sleep(0.05)  # simulate API call
    embedding = [0.1] * 1536   # placeholder — replace with real embedding call

    embedding_cache.put(text, model, embedding)
    return embedding

async def rag_query(query: str, documents: list[str]) -> dict:
    # All embeddings computed concurrently; duplicates automatically deduplicated
    query_emb, doc_embs = await asyncio.gather(
        embed(query),
        asyncio.gather(*[embed(doc) for doc in documents]),
    )

    # Cosine similarity ranking (simplified)
    def cosine_sim(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        mag_a = sum(x ** 2 for x in a) ** 0.5
        mag_b = sum(x ** 2 for x in b) ** 0.5
        return dot / (mag_a * mag_b) if mag_a and mag_b else 0.0

    ranked = sorted(
        zip(documents, doc_embs),
        key=lambda x: cosine_sim(query_emb, x[1]),
        reverse=True,
    )
    return {
        "top_doc": ranked[0][0] if ranked else None,
        "cache_stats": embedding_cache.stats,
    }
```

**When to use**: Any agent with RAG. Document chunk embeddings are computed once at ingestion and reused for every query — the cache hit rate on document chunks should approach 100%.

---

## Solution 2: Request Coalescing — Deduplicate Concurrent Identical Requests

When multiple concurrent requests need the same embedding simultaneously, compute it only once and share the result with all waiters.

```python
import asyncio
import hashlib
from typing import Any
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

class EmbeddingCoalescer:
    """
    Coalesces concurrent requests for the same embedding into a single API call.
    If 10 coroutines request embed("hello world") simultaneously,
    only 1 API call is made; all 10 receive the same result.
    """

    def __init__(self):
        self._in_flight: dict[str, asyncio.Future] = {}
        self._cache: dict[str, list[float]] = {}

    async def embed(self, text: str, model: str = "text-embedding-3-small") -> list[float]:
        key = hashlib.sha256(f"{model}:{text}".encode()).hexdigest()

        # Already cached
        if key in self._cache:
            return self._cache[key]

        # Already being computed — wait for the same future
        if key in self._in_flight:
            return await self._in_flight[key]

        # First request: create a Future and compute
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._in_flight[key] = fut

        try:
            embedding = await self._compute(text, model)
            self._cache[key] = embedding
            fut.set_result(embedding)
            return embedding
        except Exception as exc:
            fut.set_exception(exc)
            raise
        finally:
            self._in_flight.pop(key, None)

    async def _compute(self, text: str, model: str) -> list[float]:
        """Simulate embedding API call."""
        await asyncio.sleep(0.1)
        return [hash(text + str(i)) % 100 / 100 for i in range(8)]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts concurrently with automatic coalescing."""
        return await asyncio.gather(*[self.embed(t) for t in texts])

coalescer = EmbeddingCoalescer()

async def demo_coalescing():
    """Demonstrate that 10 concurrent identical requests produce 1 API call."""
    import time
    texts = ["shared query"] * 10 + ["unique query " + str(i) for i in range(5)]

    start = time.monotonic()
    embeddings = await coalescer.embed_batch(texts)
    elapsed = time.monotonic() - start

    # "shared query" embedded once (~0.1s); 5 unique queries in parallel (~0.1s)
    # Total ≈ 0.1s, not 1.5s (15 × 0.1s)
    return {"embeddings_computed": len(set(map(id, embeddings[:10]))), "elapsed_s": round(elapsed, 2)}
```

**When to use**: Agents under concurrent load where multiple users ask similar questions simultaneously. Coalescing is especially valuable for popular queries (e.g., "what are your hours?") that would otherwise be re-embedded thousands of times per day.

---

## Solution 3: Session-Scoped Embedding Pool — Reuse Within a Conversation

Within a multi-turn conversation, cache embeddings for all text seen so far in the session. Documents and prior messages don't need re-embedding each turn.

```python
import asyncio
import hashlib
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass
class SessionEmbeddingPool:
    """
    Per-session embedding pool. All embeddings computed during a session
    are available for reuse throughout that session.
    Freed when the session ends.
    """
    session_id: str
    _pool: dict[str, list[float]] = field(default_factory=dict)
    _compute_count: int = 0
    _reuse_count: int = 0

    def _key(self, text: str, model: str) -> str:
        return hashlib.sha256(f"{model}:{text}".encode()).hexdigest()

    def get(self, text: str, model: str = "text-embedding-3-small") -> list[float] | None:
        key = self._key(text, model)
        emb = self._pool.get(key)
        if emb is not None:
            self._reuse_count += 1
        return emb

    def put(self, text: str, model: str, embedding: list[float]) -> None:
        key = self._key(text, model)
        self._pool[key] = embedding
        self._compute_count += 1

    async def embed(self, text: str, model: str = "text-embedding-3-small") -> list[float]:
        existing = self.get(text, model)
        if existing is not None:
            return existing
        # Compute (simulated)
        await asyncio.sleep(0.05)
        embedding = [hash(f"{text}{i}") % 100 / 100.0 for i in range(8)]
        self.put(text, model, embedding)
        return embedding

    async def embed_messages(self, messages: list[dict]) -> list[list[float]]:
        """Embed all messages; reuse if same message appears again."""
        return await asyncio.gather(*[
            self.embed(m.get("content", "")) for m in messages
        ])

    @property
    def savings_pct(self) -> float:
        total = self._compute_count + self._reuse_count
        return round(100 * self._reuse_count / total, 1) if total else 0.0

    def destroy(self) -> None:
        self._pool.clear()

_session_pools: dict[str, SessionEmbeddingPool] = {}

def get_or_create_pool(session_id: str) -> SessionEmbeddingPool:
    if session_id not in _session_pools:
        _session_pools[session_id] = SessionEmbeddingPool(session_id=session_id)
    return _session_pools[session_id]

async def rag_agent_turn(
    session_id: str,
    user_message: str,
    document_corpus: list[str],
) -> dict:
    pool = get_or_create_pool(session_id)

    # Embed query and all documents (corpus embeddings reused across turns)
    query_emb, doc_embs = await asyncio.gather(
        pool.embed(user_message),
        asyncio.gather(*[pool.embed(doc) for doc in document_corpus]),
    )

    # Simple retrieval
    def dot(a, b): return sum(x * y for x, y in zip(a, b))
    ranked = sorted(zip(document_corpus, doc_embs), key=lambda x: dot(query_emb, x[1]), reverse=True)
    top_doc = ranked[0][0] if ranked else ""

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"Context: {top_doc}\n\n{user_message}"}],
    )

    return {
        "response": resp.content[0].text,
        "embedding_savings_pct": pool.savings_pct,
        "pool_size": len(pool._pool),
    }
    # Note: call _session_pools[session_id].destroy() when session ends
```

**When to use**: RAG agents with a fixed document corpus. The corpus embeddings are computed once per session (or once globally) and reused for every query — the dominant use case for embedding reuse.

---

## Solution 4: Approximate Match — Skip Recomputation for Near-Duplicate Queries

When a new query is very similar to a recently-seen query, reuse the prior embedding rather than computing a new one.

```python
import asyncio
import math
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x ** 2 for x in a))
    mag_b = math.sqrt(sum(x ** 2 for x in b))
    return dot / (mag_a * mag_b) if mag_a and mag_b else 0.0

def edit_distance_ratio(a: str, b: str) -> float:
    """Quick approximate similarity for short strings."""
    if a == b:
        return 1.0
    longer = max(len(a), len(b))
    if longer == 0:
        return 1.0
    common = sum(1 for x, y in zip(a, b) if x == y)
    return common / longer

@dataclass
class ApproximateEmbeddingCache:
    """
    Cache that returns an existing embedding when the input text is
    'close enough' to a previously seen input (above similarity threshold).
    """
    similarity_threshold: float = 0.95  # text similarity (edit distance)
    max_entries: int = 500
    _entries: list[tuple[str, list[float]]] = field(default_factory=list)
    _exact_hits: int = 0
    _approx_hits: int = 0
    _misses: int = 0

    async def embed(self, text: str) -> tuple[list[float], str]:
        """Returns (embedding, source) where source is 'exact'|'approx'|'computed'."""
        # Check for exact match first
        for stored_text, stored_emb in self._entries:
            if stored_text == text:
                self._exact_hits += 1
                return stored_emb, "exact"

        # Check for approximate match using lightweight text similarity
        for stored_text, stored_emb in self._entries:
            sim = edit_distance_ratio(text.lower(), stored_text.lower())
            if sim >= self.similarity_threshold:
                self._approx_hits += 1
                return stored_emb, "approx"

        # Compute new embedding
        await asyncio.sleep(0.05)  # simulate API call
        embedding = [hash(f"{text}{i}") % 100 / 100.0 for i in range(8)]

        self._entries.append((text, embedding))
        if len(self._entries) > self.max_entries:
            self._entries.pop(0)

        self._misses += 1
        return embedding, "computed"

    @property
    def stats(self) -> dict:
        total = self._exact_hits + self._approx_hits + self._misses
        return {
            "exact_hits": self._exact_hits,
            "approx_hits": self._approx_hits,
            "misses": self._misses,
            "reuse_rate": round((self._exact_hits + self._approx_hits) / total, 3) if total else 0.0,
        }

approx_cache = ApproximateEmbeddingCache(similarity_threshold=0.90)

async def agent_with_approx_reuse(queries: list[str]) -> dict:
    results = []
    for query in queries:
        emb, source = await approx_cache.embed(query)
        results.append({"query": query, "source": source})
    return {"results": results, "stats": approx_cache.stats}

# Example savings:
# "What are your business hours?" → computed
# "What are your business hours"  → approx match (missing ?)
# "What are your Business Hours?" → approx match (case difference)
# "What are your working hours?"  → miss (semantically similar but textually different)
```

**When to use**: Agents that receive paraphrased versions of common questions. Approximate matching captures 70–80% of near-duplicate queries without semantic search.

---

## Solution 5: Pre-computed Embedding Index — Load at Startup, Query at Runtime

For a fixed or slowly-changing document corpus, pre-compute all embeddings at startup and store them in memory for instant lookup.

```python
import asyncio
import json
import pickle
from pathlib import Path
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

class PrecomputedEmbeddingIndex:
    """
    Embeddings computed once (at startup or build time) and stored in memory.
    Query time: O(n) dot products — no API calls.
    """

    def __init__(self):
        self._documents: list[str] = []
        self._embeddings: list[list[float]] = []
        self._loaded = False

    async def build(self, documents: list[str], model: str = "text-embedding-3-small") -> None:
        """Build the index from a document list. Called once at startup."""
        self._documents = documents
        self._embeddings = []

        # Embed all documents in parallel
        async def embed_one(text: str) -> list[float]:
            await asyncio.sleep(0.05)  # simulate embedding API call
            return [hash(f"{text}{i}") % 100 / 100.0 for i in range(8)]

        self._embeddings = await asyncio.gather(*[embed_one(doc) for doc in documents])
        self._loaded = True
        print(f"[index] Built embedding index: {len(documents)} documents")

    def save(self, path: str) -> None:
        """Persist index to disk so it survives restarts."""
        data = {"documents": self._documents, "embeddings": self._embeddings}
        Path(path).write_bytes(pickle.dumps(data))

    def load(self, path: str) -> bool:
        """Load persisted index from disk. Returns True on success."""
        p = Path(path)
        if not p.exists():
            return False
        data = pickle.loads(p.read_bytes())
        self._documents = data["documents"]
        self._embeddings = data["embeddings"]
        self._loaded = True
        return True

    def search(self, query_embedding: list[float], top_k: int = 5) -> list[tuple[str, float]]:
        """Return top-k documents by cosine similarity. No API calls."""
        if not self._loaded:
            raise RuntimeError("Index not built or loaded")

        def dot(a, b): return sum(x * y for x, y in zip(a, b))

        scores = [(doc, dot(query_embedding, emb)) for doc, emb in zip(self._documents, self._embeddings)]
        return sorted(scores, key=lambda x: -x[1])[:top_k]

index = PrecomputedEmbeddingIndex()

async def startup():
    """Run at agent startup — build index once."""
    corpus = [
        "We are open Monday through Friday, 9 AM to 5 PM.",
        "Returns are accepted within 30 days with a receipt.",
        "Contact support at support@example.com or 1-800-EXAMPLE.",
        "Our premium plan includes unlimited storage and priority support.",
    ]

    if not index.load("/tmp/embedding_index.pkl"):
        await index.build(corpus)
        index.save("/tmp/embedding_index.pkl")

async def indexed_rag_agent(user_query: str) -> str:
    # Embed query (one API call); search index (no API call)
    await asyncio.sleep(0.05)  # simulate query embedding
    query_emb = [hash(f"{user_query}{i}") % 100 / 100.0 for i in range(8)]

    results = index.search(query_emb, top_k=2)
    context = "\n".join(f"- {doc}" for doc, score in results)

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"Context:\n{context}\n\n{user_query}"}],
    )
    return resp.content[0].text
```

**When to use**: Agents with a static FAQ, knowledge base, or product catalog. Pre-computing saves embedding API costs entirely for the corpus — only query embeddings are computed at runtime.

---

## Solution 6: Tiered Embedding Cache — L1 (In-Process) + L2 (Redis) Across Instances

Share the embedding cache across multiple agent instances using Redis as an L2 cache, so each instance benefits from embeddings computed by all others.

```python
import asyncio
import hashlib
import json
import time
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

class TieredEmbeddingCache:
    """
    Two-level embedding cache:
    L1: in-process dict (nanosecond lookup)
    L2: Redis (millisecond lookup, shared across all agent instances)
    """

    def __init__(self, redis_client=None, l1_max: int = 1000, l2_ttl: int = 86400):
        self._l1: dict[str, list[float]] = {}
        self._l1_max = l1_max
        self._redis = redis_client  # aioredis.Redis instance
        self._l2_ttl = l2_ttl
        self._l1_hits = 0
        self._l2_hits = 0
        self._misses = 0

    def _key(self, text: str, model: str) -> str:
        return f"emb:{hashlib.sha256(f'{model}:{text}'.encode()).hexdigest()}"

    async def get(self, text: str, model: str) -> list[float] | None:
        key = self._key(text, model)

        # L1 check
        if key in self._l1:
            self._l1_hits += 1
            return self._l1[key]

        # L2 check (Redis)
        if self._redis is not None:
            try:
                raw = await self._redis.get(key)
                if raw:
                    embedding = json.loads(raw)
                    self._l1[key] = embedding  # promote to L1
                    self._l2_hits += 1
                    return embedding
            except Exception:
                pass

        self._misses += 1
        return None

    async def put(self, text: str, model: str, embedding: list[float]) -> None:
        key = self._key(text, model)

        # Write to L1
        if len(self._l1) >= self._l1_max:
            self._l1.pop(next(iter(self._l1)))  # evict oldest
        self._l1[key] = embedding

        # Write to L2 (Redis)
        if self._redis is not None:
            try:
                await self._redis.set(key, json.dumps(embedding), ex=self._l2_ttl)
            except Exception:
                pass

    async def embed(self, text: str, model: str = "text-embedding-3-small") -> list[float]:
        cached = await self.get(text, model)
        if cached is not None:
            return cached

        # Compute embedding
        await asyncio.sleep(0.05)  # simulate API call
        embedding = [hash(f"{text}{i}") % 100 / 100.0 for i in range(8)]

        await self.put(text, model, embedding)
        return embedding

    @property
    def stats(self) -> dict:
        total = self._l1_hits + self._l2_hits + self._misses
        return {
            "l1_hits": self._l1_hits,
            "l2_hits": self._l2_hits,
            "misses": self._misses,
            "total_hit_rate": round((self._l1_hits + self._l2_hits) / total, 3) if total else 0.0,
            "l1_size": len(self._l1),
        }

# Usage (without Redis for single-instance):
tiered_cache = TieredEmbeddingCache(redis_client=None, l1_max=5000)

async def agent_rag(query: str, corpus: list[str]) -> str:
    query_emb, doc_embs = await asyncio.gather(
        tiered_cache.embed(query),
        asyncio.gather(*[tiered_cache.embed(doc) for doc in corpus]),
    )
    def dot(a, b): return sum(x * y for x, y in zip(a, b))
    top = max(zip(corpus, doc_embs), key=lambda x: dot(query_emb, x[1]))

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"Context: {top[0]}\n\n{query}"}],
    )
    return resp.content[0].text
```

**When to use**: Multi-instance agent deployments (Kubernetes, load-balanced servers). The Redis L2 cache amortizes embedding costs across the entire fleet — an embedding computed by instance A is immediately available to instances B, C, and D.

---

## Comparison

| Solution | Scope | API Calls Saved | Latency | Infrastructure | Best For |
|---|---|---|---|---|---|
| Content-addressed cache | Process | High (exact matches) | ~0ms | None | Single-instance agents |
| Request coalescing | Request | High (concurrent) | ~0ms | None | High-concurrency agents |
| Session-scoped pool | Session | High (corpus reuse) | ~0ms | None | RAG agents with fixed corpus |
| Approximate match | Process | Medium (near-dupes) | ~1ms | None | Agents with paraphrased queries |
| Pre-computed index | Startup | 100% (corpus) | ~0ms | Disk | Static knowledge bases |
| Tiered L1+L2 cache | Fleet | Very high | 0–5ms | Redis | Multi-instance deployments |

**Rule of thumb**: Always add a content-addressed embedding cache (Solution 1) — it costs nothing and is immediately effective. Add pre-computation (Solution 5) for any fixed corpus. Add Redis L2 (Solution 6) when running more than one agent instance.
