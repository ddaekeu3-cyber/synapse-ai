---
layout: solution
title: "Agent Doesn't Implement Semantic Search Over Memory"
category: memory
description: "Store memories with embeddings and retrieve the most relevant ones by semantic similarity — so the agent recalls contextually related facts even when keywords don't match exactly."
tags: [memory, semantic-search, embeddings, vector, sqlite, python]
---

# Agent Doesn't Implement Semantic Search Over Memory

Keyword-matching memory retrieval fails when the user asks "What did we discuss about performance?" but the memory says "We optimized database query latency." Semantic search finds memories by meaning, not keywords — surfacing relevant context even when phrasing differs.

## Option 1: Character N-gram Embedding with Cosine Similarity

```python
import anthropic
import math
import hashlib
from dataclasses import dataclass, field

client = anthropic.Anthropic()

def ngram_embed(text: str, n: int = 3, dims: int = 64) -> list[float]:
    """Lightweight character n-gram embedding (no external deps)."""
    counts: dict[str, int] = {}
    t = text.lower()
    for i in range(len(t) - n + 1):
        g = t[i:i+n]
        counts[g] = counts.get(g, 0) + 1
    vec = [0.0] * dims
    for g, c in counts.items():
        idx = int(hashlib.md5(g.encode()).hexdigest(), 16) % dims
        vec[idx] += c
    norm = math.sqrt(sum(x**2 for x in vec)) or 1
    return [x / norm for x in vec]

def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))

@dataclass
class SemanticMemory:
    memories: list[dict] = field(default_factory=list)

    def store(self, text: str, metadata: dict | None = None):
        self.memories.append({
            "text": text,
            "embedding": ngram_embed(text),
            "metadata": metadata or {},
        })
        print(f"  [STORED] {text[:60]}")

    def search(self, query: str, top_k: int = 3, threshold: float = 0.1) -> list[dict]:
        q_emb = ngram_embed(query)
        scored = [
            {**m, "score": cosine(q_emb, m["embedding"])}
            for m in self.memories
        ]
        return sorted(
            [s for s in scored if s["score"] >= threshold],
            key=lambda x: -x["score"]
        )[:top_k]

mem = SemanticMemory()
facts = [
    "The user prefers Python over JavaScript for backend development.",
    "Database query latency was improved by adding an index on the users table.",
    "The team decided to use PostgreSQL for the production database.",
    "Alice joined the team in January and focuses on infrastructure.",
    "The API rate limit is 1000 requests per minute.",
    "We optimized the search endpoint using caching.",
]
for f in facts:
    mem.store(f)

queries = [
    "What did we discuss about performance?",
    "Tell me about our database choices.",
    "What do you know about the team members?",
]
for q in queries:
    print(f"\nQuery: {q}")
    results = mem.search(q, top_k=2)
    for r in results:
        print(f"  [{r['score']:.3f}] {r['text'][:70]}")

# Expected Token Savings: Retrieve only top-k relevant memories; inject ~100 tokens vs full memory dump
# Environment: pure Python; swap ngram_embed with voyage-3 for production accuracy
```

## Option 2: SQLite + Lightweight TF-IDF Retrieval

```python
import anthropic
import sqlite3
import math
import re
import time
from collections import Counter

client = anthropic.Anthropic()
DB = "semantic_memory.db"

def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z]+", text.lower())

def tf_idf_vector(text: str, corpus_df: dict[str, int], total_docs: int) -> dict[str, float]:
    tokens = tokenize(text)
    tf = Counter(tokens)
    vec = {}
    for term, count in tf.items():
        tf_score = count / len(tokens)
        idf_score = math.log((total_docs + 1) / (corpus_df.get(term, 0) + 1))
        vec[term] = tf_score * idf_score
    return vec

def cosine_sparse(a: dict, b: dict) -> float:
    dot = sum(a.get(t, 0) * b.get(t, 0) for t in b)
    na = math.sqrt(sum(v**2 for v in a.values())) or 1
    nb = math.sqrt(sum(v**2 for v in b.values())) or 1
    return dot / (na * nb)

def init_db():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY, text TEXT, ts REAL)")
    con.commit(); con.close()

def store_memory(text: str):
    con = sqlite3.connect(DB)
    con.execute("INSERT INTO memories VALUES (NULL,?,?)", (text, time.time()))
    con.commit(); con.close()

def search_memory(query: str, top_k: int = 3) -> list[tuple[float, str]]:
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT text FROM memories").fetchall()
    con.close()
    if not rows:
        return []

    corpus = [r[0] for r in rows]
    # Build corpus DF
    df: dict[str, int] = {}
    for doc in corpus:
        for term in set(tokenize(doc)):
            df[term] = df.get(term, 0) + 1

    n = len(corpus)
    q_vec = tf_idf_vector(query, df, n)
    scored = []
    for doc in corpus:
        d_vec = tf_idf_vector(doc, df, n)
        scored.append((cosine_sparse(q_vec, d_vec), doc))
    return sorted(scored, reverse=True)[:top_k]

def agent_with_memory(user_input: str) -> str:
    relevant = search_memory(user_input, top_k=3)
    context = "\n".join(f"- {text}" for _, text in relevant if _ > 0.01)
    system = f"You are a helpful assistant.\n\nRelevant context:\n{context}" if context else "You are a helpful assistant."
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=system,
        messages=[{"role": "user", "content": user_input}],
    )
    return resp.content[0].text

init_db()
memories = [
    "Bob prefers TypeScript for all frontend work.",
    "The production server uses 32GB RAM and 16 cores.",
    "We migrated from MySQL to PostgreSQL last quarter.",
    "The deployment pipeline uses GitHub Actions.",
    "Bob joined the company in March 2025.",
]
for m in memories:
    store_memory(m)

queries = ["What database are we using?", "Tell me about Bob", "What's our server config?"]
for q in queries:
    print(f"Q: {q}")
    print(f"A: {agent_with_memory(q)[:80]}\n")

# Expected Token Savings: Top-3 context injection vs full memory = 80% context reduction
# Environment: SQLite; pure Python TF-IDF; upgrade to vector DB for large memory stores
```

## Option 3: Voyage Embeddings with SQLite BLOB Storage

```python
import anthropic
import sqlite3
import struct
import math
import time

# NOTE: This example shows the pattern for real embedding APIs.
# Replace embed_text() with your embedding provider (Voyage AI, OpenAI, etc.)

client = anthropic.Anthropic()
DB = "vector_memory.db"
DIMS = 32  # Use 1024+ for real embeddings

def embed_text(text: str) -> list[float]:
    """
    Production: replace with real embedding call, e.g.:
    import voyageai; vc = voyageai.Client()
    return vc.embed([text], model="voyage-3").embeddings[0]
    """
    import hashlib, random
    # Deterministic pseudo-embedding for demo (not semantically meaningful)
    seed = int(hashlib.sha256(text.lower().encode()).hexdigest(), 16) % (2**32)
    rng = random.Random(seed)
    vec = [rng.gauss(0, 1) for _ in range(DIMS)]
    norm = math.sqrt(sum(x**2 for x in vec)) or 1
    return [x / norm for x in vec]

def vec_to_blob(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)

def blob_to_vec(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))

def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x**2 for x in a)) or 1
    nb = math.sqrt(sum(x**2 for x in b)) or 1
    return dot / (na * nb)

def init_db():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY, text TEXT, embedding BLOB, ts REAL)")
    con.commit(); con.close()

def store(text: str):
    emb = embed_text(text)
    con = sqlite3.connect(DB)
    con.execute("INSERT INTO memories VALUES (NULL,?,?,?)", (text, vec_to_blob(emb), time.time()))
    con.commit(); con.close()

def search(query: str, top_k: int = 3, threshold: float = 0.3) -> list[dict]:
    q_emb = embed_text(query)
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT text, embedding FROM memories").fetchall()
    con.close()
    results = []
    for text, blob in rows:
        emb = blob_to_vec(blob)
        score = cosine(q_emb, emb)
        if score >= threshold:
            results.append({"text": text, "score": score})
    return sorted(results, key=lambda x: -x["score"])[:top_k]

def agent_respond(query: str) -> str:
    hits = search(query, top_k=3)
    context = "\n".join(f"- {h['text']}" for h in hits)
    system = f"You are a helpful assistant.\n\nMemory context:\n{context}" if context else "You are a helpful assistant."
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=system,
        messages=[{"role": "user", "content": query}],
    )
    return resp.content[0].text

init_db()
for m in [
    "Carol uses Go for all microservice development.",
    "Our Kubernetes cluster runs on GKE with autoscaling.",
    "The gRPC timeout is set to 30 seconds.",
    "Carol prefers functional programming patterns.",
    "We use Terraform for infrastructure as code.",
]:
    store(m)

for q in ["What does Carol prefer?", "What is our cloud infrastructure?", "What are our timeout settings?"]:
    print(f"Q: {q}")
    hits = search(q, top_k=2)
    for h in hits:
        print(f"  [{h['score']:.3f}] {h['text']}")
    print(f"A: {agent_respond(q)[:80]}\n")

# Expected Token Savings: Blob storage is compact; semantic search retrieves only relevant memories
# Environment: replace embed_text() with Voyage AI or OpenAI embeddings for real semantic matching
```

## Option 4: Chunked Memory with Sliding Window Retrieval

```python
import anthropic
import math
import re
from dataclasses import dataclass, field

client = anthropic.Anthropic()

def simple_embed(text: str, dims: int = 48) -> list[float]:
    import hashlib
    vec = [0.0] * dims
    for i, c in enumerate(text.lower()):
        vec[ord(c) % dims] += 1 / (i + 1)
    norm = math.sqrt(sum(x**2 for x in vec)) or 1
    return [x / norm for x in vec]

def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))

@dataclass
class ChunkedMemoryStore:
    chunk_size: int = 100  # words per chunk
    overlap: int = 20      # overlap between chunks
    memories: list[dict] = field(default_factory=list)

    def _chunk(self, text: str) -> list[str]:
        words = text.split()
        chunks = []
        i = 0
        while i < len(words):
            chunk = " ".join(words[i:i + self.chunk_size])
            chunks.append(chunk)
            i += self.chunk_size - self.overlap
        return chunks

    def store_document(self, text: str, source: str = ""):
        chunks = self._chunk(text)
        for i, chunk in enumerate(chunks):
            self.memories.append({
                "text": chunk,
                "source": source,
                "chunk_idx": i,
                "total_chunks": len(chunks),
                "embedding": simple_embed(chunk),
            })

    def search(self, query: str, top_k: int = 3) -> list[dict]:
        q_emb = simple_embed(query)
        scored = [{**m, "score": cosine(q_emb, m["embedding"])} for m in self.memories]
        return sorted(scored, key=lambda x: -x["score"])[:top_k]

    def search_with_context(self, query: str, top_k: int = 2) -> str:
        """Return top chunks with surrounding context."""
        hits = self.search(query, top_k)
        context_parts = []
        seen_chunks = set()
        for hit in hits:
            source = hit["source"]
            idx = hit["chunk_idx"]
            # Gather chunk and neighbors
            for m in self.memories:
                if (m["source"] == source and
                        abs(m["chunk_idx"] - idx) <= 1 and
                        (source, m["chunk_idx"]) not in seen_chunks):
                    context_parts.append(m["text"])
                    seen_chunks.add((source, m["chunk_idx"]))
        return "\n\n".join(context_parts)

store = ChunkedMemoryStore(chunk_size=30, overlap=5)

# Store longer documents
store.store_document(
    "Python asyncio is a library for writing concurrent code using the async/await syntax. "
    "It is used as a foundation for multiple Python asynchronous frameworks that provide "
    "high-performance network and web servers, database connection libraries, distributed "
    "task queues, etc. Asyncio is often the right choice for IO-bound and high-level "
    "structured network code. It uses an event loop to run coroutines.",
    source="asyncio_docs"
)
store.store_document(
    "PostgreSQL is a powerful open-source relational database. It supports JSON, "
    "full-text search, and advanced indexing. Our team migrated from MySQL to PostgreSQL "
    "for better performance with complex queries. Connection pooling via PgBouncer "
    "handles up to 10000 concurrent connections.",
    source="db_notes"
)

queries = ["How does asyncio handle concurrency?", "What database do we use for connections?"]
for q in queries:
    context = store.search_with_context(q, top_k=2)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=f"You are a helpful assistant.\n\nContext:\n{context}",
        messages=[{"role": "user", "content": q}],
    )
    print(f"Q: {q}\nA: {resp.content[0].text[:100]}\n")

# Expected Token Savings: Chunk retrieval injects only ~200 tokens vs entire document
# Environment: pure Python; adjust chunk_size to your document length; swap embed for real model
```

## Option 5: Hybrid BM25 + Semantic Re-ranking

```python
import anthropic
import math
import re
from collections import Counter
from dataclasses import dataclass, field

client = anthropic.Anthropic()

def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z]+", text.lower())

def simple_embed(text: str, dims: int = 32) -> list[float]:
    import hashlib
    vec = [0.0] * dims
    for w in tokenize(text):
        h = int(hashlib.md5(w.encode()).hexdigest(), 16) % dims
        vec[h] += 1
    norm = math.sqrt(sum(x**2 for x in vec)) or 1
    return [x / norm for x in vec]

def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))

@dataclass
class HybridMemory:
    memories: list[dict] = field(default_factory=list)
    # BM25 params
    k1: float = 1.5
    b: float = 0.75

    def _avg_dl(self) -> float:
        if not self.memories:
            return 1.0
        return sum(len(tokenize(m["text"])) for m in self.memories) / len(self.memories)

    def _df(self) -> dict[str, int]:
        df: dict[str, int] = {}
        for m in self.memories:
            for term in set(tokenize(m["text"])):
                df[term] = df.get(term, 0) + 1
        return df

    def store(self, text: str):
        self.memories.append({
            "text": text,
            "tokens": tokenize(text),
            "embedding": simple_embed(text),
        })

    def bm25_score(self, query_tokens: list[str], doc_tokens: list[str],
                   df: dict, n: int, avg_dl: float) -> float:
        tf = Counter(doc_tokens)
        dl = len(doc_tokens)
        score = 0.0
        for term in query_tokens:
            if term not in tf:
                continue
            idf = math.log((n - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5) + 1)
            tf_score = tf[term] * (self.k1 + 1) / (tf[term] + self.k1 * (1 - self.b + self.b * dl / avg_dl))
            score += idf * tf_score
        return score

    def search(self, query: str, top_k: int = 3,
               bm25_weight: float = 0.4, semantic_weight: float = 0.6) -> list[dict]:
        if not self.memories:
            return []
        q_tokens = tokenize(query)
        q_emb = simple_embed(query)
        df = self._df()
        n = len(self.memories)
        avg_dl = self._avg_dl()

        results = []
        for m in self.memories:
            bm25 = self.bm25_score(q_tokens, m["tokens"], df, n, avg_dl)
            semantic = cosine(q_emb, m["embedding"])
            # Normalize BM25 to [0,1] approximately
            combined = bm25_weight * min(bm25 / 10, 1.0) + semantic_weight * semantic
            results.append({**m, "score": combined, "bm25": bm25, "semantic": semantic})
        return sorted(results, key=lambda x: -x["score"])[:top_k]

mem = HybridMemory()
for fact in [
    "The service uses Redis for session caching with a 1-hour TTL.",
    "Database migrations run automatically on deployment via Alembic.",
    "The team uses Slack for communication and Notion for documentation.",
    "Error logs are shipped to Datadog with 30-day retention.",
    "The API uses JWT tokens for authentication with a 24-hour expiry.",
    "Python version 3.12 is required for all services.",
]:
    mem.store(fact)

for q in ["How do we handle authentication?", "What caching do we use?", "Where are logs stored?"]:
    results = mem.search(q, top_k=2)
    print(f"Q: {q}")
    for r in results:
        print(f"  [{r['score']:.3f} bm25={r['bm25']:.2f} sem={r['semantic']:.3f}] {r['text'][:70]}")
    print()

# Expected Token Savings: Hybrid search improves recall vs either method alone; fewer irrelevant memories injected
# Environment: pure Python; tune bm25_weight/semantic_weight to your query distribution
```

## Option 6: Time-Decayed Semantic Search with Access Boost

```python
import anthropic
import math
import time
import sqlite3
import struct
from dataclasses import dataclass

client = anthropic.Anthropic()
DB = "decayed_memory.db"

def embed(text: str, dims: int = 32) -> list[float]:
    import hashlib
    vec = [0.0] * dims
    words = text.lower().split()
    for i, w in enumerate(words):
        h = int(hashlib.md5(w.encode()).hexdigest(), 16) % dims
        vec[h] += 1.0 / math.log1p(i + 1)
    norm = math.sqrt(sum(x**2 for x in vec)) or 1
    return [x / norm for x in vec]

def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))

def vec_to_blob(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)

def blob_to_vec(b: bytes) -> list[float]:
    return list(struct.unpack(f"{len(b)//4}f", b))

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY, text TEXT,
            embedding BLOB, importance REAL DEFAULT 0.5,
            created_at REAL, last_accessed REAL DEFAULT 0,
            access_count INTEGER DEFAULT 0
        )
    """)
    con.commit(); con.close()

def store(text: str, importance: float = 0.5):
    now = time.time()
    con = sqlite3.connect(DB)
    con.execute("INSERT INTO memories VALUES (NULL,?,?,?,?,0,0)",
                (text, vec_to_blob(embed(text)), importance, now))
    con.commit(); con.close()

def search_with_decay(query: str, top_k: int = 3,
                       decay_halflife_days: float = 30.0,
                       access_boost: float = 0.1) -> list[dict]:
    q_emb = embed(query)
    now = time.time()
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT id, text, embedding, importance, created_at, last_accessed, access_count FROM memories"
    ).fetchall()
    con.close()

    results = []
    for row in rows:
        mid, text, blob, importance, created_at, last_accessed, access_count = row
        semantic_sim = cosine(q_emb, blob_to_vec(blob))
        # Time decay: recent memories score higher
        age_days = (now - created_at) / 86400
        decay = math.exp(-0.693 * age_days / decay_halflife_days)
        # Access boost: frequently accessed memories stay relevant
        boost = math.log1p(access_count) * access_boost
        final_score = importance * (semantic_sim * 0.5 + decay * 0.3 + boost * 0.2)
        results.append({"id": mid, "text": text, "score": final_score,
                         "semantic": semantic_sim, "decay": decay})

    # Update access metadata for top results
    top = sorted(results, key=lambda x: -x["score"])[:top_k]
    for r in top:
        con = sqlite3.connect(DB)
        con.execute("UPDATE memories SET last_accessed=?, access_count=access_count+1 WHERE id=?",
                    (now, r["id"]))
        con.commit(); con.close()
    return top

def agent_respond(query: str) -> str:
    hits = search_with_decay(query, top_k=3)
    context = "\n".join(f"- {h['text']}" for h in hits if h["score"] > 0.01)
    system = f"You are a helpful assistant.\n\nMemory:\n{context}" if context else "You are a helpful assistant."
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=system,
        messages=[{"role": "user", "content": query}],
    )
    return resp.content[0].text

init_db()
for text, importance in [
    ("Dave prefers Go for system-level services.", 0.9),
    ("The team uses Docker for all containerization.", 0.7),
    ("Dave joined the company in 2023.", 0.6),
    ("PostgreSQL handles all transactional data.", 0.8),
    ("Monthly team sync is every first Monday.", 0.5),
]:
    store(text, importance)

for q in ["What does Dave prefer?", "How do we package services?", "What database do we use?"]:
    answer = agent_respond(q)
    print(f"Q: {q}\nA: {answer[:80]}\n")

# Expected Token Savings: Decayed search auto-demotes stale memories; access boost keeps hot facts prominent
# Environment: SQLite; decay_halflife_days tunable per memory type; access_count boosts frequently-used facts
```

## Comparison

| Option | Embedding Method | Storage | Ranking Factor |
|--------|----------------|---------|---------------|
| 1 — N-gram Cosine | Character n-grams | In-memory | Cosine only |
| 2 — TF-IDF | Term frequency | SQLite text | TF-IDF cosine |
| 3 — BLOB Vector | Real embeddings (pluggable) | SQLite BLOB | Cosine only |
| 4 — Chunked + Context | Word hash | In-memory | Cosine + neighbor expand |
| 5 — BM25 + Semantic | Hash + BM25 | In-memory | Weighted hybrid |
| 6 — Decay + Access Boost | Word hash | SQLite | Semantic + decay + access |
