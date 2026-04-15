---
layout: solution
title: "Agent Recomputes Embeddings on Every Request"
category: performance
description: "Static documents are re-embedded on each API call, adding 100–500ms latency and unnecessary embedding model cost to every request."
tags: [performance, embeddings, caching, rag, token-cost]
---

## Symptom

Every user query triggers a fresh call to an embedding model before retrieval. Response latency is consistently 300–600ms higher than it needs to be. The embedding API bill grows linearly with request volume even though the underlying documents haven't changed in weeks.

## Root Cause

Embedding models convert text to vectors. For a fixed document corpus — a knowledge base, FAQ, product catalogue — the vectors never change between requests. Recomputing them is pure waste: the same bytes go in, the same floats come out, every time. Without an embedding cache, each request pays full embedding cost and latency even for content that was embedded yesterday.

## Fix

### Option 1 — In-memory dict cache keyed by content hash

```python
import hashlib
import anthropic

client  = anthropic.Anthropic()

# Simple in-memory cache: content_hash → embedding vector
_embedding_cache: dict[str, list[float]] = {}

def embed(text: str) -> list[float]:
    key = hashlib.sha256(text.encode()).hexdigest()
    if key in _embedding_cache:
        return _embedding_cache[key]

    # Replace with your actual embedding call (OpenAI, Cohere, local model, etc.)
    # Here we use a placeholder that returns a mock vector
    import struct, math
    mock_vector = [math.sin(i + hash(text) % 100) for i in range(1536)]
    _embedding_cache[key] = mock_vector
    print(f"[embed] computed (cache miss) for {text[:40]!r}")
    return mock_vector

def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot   = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(x * x for x in b) ** 0.5
    return dot / (mag_a * mag_b) if mag_a and mag_b else 0.0

DOCUMENTS = [
    "Python is a high-level programming language known for readability.",
    "FastAPI is a modern web framework for building APIs with Python.",
    "PostgreSQL is a powerful open-source relational database.",
    "Redis is an in-memory data structure store used as a cache.",
    "Docker containers package applications with their dependencies.",
]

# Pre-embed all documents once at startup
doc_embeddings = [(doc, embed(doc)) for doc in DOCUMENTS]
print(f"[embed] {len(doc_embeddings)} documents pre-embedded")

def retrieve(query: str, top_k: int = 2) -> list[str]:
    query_emb = embed(query)  # cache miss only on first call with this query
    scored    = [(cosine_similarity(query_emb, doc_emb), doc) for doc, doc_emb in doc_embeddings]
    scored.sort(reverse=True)
    return [doc for _, doc in scored[:top_k]]

def answer(question: str) -> str:
    context = "\n".join(retrieve(question))
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=f"Answer using only this context:\n{context}",
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text

# Second call with same question hits cache — no re-embedding
for q in ["What is FastAPI?", "How does Redis work?", "What is FastAPI?"]:
    print(f"\nQ: {q}")
    print(f"A: {answer(q)[:100]}")
```

**Expected Token Savings:** Zero embedding re-computation for repeated queries; document embeddings computed once at startup and never again.
**Environment:** Any RAG pipeline with a static document corpus; the simplest possible caching strategy.

---

### Option 2 — Disk-persisted embedding cache with numpy

```python
import hashlib
import os
import json
import anthropic

client = anthropic.Anthropic()

CACHE_DIR = "/tmp/embedding_cache"
os.makedirs(CACHE_DIR, exist_ok=True)

def cache_path(key: str) -> str:
    return os.path.join(CACHE_DIR, f"{key[:16]}.json")

def embed_cached(text: str) -> list[float]:
    key  = hashlib.sha256(text.encode()).hexdigest()
    path = cache_path(key)

    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)["vector"]

    # Compute embedding (replace with real embedding API)
    import math
    vector = [math.cos(i + hash(text) % 50) for i in range(512)]

    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"text_preview": text[:100], "vector": vector}, f)
    os.replace(tmp, path)   # atomic write
    print(f"[embed] computed and cached for {text[:40]!r}")
    return vector

def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed multiple texts, using cache for each."""
    return [embed_cached(t) for t in texts]

DOCS = [
    "The mitochondria is the powerhouse of the cell.",
    "DNA carries genetic information in living organisms.",
    "Photosynthesis converts light into chemical energy.",
    "The water cycle describes continuous movement of water on Earth.",
]

# Persist embeddings across process restarts
print("First run (computing):")
vecs1 = embed_batch(DOCS)

print("\nSecond run (all from cache):")
vecs2 = embed_batch(DOCS)

print(f"\nVectors identical: {all(v1 == v2 for v1, v2 in zip(vecs1, vecs2))}")
print(f"Cache files: {len(os.listdir(CACHE_DIR))}")
```

**Expected Token Savings:** Embeddings survive process restarts; a 10 000-document corpus embedded once and reused indefinitely.
**Environment:** Long-running services, scheduled jobs, or any setup where the process may restart between requests.

---

### Option 3 — TTL-aware cache with automatic invalidation

```python
import hashlib
import time
import json
import os
import anthropic

client = anthropic.Anthropic()

CACHE_FILE = "/tmp/embedding_cache_ttl.json"
TTL_SECONDS = 86400  # 24 hours — re-embed daily to catch document updates

def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            return json.load(f)
    return {}

def save_cache(cache: dict) -> None:
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, CACHE_FILE)

def embed_with_ttl(text: str) -> list[float]:
    cache = load_cache()
    key   = hashlib.sha256(text.encode()).hexdigest()
    entry = cache.get(key)

    if entry and (time.time() - entry["timestamp"]) < TTL_SECONDS:
        return entry["vector"]

    # Compute fresh embedding
    import math
    vector = [math.sin(i * 0.1 + hash(text) % 30) for i in range(256)]
    cache[key] = {"vector": vector, "timestamp": time.time(), "preview": text[:80]}
    save_cache(cache)
    print(f"[embed] fresh computation for {text[:40]!r}")
    return vector

def prune_expired(cache: dict) -> dict:
    """Remove entries older than TTL to prevent unbounded growth."""
    now = time.time()
    return {k: v for k, v in cache.items() if now - v["timestamp"] < TTL_SECONDS}

# Demonstrate TTL caching
texts = ["Machine learning is a subset of artificial intelligence.",
         "Neural networks are inspired by the human brain."]

for t in texts:
    embed_with_ttl(t)   # compute

for t in texts:
    embed_with_ttl(t)   # cache hit

# Prune and report
cache = load_cache()
before = len(cache)
cache  = prune_expired(cache)
print(f"\nCache: {before} entries, {len(cache)} after pruning")
```

**Expected Token Savings:** TTL prevents stale vectors without manual invalidation; cache grows only with unique documents.
**Environment:** RAG systems where documents are updated periodically (daily/weekly); TTL matches the update cadence.

---

### Option 4 — Versioned document store: re-embed only changed docs

```python
import hashlib
import json
import os
import anthropic

client = anthropic.Anthropic()

STORE_FILE = "/tmp/doc_embedding_store.json"

def load_store() -> dict:
    if os.path.exists(STORE_FILE):
        with open(STORE_FILE) as f:
            return json.load(f)
    return {}

def save_store(store: dict) -> None:
    tmp = STORE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(store, f, indent=2)
    os.replace(tmp, STORE_FILE)

def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]

def compute_embedding(text: str) -> list[float]:
    import math
    return [math.cos(i + hash(text) % 40) for i in range(256)]

def sync_documents(documents: dict[str, str]) -> dict[str, list[float]]:
    """
    documents: {doc_id: text}
    Returns: {doc_id: embedding}
    Only recomputes embeddings for documents whose content changed.
    """
    store = load_store()
    changed = 0

    for doc_id, text in documents.items():
        chash = content_hash(text)
        if store.get(doc_id, {}).get("content_hash") != chash:
            print(f"[embed] recomputing {doc_id!r} (content changed)")
            store[doc_id] = {
                "content_hash": chash,
                "text_preview": text[:100],
                "embedding":    compute_embedding(text),
            }
            changed += 1

    # Remove deleted documents
    deleted = set(store.keys()) - set(documents.keys())
    for doc_id in deleted:
        del store[doc_id]
        print(f"[embed] removed {doc_id!r} (document deleted)")

    save_store(store)
    print(f"[embed] sync complete: {changed} changed, {len(deleted)} deleted, {len(documents) - changed} cached")
    return {doc_id: store[doc_id]["embedding"] for doc_id in documents}

# Initial corpus
corpus_v1 = {
    "doc-1": "Python is interpreted and dynamically typed.",
    "doc-2": "Rust guarantees memory safety without garbage collection.",
    "doc-3": "Go was designed for simplicity and concurrency.",
}
embeddings = sync_documents(corpus_v1)
print(f"Embeddings loaded: {list(embeddings.keys())}")

# Update: only doc-2 changed
corpus_v2 = {
    "doc-1": "Python is interpreted and dynamically typed.",         # unchanged
    "doc-2": "Rust guarantees memory safety at compile time.",       # changed
    "doc-3": "Go was designed for simplicity and concurrency.",      # unchanged
    "doc-4": "TypeScript adds static types to JavaScript.",          # new
}
embeddings = sync_documents(corpus_v2)
```

**Expected Token Savings:** Only modified documents are re-embedded; a 10 000-document corpus with 10 daily updates costs 10 embedding calls, not 10 000.
**Environment:** Document management systems, wikis, knowledge bases with incremental updates.

---

### Option 5 — LRU cache for query embeddings

```python
import hashlib
import math
from functools import lru_cache
import anthropic

client = anthropic.Anthropic()

@lru_cache(maxsize=1000)
def embed_query(text: str) -> tuple[float, ...]:
    """Cache the 1000 most recently seen queries. Returns a tuple (hashable)."""
    # Replace with real embedding API call
    vector = tuple(math.sin(i + hash(text) % 60) for i in range(256))
    print(f"[embed] query computed (miss): {text[:50]!r}")
    return vector

def retrieve(query: str, doc_embeddings: list[tuple[str, tuple]], top_k: int = 2) -> list[str]:
    query_vec = embed_query(query)

    def cosine(a, b):
        dot   = sum(x * y for x, y in zip(a, b))
        mag_a = sum(x * x for x in a) ** 0.5
        mag_b = sum(x * x for x in b) ** 0.5
        return dot / (mag_a * mag_b) if mag_a and mag_b else 0.0

    scored = sorted([(cosine(query_vec, emb), doc) for doc, emb in doc_embeddings], reverse=True)
    return [doc for _, doc in scored[:top_k]]

DOCS = [
    "Solar panels convert sunlight into electricity.",
    "Wind turbines generate power from wind energy.",
    "Hydroelectric dams use water flow to produce electricity.",
    "Geothermal energy taps heat from inside the Earth.",
]
doc_embs = [(doc, embed_query(doc)) for doc in DOCS]

# Repeated queries hit LRU cache
queries = [
    "How does solar power work?",
    "What are renewable energy sources?",
    "How does solar power work?",   # cache hit
    "Tell me about wind energy.",
    "How does solar power work?",   # cache hit again
]
for q in queries:
    results = retrieve(q, doc_embs)
    print(f"Q: {q!r}")
    print(f"A: {results[0][:60]}\n")

info = embed_query.cache_info()
print(f"Cache: hits={info.hits}, misses={info.misses}, size={info.currsize}")
```

**Expected Token Savings:** Popular queries are embedded once; `lru_cache` is zero-overhead for cache hits.
**Environment:** User-facing search where query vocabulary is repetitive (FAQ, support chat); LRU is ideal when query space is bounded.

---

### Option 6 — Batch pre-computation at startup with progress reporting

```python
import time
import json
import os
import math
import anthropic

client = anthropic.Anthropic()

def compute_embedding_batch(texts: list[str], batch_size: int = 20) -> list[list[float]]:
    """Process embeddings in batches with progress reporting."""
    all_vectors: list[list[float]] = []
    total = len(texts)

    for i in range(0, total, batch_size):
        batch = texts[i:i + batch_size]
        # Replace with real batch embedding API call
        vectors = [[math.cos(j * 0.05 + hash(t) % 100) for j in range(256)] for t in batch]
        all_vectors.extend(vectors)
        pct = min(100, (i + len(batch)) * 100 // total)
        print(f"[embed] {i + len(batch)}/{total} ({pct}%) embedded")
        time.sleep(0.01)  # simulate API latency

    return all_vectors

def build_index(documents: list[str], index_path: str) -> dict:
    if os.path.exists(index_path):
        print(f"[embed] loading existing index from {index_path}")
        with open(index_path) as f:
            return json.load(f)

    print(f"[embed] building index for {len(documents)} documents...")
    t0      = time.time()
    vectors = compute_embedding_batch(documents)
    elapsed = time.time() - t0

    index = {
        "documents": documents,
        "embeddings": vectors,
        "built_at":  time.time(),
        "doc_count": len(documents),
    }
    tmp = index_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(index, f)
    os.replace(tmp, index_path)

    print(f"[embed] index built in {elapsed:.2f}s, saved to {index_path}")
    return index

# Simulate large corpus
docs = [f"Document {i}: topic about subject {i % 10} in category {i % 5}." for i in range(100)]

# Build once, reuse forever
index = build_index(docs, "/tmp/my_index.json")
print(f"\nIndex ready: {index['doc_count']} documents")

# Subsequent calls load from disk instantly
index2 = build_index(docs, "/tmp/my_index.json")
print(f"Reloaded: {index2['doc_count']} documents (no recomputation)")
```

**Expected Token Savings:** Build-once pattern eliminates all embedding computation at query time; startup cost amortised across all requests.
**Environment:** Applications with a fixed corpus loaded at startup (documentation sites, product search, knowledge bases).

---

## Comparison

| Option | Storage | Survives Restart | Invalidation | Best For |
|---|---|---|---|---|
| 1. In-memory dict | RAM | No | Manual | Single-process, static corpus |
| 2. Disk JSON cache | Disk | Yes | Manual | Multi-restart services |
| 3. TTL cache | Disk | Yes | Automatic (time) | Periodically updated docs |
| 4. Content-hash versioning | Disk | Yes | Automatic (hash) | Incremental document updates |
| 5. LRU for queries | RAM | No | Automatic (size) | Repeated user queries |
| 6. Batch pre-computation | Disk | Yes | Manual rebuild | Large static corpora, startup indexing |
