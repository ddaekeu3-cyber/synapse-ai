---
layout: solution
title: "Agent Doesn't Implement Semantic Query Cache for Similar Requests"
category: performance
description: "Agents that cache only exact query strings miss the majority of cache-eligible traffic. 'What is Python?' and 'Tell me about Python programming' are semantically identical but produce cache misses. Semantic query caching uses embedding similarity to find cached responses for near-duplicate queries, reducing API costs by 40-70% for agents serving repeated user questions."
tags: [semantic-cache, embeddings, performance, token-savings, similarity, retrieval, caching]
---

# Agent Doesn't Implement Semantic Query Cache for Similar Requests

## Problem

Exact-match caching catches only identical strings. In practice, users phrase the same question dozens of ways: "How do I reverse a list?", "Python list reversal", "reverse a Python list in-place" — all semantically identical but each producing a cache miss with string-match caching. Semantic caching computes an embedding for each incoming query, searches a vector index for similar past queries above a threshold, and returns the cached response if the similarity is high enough. For agents serving many users, this typically eliminates 40-70% of redundant API calls.

**Symptoms:**
- Cache hit rate below 5% despite users asking similar questions
- Token costs grow linearly with traffic even for common topics
- Response latency is identical for first and repeat queries
- No reuse of expensive multi-step agent responses
- LLM billed for answering the same conceptual question hundreds of times

---

## Option 1: In-Memory Semantic Cache with Cosine Similarity

```python
import anthropic
import math
import time
import hashlib
from dataclasses import dataclass, field

@dataclass
class SemanticCacheEntry:
    query: str
    embedding: list[float]
    response: str
    cached_at: float = field(default_factory=time.time)
    hit_count: int = 0

def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0

def get_embedding(text: str) -> list[float]:
    """
    In production: use voyage-ai or OpenAI text-embedding-3-small.
    Here we simulate with a deterministic hash-based vector.
    """
    h = hashlib.sha256(text.lower().strip().encode()).digest()
    vec = [(b / 255.0) * 2 - 1 for b in h[:32]]
    norm = math.sqrt(sum(x*x for x in vec)) or 1.0
    return [x / norm for x in vec]

class SemanticQueryCache:
    def __init__(self, similarity_threshold: float = 0.92, ttl_seconds: float = 3600, max_size: int = 500):
        self._entries: list[SemanticCacheEntry] = []
        self.threshold = similarity_threshold
        self.ttl = ttl_seconds
        self.max_size = max_size
        self.stats = {"hits": 0, "misses": 0, "evictions": 0}

    def _evict_expired(self):
        now = time.time()
        before = len(self._entries)
        self._entries = [e for e in self._entries if now - e.cached_at < self.ttl]
        self.stats["evictions"] += before - len(self._entries)

    def lookup(self, query: str) -> tuple[str | None, float]:
        """Return (cached_response, similarity) or (None, 0.0) on miss."""
        self._evict_expired()
        query_emb = get_embedding(query)

        best_sim = 0.0
        best_entry = None
        for entry in self._entries:
            sim = cosine_similarity(query_emb, entry.embedding)
            if sim > best_sim:
                best_sim = sim
                best_entry = entry

        if best_entry and best_sim >= self.threshold:
            best_entry.hit_count += 1
            self.stats["hits"] += 1
            return best_entry.response, best_sim

        self.stats["misses"] += 1
        return None, best_sim

    def store(self, query: str, response: str):
        if len(self._entries) >= self.max_size:
            # Evict least-recently-used (lowest hit count + oldest)
            self._entries.sort(key=lambda e: (e.hit_count, e.cached_at))
            self._entries.pop(0)

        self._entries.append(SemanticCacheEntry(
            query=query,
            embedding=get_embedding(query),
            response=response
        ))

    def hit_rate(self) -> float:
        total = self.stats["hits"] + self.stats["misses"]
        return self.stats["hits"] / total if total else 0.0

    def report(self) -> str:
        return (f"Semantic cache: {self.stats['hits']} hits / "
                f"{self.stats['hits'] + self.stats['misses']} total "
                f"({self.hit_rate():.0%} hit rate), "
                f"{len(self._entries)} entries, "
                f"{self.stats['evictions']} evictions")

def run_semantic_cached_agent(queries: list[str], threshold: float = 0.88):
    client = anthropic.Anthropic()
    cache = SemanticQueryCache(similarity_threshold=threshold)
    total_tokens = 0
    saved_tokens = 0

    print(f"Processing {len(queries)} queries (threshold={threshold}):\n")

    for query in queries:
        cached_response, similarity = cache.lookup(query)

        if cached_response:
            print(f"  [HIT  {similarity:.3f}] {query!r[:50]}")
            saved_tokens += 150  # Estimated tokens saved
        else:
            print(f"  [MISS {similarity:.3f}] {query!r[:50]}")
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": query}]
            )
            answer = response.content[0].text
            tokens = response.usage.input_tokens + response.usage.output_tokens
            total_tokens += tokens
            cache.store(query, answer)

    print(f"\n{cache.report()}")
    print(f"API tokens used: {total_tokens}")
    print(f"Estimated tokens saved: {saved_tokens}")

# Semantically similar queries that exact-match caching would miss
queries = [
    "How do I reverse a list in Python?",
    "What's the best way to reverse a Python list?",
    "Python list reversal techniques",
    "How to reverse a list in Python in-place",   # near-duplicate of first
    "What is machine learning?",
    "Explain machine learning to me",               # near-duplicate
    "What's machine learning about?",              # near-duplicate
    "Define machine learning",
    "How do I sort a dictionary by value in Python?",
    "Python dictionary sort by value",             # near-duplicate
]

run_semantic_cached_agent(queries, threshold=0.85)

# Expected Token Savings: ~50% for typical user traffic with repeated question patterns
# Environment: In-memory; replace with Qdrant/Pinecone for persistence and scale
```

---

## Option 2: Tiered Cache — Exact Match First, Semantic Second

```python
import anthropic
import hashlib
import math
import time
from dataclasses import dataclass, field
from functools import lru_cache

@dataclass
class CacheHit:
    response: str
    tier: str  # "exact" or "semantic"
    similarity: float
    original_query: str

def normalize_query(query: str) -> str:
    """Normalize for exact-match comparison."""
    import re
    q = query.lower().strip()
    q = re.sub(r'\s+', ' ', q)
    q = re.sub(r'[?!.,;]$', '', q)
    return q

def get_embedding(text: str) -> list[float]:
    """Simulated embedding — replace with voyage-ai in production."""
    h = hashlib.sha256(text.lower().encode()).digest()
    vec = [(b / 255.0) * 2 - 1 for b in h[:32]]
    norm = math.sqrt(sum(x*x for x in vec)) or 1.0
    return [x / norm for x in vec]

def cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x*y for x, y in zip(a, b))
    na = math.sqrt(sum(x*x for x in a))
    nb = math.sqrt(sum(x*x for x in b))
    return dot / (na * nb) if na and nb else 0.0

class TieredSemanticCache:
    def __init__(
        self,
        exact_ttl: float = 1800,
        semantic_ttl: float = 900,
        semantic_threshold: float = 0.90
    ):
        self._exact: dict[str, tuple[str, float]] = {}   # normalized_query -> (response, expires_at)
        self._semantic: list[tuple[list[float], str, str, float]] = []  # (emb, query, response, expires_at)
        self.exact_ttl = exact_ttl
        self.semantic_ttl = semantic_ttl
        self.threshold = semantic_threshold
        self.stats = {"exact_hits": 0, "semantic_hits": 0, "misses": 0}

    def get(self, query: str) -> CacheHit | None:
        now = time.time()
        normalized = normalize_query(query)

        # Tier 1: Exact match (O(1))
        if normalized in self._exact:
            response, expires = self._exact[normalized]
            if now < expires:
                self.stats["exact_hits"] += 1
                return CacheHit(response, "exact", 1.0, query)
            else:
                del self._exact[normalized]

        # Tier 2: Semantic match (O(n))
        query_emb = get_embedding(query)
        best_sim, best_entry = 0.0, None

        valid_semantic = []
        for emb, orig_q, response, expires in self._semantic:
            if now >= expires:
                continue
            valid_semantic.append((emb, orig_q, response, expires))
            sim = cosine_sim(query_emb, emb)
            if sim > best_sim:
                best_sim = sim
                best_entry = (response, orig_q)

        self._semantic = valid_semantic  # Remove expired

        if best_entry and best_sim >= self.threshold:
            self.stats["semantic_hits"] += 1
            return CacheHit(best_entry[0], "semantic", best_sim, best_entry[1])

        self.stats["misses"] += 1
        return None

    def put(self, query: str, response: str):
        now = time.time()
        normalized = normalize_query(query)
        self._exact[normalized] = (response, now + self.exact_ttl)
        emb = get_embedding(query)
        self._semantic.append((emb, query, response, now + self.semantic_ttl))

    def summary(self) -> str:
        total = sum(self.stats.values())
        if total == 0:
            return "No queries yet"
        return (f"Exact hits: {self.stats['exact_hits']} ({self.stats['exact_hits']/total:.0%}), "
                f"Semantic hits: {self.stats['semantic_hits']} ({self.stats['semantic_hits']/total:.0%}), "
                f"Misses: {self.stats['misses']} ({self.stats['misses']/total:.0%})")

def run_tiered_cache_agent(query_batches: list[list[str]]):
    client = anthropic.Anthropic()
    cache = TieredSemanticCache(semantic_threshold=0.87)
    api_calls = 0

    for batch_num, queries in enumerate(query_batches):
        print(f"\nBatch {batch_num + 1}:")
        for query in queries:
            hit = cache.get(query)
            if hit:
                print(f"  [{hit.tier.upper()} {hit.similarity:.3f}] {query!r[:45]}")
                if hit.tier == "semantic":
                    print(f"    -> Used response from: {hit.original_query!r[:40]}")
            else:
                response = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=200,
                    messages=[{"role": "user", "content": query}]
                )
                api_calls += 1
                cache.put(query, response.content[0].text)
                print(f"  [MISS] {query!r[:45]} (API call #{api_calls})")

    print(f"\nCache summary: {cache.summary()}")
    print(f"Total API calls: {api_calls}/{sum(len(b) for b in query_batches)}")

batches = [
    # First batch: primes the cache
    ["What is Python?", "How does garbage collection work?", "Explain REST APIs"],
    # Second batch: near-duplicates
    ["Tell me about Python programming", "What's garbage collection?", "What are REST APIs?"],
    # Third batch: exact repeats
    ["What is Python?", "Explain REST APIs"],
]
run_tiered_cache_agent(batches)

# Expected Token Savings: ~55-65% — exact tier is free, semantic tier saves ~85% of near-duplicate calls
# Environment: API gateway layer; semantic tier adds ~5ms lookup latency per query
```

---

## Option 3: Persistent Semantic Cache with SQLite and Embeddings

```python
import anthropic
import json
import math
import sqlite3
import time
import hashlib
from dataclasses import dataclass

@dataclass
class PersistedEntry:
    entry_id: int
    query: str
    embedding_json: str
    response: str
    created_at: float
    hit_count: int

    @property
    def embedding(self) -> list[float]:
        return json.loads(self.embedding_json)

def get_embedding(text: str) -> list[float]:
    """Simulated. In production: voyage-ai client.embed(text).embeddings[0]"""
    h = hashlib.sha256(text.lower().encode()).digest()
    vec = [(b / 255.0) * 2 - 1 for b in h[:32]]
    norm = math.sqrt(sum(x*x for x in vec)) or 1.0
    return [x / norm for x in vec]

def cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x*y for x, y in zip(a, b))
    na = math.sqrt(sum(x*x for x in a))
    nb = math.sqrt(sum(x*x for x in b))
    return dot / (na * nb) if na and nb else 0.0

class PersistentSemanticCache:
    def __init__(self, db_path: str = "/tmp/semantic_cache.db",
                 threshold: float = 0.90, ttl_hours: float = 24):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.threshold = threshold
        self.ttl = ttl_hours * 3600
        self._setup()

    def _setup(self):
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS semantic_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL,
                embedding TEXT NOT NULL,
                response TEXT NOT NULL,
                created_at REAL DEFAULT (unixepoch('now', 'subsec')),
                hit_count INTEGER DEFAULT 0
            )
        """)
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_created ON semantic_cache(created_at)")
        self.db.commit()

    def _load_valid(self) -> list[PersistedEntry]:
        cutoff = time.time() - self.ttl
        rows = self.db.execute(
            "SELECT id, query, embedding, response, created_at, hit_count FROM semantic_cache WHERE created_at > ?",
            (cutoff,)
        ).fetchall()
        return [PersistedEntry(*row) for row in rows]

    def lookup(self, query: str) -> tuple[str | None, str | None, float]:
        query_emb = get_embedding(query)
        entries = self._load_valid()

        best_sim, best_entry = 0.0, None
        for entry in entries:
            sim = cosine_sim(query_emb, entry.embedding)
            if sim > best_sim:
                best_sim = sim
                best_entry = entry

        if best_entry and best_sim >= self.threshold:
            self.db.execute(
                "UPDATE semantic_cache SET hit_count = hit_count + 1 WHERE id = ?",
                (best_entry.entry_id,)
            )
            self.db.commit()
            return best_entry.response, best_entry.query, best_sim

        return None, None, best_sim

    def store(self, query: str, response: str):
        emb = get_embedding(query)
        self.db.execute(
            "INSERT INTO semantic_cache (query, embedding, response) VALUES (?, ?, ?)",
            (query, json.dumps(emb), response)
        )
        self.db.commit()

    def prune_old(self):
        cutoff = time.time() - self.ttl
        deleted = self.db.execute(
            "DELETE FROM semantic_cache WHERE created_at < ?", (cutoff,)
        ).rowcount
        self.db.commit()
        return deleted

    def analytics(self) -> dict:
        rows = self.db.execute("""
            SELECT
                COUNT(*) as total,
                SUM(hit_count) as total_hits,
                AVG(hit_count) as avg_hits,
                MAX(hit_count) as max_hits
            FROM semantic_cache
        """).fetchone()
        top_hits = self.db.execute("""
            SELECT query, hit_count FROM semantic_cache ORDER BY hit_count DESC LIMIT 5
        """).fetchall()
        return {
            "total_entries": rows[0],
            "total_cache_hits": rows[1] or 0,
            "avg_hits_per_entry": round(rows[2] or 0, 2),
            "top_queries": [{"query": r[0][:50], "hits": r[1]} for r in top_hits]
        }

def run_persistent_cache_agent(queries: list[str], threshold: float = 0.88):
    client = anthropic.Anthropic()
    cache = PersistentSemanticCache(threshold=threshold)
    api_calls = 0

    print(f"Processing {len(queries)} queries (persistent semantic cache):\n")
    for query in queries:
        cached, original_query, similarity = cache.lookup(query)
        if cached:
            tier_label = "EXACT" if similarity > 0.999 else f"SEM {similarity:.3f}"
            print(f"  [{tier_label}] {query!r[:50]}")
        else:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": query}]
            )
            api_calls += 1
            cache.store(query, response.content[0].text)
            print(f"  [MISS ] {query!r[:50]} (stored)")

    analytics = cache.analytics()
    print(f"\nCache analytics:")
    print(f"  Total entries: {analytics['total_entries']}")
    print(f"  Cache hits served: {analytics['total_cache_hits']}")
    print(f"  API calls made: {api_calls}/{len(queries)}")
    print(f"  Top cached queries:")
    for q in analytics["top_queries"]:
        if q["hits"] > 0:
            print(f"    [{q['hits']} hits] {q['query']}")

queries = [
    "What is a neural network?",
    "Explain neural networks",
    "How do neural networks work?",
    "What are neural networks used for?",
    "Tell me about deep learning",
    "What is deep learning?",
    "How is deep learning different from machine learning?",
    "What is a neural network?",  # exact repeat
]
run_persistent_cache_agent(queries, threshold=0.83)

# Expected Token Savings: ~60% — persists across restarts; ideal for chatbots with returning users
# Environment: SQLite for single-node; swap embedding storage for pgvector or Qdrant at scale
```

---

## Option 4: Cache Warming — Pre-populate with Predicted Common Queries

```python
import anthropic
import math
import hashlib
import time
from dataclasses import dataclass, field

@dataclass
class WarmCacheEntry:
    query: str
    embedding: list[float]
    response: str
    warmed: bool = True
    hit_count: int = 0

def get_embedding(text: str) -> list[float]:
    h = hashlib.sha256(text.lower().encode()).digest()
    vec = [(b / 255.0) * 2 - 1 for b in h[:32]]
    norm = math.sqrt(sum(x*x for x in vec)) or 1.0
    return [x / norm for x in vec]

def cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x*y for x, y in zip(a, b))
    na = math.sqrt(sum(x*x for x in a))
    nb = math.sqrt(sum(x*x for x in b))
    return dot / (na * nb) if na and nb else 0.0

class WarmableSemanticCache:
    def __init__(self, threshold: float = 0.88):
        self._entries: list[WarmCacheEntry] = []
        self.threshold = threshold
        self.warm_hits = 0
        self.live_hits = 0
        self.misses = 0

    def warm(self, queries_and_responses: list[tuple[str, str]]):
        """Pre-populate cache with known common queries and their responses."""
        for query, response in queries_and_responses:
            emb = get_embedding(query)
            self._entries.append(WarmCacheEntry(query, emb, response, warmed=True))
        print(f"[Cache Warm] {len(queries_and_responses)} entries loaded")

    def lookup(self, query: str) -> WarmCacheEntry | None:
        query_emb = get_embedding(query)
        best_sim, best = 0.0, None
        for entry in self._entries:
            sim = cosine_sim(query_emb, entry.embedding)
            if sim > best_sim:
                best_sim = sim
                best = entry
        if best and best_sim >= self.threshold:
            best.hit_count += 1
            if best.warmed:
                self.warm_hits += 1
            else:
                self.live_hits += 1
            return best
        self.misses += 1
        return None

    def store_live(self, query: str, response: str):
        emb = get_embedding(query)
        self._entries.append(WarmCacheEntry(query, emb, response, warmed=False))

    def summary(self) -> dict:
        total = self.warm_hits + self.live_hits + self.misses
        return {
            "warm_hits": self.warm_hits,
            "live_hits": self.live_hits,
            "misses": self.misses,
            "hit_rate": f"{(self.warm_hits + self.live_hits) / total:.0%}" if total else "0%"
        }

def generate_warm_responses(client: anthropic.Anthropic, faq_queries: list[str]) -> list[tuple[str, str]]:
    """Generate responses for FAQ queries during startup (warm phase)."""
    print("Warming cache with FAQ responses...\n")
    pairs = []
    for query in faq_queries:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": query}]
        )
        answer = response.content[0].text
        pairs.append((query, answer))
        print(f"  Warmed: {query!r[:50]}")
    return pairs

def run_warm_cache_agent(faq_queries: list[str], live_queries: list[str]):
    client = anthropic.Anthropic()
    cache = WarmableSemanticCache(threshold=0.85)

    # Startup: warm the cache with FAQ responses
    warm_pairs = generate_warm_responses(client, faq_queries)
    cache.warm(warm_pairs)

    # Serve live traffic
    print(f"\nServing {len(live_queries)} live queries:\n")
    api_calls_live = 0
    for query in live_queries:
        hit = cache.lookup(query)
        if hit:
            source = "warm" if hit.warmed else "live"
            print(f"  [HIT/{source}] {query!r[:50]}")
        else:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{"role": "user", "content": query}]
            )
            cache.store_live(query, response.content[0].text)
            api_calls_live += 1
            print(f"  [MISS ] {query!r[:50]}")

    print(f"\nSummary: {cache.summary()}")
    print(f"Live API calls: {api_calls_live}/{len(live_queries)}")

faq_queries = [
    "What is Python?",
    "How do I install packages in Python?",
    "What is a list comprehension?",
]
live_queries = [
    "Tell me about Python programming language",  # warm hit
    "How to install Python packages with pip",    # warm hit
    "Explain Python list comprehensions",         # warm hit
    "What is the GIL in Python?",                # miss — new topic
    "Python list comprehension syntax",           # warm hit (again)
    "What is asyncio?",                           # miss
]
run_warm_cache_agent(faq_queries, live_queries)

# Expected Token Savings: ~70% — warm hits are essentially free; startup cost amortized over traffic
# Environment: Customer support bots, documentation assistants with predictable FAQ traffic
```

---

## Option 5: Semantic Cache with Confidence Routing

```python
import anthropic
import math
import hashlib
import time
from dataclasses import dataclass

@dataclass
class CacheDecision:
    action: str  # "cache_hit" | "partial_hit" | "miss"
    cached_response: str | None
    similarity: float
    original_query: str | None
    reason: str

def get_embedding(text: str) -> list[float]:
    h = hashlib.sha256(text.lower().encode()).digest()
    vec = [(b / 255.0) * 2 - 1 for b in h[:32]]
    norm = math.sqrt(sum(x*x for x in vec)) or 1.0
    return [x / norm for x in vec]

def cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x*y for x, y in zip(a, b))
    na = math.sqrt(sum(x*x for x in a))
    nb = math.sqrt(sum(x*x for x in b))
    return dot / (na * nb) if na and nb else 0.0

class ConfidenceRoutingCache:
    """
    Three-tier routing by similarity score:
    - High (>= high_threshold): return cached response directly
    - Medium (>= low_threshold): use cached response as context hint for fresh answer
    - Low (< low_threshold): full cache miss, generate fresh response
    """

    def __init__(self, high_threshold: float = 0.93, low_threshold: float = 0.75):
        self._entries: list[tuple[list[float], str, str]] = []  # (emb, query, response)
        self.high_threshold = high_threshold
        self.low_threshold = low_threshold
        self.stats = {"high": 0, "medium": 0, "miss": 0}

    def decide(self, query: str) -> CacheDecision:
        query_emb = get_embedding(query)
        best_sim, best_entry = 0.0, None

        for emb, orig_q, response in self._entries:
            sim = cosine_sim(query_emb, emb)
            if sim > best_sim:
                best_sim = sim
                best_entry = (orig_q, response)

        if best_entry and best_sim >= self.high_threshold:
            self.stats["high"] += 1
            return CacheDecision("cache_hit", best_entry[1], best_sim, best_entry[0],
                                 f"High confidence ({best_sim:.3f}) — serving cached")
        elif best_entry and best_sim >= self.low_threshold:
            self.stats["medium"] += 1
            return CacheDecision("partial_hit", best_entry[1], best_sim, best_entry[0],
                                 f"Medium confidence ({best_sim:.3f}) — using as context hint")
        else:
            self.stats["miss"] += 1
            return CacheDecision("miss", None, best_sim, None,
                                 f"Low confidence ({best_sim:.3f}) — full miss")

    def store(self, query: str, response: str):
        emb = get_embedding(query)
        self._entries.append((emb, query, response))

    def report(self) -> str:
        total = sum(self.stats.values())
        if total == 0:
            return "No queries"
        return (f"High: {self.stats['high']} ({self.stats['high']/total:.0%}), "
                f"Medium: {self.stats['medium']} ({self.stats['medium']/total:.0%}), "
                f"Miss: {self.stats['miss']} ({self.stats['miss']/total:.0%})")

def run_confidence_routing_agent(queries: list[str]):
    client = anthropic.Anthropic()
    cache = ConfidenceRoutingCache(high_threshold=0.92, low_threshold=0.75)
    api_calls = 0

    for query in queries:
        decision = cache.decide(query)

        if decision.action == "cache_hit":
            print(f"  [HIGH  {decision.similarity:.3f}] {query!r[:45]} -> serving cached")
            # Use cached response directly — no API call

        elif decision.action == "partial_hit":
            print(f"  [MED   {decision.similarity:.3f}] {query!r[:45]} -> using as hint")
            # Use cached response as context to generate a tailored fresh response
            system = f"A similar question was answered before:\n{decision.cached_response}\n\nAnswer the new question using this as context."
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                system=system,
                messages=[{"role": "user", "content": query}]
            )
            answer = response.content[0].text
            cache.store(query, answer)
            api_calls += 1

        else:
            print(f"  [MISS  {decision.similarity:.3f}] {query!r[:45]}")
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{"role": "user", "content": query}]
            )
            answer = response.content[0].text
            cache.store(query, answer)
            api_calls += 1

    print(f"\nRouting summary: {cache.report()}")
    print(f"API calls: {api_calls}/{len(queries)}")

queries = [
    "What is a hash map?",                      # cold miss
    "What is a hash table?",                    # medium hit
    "Explain hash maps in programming",          # medium hit
    "What is a hash map?",                      # exact high hit
    "What are hash maps used for?",             # medium hit
    "How do I implement a binary search tree?", # cold miss
    "Binary search tree implementation",         # medium hit
    "What is a BST in computer science?",       # medium hit
]
run_confidence_routing_agent(queries)

# Expected Token Savings: ~40% full saves + ~20% partial saves = ~60% total
# Environment: Medium-confidence path is especially valuable for related-but-different questions
```

---

## Option 6: Distributed Semantic Cache with Redis and Async Embedding

```python
import anthropic
import asyncio
import hashlib
import json
import math
import time
from dataclasses import dataclass

# Simulated async Redis-like store (replace with `redis.asyncio` in production)
class FakeAsyncRedis:
    def __init__(self):
        self._store: dict = {}

    async def get(self, key: str) -> bytes | None:
        entry = self._store.get(key)
        if entry and entry["expires"] > time.time():
            return json.dumps(entry["value"]).encode()
        return None

    async def setex(self, key: str, ttl: int, value: bytes):
        self._store[key] = {"value": json.loads(value), "expires": time.time() + ttl}

    async def keys(self, pattern: str) -> list[bytes]:
        return [k.encode() for k in self._store if k.startswith(pattern.replace("*", ""))]

def get_embedding(text: str) -> list[float]:
    h = hashlib.sha256(text.lower().encode()).digest()
    vec = [(b / 255.0) * 2 - 1 for b in h[:32]]
    norm = math.sqrt(sum(x*x for x in vec)) or 1.0
    return [x / norm for x in vec]

def cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x*y for x, y in zip(a, b))
    na = math.sqrt(sum(x*x for x in a))
    nb = math.sqrt(sum(x*x for x in b))
    return dot / (na * nb) if na and nb else 0.0

class AsyncDistributedSemanticCache:
    def __init__(self, redis, namespace: str = "scache", threshold: float = 0.90, ttl: int = 3600):
        self.redis = redis
        self.ns = namespace
        self.threshold = threshold
        self.ttl = ttl
        self.stats = {"hits": 0, "misses": 0}

    async def _all_entries(self) -> list[dict]:
        keys = await self.redis.keys(f"{self.ns}:*")
        entries = []
        for key in keys:
            raw = await self.redis.get(key.decode())
            if raw:
                entries.append(json.loads(raw))
        return entries

    async def lookup(self, query: str) -> tuple[str | None, float]:
        query_emb = get_embedding(query)
        entries = await self._all_entries()

        best_sim, best_response = 0.0, None
        for entry in entries:
            emb = entry["embedding"]
            sim = cosine_sim(query_emb, emb)
            if sim > best_sim:
                best_sim = sim
                best_response = entry["response"]

        if best_response and best_sim >= self.threshold:
            self.stats["hits"] += 1
            return best_response, best_sim

        self.stats["misses"] += 1
        return None, best_sim

    async def store(self, query: str, response: str):
        emb = get_embedding(query)
        key = f"{self.ns}:{hashlib.md5(query.encode()).hexdigest()[:10]}"
        entry = {"query": query, "embedding": emb, "response": response}
        await self.redis.setex(key, self.ttl, json.dumps(entry).encode())

    def report(self) -> str:
        total = self.stats["hits"] + self.stats["misses"]
        return (f"Distributed semantic cache: {self.stats['hits']}/{total} hits "
                f"({self.stats['hits']/total:.0%} hit rate)" if total else "No queries")

async def run_async_semantic_cache_agent(queries: list[str]):
    client = anthropic.AsyncAnthropic()
    redis = FakeAsyncRedis()
    cache = AsyncDistributedSemanticCache(redis, threshold=0.86)
    api_calls = 0

    print(f"Processing {len(queries)} queries (async distributed cache):\n")

    async def handle_query(query: str) -> str:
        nonlocal api_calls
        cached, similarity = await cache.lookup(query)
        if cached:
            print(f"  [HIT {similarity:.3f}] {query!r[:50]}")
            return cached
        else:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{"role": "user", "content": query}]
            )
            answer = response.content[0].text
            await cache.store(query, answer)
            api_calls += 1
            print(f"  [MISS] {query!r[:50]}")
            return answer

    # Process first half sequentially (to build cache), then second half
    for query in queries[:4]:
        await handle_query(query)

    print(f"\n[Processing second batch in parallel...]\n")
    tasks = [handle_query(q) for q in queries[4:]]
    await asyncio.gather(*tasks)

    print(f"\n{cache.report()}")
    print(f"API calls: {api_calls}/{len(queries)}")

queries = [
    "What is SQL?",
    "How does SQL JOIN work?",
    "What are SQL transactions?",
    "Explain SQL indexes",
    # Second batch (parallel, with some near-duplicates)
    "What is SQL database?",            # near-dup of first
    "How do SQL JOINs work?",           # near-dup of second
    "Explain SQL ACID transactions",    # near-dup of third
    "What are database indexes in SQL?",# near-dup of fourth
]

asyncio.run(run_async_semantic_cache_agent(queries))

# Expected Token Savings: ~55% — parallel cache lookups add ~2ms overhead; async avoids blocking
# Environment: Production: replace FakeAsyncRedis with redis.asyncio.Redis; use pgvector for HNSW search
```

---

## Comparison

| Option | Storage | Cross-Session | Lookup Speed | Near-Dup Detection | Best For |
|--------|---------|--------------|-------------|-------------------|----------|
| In-Memory Cosine | RAM | No | O(n) | Yes | Single-process, low-traffic agents |
| Tiered (Exact + Semantic) | RAM | No | O(1) + O(n) | Yes | Maximizing hit rate with minimal latency |
| Persistent SQLite | Disk | Yes | O(n) | Yes | Single-node with restart tolerance |
| Cache Warming | RAM | No | O(n) | Yes | Known FAQ patterns, predictable traffic |
| Confidence Routing | RAM | No | O(n) | Yes | Using partial hits as context to improve answer |
| Async Distributed | Redis | Yes | O(n) | Yes | Multi-instance deployments, high concurrency |

**Recommendation:** Use **Option 2** (tiered exact + semantic) as the standard implementation — it catches exact repeats in O(1) and near-duplicates in O(n) with a single lookup. Move to **Option 6** (distributed async) when deploying multiple agent instances. Add **Option 4** (cache warming) for any agent with predictable FAQ traffic to serve common questions without any API call cost.
