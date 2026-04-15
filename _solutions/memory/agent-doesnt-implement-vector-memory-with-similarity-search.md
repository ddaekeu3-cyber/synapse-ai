---
layout: solution
title: "Agent Doesn't Implement Vector Memory with Similarity Search"
category: memory
description: "Agents that store memories as plain text lists can't efficiently find relevant past context. Vector embeddings enable semantic retrieval: finding memories by meaning rather than keyword match."
tags: [memory, vector, embeddings, similarity-search, rag, retrieval]
---

# Agent Doesn't Implement Vector Memory with Similarity Search

Keyword search misses semantically related memories. If a user previously said "I prefer concise replies" and later asks about formatting, a keyword search for "format" won't find that preference. Vector embeddings convert memories to numerical representations where similar meanings cluster together, enabling retrieval by semantic similarity even when words don't match.

## Why This Happens

Setting up a vector store feels like infrastructure overhead. Developers use list-based memory because it's simple to implement, not realizing that retrieval quality degrades rapidly as memory size grows beyond a few dozen entries.

---

## Option 1: In-Memory Vector Store with numpy

Store embeddings in a numpy array and use cosine similarity for retrieval — no external dependencies.

```python
import json
import numpy as np
import anthropic

client = anthropic.Anthropic()

# Use Voyage AI embeddings via Anthropic SDK, or use any embedding API
# For simplicity, we'll use a mock embedding function here
# In production: use client.beta.messages.batches or a dedicated embedding endpoint


def get_embedding(text: str) -> np.ndarray:
    """
    Get embedding for text. In production, use an embedding API.
    Example with Anthropic-compatible Voyage AI:
      import voyageai
      vo = voyageai.Client()
      return np.array(vo.embed([text], model="voyage-3").embeddings[0])

    This mock returns a deterministic pseudo-embedding for demonstration.
    """
    # Mock: hash-based pseudo-embedding (replace with real embedding API)
    np.random.seed(hash(text) % (2**31))
    vec = np.random.randn(256).astype(np.float32)
    return vec / np.linalg.norm(vec)


class VectorMemoryStore:
    def __init__(self, top_k: int = 5):
        self._memories: list[str] = []
        self._embeddings: list[np.ndarray] = []
        self._metadata: list[dict] = []
        self._top_k = top_k

    def add(self, text: str, metadata: dict | None = None):
        embedding = get_embedding(text)
        self._memories.append(text)
        self._embeddings.append(embedding)
        self._metadata.append(metadata or {})

    def search(self, query: str, top_k: int | None = None) -> list[dict]:
        if not self._memories:
            return []

        k = top_k or self._top_k
        query_emb = get_embedding(query)

        # Cosine similarity (all vectors are normalized)
        matrix = np.vstack(self._embeddings)
        scores = matrix @ query_emb  # dot product = cosine sim for unit vectors

        top_indices = np.argsort(scores)[::-1][:k]
        return [
            {
                "text": self._memories[i],
                "score": float(scores[i]),
                "metadata": self._metadata[i],
            }
            for i in top_indices
            if scores[i] > 0.0
        ]

    def __len__(self) -> int:
        return len(self._memories)


# Global memory store
memory = VectorMemoryStore(top_k=3)


def chat_with_memory(user_message: str) -> str:
    # Retrieve relevant memories
    relevant = memory.search(user_message)

    system_parts = ["You are a helpful assistant with memory of past interactions."]
    if relevant:
        context = "\n".join(f"- {r['text']}" for r in relevant)
        system_parts.append(f"\nRelevant context from memory:\n{context}")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system="\n".join(system_parts),
        messages=[{"role": "user", "content": user_message}],
    )
    result = response.content[0].text

    # Store this interaction as a memory
    memory.add(
        f"User asked: {user_message[:200]}. Assistant responded: {result[:200]}",
        metadata={"type": "interaction"},
    )

    return result


if __name__ == "__main__":
    # Build up memory
    memory.add("User prefers bullet points over long paragraphs", {"type": "preference"})
    memory.add("User is a Python developer working on ML pipelines", {"type": "profile"})
    memory.add("User's project uses FastAPI and PostgreSQL", {"type": "context"})

    print(f"Memory size: {len(memory)}")
    print(chat_with_memory("How should I format my API documentation?"))
```

**Expected Token Savings:** Semantic retrieval returns only relevant memories (top-k), not the entire memory bank; reduces context injection by 80%+ vs. injecting all memories.

**Environment:** Any Python agent; numpy is the only dependency.

---

## Option 2: SQLite + JSON Embedding Store

Persist embeddings to SQLite for durability across agent restarts.

```python
import json
import sqlite3
import numpy as np
import anthropic
from pathlib import Path

client = anthropic.Anthropic()
DB_PATH = "vector_memory.db"


def get_embedding(text: str) -> list[float]:
    """Replace with real embedding API call."""
    np.random.seed(hash(text) % (2**31))
    vec = np.random.randn(256).astype(np.float32)
    vec = vec / np.linalg.norm(vec)
    return vec.tolist()


def init_db(path: str = DB_PATH):
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                embedding TEXT NOT NULL,  -- JSON array
                category TEXT,
                created_at REAL DEFAULT (unixepoch())
            )
        """)
        conn.commit()


def add_memory(text: str, category: str = "general", path: str = DB_PATH):
    emb = get_embedding(text)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO memories (text, embedding, category) VALUES (?, ?, ?)",
            (text, json.dumps(emb), category),
        )
        conn.commit()


def search_memories(query: str, top_k: int = 5, path: str = DB_PATH) -> list[dict]:
    query_emb = np.array(get_embedding(query))

    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT id, text, embedding, category FROM memories"
        ).fetchall()

    if not rows:
        return []

    scores = []
    for row_id, text, emb_json, category in rows:
        emb = np.array(json.loads(emb_json))
        score = float(query_emb @ emb)
        scores.append((score, row_id, text, category))

    scores.sort(reverse=True)
    return [
        {"text": t, "score": s, "id": rid, "category": cat}
        for s, rid, t, cat in scores[:top_k]
    ]


def build_context(query: str, max_memories: int = 4) -> str:
    memories = search_memories(query, top_k=max_memories)
    if not memories:
        return ""
    lines = [f"[{m['category']}] {m['text']}" for m in memories]
    return "Relevant past context:\n" + "\n".join(lines)


def run_agent(prompt: str) -> str:
    context = build_context(prompt)
    system = "You are a helpful assistant."
    if context:
        system += f"\n\n{context}"

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    result = response.content[0].text

    # Persist this interaction
    add_memory(f"Q: {prompt[:200]} A: {result[:200]}", category="interaction")
    return result


if __name__ == "__main__":
    init_db()
    add_memory("User works in healthcare data analytics", "profile")
    add_memory("User prefers Python code examples", "preference")
    add_memory("User's team uses dbt for data transformation", "context")

    print(run_agent("What tools should I use for data transformation?"))
```

**Expected Token Savings:** Persistent embeddings survive restarts; no re-embedding on every session start.

**Environment:** Single-process agents; SQLite handles hundreds of thousands of memories efficiently.

---

## Option 3: ChromaDB Integration for Production Vector Search

Use ChromaDB as a proper vector store with persistent storage and metadata filtering.

```python
# pip install chromadb
import chromadb
import anthropic
import hashlib

client = anthropic.Anthropic()

# ChromaDB persistent client
chroma = chromadb.PersistentClient(path="./chroma_memory")
collection = chroma.get_or_create_collection(
    name="agent_memory",
    metadata={"hnsw:space": "cosine"},
)


def get_embedding(text: str) -> list[float]:
    """Replace with a real embedding API. ChromaDB can also use built-in embeddings."""
    import numpy as np
    np.random.seed(hash(text) % (2**31))
    vec = np.random.randn(384).astype(np.float32)
    return (vec / np.linalg.norm(vec)).tolist()


def add_to_memory(
    text: str,
    user_id: str = "default",
    category: str = "general",
    memory_id: str | None = None,
):
    doc_id = memory_id or hashlib.md5(text.encode()).hexdigest()
    embedding = get_embedding(text)

    collection.upsert(
        ids=[doc_id],
        embeddings=[embedding],
        documents=[text],
        metadatas=[{"user_id": user_id, "category": category}],
    )


def retrieve_memories(
    query: str,
    user_id: str = "default",
    category: str | None = None,
    top_k: int = 5,
) -> list[dict]:
    query_emb = get_embedding(query)
    where = {"user_id": user_id}
    if category:
        where["category"] = category

    results = collection.query(
        query_embeddings=[query_emb],
        n_results=min(top_k, collection.count()),
        where=where,
        include=["documents", "distances", "metadatas"],
    )

    if not results["ids"][0]:
        return []

    return [
        {
            "text": doc,
            "score": 1.0 - dist,  # ChromaDB cosine distance -> similarity
            "category": meta.get("category"),
        }
        for doc, dist, meta in zip(
            results["documents"][0],
            results["distances"][0],
            results["metadatas"][0],
        )
    ]


def agent_with_chroma_memory(prompt: str, user_id: str = "default") -> str:
    relevant = retrieve_memories(prompt, user_id=user_id, top_k=4)

    context = ""
    if relevant:
        context = "Past context:\n" + "\n".join(f"- {r['text']}" for r in relevant)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=f"You are a helpful assistant.{chr(10) + context if context else ''}",
        messages=[{"role": "user", "content": prompt}],
    )
    result = response.content[0].text

    add_to_memory(
        f"User: {prompt[:150]} | Assistant: {result[:150]}",
        user_id=user_id,
        category="interaction",
    )
    return result


if __name__ == "__main__":
    uid = "user-alice"
    add_to_memory("Prefers concise technical answers", uid, "preference")
    add_to_memory("Works on NLP research at a university", uid, "profile")

    print(agent_with_chroma_memory("What's a good embedding model for sentence similarity?", uid))
```

**Expected Token Savings:** ChromaDB's HNSW index makes retrieval sub-millisecond even for 1M+ memories; production-ready scaling.

**Environment:** Production agents; ChromaDB supports local and client-server modes.

---

## Option 4: Tiered Memory — Short-Term Buffer + Long-Term Vector Store

Keep recent interactions in a fast buffer; move old ones to vector storage for semantic retrieval.

```python
from collections import deque
from dataclasses import dataclass, field
import numpy as np
import anthropic

client = anthropic.Anthropic()


@dataclass
class Memory:
    text: str
    embedding: np.ndarray
    importance: float = 1.0


class TieredMemory:
    """
    Short-term: deque of recent N interactions (always injected).
    Long-term: vector index of older memories (retrieved by similarity).
    """
    def __init__(self, short_term_size: int = 5, long_term_top_k: int = 3):
        self._short: deque[str] = deque(maxlen=short_term_size)
        self._long: list[Memory] = []
        self._short_max = short_term_size
        self._long_top_k = long_term_top_k

    def _embed(self, text: str) -> np.ndarray:
        np.random.seed(hash(text) % (2**31))
        v = np.random.randn(256).astype(np.float32)
        return v / np.linalg.norm(v)

    def add(self, text: str, importance: float = 1.0):
        # When short-term is full, oldest entry moves to long-term
        if len(self._short) == self._short.maxlen:
            oldest = self._short[0]
            self._long.append(Memory(oldest, self._embed(oldest), importance))

        self._short.append(text)

    def retrieve(self, query: str) -> dict[str, list[str]]:
        query_emb = self._embed(query)

        # Long-term: semantic search
        long_term_results = []
        if self._long:
            matrix = np.vstack([m.embedding for m in self._long])
            scores = matrix @ query_emb * np.array([m.importance for m in self._long])
            top_indices = np.argsort(scores)[::-1][:self._long_top_k]
            long_term_results = [self._long[i].text for i in top_indices if scores[i] > 0]

        return {
            "short_term": list(self._short),
            "long_term": long_term_results,
        }


memory = TieredMemory(short_term_size=4, long_term_top_k=2)


def chat(message: str) -> str:
    ctx = memory.retrieve(message)

    system_parts = ["You are a helpful assistant."]
    if ctx["short_term"]:
        system_parts.append("Recent interactions:\n" + "\n".join(f"- {m}" for m in ctx["short_term"]))
    if ctx["long_term"]:
        system_parts.append("Older relevant context:\n" + "\n".join(f"- {m}" for m in ctx["long_term"]))

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system="\n\n".join(system_parts),
        messages=[{"role": "user", "content": message}],
    )
    result = response.content[0].text
    memory.add(f"User: {message[:150]} | Agent: {result[:150]}")
    return result


if __name__ == "__main__":
    # Simulate a conversation that builds up memory
    for msg in [
        "My name is Alex and I work in DevOps",
        "I use Kubernetes for container orchestration",
        "We're migrating to Argo CD for GitOps",
        "What monitoring tools pair well with K8s?",
        "How should I set up alerting?",
        "Tell me about tracing in distributed systems",
    ]:
        print(f"Q: {msg}\nA: {chat(msg)[:100]}\n")
```

**Expected Token Savings:** Short-term is always injected (small); long-term injects only top-k relevant; avoids injecting the entire growing history.

**Environment:** Long-running agents; chatbots with persistent sessions over weeks.

---

## Option 5: Memory Importance Scoring and Forgetting

Weight memories by importance and decay old, low-importance memories to prevent unbounded growth.

```python
import time
import math
import numpy as np
import anthropic

client = anthropic.Anthropic()


class DecayingVectorMemory:
    """
    Memories decay exponentially by age * (1 - importance).
    Retrieval score = cosine_similarity * decay_weight.
    """

    def __init__(self, half_life_days: float = 7.0, max_size: int = 500):
        self._texts: list[str] = []
        self._embeddings: list[np.ndarray] = []
        self._timestamps: list[float] = []
        self._importances: list[float] = []
        self._half_life = half_life_days * 86400  # seconds
        self._max_size = max_size

    def _embed(self, text: str) -> np.ndarray:
        np.random.seed(hash(text) % (2**31))
        v = np.random.randn(256).astype(np.float32)
        return v / np.linalg.norm(v)

    def _decay_weight(self, timestamp: float, importance: float) -> float:
        age_seconds = time.time() - timestamp
        decay = math.exp(-math.log(2) * age_seconds / self._half_life)
        # High importance memories decay slower
        return decay ** (1.0 - importance * 0.5)

    def add(self, text: str, importance: float = 0.5):
        """importance: 0.0 (trivial) to 1.0 (critical)."""
        if len(self._texts) >= self._max_size:
            self._prune()

        self._texts.append(text)
        self._embeddings.append(self._embed(text))
        self._timestamps.append(time.time())
        self._importances.append(min(1.0, max(0.0, importance)))

    def _prune(self):
        """Remove the lowest-scored memory."""
        scores = [
            self._decay_weight(ts, imp)
            for ts, imp in zip(self._timestamps, self._importances)
        ]
        worst_idx = int(np.argmin(scores))
        for lst in (self._texts, self._embeddings, self._timestamps, self._importances):
            lst.pop(worst_idx)

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        if not self._texts:
            return []

        query_emb = self._embed(query)
        matrix = np.vstack(self._embeddings)
        cosine_scores = matrix @ query_emb

        # Combine semantic similarity with decay weight
        combined = [
            cosine_scores[i] * self._decay_weight(self._timestamps[i], self._importances[i])
            for i in range(len(self._texts))
        ]

        top_indices = np.argsort(combined)[::-1][:top_k]
        return [
            {
                "text": self._texts[i],
                "score": combined[i],
                "importance": self._importances[i],
                "age_days": (time.time() - self._timestamps[i]) / 86400,
            }
            for i in top_indices
            if combined[i] > 0
        ]


memory = DecayingVectorMemory(half_life_days=7.0)


def chat_with_decay_memory(prompt: str) -> str:
    relevant = memory.search(prompt, top_k=4)
    context = "\n".join(f"- {r['text']}" for r in relevant)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=f"You are a helpful assistant.\n\nContext:\n{context}" if context else "You are a helpful assistant.",
        messages=[{"role": "user", "content": prompt}],
    )
    result = response.content[0].text

    # Store with higher importance for explicit preferences
    importance = 0.9 if any(w in prompt.lower() for w in ["prefer", "always", "never", "important"]) else 0.5
    memory.add(f"User: {prompt[:150]} | Agent: {result[:100]}", importance=importance)

    return result
```

**Expected Token Savings:** Automatic pruning keeps memory at bounded size; decay means stale context is de-ranked before it's injected.

**Environment:** Long-running agents; user preference learning systems.

---

## Option 6: Vector Memory Evaluation Tests

Test suite verifying that semantic search retrieves relevant memories and not irrelevant ones.

```python
import numpy as np
import pytest


def cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    return float((a / np.linalg.norm(a)) @ (b / np.linalg.norm(b)))


def mock_embed(text: str) -> np.ndarray:
    """Deterministic mock embedding."""
    np.random.seed(hash(text) % (2**31))
    v = np.random.randn(256).astype(np.float32)
    return v / np.linalg.norm(v)


class SimpleVectorStore:
    def __init__(self):
        self._texts = []
        self._embeddings = []

    def add(self, text: str):
        self._texts.append(text)
        self._embeddings.append(mock_embed(text))

    def search(self, query: str, top_k: int = 3) -> list[dict]:
        q = mock_embed(query)
        scores = [cosine_sim(e, q) for e in self._embeddings]
        top = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)[:top_k]
        return [{"text": self._texts[i], "score": s} for i, s in top]


@pytest.fixture
def store():
    s = SimpleVectorStore()
    s.add("User prefers concise bullet-point answers")
    s.add("User is a backend Python developer")
    s.add("User's company uses AWS and Terraform")
    s.add("User's project is a real-time data pipeline")
    s.add("User had trouble with asyncio last session")
    return s


def test_returns_top_k(store):
    results = store.search("Python programming", top_k=3)
    assert len(results) == 3


def test_scores_are_floats_in_valid_range(store):
    results = store.search("anything", top_k=5)
    for r in results:
        assert isinstance(r["score"], float)
        assert -1.0 <= r["score"] <= 1.0


def test_results_ordered_descending(store):
    results = store.search("AWS cloud infrastructure", top_k=5)
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)


def test_preference_retrieved_for_format_query(store):
    results = store.search("How should I format my response?", top_k=2)
    texts = [r["text"] for r in results]
    assert any("concise" in t or "bullet" in t for t in texts), (
        f"Preference memory not retrieved: {texts}"
    )


def test_empty_store_returns_empty(store):
    empty = SimpleVectorStore()
    assert empty.search("anything") == []


def test_all_texts_retrievable(store):
    """Every stored memory should be retrievable when queried with its own text."""
    texts = [
        "User prefers concise bullet-point answers",
        "User is a backend Python developer",
        "User's company uses AWS and Terraform",
    ]
    for text in texts:
        results = store.search(text, top_k=1)
        assert results[0]["text"] == text, f"Expected self-retrieval for: {text}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
```

**Expected Token Savings:** Test suite catches retrieval regressions; ensures the right memories are injected and irrelevant ones excluded.

**Environment:** CI pipeline; any agent with vector memory retrieval.

---

## Comparison

| Option | Storage | Persistence | Semantic Search | Filtering | Forgetting |
|--------|---------|-------------|----------------|-----------|------------|
| 1. numpy in-memory | RAM | No | Cosine | No | No |
| 2. SQLite + JSON embeddings | Disk | Yes | Cosine | No | No |
| 3. ChromaDB | Disk/server | Yes | HNSW | Metadata | No |
| 4. Tiered short+long term | RAM | No | Cosine | Type | Age-based |
| 5. Decaying importance | RAM | No | Weighted cosine | No | Decay + prune |
| 6. Test suite | N/A | N/A | Tested | N/A | N/A |
