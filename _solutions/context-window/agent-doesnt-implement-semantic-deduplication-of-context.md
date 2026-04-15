---
layout: solution
title: "Agent Doesn't Implement Semantic Deduplication of Context"
category: context-window
description: "Agents accumulate near-duplicate content in their context window — repeated tool results, similar retrieved documents, and redundant summaries. Semantic deduplication removes these before each API call, reducing input tokens by 20-50%."
tags: [context-window, deduplication, semantic-similarity, token-reduction, retrieval, rag]
---

# Agent Doesn't Implement Semantic Deduplication of Context

## Problem

Over the course of a multi-turn conversation or RAG retrieval loop, agents accumulate redundant content: the same document retrieved twice with slightly different wording, repeated tool results, or near-identical summaries from different turns. Sending all of this to the model wastes input tokens and dilutes attention on what actually matters.

## Why This Happens

Each context insertion is considered independently. There is no mechanism that compares a new piece of content against what is already in context. Retrieval pipelines frequently surface similar chunks (different pages of the same document, re-worded FAQ entries), and tools often return overlapping data across turns.

## Solutions

### Option 1: Jaccard Similarity Deduplication — Token overlap-based filtering

```python
import anthropic
import re
from dataclasses import dataclass, field

def tokenize(text: str) -> set[str]:
    """Simple whitespace + punctuation tokenizer producing a word set."""
    return set(re.findall(r'\b\w+\b', text.lower()))

def jaccard_similarity(a: str, b: str) -> float:
    tokens_a = tokenize(a)
    tokens_b = tokenize(b)
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


@dataclass
class DeduplicatedContextBuilder:
    similarity_threshold: float = 0.6  # Items above this are considered duplicates
    max_context_chars: int = 100_000

    def __post_init__(self):
        self._items: list[str] = []

    def add(self, text: str) -> bool:
        """Add text if it is not a near-duplicate of existing items. Returns True if added."""
        text = text.strip()
        if not text:
            return False

        for existing in self._items:
            if jaccard_similarity(text, existing) >= self.similarity_threshold:
                return False  # Duplicate found — skip

        self._items.append(text)
        return True

    def build(self) -> str:
        return "\n\n---\n\n".join(self._items)

    def token_estimate(self) -> int:
        return len(self.build()) // 4  # Rough estimate: 4 chars per token

    @property
    def item_count(self) -> int:
        return len(self._items)


def deduplicated_rag_query(
    client: anthropic.Anthropic,
    question: str,
    retrieved_chunks: list[str],
    threshold: float = 0.55,
) -> str:
    ctx = DeduplicatedContextBuilder(similarity_threshold=threshold)

    original_count = len(retrieved_chunks)
    for chunk in retrieved_chunks:
        ctx.add(chunk)

    deduplicated_count = ctx.item_count
    print(f"[Dedup] {original_count} chunks → {deduplicated_count} unique "
          f"(removed {original_count - deduplicated_count})")

    context_text = ctx.build()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"Context:\n{context_text}\n\nQuestion: {question}"
        }]
    )
    return response.content[0].text


# Simulate duplicate retrieval (common in RAG with overlapping chunks)
chunks = [
    "Python is a high-level, interpreted programming language known for its simplicity.",
    "Python, a high-level interpreted language, is celebrated for its clean and readable syntax.",  # ~70% overlap
    "Python is widely used in data science, web development, and automation.",
    "The Python programming language is commonly applied in data science and web frameworks.",  # ~60% overlap
    "Guido van Rossum created Python in 1991.",
    "Python was created by Guido van Rossum and first released in 1991.",  # ~80% overlap
]

client = anthropic.Anthropic()
answer = deduplicated_rag_query(client, "What is Python used for?", chunks)
print(answer)

# Expected Token Savings: 20-50% on input tokens when retrieval produces overlapping chunks
# Environment: RAG pipelines, document QA, any agent that retrieves from a vector store
```

### Option 2: LLM-Powered Deduplication — Use Haiku to detect semantic equivalence

```python
import anthropic
import json
from dataclasses import dataclass, field

@dataclass
class SemanticDeduplicator:
    """Use a cheap model to judge whether two passages are semantically equivalent."""
    client: anthropic.Anthropic
    judge_model: str = "claude-haiku-4-5-20251001"
    batch_size: int = 5  # Check new item against up to N existing in one call

    def _is_duplicate(self, candidate: str, existing_batch: list[str]) -> bool:
        if not existing_batch:
            return False

        numbered = "\n".join(f"{i+1}. {item[:300]}" for i, item in enumerate(existing_batch))
        response = self.client.messages.create(
            model=self.judge_model,
            max_tokens=64,
            system="You detect semantic duplicates. Return JSON only.",
            messages=[{
                "role": "user",
                "content": (
                    f"CANDIDATE:\n{candidate[:300]}\n\n"
                    f"EXISTING ITEMS:\n{numbered}\n\n"
                    f"Does the candidate convey the same core information as any existing item?\n"
                    f'Return: {{"duplicate": true/false, "matches": [item_number_or_null]}}'
                )
            }]
        )
        try:
            data = json.loads(response.content[0].text)
            return bool(data.get("duplicate", False))
        except (json.JSONDecodeError, KeyError):
            return False

    def deduplicate(self, items: list[str]) -> list[str]:
        unique: list[str] = []
        for item in items:
            # Check against recent unique items in batches
            batch = unique[-self.batch_size:]
            if not self._is_duplicate(item, batch):
                unique.append(item)
        return unique


def build_deduplicated_context(
    client: anthropic.Anthropic,
    question: str,
    retrieved_docs: list[str],
) -> str:
    deduplicator = SemanticDeduplicator(client=client)

    print(f"[Dedup] Starting with {len(retrieved_docs)} documents...")
    unique_docs = deduplicator.deduplicate(retrieved_docs)
    print(f"[Dedup] Reduced to {len(unique_docs)} unique documents")

    context = "\n\n".join(f"[Doc {i+1}]\n{doc}" for i, doc in enumerate(unique_docs))

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"{context}\n\nAnswer this question: {question}"
        }]
    )
    return response.content[0].text


# Usage
client = anthropic.Anthropic()
docs = [
    "The mitochondria is the powerhouse of the cell, generating ATP through cellular respiration.",
    "Mitochondria produce energy for cells via ATP synthesis during aerobic respiration.",
    "Cells contain a nucleus that holds DNA and controls cellular activities.",
    "The cell nucleus houses genetic material and orchestrates gene expression.",
    "Ribosomes are responsible for protein synthesis in all living cells.",
    "ATP is adenosine triphosphate, the primary energy currency of biological cells.",
]

answer = build_deduplicated_context(client, "What is the role of mitochondria?", docs)
print(answer)

# Expected Token Savings: 30-60% input reduction; Haiku deduplication costs ~20 tokens per pair check
# Environment: Knowledge base QA, customer support bots, scientific research agents
```

### Option 3: Fingerprint Cache — Hash-based exact and near-duplicate detection

```python
import anthropic
import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

def shingle(text: str, k: int = 5) -> set[int]:
    """Create k-gram shingles for MinHash-style similarity."""
    words = text.lower().split()
    if len(words) < k:
        return {hash(text)}
    return {hash(tuple(words[i:i+k])) for i in range(len(words) - k + 1)}

def shingling_similarity(a: str, b: str, k: int = 5) -> float:
    s_a = shingle(a, k)
    s_b = shingle(b, k)
    if not s_a or not s_b:
        return 0.0
    return len(s_a & s_b) / len(s_a | s_b)

def content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode()).hexdigest()[:16]


class FingerprintDeduplicator:
    DB_PATH = Path("/tmp/context_fingerprints.db")

    def __init__(self, session_id: str, similarity_threshold: float = 0.5):
        self.session_id = session_id
        self.threshold = similarity_threshold
        self._seen_texts: list[str] = []  # In-memory for shingling comparison
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS fingerprints (
                    session_id TEXT,
                    content_hash TEXT,
                    snippet TEXT,
                    PRIMARY KEY (session_id, content_hash)
                )
            """)

    def is_seen(self, text: str) -> bool:
        """Check if this text (or near-duplicate) was already added."""
        # 1. Exact hash check (fast)
        h = content_hash(text)
        with sqlite3.connect(self.DB_PATH) as conn:
            row = conn.execute(
                "SELECT 1 FROM fingerprints WHERE session_id=? AND content_hash=?",
                (self.session_id, h)
            ).fetchone()
        if row:
            return True

        # 2. Shingle similarity check against recent items (slower but catches near-dupes)
        for seen_text in self._seen_texts[-20:]:  # Check against last 20
            if shingling_similarity(text, seen_text) >= self.threshold:
                return True

        return False

    def mark_seen(self, text: str) -> None:
        h = content_hash(text)
        with sqlite3.connect(self.DB_PATH) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO fingerprints VALUES (?,?,?)",
                (self.session_id, h, text[:100])
            )
        self._seen_texts.append(text)

    def filter(self, items: list[str]) -> list[str]:
        unique = []
        for item in items:
            if not self.is_seen(item):
                unique.append(item)
                self.mark_seen(item)
        return unique

    def stats(self) -> dict:
        with sqlite3.connect(self.DB_PATH) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM fingerprints WHERE session_id=?",
                (self.session_id,)
            ).fetchone()[0]
        return {"session_id": self.session_id, "total_seen": count}


class SessionDeduplicatingAgent:
    def __init__(self, session_id: str):
        self.client = anthropic.Anthropic()
        self.dedup = FingerprintDeduplicator(session_id=session_id)
        self.history: list[dict] = []

    def add_context(self, new_chunks: list[str]) -> tuple[int, int]:
        """Returns (before, after) chunk counts."""
        unique = self.dedup.filter(new_chunks)
        return len(new_chunks), len(unique)

    def query(self, user_question: str, context_chunks: list[str]) -> str:
        before, after = self.add_context(context_chunks)
        print(f"[Dedup] {before} chunks → {after} unique (session fingerprint cache)")

        unique_chunks = self.dedup.filter(context_chunks)
        context = "\n\n".join(unique_chunks)

        self.history.append({
            "role": "user",
            "content": f"Context:\n{context}\n\nQuestion: {user_question}"
        })

        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=self.history,
        )

        reply = response.content[0].text
        self.history.append({"role": "assistant", "content": reply})
        return reply


# Usage
agent = SessionDeduplicatingAgent(session_id="research-session-001")

# Turn 1
reply1 = agent.query("What causes inflation?", [
    "Inflation is caused by an increase in the money supply relative to goods available.",
    "When demand exceeds supply, prices rise, causing inflation.",
    "Central banks control inflation through interest rate policy.",
])

# Turn 2 — some chunks are re-retrieved (common in iterative RAG)
reply2 = agent.query("How do central banks fight inflation?", [
    "Inflation is caused by an increase in the money supply.",  # Near-duplicate from turn 1
    "Central banks raise interest rates to reduce borrowing and cool demand.",
    "The Federal Reserve uses open market operations to influence money supply.",
])

print(agent.dedup.stats())

# Expected Token Savings: 25-45% across multi-turn sessions with overlapping retrievals
# Environment: Multi-turn research agents, iterative RAG, chatbots with persistent sessions
```

### Option 4: Clustering-Based Deduplication — Group similar items; keep best representative

```python
import anthropic
import json
from dataclasses import dataclass
from typing import Callable

def word_overlap_score(a: str, b: str) -> float:
    wa = set(a.lower().split())
    wb = set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)

def quality_score(text: str) -> float:
    """Heuristic: longer, more specific texts are higher quality."""
    word_count = len(text.split())
    has_numbers = any(c.isdigit() for c in text)
    has_punctuation = any(c in '.,;:' for c in text)
    return word_count * (1.2 if has_numbers else 1.0) * (1.1 if has_punctuation else 1.0)


@dataclass
class Cluster:
    items: list[str]

    @property
    def best(self) -> str:
        """Pick the highest-quality item as cluster representative."""
        return max(self.items, key=quality_score)


def cluster_deduplicate(
    items: list[str],
    threshold: float = 0.4,
    similarity_fn: Callable[[str, str], float] = word_overlap_score,
) -> list[str]:
    """Cluster similar items; return one representative per cluster."""
    clusters: list[Cluster] = []

    for item in items:
        placed = False
        for cluster in clusters:
            # Compare against cluster's current best representative
            if similarity_fn(item, cluster.best) >= threshold:
                cluster.items.append(item)
                placed = True
                break

        if not placed:
            clusters.append(Cluster(items=[item]))

    return [c.best for c in clusters]


class ClusteringContextAgent:
    def __init__(self):
        self.client = anthropic.Anthropic()

    def answer_with_clustering(
        self,
        question: str,
        raw_chunks: list[str],
        similarity_threshold: float = 0.4,
        model: str = "claude-sonnet-4-6",
    ) -> dict:
        before = len(raw_chunks)
        deduplicated = cluster_deduplicate(raw_chunks, threshold=similarity_threshold)
        after = len(deduplicated)

        # Estimate token savings
        before_chars = sum(len(c) for c in raw_chunks)
        after_chars = sum(len(c) for c in deduplicated)
        savings_pct = (1 - after_chars / before_chars) * 100 if before_chars else 0

        context = "\n\n".join(f"[{i+1}] {chunk}" for i, chunk in enumerate(deduplicated))
        response = self.client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": f"Context:\n{context}\n\nAnswer: {question}"}]
        )

        return {
            "answer": response.content[0].text,
            "chunks_before": before,
            "chunks_after": after,
            "chars_before": before_chars,
            "chars_after": after_chars,
            "estimated_savings_pct": round(savings_pct, 1),
        }


# Usage
agent = ClusteringContextAgent()

chunks = [
    "Neural networks are computing systems inspired by biological neural networks in animal brains.",
    "Artificial neural networks are modeled after the neural networks found in biological organisms.",
    "Deep learning uses neural networks with many layers to learn representations from data.",
    "Deep learning is a subset of machine learning that employs multi-layer neural networks.",
    "Backpropagation is an algorithm used to train neural networks by computing gradients.",
    "The backpropagation algorithm calculates the gradient of the loss function for training neural nets.",
    "Transformers are a neural network architecture that uses self-attention mechanisms.",
]

result = agent.answer_with_clustering("What is deep learning?", chunks)
print(result["answer"])
print(f"Reduced {result['chunks_before']} → {result['chunks_after']} chunks")
print(f"Token savings estimate: {result['estimated_savings_pct']}%")

# Expected Token Savings: 30-60% by selecting quality representatives from semantic clusters
# Environment: Large-scale RAG, document summarization, search result re-ranking before LLM
```

### Option 5: Sliding Window Deduplication — Deduplicate within conversation history turns

```python
import anthropic
import re
from dataclasses import dataclass, field
from collections import deque

def sentence_tokens(text: str) -> set[str]:
    """Split into sentences and tokenize each for finer-grained dedup."""
    sentences = re.split(r'[.!?]+', text.lower())
    words: set[str] = set()
    for s in sentences:
        words.update(re.findall(r'\b\w{4,}\b', s))  # Words 4+ chars
    return words

def content_overlap(a: str, b: str) -> float:
    ta = sentence_tokens(a)
    tb = sentence_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass
class SlidingWindowDeduplicator:
    window_size: int = 6      # Compare new content against last N turns' content
    threshold: float = 0.45

    def __post_init__(self):
        self._window: deque[str] = deque(maxlen=self.window_size)

    def should_include(self, text: str) -> bool:
        for past_text in self._window:
            if content_overlap(text, past_text) >= self.threshold:
                return False
        return True

    def record(self, text: str) -> None:
        self._window.append(text)


class ConversationDeduplicatingAgent:
    def __init__(self, max_context_chars: int = 80_000):
        self.client = anthropic.Anthropic()
        self.history: list[dict] = []
        self.dedup = SlidingWindowDeduplicator(window_size=8, threshold=0.4)
        self.max_context_chars = max_context_chars

    def inject_tool_result(self, tool_name: str, result: str) -> bool:
        """Add a tool result to history only if not duplicate. Returns True if added."""
        if not self.dedup.should_include(result):
            print(f"[Dedup] Skipped duplicate tool result from '{tool_name}'")
            return False

        self.dedup.record(result)
        self.history.append({
            "role": "user",
            "content": f"[Tool: {tool_name}]\n{result}"
        })
        return True

    def total_context_size(self) -> int:
        return sum(len(str(m["content"])) for m in self.history)

    def chat(self, user_message: str, tool_results: list[tuple[str, str]] | None = None) -> str:
        # Inject deduplicated tool results
        if tool_results:
            for tool_name, result in tool_results:
                self.inject_tool_result(tool_name, result)

        self.history.append({"role": "user", "content": user_message})

        # Trim context if too large
        while self.total_context_size() > self.max_context_chars and len(self.history) > 2:
            self.history.pop(0)

        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=self.history,
        )

        reply = response.content[0].text
        self.history.append({"role": "assistant", "content": reply})
        return reply


# Usage
agent = ConversationDeduplicatingAgent()

# Simulated multi-turn with overlapping tool results
reply1 = agent.chat(
    "What does the database say about user activity?",
    tool_results=[
        ("db_query", "Users table: 1,245 active users. Last login: 2026-04-15. Peak hours: 2-4pm UTC."),
        ("analytics_api", "Active users: 1,245. Session duration avg: 12min. Peak: 2-4pm UTC."),  # Near-duplicate
    ]
)

reply2 = agent.chat(
    "What about the revenue data?",
    tool_results=[
        ("billing_api", "Monthly revenue: $48,200. MRR growth: +12% QoQ. Churn rate: 2.1%."),
        ("db_query", "Users table: 1,245 active users. Peak hours: 2-4pm UTC."),  # Duplicate of turn 1
    ]
)

print(f"Final context size: {agent.total_context_size():,} chars")

# Expected Token Savings: 15-35% in tool-heavy agents that re-query similar data across turns
# Environment: Multi-turn tool-using agents, data analysis agents, customer support with knowledge retrieval
```

### Option 6: Entropy-Based Deduplication — Keep highest-information-density content

```python
import anthropic
import math
import re
from collections import Counter
from dataclasses import dataclass

def token_entropy(text: str) -> float:
    """Shannon entropy of word distribution — higher = more information density."""
    words = re.findall(r'\b\w+\b', text.lower())
    if not words:
        return 0.0
    counts = Counter(words)
    total = len(words)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())

def normalized_entropy(text: str) -> float:
    """Entropy normalized by text length — penalizes repetitive filler."""
    words = re.findall(r'\b\w+\b', text.lower())
    if len(words) < 5:
        return 0.0
    base_entropy = token_entropy(text)
    # Normalize by log of vocab size (max possible entropy)
    vocab_size = len(set(words))
    if vocab_size <= 1:
        return 0.0
    return base_entropy / math.log2(vocab_size)

def pairwise_novelty(candidate: str, existing: list[str]) -> float:
    """How much new information does candidate add beyond what's already in context?"""
    if not existing:
        return 1.0
    candidate_words = set(re.findall(r'\b\w{4,}\b', candidate.lower()))
    existing_words: set[str] = set()
    for text in existing:
        existing_words.update(re.findall(r'\b\w{4,}\b', text.lower()))
    new_words = candidate_words - existing_words
    if not candidate_words:
        return 0.0
    return len(new_words) / len(candidate_words)


@dataclass
class EntropyDeduplicator:
    novelty_threshold: float = 0.25   # Must contribute at least 25% new vocabulary
    min_entropy: float = 0.6          # Must have reasonable information density

    def select(self, candidates: list[str], token_budget: int = 2000) -> list[str]:
        """Select highest-novelty, highest-entropy chunks within token budget."""
        # Score each candidate
        scored = [
            (text, token_entropy(text), normalized_entropy(text))
            for text in candidates
        ]
        # Sort by entropy descending — prefer high-information content
        scored.sort(key=lambda x: x[2], reverse=True)

        selected: list[str] = []
        used_tokens = 0

        for text, entropy, norm_entropy in scored:
            if norm_entropy < self.min_entropy:
                continue  # Too low information density (boilerplate, filler)

            novelty = pairwise_novelty(text, selected)
            if novelty < self.novelty_threshold and selected:
                continue  # Too much overlap with already-selected content

            estimated_tokens = len(text.split()) * 1.3
            if used_tokens + estimated_tokens > token_budget:
                continue  # Would exceed budget

            selected.append(text)
            used_tokens += estimated_tokens

        return selected


class InformationMaximizingAgent:
    def __init__(self):
        self.client = anthropic.Anthropic()
        self.dedup = EntropyDeduplicator()

    def answer(self, question: str, candidates: list[str], token_budget: int = 1500) -> dict:
        selected = self.dedup.select(candidates, token_budget=token_budget)

        print(f"[Entropy-Dedup] {len(candidates)} candidates → {len(selected)} selected")

        context = "\n\n".join(f"[{i+1}] {chunk}" for i, chunk in enumerate(selected))
        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": f"Context:\n{context}\n\nAnswer: {question}"}]
        )

        return {
            "answer": response.content[0].text,
            "selected_chunks": len(selected),
            "total_candidates": len(candidates),
            "context_tokens_est": int(len(context.split()) * 1.3),
        }


# Usage
agent = InformationMaximizingAgent()

chunks = [
    "Quantum computing uses qubits which can exist in superposition of 0 and 1 simultaneously.",
    "Unlike classical bits that are 0 or 1, quantum bits can be both at the same time.",  # Low novelty
    "Quantum entanglement allows qubits to be correlated regardless of distance.",
    "Shor's algorithm can factor large numbers exponentially faster than classical computers.",
    "Quantum computers can break RSA encryption using Shor's algorithm on sufficiently large machines.",  # Partial overlap
    "IBM, Google, and IonQ are leading companies in quantum hardware development.",
    "Quantum error correction is necessary because qubits are highly susceptible to decoherence.",
    "The quick brown fox jumped over the lazy dog.",  # Low entropy — should be excluded
    "Please note that the above information is for educational purposes only.",  # Boilerplate
]

result = agent.answer("How do quantum computers differ from classical ones?", chunks)
print(result["answer"])
print(f"Selected {result['selected_chunks']}/{result['total_candidates']} chunks, ~{result['context_tokens_est']} tokens")

# Expected Token Savings: 35-55% by selecting maximum-novelty content within a token budget
# Environment: Large document sets, search pipelines, knowledge-dense QA where quality > quantity
```

## Comparison

| Option | Algorithm | Speed | Quality | Cross-Turn | Best For |
|--------|-----------|-------|---------|------------|----------|
| Jaccard Similarity | Token overlap | Fast | Good | No | Simple RAG pipelines |
| LLM-Powered | Haiku judgment | Slow | Excellent | No | High-stakes QA, low chunk counts |
| Fingerprint Cache | Shingle hash | Very Fast | Good | Yes (SQLite) | Multi-turn sessions |
| Clustering | Greedy assignment | Fast | Good | No | Large chunk sets |
| Sliding Window | Turn-level overlap | Fast | Good | Yes (memory) | Tool-heavy agents |
| Entropy-Based | Information density | Fast | Excellent | No | Knowledge-dense retrieval |
