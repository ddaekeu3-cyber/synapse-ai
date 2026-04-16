---
title: "Agent Doesn't Implement Semantic Response Caching"
description: "Cache LLM responses by semantic similarity rather than exact string match, serving cached answers for paraphrased versions of the same question."
category: token-cost
difficulty: intermediate
tags: [caching, semantic, embeddings, token-cost, latency, deduplication]
---

# Agent Doesn't Implement Semantic Response Caching

## Problem

Exact-string caching misses the majority of reusable responses: "What is asyncio?" and "Can you explain asyncio?" are semantically identical but produce two full API calls. Semantic caching computes vector similarity between queries and returns cached responses when a similar-enough query has been seen before — cutting API costs and latency dramatically for repeated-topic conversations.

---

## Option 1: In-Memory Cosine Similarity Cache

```python
import asyncio
import anthropic
import math
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)

@dataclass
class CacheEntry:
    query: str
    embedding: list[float]
    response: str
    hits: int = 0

class SemanticCache:
    def __init__(self, similarity_threshold: float = 0.92, max_size: int = 500):
        self.entries: list[CacheEntry] = []
        self.threshold = similarity_threshold
        self.max_size = max_size
        self._stats = {"hits": 0, "misses": 0}

    async def _embed(self, text: str) -> list[float]:
        """Use a small model to get embeddings via a lightweight completion."""
        # Approximate embedding using token hash distribution
        # In production, use a real embedding API (e.g. voyage-3, text-embedding-3-small)
        # Here we use a simple character n-gram fingerprint as a stand-in
        import hashlib
        ngrams = [text[i:i+3] for i in range(len(text) - 2)]
        vec = [0.0] * 64
        for ng in ngrams:
            h = int(hashlib.md5(ng.encode()).hexdigest(), 16)
            vec[h % 64] += 1.0
        norm = math.sqrt(sum(x*x for x in vec)) or 1.0
        return [x / norm for x in vec]

    async def get(self, query: str) -> str | None:
        emb = await self._embed(query)
        best_score = 0.0
        best_entry: CacheEntry | None = None
        for entry in self.entries:
            score = cosine_similarity(emb, entry.embedding)
            if score > best_score:
                best_score = score
                best_entry = entry
        if best_entry and best_score >= self.threshold:
            best_entry.hits += 1
            self._stats["hits"] += 1
            print(f"[CACHE HIT] similarity={best_score:.3f} for: {query[:50]}")
            return best_entry.response
        self._stats["misses"] += 1
        return None

    async def put(self, query: str, response: str):
        emb = await self._embed(query)
        if len(self.entries) >= self.max_size:
            # Evict least-used entry
            self.entries.sort(key=lambda e: e.hits)
            self.entries.pop(0)
        self.entries.append(CacheEntry(query=query, embedding=emb, response=response))

    def stats(self) -> dict:
        total = self._stats["hits"] + self._stats["misses"]
        return {**self._stats, "hit_rate": self._stats["hits"] / total if total else 0.0}

cache = SemanticCache(similarity_threshold=0.90)

async def ask(question: str, messages: list[dict]) -> str:
    cached = await cache.get(question)
    if cached:
        return cached
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[*messages, {"role": "user", "content": question}]
    )
    text = resp.content[0].text
    await cache.put(question, text)
    return text

async def main():
    conv: list[dict] = []
    questions = [
        "What is Python's asyncio?",
        "Can you explain Python asyncio to me?",       # semantic duplicate
        "Tell me about the asyncio module in Python.",  # semantic duplicate
        "How does asyncio work?",                       # slightly different
        "What is the GIL?",                            # different topic
    ]
    for q in questions:
        answer = await ask(q, conv)
        print(f"Q: {q}\nA: {answer[:80]}...\n")

    print(f"Stats: {cache.stats()}")

asyncio.run(main())
```

---

## Option 2: LRU Semantic Cache with TTL Expiry

```python
import asyncio
import anthropic
import time
import hashlib
import math
from dataclasses import dataclass, field
from collections import OrderedDict

client = anthropic.AsyncAnthropic()

@dataclass
class TTLEntry:
    query: str
    embedding: list[float]
    response: str
    created_at: float = field(default_factory=time.time)
    ttl_seconds: float = 3600.0  # 1 hour default

    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl_seconds

def simple_embed(text: str, dim: int = 128) -> list[float]:
    """Deterministic character-level embedding."""
    vec = [0.0] * dim
    words = text.lower().split()
    for word in words:
        h = int(hashlib.sha256(word.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
        vec[(h >> 8) % dim] += 0.5
    norm = math.sqrt(sum(x*x for x in vec)) or 1.0
    return [x / norm for x in vec]

def cosine(a: list[float], b: list[float]) -> float:
    return sum(x*y for x,y in zip(a,b))

class LRUSemanticCache:
    def __init__(self, max_size: int = 200, threshold: float = 0.88, default_ttl: float = 3600.0):
        self._cache: OrderedDict[str, TTLEntry] = OrderedDict()
        self.max_size = max_size
        self.threshold = threshold
        self.default_ttl = default_ttl
        self._lock = asyncio.Lock()

    async def lookup(self, query: str) -> str | None:
        emb = simple_embed(query)
        async with self._lock:
            # Sweep for expired entries
            expired = [k for k, v in self._cache.items() if v.is_expired()]
            for k in expired:
                del self._cache[k]

            best_key: str | None = None
            best_score = 0.0
            for key, entry in self._cache.items():
                score = cosine(emb, entry.embedding)
                if score > best_score:
                    best_score = score
                    best_key = key

            if best_key and best_score >= self.threshold:
                # Move to end (most recently used)
                self._cache.move_to_end(best_key)
                print(f"[LRU CACHE] hit={best_score:.3f}")
                return self._cache[best_key].response
        return None

    async def store(self, query: str, response: str, ttl: float | None = None):
        emb = simple_embed(query)
        entry = TTLEntry(
            query=query, embedding=emb, response=response,
            ttl_seconds=ttl or self.default_ttl
        )
        async with self._lock:
            key = hashlib.md5(query.encode()).hexdigest()
            self._cache[key] = entry
            self._cache.move_to_end(key)
            if len(self._cache) > self.max_size:
                self._cache.popitem(last=False)  # evict LRU

lru_cache = LRUSemanticCache(threshold=0.87, default_ttl=1800.0)

async def cached_call(question: str) -> str:
    cached = await lru_cache.lookup(question)
    if cached:
        return cached
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": question}]
    )
    text = resp.content[0].text
    # Short TTL for time-sensitive queries
    ttl = 300.0 if any(w in question.lower() for w in ["today", "current", "latest", "now"]) else 3600.0
    await lru_cache.store(question, text, ttl=ttl)
    return text
```

---

## Option 3: Tiered Cache (Exact → Semantic → Generate)

```python
import asyncio
import anthropic
import hashlib
import math
import time
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

def embed(text: str, dim: int = 64) -> list[float]:
    vec = [0.0] * dim
    for i in range(0, len(text) - 2, 1):
        tri = text[i:i+3].lower()
        h = int(hashlib.md5(tri.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(x*x for x in vec)) or 1.0
    return [x/norm for x in vec]

def cosine(a: list[float], b: list[float]) -> float:
    return sum(x*y for x,y in zip(a,b))

@dataclass
class TieredEntry:
    query: str
    response: str
    embedding: list[float]
    created_at: float = field(default_factory=time.time)

class TieredSemanticCache:
    def __init__(self, semantic_threshold: float = 0.90):
        # Tier 1: exact string match (O(1))
        self._exact: dict[str, TieredEntry] = {}
        # Tier 2: semantic match (O(n))
        self._semantic: list[TieredEntry] = []
        self.threshold = semantic_threshold
        self._stats = {"exact_hits": 0, "semantic_hits": 0, "misses": 0, "api_calls": 0}

    def _normalize(self, text: str) -> str:
        return " ".join(text.lower().strip().split())

    async def get(self, query: str) -> tuple[str | None, str]:
        """Returns (response, tier) where tier is 'exact', 'semantic', or 'miss'."""
        normalized = self._normalize(query)

        # Tier 1: exact
        if normalized in self._exact:
            self._stats["exact_hits"] += 1
            return self._exact[normalized].response, "exact"

        # Tier 2: semantic
        emb = embed(query)
        best_score = 0.0
        best_entry: TieredEntry | None = None
        for entry in self._semantic:
            score = cosine(emb, entry.embedding)
            if score > best_score:
                best_score = score
                best_entry = entry

        if best_entry and best_score >= self.threshold:
            # Promote to exact cache for next time
            self._exact[normalized] = best_entry
            self._stats["semantic_hits"] += 1
            return best_entry.response, f"semantic({best_score:.3f})"

        self._stats["misses"] += 1
        return None, "miss"

    async def put(self, query: str, response: str):
        normalized = self._normalize(query)
        emb = embed(query)
        entry = TieredEntry(query=query, response=response, embedding=emb)
        self._exact[normalized] = entry
        self._semantic.append(entry)
        # Cap semantic tier size
        if len(self._semantic) > 1000:
            self._semantic = self._semantic[-800:]

    def stats(self) -> dict:
        total = sum(self._stats[k] for k in ("exact_hits", "semantic_hits", "misses"))
        return {
            **self._stats,
            "total": total,
            "cache_hit_rate": (self._stats["exact_hits"] + self._stats["semantic_hits"]) / total if total else 0.0
        }

tiered = TieredSemanticCache(semantic_threshold=0.89)

async def ask(question: str) -> str:
    cached, tier = await tiered.get(question)
    if cached:
        print(f"[{tier}] {question[:50]}")
        return cached

    resp = await client.messages.create(
        model="claude-sonnet-4-6", max_tokens=512,
        messages=[{"role": "user", "content": question}]
    )
    text = resp.content[0].text
    tiered._stats["api_calls"] += 1
    await tiered.put(question, text)
    return text
```

---

## Option 4: Async Concurrent Cache with Write-Through

```python
import asyncio
import anthropic
import hashlib
import math
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

def embed(text: str) -> list[float]:
    dim = 96
    vec = [0.0] * dim
    tokens = text.lower().split()
    for i, tok in enumerate(tokens):
        h = int(hashlib.sha256(tok.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0 / (i + 1)  # positional decay
    norm = math.sqrt(sum(x*x for x in vec)) or 1.0
    return [x/norm for x in vec]

def cosine(a: list[float], b: list[float]) -> float:
    return sum(x*y for x,y in zip(a,b))

@dataclass
class AsyncCacheEntry:
    embedding: list[float]
    response: str
    access_count: int = 0

class ConcurrentSemanticCache:
    def __init__(self, threshold: float = 0.91, capacity: int = 300):
        self._store: list[AsyncCacheEntry] = []
        self._threshold = threshold
        self._capacity = capacity
        self._read_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        # Pending writes — coalesce concurrent misses for same query
        self._pending: dict[str, asyncio.Future] = {}

    async def get_or_generate(self, query: str, messages: list[dict]) -> str:
        emb = embed(query)

        # Check cache (read path)
        async with self._read_lock:
            for entry in self._store:
                if cosine(emb, entry.embedding) >= self._threshold:
                    entry.access_count += 1
                    return entry.response

        # Check if another coroutine is already generating this query
        async with self._write_lock:
            key = hashlib.md5(query.encode()).hexdigest()
            if key in self._pending:
                fut = self._pending[key]
            else:
                fut: asyncio.Future = asyncio.get_event_loop().create_future()
                self._pending[key] = fut
                should_generate = True

        if not should_generate:
            return await asyncio.shield(fut)

        # Generate
        try:
            resp = await client.messages.create(
                model="claude-sonnet-4-6", max_tokens=512,
                messages=[*messages, {"role": "user", "content": query}]
            )
            text = resp.content[0].text
        except Exception as e:
            async with self._write_lock:
                self._pending.pop(key, None)
            raise

        # Write to cache
        async with self._write_lock:
            self._pending.pop(key, None)
            if len(self._store) >= self._capacity:
                # Evict least accessed
                self._store.sort(key=lambda e: e.access_count)
                self._store.pop(0)
            self._store.append(AsyncCacheEntry(embedding=emb, response=text))

        if not fut.done():
            fut.set_result(text)
        return text

concurrent_cache = ConcurrentSemanticCache(threshold=0.90)

async def main():
    # Simulate concurrent identical queries
    queries = ["What is machine learning?"] * 5 + ["Explain machine learning"] * 3
    results = await asyncio.gather(*[concurrent_cache.get_or_generate(q, []) for q in queries])
    print(f"All results identical: {len(set(r[:20] for r in results)) == 1}")
    print(f"Cache size: {len(concurrent_cache._store)}")

asyncio.run(main())
```

---

## Option 5: Partitioned Cache by Topic with Per-Partition Thresholds

```python
import asyncio
import anthropic
import hashlib
import math
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

PARTITIONS = {
    "technical": {"threshold": 0.93, "keywords": ["code", "function", "api", "error", "python", "javascript", "sql"]},
    "factual": {"threshold": 0.88, "keywords": ["what is", "who is", "when did", "where is", "capital", "history"]},
    "creative": {"threshold": 0.97, "keywords": ["write", "poem", "story", "creative", "imagine", "compose"]},
    "general": {"threshold": 0.90, "keywords": []},  # fallback
}

def classify(query: str) -> str:
    q_lower = query.lower()
    for partition, config in PARTITIONS.items():
        if partition == "general":
            continue
        if any(kw in q_lower for kw in config["keywords"]):
            return partition
    return "general"

def embed(text: str, dim: int = 80) -> list[float]:
    vec = [0.0] * dim
    for word in text.lower().split():
        h = int(hashlib.md5(word.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(x*x for x in vec)) or 1.0
    return [x/norm for x in vec]

def cosine(a: list[float], b: list[float]) -> float:
    return sum(x*y for x,y in zip(a,b))

@dataclass
class PartitionedCache:
    stores: dict[str, list] = field(default_factory=lambda: {p: [] for p in PARTITIONS})

    async def lookup(self, query: str) -> str | None:
        partition = classify(query)
        threshold = PARTITIONS[partition]["threshold"]
        emb = embed(query)
        for entry_emb, response in self.stores[partition]:
            if cosine(emb, entry_emb) >= threshold:
                print(f"[CACHE:{partition}] hit")
                return response
        return None

    async def store(self, query: str, response: str):
        partition = classify(query)
        emb = embed(query)
        self.stores[partition].append((emb, response))
        # Trim partition
        if len(self.stores[partition]) > 200:
            self.stores[partition] = self.stores[partition][-150:]

pcache = PartitionedCache()

async def ask(question: str) -> str:
    cached = await pcache.lookup(question)
    if cached:
        return cached
    resp = await client.messages.create(
        model="claude-sonnet-4-6", max_tokens=512,
        messages=[{"role": "user", "content": question}]
    )
    text = resp.content[0].text
    await pcache.store(question, text)
    return text
```

---

## Option 6: Persistent SQLite Semantic Cache with Embedding Reuse

```python
import asyncio
import anthropic
import aiosqlite
import hashlib
import json
import math
import time

client = anthropic.AsyncAnthropic()
DB_PATH = "semantic_cache.db"

def embed(text: str, dim: int = 128) -> list[float]:
    vec = [0.0] * dim
    bigrams = [text[i:i+2].lower() for i in range(len(text)-1)]
    for bg in bigrams:
        h = int(hashlib.md5(bg.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(x*x for x in vec)) or 1.0
    return [x/norm for x in vec]

def cosine(a: list[float], b: list[float]) -> float:
    return sum(x*y for x,y in zip(a,b))

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                id INTEGER PRIMARY KEY,
                query TEXT NOT NULL,
                embedding TEXT NOT NULL,
                response TEXT NOT NULL,
                created_at REAL,
                hits INTEGER DEFAULT 0,
                ttl_seconds REAL DEFAULT 3600
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_created ON cache(created_at)")
        await db.commit()

async def cache_get(query: str, threshold: float = 0.90) -> str | None:
    emb = embed(query)
    async with aiosqlite.connect(DB_PATH) as db:
        min_time = time.time() - 86400  # max 24h entries
        async with db.execute(
            "SELECT id, embedding, response FROM cache WHERE created_at > ? AND (created_at + ttl_seconds) > ?",
            (min_time, time.time())
        ) as cursor:
            rows = await cursor.fetchall()
        for row_id, emb_json, response in rows:
            stored_emb = json.loads(emb_json)
            if cosine(emb, stored_emb) >= threshold:
                await db.execute("UPDATE cache SET hits = hits + 1 WHERE id = ?", (row_id,))
                await db.commit()
                return response
    return None

async def cache_put(query: str, response: str, ttl: float = 3600.0):
    emb = embed(query)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO cache (query, embedding, response, created_at, ttl_seconds) VALUES (?, ?, ?, ?, ?)",
            (query, json.dumps(emb), response, time.time(), ttl)
        )
        # Prune old/expired entries
        await db.execute("DELETE FROM cache WHERE (created_at + ttl_seconds) < ?", (time.time(),))
        await db.commit()

async def ask(question: str) -> str:
    await init_db()
    cached = await cache_get(question)
    if cached:
        return cached
    resp = await client.messages.create(
        model="claude-sonnet-4-6", max_tokens=512,
        messages=[{"role": "user", "content": question}]
    )
    text = resp.content[0].text
    await cache_put(question, text)
    return text

async def main():
    questions = [
        "What is semantic caching?",
        "Explain semantic caching in simple terms.",
        "How does semantic caching work?",
        "What is a vector database?",
    ]
    for q in questions:
        answer = await ask(q)
        print(f"Q: {q}\nA: {answer[:80]}...\n")

asyncio.run(main())
```

---

## Comparison

| Option | Storage | Lookup | Eviction | Persistence | Best For |
|--------|---------|--------|----------|-------------|----------|
| 1 – In-Memory Cosine | List | O(n) | LFU | No | Prototyping |
| 2 – LRU + TTL | OrderedDict | O(n) | LRU + TTL | No | Session-scoped caching |
| 3 – Tiered (Exact→Semantic) | Dict + List | O(1) then O(n) | None | No | Mixed exact/semantic workloads |
| 4 – Async Concurrent | List | O(n) | LFU | No | High-concurrency servers |
| 5 – Partitioned | Dict of lists | O(n/partitions) | FIFO per partition | No | Multi-domain agents |
| 6 – SQLite Persistent | SQLite | O(n) | TTL-based | Yes (disk) | Long-lived production agents |

**Recommendation:** Use Option 3 (tiered) for most production agents — exact match is O(1) and handles repeated queries cheaply, while semantic fallback catches paraphrases. Combine with Option 6's SQLite persistence to retain cache across restarts. In production, replace the simple embedding function with a real embedding model (Voyage AI, OpenAI text-embedding-3-small) for much higher similarity accuracy.
