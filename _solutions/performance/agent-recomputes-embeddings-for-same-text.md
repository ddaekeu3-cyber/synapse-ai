---
layout: solution
title: "Agent Recomputes Embeddings for the Same Text — No Embedding Cache"
category: performance
description: "Agent embeds the same 10,000 product descriptions on every startup. Or the same user query gets embedded 5 times across retries. Embedding API calls cost money and take time. Computing the same embedding twice wastes both."
tags: [performance, embeddings, cache, vector, deduplication, redis, cost, rag, content-addressed]
---

# Agent Recomputes Embeddings for the Same Text — No Embedding Cache

## Symptom
- Agent startup takes 45 seconds — all spent calling the embedding API for the same documents
- Same user query embedded 3 times across retries — identical API calls, identical results
- Embedding API cost dominates the bill despite no change in underlying documents
- RAG pipeline re-embeds the entire knowledge base on every deploy
- Duplicate documents in the knowledge base are embedded separately — wasted cost
- Agent embeds tool descriptions, system prompt snippets, or fixed examples on every call

## Root Cause
Embedding computation is deterministic — the same text always produces the same vector. But agents that don't cache embeddings recompute them every time: on every startup, every retry, every parallel call. Embedding costs accumulate linearly with call count. The fix is content-addressed caching: use a hash of the text as the cache key, and return the cached vector whenever the text has been seen before.

## Fix

### Option 1: Content-addressed embedding cache (SQLite)

```python
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Optional
import anthropic

def text_hash(text: str) -> str:
    """Stable hash of text — same text always produces same hash"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]

class EmbeddingCache:
    """
    SQLite-backed embedding cache — content-addressed by text hash.
    Survives restarts. Zero cost for repeated texts.
    """

    def __init__(
        self,
        db_path: str = "/data/embeddings.db",
        model: str = "voyage-3"  # or your embedding model
    ):
        self.model = model
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._hits = 0
        self._misses = 0

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS embeddings (
                    text_hash TEXT PRIMARY KEY,
                    model TEXT NOT NULL,
                    embedding TEXT NOT NULL,  -- JSON array
                    text_length INTEGER,
                    created_at REAL DEFAULT (unixepoch())
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_model ON embeddings(model)")
            conn.commit()

    def get(self, text: str) -> Optional[list[float]]:
        """Retrieve cached embedding or None if not cached"""
        key = text_hash(text)
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT embedding FROM embeddings WHERE text_hash=? AND model=?",
                (key, self.model)
            ).fetchone()
        if row:
            self._hits += 1
            return json.loads(row[0])
        self._misses += 1
        return None

    def set(self, text: str, embedding: list[float]):
        """Store embedding in cache"""
        key = text_hash(text)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO embeddings (text_hash, model, embedding, text_length)
                VALUES (?, ?, ?, ?)
            """, (key, self.model, json.dumps(embedding), len(text)))
            conn.commit()

    def get_or_compute(self, text: str, compute_fn) -> list[float]:
        """Get from cache or compute and store"""
        cached = self.get(text)
        if cached is not None:
            return cached
        embedding = compute_fn(text)
        self.set(text, embedding)
        return embedding

    @property
    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": f"{self._hits/total*100:.0f}%" if total > 0 else "0%",
            "cache_size": self._get_cache_size()
        }

    def _get_cache_size(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM embeddings WHERE model=?", (self.model,)).fetchone()[0]

embedding_cache = EmbeddingCache(model="voyage-3")

# Usage — never pays twice for the same text:
def get_embedding(text: str) -> list[float]:
    return embedding_cache.get_or_compute(text, compute_embedding_via_api)
```

### Option 2: Batch embedding with deduplication

```python
import hashlib
from collections import defaultdict

def embed_texts_deduplicated(
    texts: list[str],
    cache: EmbeddingCache,
    embed_batch_fn,
    batch_size: int = 96
) -> list[list[float]]:
    """
    Embed a list of texts with:
    1. Cache lookup for already-seen texts
    2. Deduplication — identical texts embedded once even if listed multiple times
    3. Batching — remaining texts sent in batches
    Returns embeddings in same order as input texts.
    """
    # Step 1: check cache and identify unique uncached texts
    results: dict[str, list[float]] = {}  # hash → embedding
    uncached_hashes: list[str] = []
    hash_to_text: dict[str, str] = {}

    for text in texts:
        h = text_hash(text)
        if h in results:
            continue  # Already resolved (duplicate in input)

        cached = cache.get(text)
        if cached is not None:
            results[h] = cached  # Cache hit
        else:
            uncached_hashes.append(h)
            hash_to_text[h] = text

    # Step 2: Deduplicate uncached texts
    unique_uncached = list(dict.fromkeys(uncached_hashes))  # Preserve order, remove dupes
    unique_texts = [hash_to_text[h] for h in unique_uncached]

    print(
        f"Embedding: {len(texts)} inputs → "
        f"{len(unique_uncached)} unique uncached (deduped from {len(uncached_hashes)}, "
        f"{len(texts) - len(uncached_hashes)} cache hits)"
    )

    # Step 3: Batch-embed uncached texts
    for i in range(0, len(unique_texts), batch_size):
        batch = unique_texts[i:i + batch_size]
        batch_embeddings = embed_batch_fn(batch)
        for text, embedding in zip(batch, batch_embeddings):
            h = text_hash(text)
            results[h] = embedding
            cache.set(text, embedding)  # Store in cache

    # Step 4: Return in original order
    return [results[text_hash(text)] for text in texts]
```

### Option 3: Redis embedding cache for distributed agents

```python
import redis
import json
import hashlib
import os
from typing import Optional

class RedisEmbeddingCache:
    """
    Redis-backed embedding cache — shared across multiple agent instances.
    Content-addressed: same text → same key → same embedding everywhere.
    """

    def __init__(
        self,
        model: str = "voyage-3",
        ttl_days: int = 30,
        redis_url: str = None
    ):
        self.model = model
        self.ttl = ttl_days * 86400
        self.r = redis.Redis.from_url(
            redis_url or os.environ.get("REDIS_URL", "redis://localhost:6379"),
            decode_responses=True
        )
        self._prefix = f"emb:{model}:"

    def _key(self, text: str) -> str:
        return self._prefix + hashlib.sha256(text.encode()).hexdigest()[:32]

    def get(self, text: str) -> Optional[list[float]]:
        raw = self.r.get(self._key(text))
        if raw:
            return json.loads(raw)
        return None

    def set(self, text: str, embedding: list[float]):
        key = self._key(text)
        self.r.setex(key, self.ttl, json.dumps(embedding))

    def mget(self, texts: list[str]) -> list[Optional[list[float]]]:
        """Batch cache lookup — single round trip"""
        keys = [self._key(t) for t in texts]
        raw_values = self.r.mget(keys)
        return [json.loads(v) if v else None for v in raw_values]

    def mset(self, texts: list[str], embeddings: list[list[float]]):
        """Batch cache store — single round trip"""
        pipe = self.r.pipeline()
        for text, embedding in zip(texts, embeddings):
            pipe.setex(self._key(text), self.ttl, json.dumps(embedding))
        pipe.execute()

async def embed_with_redis_cache(
    texts: list[str],
    cache: RedisEmbeddingCache,
    embed_api_fn
) -> list[list[float]]:
    """
    Batch embedding with Redis cache — O(1) round trips for cache lookup.
    """
    # Batch cache check
    cached = cache.mget(texts)
    results = [None] * len(texts)
    uncached_indices = []
    uncached_texts = []

    for i, (text, embedding) in enumerate(zip(texts, cached)):
        if embedding is not None:
            results[i] = embedding
        else:
            uncached_indices.append(i)
            uncached_texts.append(text)

    print(f"Cache: {len(texts) - len(uncached_texts)} hits, {len(uncached_texts)} misses")

    if uncached_texts:
        new_embeddings = await embed_api_fn(uncached_texts)
        cache.mset(uncached_texts, new_embeddings)  # Store all at once
        for i, embedding in zip(uncached_indices, new_embeddings):
            results[i] = embedding

    return results
```

### Option 4: Startup document embedding with change detection

```python
import hashlib
import json
from pathlib import Path

class DocumentEmbeddingIndex:
    """
    Embeds documents once and caches permanently.
    Re-embeds ONLY documents that have changed since last run.
    Saves 95%+ of embedding API calls on restart.
    """

    def __init__(
        self,
        index_path: str = "/data/doc_embeddings.json",
        embed_fn = None
    ):
        self.index_path = Path(index_path)
        self.embed_fn = embed_fn
        self._index: dict[str, dict] = self._load()

    def _load(self) -> dict:
        if self.index_path.exists():
            data = json.loads(self.index_path.read_text())
            print(f"Loaded {len(data)} cached document embeddings")
            return data
        return {}

    def _save(self):
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._index))
        tmp.replace(self.index_path)

    def _doc_hash(self, content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    async def index_documents(self, documents: list[dict]) -> dict:
        """
        Index documents — only re-embed those that changed.
        documents: [{"id": "doc1", "content": "...text..."}, ...]
        """
        to_embed = []
        unchanged = 0

        for doc in documents:
            doc_id = doc["id"]
            content_hash = self._doc_hash(doc["content"])

            if doc_id in self._index:
                cached = self._index[doc_id]
                if cached.get("content_hash") == content_hash:
                    unchanged += 1
                    continue  # Document unchanged — skip re-embedding

            to_embed.append(doc)

        print(
            f"Document index: {unchanged} unchanged (cached), "
            f"{len(to_embed)} need embedding"
        )

        if to_embed:
            # Batch embed only changed documents
            texts = [doc["content"] for doc in to_embed]
            embeddings = await self.embed_fn(texts)

            for doc, embedding in zip(to_embed, embeddings):
                self._index[doc["id"]] = {
                    "embedding": embedding,
                    "content_hash": self._doc_hash(doc["content"]),
                    "doc_id": doc["id"]
                }

            self._save()
            print(f"Indexed {len(to_embed)} new/changed documents")

        return {
            doc_id: entry["embedding"]
            for doc_id, entry in self._index.items()
        }

doc_index = DocumentEmbeddingIndex()
```

### Option 5: In-process LRU cache for hot embeddings

```python
from functools import lru_cache
import hashlib

# For embeddings computed during agent's lifetime (not persisted across restarts)
# Use lru_cache for hot, frequently accessed embeddings

_embedding_lru: dict[str, list[float]] = {}
_lru_order: list[str] = []
_LRU_MAX = 1000

def lru_get_embedding(text: str, embed_fn) -> list[float]:
    """
    In-process LRU cache for embeddings.
    Fast (no I/O), ephemeral (clears on restart).
    Use alongside persistent cache for best of both.
    """
    key = hashlib.sha256(text.encode()).hexdigest()[:16]

    if key in _embedding_lru:
        # Move to front (most recently used)
        _lru_order.remove(key)
        _lru_order.append(key)
        return _embedding_lru[key]

    # Cache miss — compute
    embedding = embed_fn(text)

    # Evict oldest if at capacity
    if len(_embedding_lru) >= _LRU_MAX:
        oldest = _lru_order.pop(0)
        del _embedding_lru[oldest]

    _embedding_lru[key] = embedding
    _lru_order.append(key)
    return embedding

# Two-tier caching:
def get_embedding_two_tier(text: str, persistent_cache: EmbeddingCache, api_fn) -> list[float]:
    """
    Tier 1: in-process LRU (zero I/O, ephemeral)
    Tier 2: SQLite/Redis (persistent across restarts)
    Tier 3: API call (only when both miss)
    """
    key = hashlib.sha256(text.encode()).hexdigest()[:16]

    # Tier 1: LRU
    if key in _embedding_lru:
        return _embedding_lru[key]

    # Tier 2: Persistent cache
    cached = persistent_cache.get(text)
    if cached is not None:
        # Warm LRU
        _embedding_lru[key] = cached
        _lru_order.append(key)
        return cached

    # Tier 3: API call
    embedding = api_fn(text)
    persistent_cache.set(text, embedding)
    _embedding_lru[key] = embedding
    _lru_order.append(key)
    return embedding
```

### Option 6: Cache warming at startup

```python
import asyncio

async def warm_embedding_cache(
    fixed_texts: list[str],
    cache: EmbeddingCache,
    embed_batch_fn,
    batch_size: int = 32
) -> dict:
    """
    Pre-warm cache with known fixed texts at agent startup.
    Call this before serving traffic — no cold misses during runtime.
    """
    uncached = [t for t in fixed_texts if cache.get(t) is None]

    if not uncached:
        print(f"Cache already warm: all {len(fixed_texts)} fixed texts cached")
        return {"warmed": 0, "already_cached": len(fixed_texts)}

    print(f"Warming cache: {len(uncached)} texts to embed ({len(fixed_texts)-len(uncached)} already cached)")

    warmed = 0
    for i in range(0, len(uncached), batch_size):
        batch = uncached[i:i + batch_size]
        embeddings = await embed_batch_fn(batch)
        for text, embedding in zip(batch, embeddings):
            cache.set(text, embedding)
        warmed += len(batch)
        print(f"Cache warming: {warmed}/{len(uncached)}")

    print(f"Cache warm complete: {warmed} new texts embedded")
    return {"warmed": warmed, "already_cached": len(fixed_texts) - len(uncached)}

# At agent startup:
FIXED_TEXTS = [
    "Search for information about...",
    "Summarize the following...",
    # Tool descriptions, example queries, etc.
]

await warm_embedding_cache(FIXED_TEXTS, embedding_cache, embed_batch_fn)
```

## Cache Strategy Comparison

| Strategy | Hit Latency | Persistence | Multi-instance | Best For |
|---|---|---|---|---|
| In-process LRU | < 1ms | No | No | Hot texts, same session |
| SQLite cache | 1–5ms | Yes | Single-writer | Single-instance agent |
| Redis cache | 1–3ms | Yes | Yes | Multi-instance agents |
| Two-tier (LRU + SQLite) | < 1ms hit, 2ms warm | Yes | Single | Best of both |

## Embedding Cost Savings Example

| Scenario | Without Cache | With Cache (90% hit rate) |
|---|---|---|
| 10,000 docs, daily re-embed | 10,000 calls/day | ~1,000 calls/day |
| 5 retries × same query | 5 calls | 1 call |
| 100 duplicate docs in KB | 100 calls | 1 call |
| Hourly startup re-embed | 10,000 calls/hour | 0 calls (unchanged) |

## Expected Token Savings
Re-embedding 10,000 docs on each restart × 10 restarts/day: 100,000 embedding calls
Cached: 0 repeated embedding calls — 100% saved on unchanged documents

## Environment
- Any agent using RAG, semantic search, or vector similarity; critical for agents with large knowledge bases, high-volume query processing, or embedding-dependent tools
- Source: direct experience; absent embedding caches are the most common cause of inflated embedding API costs in production RAG agents
