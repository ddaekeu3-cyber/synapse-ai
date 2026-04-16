---
layout: solution
title: "Agent Doesn't Implement Selective Context Retrieval with Relevance Threshold"
category: context-window
description: "Instead of injecting all available context, score each candidate chunk against the current query and only include chunks that exceed a relevance threshold—reducing token waste and improving signal-to-noise ratio."
tags: [context-window, retrieval, relevance-scoring, rag, token-efficiency]
---

# Agent Doesn't Implement Selective Context Retrieval with Relevance Threshold

## Problem

Agents that inject all available context (full conversation history, all retrieved documents, complete tool results) waste tokens on irrelevant content, dilute the signal for the model, and hit context limits unnecessarily. Selective retrieval includes only what actually matters for the current turn.

## Solution Options

### Option 1: Keyword Overlap Relevance Scorer

```python
import anthropic
import re
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class ContextChunk:
    id: str
    text: str
    source: str
    relevance_score: float = 0.0

def score_relevance_keyword(query: str, chunk: str, boost_exact: float = 2.0) -> float:
    """Score by keyword overlap between query and chunk."""
    query_words = set(re.findall(r'\b[a-z]{3,}\b', query.lower()))
    chunk_words = set(re.findall(r'\b[a-z]{3,}\b', chunk.lower()))

    stopwords = {'the', 'and', 'for', 'are', 'but', 'not', 'you', 'all', 'can',
                  'has', 'her', 'was', 'one', 'our', 'out', 'day', 'get', 'use'}
    query_words -= stopwords
    chunk_words -= stopwords

    if not query_words:
        return 0.0

    overlap = query_words & chunk_words
    base_score = len(overlap) / len(query_words)

    # Boost for exact phrase match
    if any(w in chunk.lower() for w in query.lower().split() if len(w) > 4):
        base_score *= boost_exact

    return min(1.0, base_score)

def select_context(query: str, chunks: list[ContextChunk],
                   threshold: float = 0.3, max_chunks: int = 3) -> list[ContextChunk]:
    for chunk in chunks:
        chunk.relevance_score = score_relevance_keyword(query, chunk.text)

    selected = [c for c in sorted(chunks, key=lambda x: x.relevance_score, reverse=True)
                if c.relevance_score >= threshold][:max_chunks]
    print(f"Selected {len(selected)}/{len(chunks)} chunks (threshold={threshold})")
    return selected

# Sample knowledge base
knowledge_base = [
    ContextChunk("c1", "Redis is an in-memory data structure store used as a database, cache, and message broker.", "docs"),
    ContextChunk("c2", "Kafka is a distributed event streaming platform used for high-throughput data pipelines.", "docs"),
    ContextChunk("c3", "Redis supports strings, hashes, lists, sets, sorted sets, bitmaps, and hyperloglogs.", "docs"),
    ContextChunk("c4", "PostgreSQL is a relational database with ACID compliance and advanced indexing.", "docs"),
    ContextChunk("c5", "Redis persistence can be achieved via RDB snapshots or AOF (Append Only File) logging.", "docs"),
]

query = "How does Redis handle data persistence?"
selected = select_context(query, knowledge_base, threshold=0.25)
context_text = "\n\n".join(f"[{c.source}:{c.id}] {c.text}" for c in selected)

resp = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=256,
    messages=[{
        "role": "user",
        "content": f"Context:\n{context_text}\n\nQuestion: {query}"
    }]
)
print(f"\nAnswer: {resp.content[0].text[:150]}")

# Expected Token Savings: filtering 5 chunks to 2 relevant ones saves ~40% context tokens
# Environment: RAG systems, knowledge base Q&A, document retrieval pipelines
```

### Option 2: LLM-Based Relevance Judge with Threshold Gate

```python
import anthropic
import json
import re
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class ScoredChunk:
    id: str
    text: str
    relevance: float
    reasoning: str

def llm_score_chunk(query: str, chunk: str, chunk_id: str) -> ScoredChunk:
    """Use a fast model to judge relevance before paying for full context injection."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{
            "role": "user",
            "content": f"""Rate the relevance of this context chunk to the query.

Query: {query}
Chunk: {chunk[:300]}

JSON: {{"relevance": 0.0-1.0, "reason": "one word"}}"""
        }]
    )
    text = resp.content[0].text
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            return ScoredChunk(
                id=chunk_id,
                text=chunk,
                relevance=float(data.get("relevance", 0.5)),
                reasoning=data.get("reason", "")
            )
        except (json.JSONDecodeError, ValueError):
            pass
    return ScoredChunk(id=chunk_id, text=chunk, relevance=0.5, reasoning="parse_error")

def build_selective_context(query: str, chunks: dict[str, str],
                             threshold: float = 0.6, max_tokens: int = 2000) -> str:
    scored = [llm_score_chunk(query, text, cid) for cid, text in chunks.items()]
    scored.sort(key=lambda x: x.relevance, reverse=True)

    selected = []
    total_tokens = 0
    for chunk in scored:
        if chunk.relevance < threshold:
            break
        chunk_tokens = len(chunk.text.split()) * 1.3  # rough estimate
        if total_tokens + chunk_tokens > max_tokens:
            break
        selected.append(chunk)
        total_tokens += chunk_tokens

    print(f"Scored {len(scored)} chunks | Selected {len(selected)} above threshold={threshold}")
    for c in scored:
        mark = "✓" if c in selected else "✗"
        print(f"  {mark} [{c.id}] relevance={c.relevance:.2f} ({c.reasoning})")

    return "\n\n".join(f"[{c.id}] {c.text}" for c in selected)

chunks = {
    "redis_overview": "Redis is an in-memory data store supporting strings, hashes, lists, sets.",
    "redis_persistence": "Redis offers RDB snapshots and AOF logging for persistence. AOF logs every write operation.",
    "kafka_overview": "Apache Kafka is a distributed streaming platform for building data pipelines.",
    "redis_eviction": "Redis supports LRU, LFU, and TTL-based eviction policies for memory management.",
    "postgres_joins": "PostgreSQL supports INNER JOIN, LEFT JOIN, and FULL OUTER JOIN with query planning."
}

query = "What are Redis persistence options and how do they differ?"
context = build_selective_context(query, chunks, threshold=0.55)

resp = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=256,
    messages=[{"role": "user", "content": f"Context:\n{context}\n\nQuestion: {query}"}]
)
print(f"\nAnswer: {resp.content[0].text[:200]}")

# Expected Token Savings: LLM judge costs ~20 tokens per chunk; saves 3-5x more on context injection
# Environment: high-quality RAG systems, precision-critical Q&A, domain-specific assistants
```

### Option 3: TF-IDF Style Relevance with Inverted Index

```python
import anthropic
import re
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class TextCorpus:
    documents: dict[str, str] = field(default_factory=dict)
    idf: dict[str, float] = field(default_factory=dict)
    _built: bool = False

    def add(self, doc_id: str, text: str) -> None:
        self.documents[doc_id] = text
        self._built = False

    def build_index(self) -> None:
        N = len(self.documents)
        df: Counter = Counter()
        for text in self.documents.values():
            words = set(re.findall(r'\b[a-z]{3,}\b', text.lower()))
            df.update(words)
        self.idf = {w: math.log(N / (1 + df[w])) for w in df}
        self._built = True

    def tfidf_score(self, query: str, doc_id: str) -> float:
        if not self._built:
            self.build_index()
        doc = self.documents.get(doc_id, "")
        query_words = re.findall(r'\b[a-z]{3,}\b', query.lower())
        doc_words = re.findall(r'\b[a-z]{3,}\b', doc.lower())
        if not doc_words or not query_words:
            return 0.0
        doc_tf = Counter(doc_words)
        doc_len = len(doc_words)
        score = 0.0
        for qw in query_words:
            tf = doc_tf.get(qw, 0) / doc_len
            idf = self.idf.get(qw, 0)
            score += tf * idf
        return score

    def retrieve(self, query: str, threshold: float = 0.001, top_k: int = 4) -> list[tuple[str, float]]:
        scores = [(doc_id, self.tfidf_score(query, doc_id)) for doc_id in self.documents]
        scores.sort(key=lambda x: x[1], reverse=True)
        return [(doc_id, score) for doc_id, score in scores if score >= threshold][:top_k]

corpus = TextCorpus()
corpus.add("doc_redis_basics", "Redis is an in-memory key-value store. It supports various data structures including strings, lists, sets, and hashes.")
corpus.add("doc_redis_perf", "Redis performance comes from in-memory operations. Read and write operations complete in microseconds. Redis can handle millions of requests per second.")
corpus.add("doc_redis_persistence", "Redis offers two persistence mechanisms: RDB creates point-in-time snapshots. AOF logs every write command. Both can be used together for better durability.")
corpus.add("doc_kafka_basics", "Kafka is a distributed log. Producers write messages to topics. Consumers read from partitions. Kafka provides strong ordering guarantees within partitions.")
corpus.add("doc_postgres_indexes", "PostgreSQL supports B-tree, hash, GIN, and GiST indexes. B-tree indexes work well for equality and range queries.")

query = "How does Redis achieve high performance?"
results = corpus.retrieve(query, threshold=0.001, top_k=3)
print(f"Query: {query}")
for doc_id, score in results:
    print(f"  [{score:.4f}] {doc_id}")

context = "\n\n".join(corpus.documents[doc_id] for doc_id, _ in results)
resp = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=256,
    messages=[{"role": "user", "content": f"Context:\n{context}\n\nQuestion: {query}"}]
)
print(f"\nAnswer: {resp.content[0].text[:180]}")

# Expected Token Savings: TF-IDF index lookup is O(|query|); no extra model calls needed
# Environment: large knowledge bases, offline-indexed corpora, latency-sensitive retrieval
```

### Option 4: Recency + Relevance Combined Scorer for Conversation History

```python
import anthropic
import re
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class HistoryEntry:
    role: str
    content: str
    timestamp: float = field(default_factory=time.time)
    turn_index: int = 0

def keyword_overlap_score(query: str, text: str) -> float:
    q_words = set(re.findall(r'\b[a-z]{4,}\b', query.lower()))
    t_words = set(re.findall(r'\b[a-z]{4,}\b', text.lower()))
    if not q_words:
        return 0.0
    return len(q_words & t_words) / len(q_words)

def recency_score(entry: HistoryEntry, total_turns: int) -> float:
    """More recent turns get higher scores (0.2 to 1.0)."""
    if total_turns <= 1:
        return 1.0
    return 0.2 + 0.8 * (entry.turn_index / (total_turns - 1))

def combined_score(query: str, entry: HistoryEntry, total_turns: int,
                   recency_weight: float = 0.3, relevance_weight: float = 0.7) -> float:
    rec = recency_score(entry, total_turns)
    rel = keyword_overlap_score(query, entry.content)
    return recency_weight * rec + relevance_weight * rel

def select_history_context(query: str, history: list[HistoryEntry],
                            threshold: float = 0.2, max_entries: int = 6,
                            always_include_last_n: int = 2) -> list[HistoryEntry]:
    total = len(history)
    scored = [(entry, combined_score(query, entry, total)) for entry in history]

    # Always include the last N turns
    always_include = set(id(e) for e in history[-always_include_last_n:])
    selected = [e for e in history if id(e) in always_include]
    remaining_slots = max_entries - len(selected)

    # Fill remaining slots by relevance+recency score
    scored_remaining = [(e, s) for e, s in scored if id(e) not in always_include]
    scored_remaining.sort(key=lambda x: x[1], reverse=True)
    for entry, score in scored_remaining[:remaining_slots]:
        if score >= threshold:
            selected.append(entry)

    # Sort by turn_index to maintain chronological order
    selected.sort(key=lambda e: e.turn_index)
    print(f"Selected {len(selected)}/{total} history turns (threshold={threshold})")
    return selected

# Simulate a long conversation
history = []
conversation_turns = [
    ("user", "What is Redis?"),
    ("assistant", "Redis is an in-memory key-value store used for caching and data storage."),
    ("user", "What databases support ACID transactions?"),
    ("assistant", "PostgreSQL, MySQL, and Oracle support full ACID compliance."),
    ("user", "Can Redis persist data to disk?"),
    ("assistant", "Yes, Redis supports RDB snapshots and AOF logging for persistence."),
    ("user", "What is the difference between SQL and NoSQL?"),
    ("assistant", "SQL databases use structured schemas; NoSQL databases are schema-flexible."),
    ("user", "How fast is Redis compared to disk-based databases?"),
    ("assistant", "Redis operations typically complete in under a millisecond due to in-memory storage."),
]

for i, (role, content) in enumerate(conversation_turns):
    history.append(HistoryEntry(role=role, content=content, turn_index=i))

# Current query
current_query = "What are the performance characteristics of Redis?"
selected = select_history_context(current_query, history, threshold=0.15, max_entries=5)

messages = [{"role": e.role, "content": e.content} for e in selected]
messages.append({"role": "user", "content": current_query})

resp = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=256,
    messages=messages
)
print(f"\nAnswer: {resp.content[0].text[:180]}")

# Expected Token Savings: selecting 5 of 10 history entries saves ~50% history tokens
# Environment: long-running assistants, multi-topic conversations, context-window-constrained agents
```

### Option 5: Semantic Chunking with Threshold-Based Window Expansion

```python
import anthropic
import re
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class SemanticChunk:
    id: str
    text: str
    topic_keywords: frozenset
    char_count: int

def extract_keywords(text: str, top_n: int = 8) -> frozenset:
    from collections import Counter
    words = re.findall(r'\b[a-z]{4,}\b', text.lower())
    stopwords = {'that', 'this', 'with', 'from', 'have', 'will', 'been', 'were',
                 'they', 'their', 'what', 'when', 'where', 'which', 'each', 'also',
                 'into', 'over', 'some', 'time', 'very', 'just', 'more', 'then'}
    filtered = [w for w in words if w not in stopwords]
    top = {w for w, _ in Counter(filtered).most_common(top_n)}
    return frozenset(top)

def semantic_overlap(query_kw: frozenset, chunk_kw: frozenset) -> float:
    if not query_kw or not chunk_kw:
        return 0.0
    return len(query_kw & chunk_kw) / len(query_kw | chunk_kw)

def expand_to_neighbors(selected_ids: set[str], all_chunks: list[SemanticChunk],
                          window: int = 1) -> list[SemanticChunk]:
    """Expand selection to include neighboring chunks for context continuity."""
    id_to_idx = {c.id: i for i, c in enumerate(all_chunks)}
    expanded_indices = set()
    for sid in selected_ids:
        idx = id_to_idx.get(sid)
        if idx is not None:
            for offset in range(-window, window + 1):
                if 0 <= idx + offset < len(all_chunks):
                    expanded_indices.add(idx + offset)
    return [all_chunks[i] for i in sorted(expanded_indices)]

def selective_retrieve(query: str, chunks: list[SemanticChunk],
                        threshold: float = 0.2, expand_window: int = 0,
                        token_budget: int = 1500) -> list[SemanticChunk]:
    query_kw = extract_keywords(query)
    scored = [(c, semantic_overlap(query_kw, c.topic_keywords)) for c in chunks]
    scored.sort(key=lambda x: x[1], reverse=True)

    selected_ids = {c.id for c, score in scored if score >= threshold}
    if expand_window > 0:
        selected = expand_to_neighbors(selected_ids, chunks, expand_window)
    else:
        selected = [c for c, score in scored if score >= threshold]

    # Enforce token budget
    result = []
    used_chars = 0
    for chunk in selected:
        if used_chars + chunk.char_count > token_budget * 4:  # ~4 chars/token
            break
        result.append(chunk)
        used_chars += chunk.char_count

    print(f"Retrieved {len(result)}/{len(chunks)} chunks (threshold={threshold}, budget={token_budget}t)")
    return result

# Build corpus
raw_sections = [
    ("s1", "Redis Architecture: Redis uses a single-threaded event loop for command processing. This avoids locking overhead."),
    ("s2", "Redis Memory: All data is stored in RAM. Redis uses different encoding strategies to optimize memory per data type."),
    ("s3", "Redis Performance: Redis can process millions of operations per second. Latency is typically under 1 millisecond."),
    ("s4", "Redis Clustering: Redis Cluster provides horizontal scaling through hash slot partitioning across multiple nodes."),
    ("s5", "Kafka Architecture: Kafka uses a distributed log structure. Topics are divided into partitions stored on brokers."),
    ("s6", "Kafka Performance: Kafka achieves high throughput via sequential disk I/O and zero-copy data transfer."),
    ("s7", "PostgreSQL Indexes: B-tree indexes support equality, range, and prefix lookups efficiently in PostgreSQL."),
]

chunks = [SemanticChunk(id=sid, text=text, topic_keywords=extract_keywords(text), char_count=len(text))
          for sid, text in raw_sections]

query = "How does Redis achieve low latency and high throughput?"
selected = selective_retrieve(query, chunks, threshold=0.15, token_budget=800)
context = "\n\n".join(c.text for c in selected)

resp = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=256,
    messages=[{"role": "user", "content": f"Context:\n{context}\n\nQuestion: {query}"}]
)
print(f"\nAnswer: {resp.content[0].text[:200]}")

# Expected Token Savings: semantic chunking + budget cap saves 40-70% vs injecting all sections
# Environment: technical documentation Q&A, API reference agents, structured knowledge retrieval
```

### Option 6: Adaptive Threshold with Fallback Expansion

```python
import anthropic
import re
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class RetrievalResult:
    chunks: list[dict]
    threshold_used: float
    strategy: str
    total_tokens_estimated: int

def keyword_score(query: str, text: str) -> float:
    q = set(re.findall(r'\b[a-z]{4,}\b', query.lower()))
    t = set(re.findall(r'\b[a-z]{4,}\b', text.lower()))
    if not q:
        return 0.0
    return len(q & t) / len(q)

def adaptive_retrieve(query: str, corpus: list[dict],
                       initial_threshold: float = 0.5,
                       min_threshold: float = 0.1,
                       min_chunks: int = 1,
                       max_chunks: int = 4) -> RetrievalResult:
    """
    Start with high threshold. If too few chunks pass, lower threshold
    until min_chunks are found or min_threshold is reached.
    """
    scored = [(doc, keyword_score(query, doc["text"])) for doc in corpus]
    scored.sort(key=lambda x: x[1], reverse=True)

    threshold = initial_threshold
    strategy = "initial"

    while threshold >= min_threshold:
        selected = [(doc, score) for doc, score in scored if score >= threshold]
        if len(selected) >= min_chunks:
            break
        threshold = max(min_threshold, threshold - 0.1)
        strategy = f"lowered_to_{threshold:.1f}"

    # Cap at max_chunks
    final = [doc for doc, _ in selected[:max_chunks]]
    total_chars = sum(len(doc["text"]) for doc in final)

    return RetrievalResult(
        chunks=final,
        threshold_used=threshold,
        strategy=strategy,
        total_tokens_estimated=total_chars // 4
    )

corpus = [
    {"id": "c1", "text": "Redis eviction policies: LRU removes least recently used keys. LFU removes least frequently used."},
    {"id": "c2", "text": "Redis memory optimization: Use appropriate data types. Hash encoding compresses small hashes."},
    {"id": "c3", "text": "Kafka consumer groups allow multiple consumers to read from a topic in parallel."},
    {"id": "c4", "text": "PostgreSQL VACUUM reclaims storage from dead tuples and updates visibility maps."},
    {"id": "c5", "text": "Redis maxmemory policy controls behavior when memory limit is reached."},
]

queries = [
    "How does Redis handle running out of memory?",  # clear match
    "What is the best database for analytics?",       # no clear match — should lower threshold
]

for query in queries:
    result = adaptive_retrieve(query, corpus)
    context = "\n".join(c["text"] for c in result.chunks)
    print(f"\nQuery: {query}")
    print(f"Strategy: {result.strategy}, threshold={result.threshold_used:.1f}, {len(result.chunks)} chunks (~{result.total_tokens_estimated}t)")

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": f"Context (may be limited):\n{context}\n\nQuestion: {query}"}]
    )
    print(f"Answer: {resp.content[0].text[:120]}")

# Expected Token Savings: high threshold filters 60-80% of irrelevant chunks; fallback prevents empty context
# Environment: general-purpose RAG, variable-topic Q&A, robustness-critical retrieval
```

## Comparison

| Option | Scoring Method | Extra LLM Calls | Handles No-Match | Best For |
|--------|---------------|-----------------|------------------|----------|
| 1 | Keyword overlap | No | No | Simple fast retrieval |
| 2 | LLM judge | Yes (haiku) | No | High-precision Q&A |
| 3 | TF-IDF index | No | No | Large indexed corpora |
| 4 | Recency + relevance | No | No | Conversation history |
| 5 | Semantic keyword + budget | No | No | Structured documentation |
| 6 | Adaptive threshold | No | Yes (fallback) | Robust general retrieval |
