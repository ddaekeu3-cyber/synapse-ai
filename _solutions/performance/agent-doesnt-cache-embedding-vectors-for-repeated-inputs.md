---
layout: solution
title: "Agent Doesn't Cache Embedding Vectors for Repeated Inputs"
category: performance
description: "Agent recomputes embeddings for the same text on every call, wasting API quota and adding latency to retrieval-heavy workflows."
tags: [performance, embeddings, caching, vector-search, retrieval, cost]
---

## Symptom

Agent re-embeds the same text repeatedly across calls:

```python
# System prompt, tool descriptions, or frequent queries re-embedded every turn
async def search_memory(query: str) -> list[str]:
    # This query "what did the user say about Python?" appears 50 times per session
    embedding = await embed(query)  # API call every time — same query, same vector
    return vector_search(embedding, memories)

# Also: document chunks re-embedded on every page load
for doc_chunk in document_chunks:  # 500 chunks
    vec = embed(doc_chunk)         # 500 embedding API calls — all identical to yesterday's
    store(doc_chunk, vec)

# Embedding API cost: $0.00002/1K tokens (voyage-3-lite)
# 500 chunks × 200 tokens = 100K tokens = $0.002 per unnecessary re-embed
# At 1,000 re-embeds/day = $2/day wasted on duplicate computation
```

For retrieval-augmented agents, embeddings are computed far more frequently than they change. Re-embedding is pure waste.

## Root Cause

Embedding functions are called as if they are cheap stateless operations. Without a cache, each call dispatches an API request even when the input is byte-for-byte identical to a previous call. Document embeddings rarely change; query embeddings for common queries repeat frequently. The cost compounds at scale.

## Fix

---

### Option 1: In-Memory LRU Cache for Session-Scoped Embeddings

Use `functools.lru_cache` or a manual LRU for embeddings within a single session. Zero dependencies; works immediately.

```python
import hashlib
from functools import lru_cache
from collections import OrderedDict
import voyageai  # pip install voyageai

vo = voyageai.Client()

@lru_cache(maxsize=512)  # Cache up to 512 unique texts in memory
def embed_cached(text: str) -> tuple[float, ...]:
    """Embed text, returning a tuple (hashable, so lru_cache works)."""
    result = vo.embed([text], model="voyage-3-lite")
    return tuple(result.embeddings[0])

# Usage: identical calls hit cache
import time

texts = ["What did the user say about Python?"] * 100  # same query 100 times

start = time.perf_counter()
for text in texts:
    vec = embed_cached(text)
elapsed = time.perf_counter() - start

info = embed_cached.cache_info()
print(f"100 calls in {elapsed:.3f}s: {info.hits} hits, {info.misses} misses, {info.currsize} cached")
# → 100 calls in 0.001s: 99 hits, 1 miss, 1 cached

# For numpy arrays (not hashable): wrap in a class
import numpy as np

class EmbeddingCache:
    def __init__(self, maxsize: int = 512):
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self.maxsize = maxsize
        self.hits = 0
        self.misses = 0

    def _key(self, text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()

    def get_or_embed(self, text: str) -> np.ndarray:
        key = self._key(text)
        if key in self._cache:
            self._cache.move_to_end(key)
            self.hits += 1
            return self._cache[key]

        # Cache miss: compute
        result = vo.embed([text], model="voyage-3-lite")
        vec = np.array(result.embeddings[0], dtype=np.float32)
        self._cache[key] = vec
        self._cache.move_to_end(key)
        if len(self._cache) > self.maxsize:
            self._cache.popitem(last=False)
        self.misses += 1
        return vec

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate_pct": 100 * self.hits / max(1, total),
            "cached_entries": len(self._cache),
        }

cache = EmbeddingCache(maxsize=1024)

# Embed 200 queries (50 unique repeated 4 times)
queries = [f"query about topic {i % 50}" for i in range(200)]
for q in queries:
    cache.get_or_embed(q)

print(cache.stats())
# → {'hits': 150, 'misses': 50, 'hit_rate_pct': 75.0, 'cached_entries': 50}
```

**Expected Token Savings:** For 200 queries with 75% hit rate: 150 avoided API calls × 20 tokens/query = 3,000 tokens saved. Latency: 150 cache hits at ~0.001ms vs 150 API calls at ~100ms = 15 seconds saved per session.
**Environment:** In-memory cache is lost on process restart. Suitable for session-scoped caching. For persistence across restarts, use Options 2-4.

---

### Option 2: SQLite Embedding Cache — Persistent Across Restarts

Store embeddings in a SQLite database keyed by content hash. Survives process restarts; zero external dependencies beyond sqlite3.

```python
import hashlib
import json
import sqlite3
import time
from pathlib import Path
import numpy as np
import voyageai

CACHE_DB = Path(".embedding_cache.db")
vo = voyageai.Client()

class SQLiteEmbeddingCache:
    def __init__(self, db_path: Path = CACHE_DB, model: str = "voyage-3-lite"):
        self.model = model
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._setup()
        self.hits = 0
        self.misses = 0

    def _setup(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS embeddings (
                content_hash TEXT PRIMARY KEY,
                model TEXT NOT NULL,
                embedding BLOB NOT NULL,
                text_preview TEXT,
                created_at REAL,
                access_count INTEGER DEFAULT 0
            )
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_model ON embeddings(model)")
        self.conn.commit()

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self.model}:{text}".encode()).hexdigest()

    def get(self, text: str) -> np.ndarray | None:
        key = self._key(text)
        row = self.conn.execute(
            "SELECT embedding FROM embeddings WHERE content_hash = ? AND model = ?",
            (key, self.model),
        ).fetchone()
        if row:
            self.conn.execute(
                "UPDATE embeddings SET access_count = access_count + 1 WHERE content_hash = ?",
                (key,),
            )
            self.conn.commit()
            self.hits += 1
            return np.frombuffer(row[0], dtype=np.float32)
        return None

    def put(self, text: str, embedding: np.ndarray) -> None:
        key = self._key(text)
        self.conn.execute(
            """INSERT OR REPLACE INTO embeddings
               (content_hash, model, embedding, text_preview, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (key, self.model, embedding.tobytes(), text[:100], time.time()),
        )
        self.conn.commit()
        self.misses += 1

    def get_or_embed(self, text: str) -> np.ndarray:
        cached = self.get(text)
        if cached is not None:
            return cached
        result = vo.embed([text], model=self.model)
        vec = np.array(result.embeddings[0], dtype=np.float32)
        self.put(text, vec)
        return vec

    def get_or_embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        """Batch embed — only call API for cache misses."""
        results: list[np.ndarray | None] = [None] * len(texts)
        miss_indices: list[int] = []

        for i, text in enumerate(texts):
            cached = self.get(text)
            if cached is not None:
                results[i] = cached
            else:
                miss_indices.append(i)

        if miss_indices:
            miss_texts = [texts[i] for i in miss_indices]
            api_result = vo.embed(miss_texts, model=self.model)
            for j, idx in enumerate(miss_indices):
                vec = np.array(api_result.embeddings[j], dtype=np.float32)
                self.put(texts[idx], vec)
                results[idx] = vec

        print(f"Batch: {len(texts) - len(miss_indices)} hits, {len(miss_indices)} API calls")
        return results  # type: ignore[return-value]

    def prune_old(self, older_than_days: int = 30) -> int:
        cutoff = time.time() - older_than_days * 86400
        cursor = self.conn.execute(
            "DELETE FROM embeddings WHERE created_at < ? AND access_count < 2",
            (cutoff,),
        )
        self.conn.commit()
        return cursor.rowcount

cache = SQLiteEmbeddingCache()

# First run: all misses (API calls)
docs = [f"Document about topic {i}" for i in range(20)]
vecs1 = cache.get_or_embed_batch(docs)
print(f"First run stats: hits={cache.hits}, misses={cache.misses}")

# Second run (after restart): all hits
cache2 = SQLiteEmbeddingCache()  # new instance, same DB
vecs2 = cache2.get_or_embed_batch(docs)
print(f"Second run stats: hits={cache2.hits}, misses={cache2.misses}")
```

**Expected Token Savings:** Document embeddings (stable content) achieve ~100% cache hit rate after first run. For 1,000 document chunks re-embedded daily without cache: 1,000 × 200 tokens × $0.00002/1K = $4/day. With SQLite cache: $0/day after first run.
**Environment:** SQLite handles concurrent reads safely; concurrent writes need connection-level locking. For multi-process/multi-instance deployments, use Redis (Option 4) instead.

---

### Option 3: Content-Addressed Cache with Automatic Invalidation

Hash the content to detect when text changes, automatically invalidating stale embeddings without manual cache management.

```python
import hashlib
import json
import time
from pathlib import Path
import numpy as np
import voyageai

vo = voyageai.Client()
CACHE_FILE = Path(".embed_cache.json")

class ContentAddressedEmbeddingCache:
    """Cache keyed by (model, content_hash). Stale entries auto-invalidate on content change."""

    def __init__(self, model: str = "voyage-3-lite", max_entries: int = 10_000):
        self.model = model
        self.max_entries = max_entries
        self._data: dict[str, dict] = self._load()

    def _load(self) -> dict:
        if CACHE_FILE.exists():
            return json.loads(CACHE_FILE.read_text())
        return {}

    def _save(self) -> None:
        CACHE_FILE.write_text(json.dumps(self._data))

    def _key(self, content: str) -> str:
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:24]
        return f"{self.model}:{content_hash}"

    def embed(self, text: str, source_id: str | None = None) -> np.ndarray:
        """Embed text. If source_id is given, check if content changed since last embed."""
        key = self._key(text)

        if key in self._data:
            entry = self._data[key]
            vec = np.array(entry["embedding"], dtype=np.float32)
            entry["last_accessed"] = time.time()
            return vec

        # Cache miss: embed
        result = vo.embed([text], model=self.model)
        vec = np.array(result.embeddings[0], dtype=np.float32)
        self._data[key] = {
            "embedding": vec.tolist(),
            "source_id": source_id,
            "created_at": time.time(),
            "last_accessed": time.time(),
        }

        # Evict if over limit (LRU by last_accessed)
        if len(self._data) > self.max_entries:
            oldest_key = min(self._data, key=lambda k: self._data[k]["last_accessed"])
            del self._data[oldest_key]

        self._save()
        return vec

    def embed_document(self, doc_id: str, text: str) -> tuple[np.ndarray, bool]:
        """Embed a document. Returns (vector, was_cache_hit)."""
        key = self._key(text)
        was_hit = key in self._data
        vec = self.embed(text, source_id=doc_id)
        return vec, was_hit

    def invalidate_by_source(self, source_id: str) -> int:
        """Remove all embeddings from a specific source (e.g., when a file is updated)."""
        to_remove = [k for k, v in self._data.items() if v.get("source_id") == source_id]
        for k in to_remove:
            del self._data[k]
        if to_remove:
            self._save()
        return len(to_remove)

cache = ContentAddressedEmbeddingCache()

# Embed documents — second call is always a cache hit (same content = same hash)
documents = {
    "readme.md": "This project implements a vector search system using FAISS.",
    "design.md": "The architecture uses a three-tier approach with caching at each layer.",
    "api.md": "The REST API exposes endpoints for search, index, and health check.",
}

for doc_id, content in documents.items():
    vec, hit = cache.embed_document(doc_id, content)
    print(f"{doc_id}: {'HIT' if hit else 'MISS'} ({len(vec)}d vector)")

# Simulate file update: invalidate and re-embed
cache.invalidate_by_source("readme.md")
vec, hit = cache.embed_document("readme.md", "Updated: This project now uses Qdrant for vector search.")
print(f"After update: {'HIT' if hit else 'MISS'}")
```

**Expected Token Savings:** Content-addressed cache achieves 100% hit rate when content is stable (static docs, fixed prompts). Change detection ensures stale embeddings are never served. For a 500-page document corpus re-indexed daily: saves 499/500 embedding API calls after first index.
**Environment:** JSON-based cache is simple but not suitable for vectors >1M entries (file gets large). Migrate to SQLite (Option 2) or Redis (Option 4) at scale. File-based is fine for single-instance deployments up to ~50K entries.

---

### Option 4: Redis Embedding Cache — Shared Across Multiple Agent Instances

Store embeddings in Redis with TTL-based expiry. Shared across all agent processes and instances; supports horizontal scaling.

```python
import hashlib
import struct
import time
import numpy as np
import redis  # pip install redis
import voyageai

vo = voyageai.Client()
r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=False)

EMBED_DIM = 512  # voyage-3-lite output dimension
DEFAULT_TTL = 7 * 24 * 3600  # 7 days

def embed_key(text: str, model: str = "voyage-3-lite") -> str:
    h = hashlib.sha256(f"{model}:{text}".encode()).hexdigest()[:24]
    return f"embed:v1:{h}"

def vec_to_bytes(vec: np.ndarray) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec.tolist())

def bytes_to_vec(data: bytes) -> np.ndarray:
    n = len(data) // 4
    return np.array(struct.unpack(f"{n}f", data), dtype=np.float32)

class RedisEmbeddingCache:
    def __init__(self, ttl_seconds: int = DEFAULT_TTL, model: str = "voyage-3-lite"):
        self.ttl = ttl_seconds
        self.model = model
        self.hits = 0
        self.misses = 0

    def get_or_embed(self, text: str) -> np.ndarray:
        key = embed_key(text, self.model)
        cached = r.get(key)
        if cached:
            r.expire(key, self.ttl)  # Refresh TTL on access
            self.hits += 1
            return bytes_to_vec(cached)

        result = vo.embed([text], model=self.model)
        vec = np.array(result.embeddings[0], dtype=np.float32)
        r.setex(key, self.ttl, vec_to_bytes(vec))
        self.misses += 1
        return vec

    def get_or_embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        """Pipeline Redis gets; only embed cache misses."""
        keys = [embed_key(t, self.model) for t in texts]

        # Batch GET with pipeline
        pipe = r.pipeline()
        for key in keys:
            pipe.get(key)
        cached_values = pipe.execute()

        results: list[np.ndarray | None] = [None] * len(texts)
        miss_indices: list[int] = []

        for i, (text, cached) in enumerate(zip(texts, cached_values)):
            if cached:
                results[i] = bytes_to_vec(cached)
                r.expire(keys[i], self.ttl)
                self.hits += 1
            else:
                miss_indices.append(i)

        if miss_indices:
            miss_texts = [texts[i] for i in miss_indices]
            api_result = vo.embed(miss_texts, model=self.model)
            pipe = r.pipeline()
            for j, idx in enumerate(miss_indices):
                vec = np.array(api_result.embeddings[j], dtype=np.float32)
                results[idx] = vec
                pipe.setex(keys[idx], self.ttl, vec_to_bytes(vec))
                self.misses += 1
            pipe.execute()

        return results  # type: ignore[return-value]

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate_pct": round(100 * self.hits / max(1, total), 1),
        }

try:
    cache = RedisEmbeddingCache(ttl_seconds=86400)
    texts = [f"Chunk {i} of the product documentation" for i in range(100)]
    vecs = cache.get_or_embed_batch(texts)
    print(f"First batch: {cache.stats()}")

    # Second batch (same texts) — all from cache
    vecs2 = cache.get_or_embed_batch(texts)
    print(f"Second batch: {cache.stats()}")
except redis.ConnectionError:
    print("Redis not available — use SQLite cache (Option 2) for local dev")
```

**Expected Token Savings:** Redis cache is shared across all agent instances. For 10 agents each processing 100 queries/hour (50 unique): without cache = 10 × 100 × 50 tokens = 50,000 tokens/hour; with Redis cache = 100 × 50 tokens = 5,000 tokens/hour (90% reduction across all instances).
**Environment:** Requires Redis. TTL should match the expected content staleness — 7 days for document embeddings, 1 hour for dynamic query embeddings. Use `redis.StrictRedis` with connection pool for production.

---

### Option 5: Two-Level Cache — Hot Memory Layer + Cold Disk Layer

Combine in-memory LRU (fast, small) with disk cache (slow, large). Hot queries hit memory; cold queries hit disk; misses hit the API.

```python
import hashlib
import pickle
from collections import OrderedDict
from pathlib import Path
import numpy as np
import voyageai

vo = voyageai.Client()
DISK_CACHE_DIR = Path(".embed_disk_cache")
DISK_CACHE_DIR.mkdir(exist_ok=True)

class TwoLevelEmbeddingCache:
    def __init__(self, hot_capacity: int = 128, model: str = "voyage-3-lite"):
        self.model = model
        self.hot: OrderedDict[str, np.ndarray] = OrderedDict()
        self.hot_capacity = hot_capacity
        self.stats = {"hot_hits": 0, "cold_hits": 0, "api_calls": 0}

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self.model}:{text}".encode()).hexdigest()

    def _disk_path(self, key: str) -> Path:
        return DISK_CACHE_DIR / f"{key[:2]}" / f"{key}.npy"

    def _disk_get(self, key: str) -> np.ndarray | None:
        path = self._disk_path(key)
        if path.exists():
            return np.load(str(path))
        return None

    def _disk_put(self, key: str, vec: np.ndarray) -> None:
        path = self._disk_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        np.save(str(path), vec)

    def _hot_put(self, key: str, vec: np.ndarray) -> None:
        self.hot[key] = vec
        self.hot.move_to_end(key)
        if len(self.hot) > self.hot_capacity:
            self.hot.popitem(last=False)

    def get_or_embed(self, text: str) -> np.ndarray:
        key = self._key(text)

        # Level 1: hot memory
        if key in self.hot:
            self.hot.move_to_end(key)
            self.stats["hot_hits"] += 1
            return self.hot[key]

        # Level 2: cold disk
        disk_vec = self._disk_get(key)
        if disk_vec is not None:
            self._hot_put(key, disk_vec)  # promote to hot
            self.stats["cold_hits"] += 1
            return disk_vec

        # Level 3: API
        result = vo.embed([text], model=self.model)
        vec = np.array(result.embeddings[0], dtype=np.float32)
        self._hot_put(key, vec)
        self._disk_put(key, vec)
        self.stats["api_calls"] += 1
        return vec

    def summary(self) -> dict:
        total = sum(self.stats.values())
        return {
            **self.stats,
            "api_rate_pct": round(100 * self.stats["api_calls"] / max(1, total), 1),
        }

# Simulate realistic access pattern: 10 hot queries repeated many times + many unique queries
cache = TwoLevelEmbeddingCache(hot_capacity=10)
hot_queries = [f"Common query {i}" for i in range(10)]
cold_queries = [f"Rare query {i}" for i in range(100)]

# First pass: all misses (API calls)
for q in hot_queries + cold_queries:
    cache.get_or_embed(q)
print(f"After first pass: {cache.summary()}")

# Second pass: hot queries hit memory, cold queries hit disk
for _ in range(5):
    for q in hot_queries:
        cache.get_or_embed(q)
for q in cold_queries:
    cache.get_or_embed(q)

print(f"After repeated access: {cache.summary()}")
```

**Expected Token Savings:** Two-level cache achieves near-100% hit rate for repeated queries. Hot tier serves the most frequent 128 queries at <0.1ms; cold tier serves the rest at ~5ms (disk I/O). Only truly new queries hit the API. For a typical session: 90%+ hit rate → 90% fewer embedding API calls.
**Environment:** Disk cache is persistent across restarts. Use `npy` format for fast numpy array I/O. Sub-directory sharding (`key[:2]`) prevents large directory listings. Prune old disk entries with a weekly cleanup job.

---

### Option 6: Semantic Dedup Before Embedding — Skip Near-Duplicates

Before embedding, detect near-duplicate inputs using MinHash or simple n-gram overlap. Route duplicates to the most similar cached embedding instead of computing a new one.

```python
import hashlib
import re
from collections import defaultdict
import numpy as np
import voyageai

vo = voyageai.Client()

def ngrams(text: str, n: int = 3) -> set[str]:
    tokens = re.findall(r"\w+", text.lower())
    return {" ".join(tokens[i:i+n]) for i in range(len(tokens) - n + 1)}

def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)

class SemanticDeduplicatingCache:
    """Cache that detects near-duplicate inputs and reuses existing embeddings."""

    def __init__(self, model: str = "voyage-3-lite", similarity_threshold: float = 0.8):
        self.model = model
        self.threshold = similarity_threshold
        self._cache: dict[str, np.ndarray] = {}        # exact key → vec
        self._ngrams: dict[str, set[str]] = {}         # exact key → ngrams
        self.stats = {"exact_hits": 0, "fuzzy_hits": 0, "api_calls": 0}

    def _key(self, text: str) -> str:
        return hashlib.sha256(text.strip().lower().encode()).hexdigest()[:16]

    def _find_similar(self, text: str) -> np.ndarray | None:
        text_ngrams = ngrams(text)
        best_sim, best_vec = 0.0, None
        for key, cached_ngrams in self._ngrams.items():
            sim = jaccard(text_ngrams, cached_ngrams)
            if sim > best_sim and sim >= self.threshold:
                best_sim = sim
                best_vec = self._cache[key]
        return best_vec

    def get_or_embed(self, text: str) -> np.ndarray:
        key = self._key(text)

        # Exact match
        if key in self._cache:
            self.stats["exact_hits"] += 1
            return self._cache[key]

        # Fuzzy match
        similar = self._find_similar(text)
        if similar is not None:
            self.stats["fuzzy_hits"] += 1
            # Store under new key too for future exact matches
            self._cache[key] = similar
            self._ngrams[key] = ngrams(text)
            return similar

        # API call
        result = vo.embed([text], model=self.model)
        vec = np.array(result.embeddings[0], dtype=np.float32)
        self._cache[key] = vec
        self._ngrams[key] = ngrams(text)
        self.stats["api_calls"] += 1
        return vec

# Comparison table
"""
| Approach | Persistence | Shared? | Throughput | Best For |
|---|---|---|---|---|
| Option 1: LRU in-memory | Session only | No | Fastest | Single-process, session |
| Option 2: SQLite | Persistent | No | Fast | Single-instance |
| Option 3: Content-addressed | Persistent | No | Fast | Static document corpora |
| Option 4: Redis | Persistent | Yes | Fast | Multi-instance production |
| Option 5: Two-level | Persistent | No | Very Fast | Mixed hot/cold workloads |
| Option 6: Semantic dedup | Session only | No | Medium | Varied paraphrase queries |
"""

cache = SemanticDeduplicatingCache(similarity_threshold=0.75)

# Simulate paraphrased queries
query_variations = [
    "What did the user say about Python programming?",
    "What has the user mentioned about Python?",          # ~80% similar
    "What were user comments about Python?",              # ~70% similar
    "What is the capital of France?",                     # unrelated
    "Tell me about the user's Python preferences",        # ~65% similar
]

for q in query_variations:
    vec = cache.get_or_embed(q)
    print(f"'{q[:50]}' → vec norm={np.linalg.norm(vec):.3f}")

print(f"\nStats: {cache.stats}")
```

**Expected Token Savings:** Semantic dedup catches paraphrased queries that exact matching misses. For a query like "What did the user say about Python?" that appears in 20 surface variants across a session: without dedup = 20 API calls; with dedup = 1 API call + 19 fuzzy hits = 95% reduction. Each saved call: ~15 tokens × $0.00002/1K ≈ negligible, but at 10,000 queries/day adds up significantly.
**Environment:** N-gram Jaccard similarity is fast (no API call) but requires tuning the threshold. Lower threshold (0.7) catches more paraphrases but risks false positives. For semantic similarity, replace Jaccard with a local embedding model comparison.
