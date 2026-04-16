---
layout: solution
title: "Agent Doesn't Implement Semantic Deduplication Before Context Insertion"
category: token-cost
description: "Before inserting a new fact, memory, or context snippet, check whether semantically equivalent content already exists and skip insertion to prevent redundant token waste."
tags: [token-cost, semantic-deduplication, memory, context, embeddings, deduplication, efficiency]
---

# Agent Doesn't Implement Semantic Deduplication Before Context Insertion

## Problem

As agents accumulate context — from tool results, retrieved memories, injected facts, and conversation history — they frequently insert semantically redundant information. "Python was created in 1991" and "Python's first release was in 1991" are distinct strings but convey identical facts. Without semantic deduplication, the context window fills with near-duplicates, wasting tokens and diluting the signal-to-noise ratio of the prompt.

## Solutions

### Option 1: Edit-Distance Deduplication for Near-Identical Strings

Use Levenshtein edit distance to detect near-duplicate strings before inserting them into context.

```python
import anthropic

client = anthropic.Anthropic()


def edit_distance(a: str, b: str) -> int:
    """Standard DP Levenshtein distance."""
    m, n = len(a), len(b)
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev = dp[0]
        dp[0] = i
        for j in range(1, n + 1):
            tmp = dp[j]
            dp[j] = prev if a[i-1] == b[j-1] else 1 + min(dp[j], dp[j-1], prev)
            prev = tmp
    return dp[n]


def similarity_ratio(a: str, b: str) -> float:
    max_len = max(len(a), len(b), 1)
    return 1.0 - edit_distance(a, b) / max_len


class EditDistanceContextStore:
    def __init__(self, threshold: float = 0.85) -> None:
        self.threshold = threshold
        self.entries: list[str] = []
        self.rejected: int = 0

    def try_insert(self, text: str) -> bool:
        """Returns True if inserted, False if duplicate detected."""
        text_norm = text.strip().lower()
        for existing in self.entries:
            if similarity_ratio(text_norm, existing.strip().lower()) >= self.threshold:
                self.rejected += 1
                return False
        self.entries.append(text)
        return True

    def build_context(self) -> str:
        return "\n".join(self.entries)

    def stats(self) -> dict:
        return {
            "total_entries": len(self.entries),
            "rejected_duplicates": self.rejected,
            "token_estimate": sum(len(e) // 4 for e in self.entries),
        }


def chat_with_dedup(facts: list[str], question: str) -> str:
    store = EditDistanceContextStore(threshold=0.82)
    for fact in facts:
        inserted = store.try_insert(fact)
        status = "OK" if inserted else "DUP"
        print(f"  [{status}] {fact[:70]}")

    context = store.build_context()
    print(f"\n  Stats: {store.stats()}")

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=f"Use the following context to answer questions:\n\n{context}",
        messages=[{"role": "user", "content": question}],
    )
    return resp.content[0].text


if __name__ == "__main__":
    facts = [
        "Python was created by Guido van Rossum in 1991.",
        "Python's first release was in 1991 by Guido van Rossum.",   # near-duplicate
        "Python is a high-level, interpreted programming language.",
        "Python is an interpreted, high-level programming language.", # near-duplicate
        "Python supports multiple programming paradigms.",
        "Python is widely used in data science and AI.",
    ]
    answer = chat_with_dedup(facts, "When was Python created and by whom?")
    print(f"\nAnswer: {answer[:150]}")

# Expected Token Savings: 20–40% reduction in context size for fact-dense knowledge bases
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 2: TF-IDF Cosine Similarity Deduplication

Use TF-IDF vectors with cosine similarity for lightweight semantic comparison without embedding API calls.

```python
import anthropic
import math
import re
from collections import Counter

client = anthropic.Anthropic()


def tokenize(text: str) -> list[str]:
    return re.findall(r'\b[a-z]{2,}\b', text.lower())


def tfidf_vector(tokens: list[str], idf: dict[str, float]) -> dict[str, float]:
    tf = Counter(tokens)
    total = len(tokens) or 1
    return {t: (tf[t] / total) * idf.get(t, 1.0) for t in tf}


def cosine_sim(a: dict[str, float], b: dict[str, float]) -> float:
    keys = set(a) & set(b)
    dot  = sum(a[k] * b[k] for k in keys)
    mag_a = math.sqrt(sum(v**2 for v in a.values()))
    mag_b = math.sqrt(sum(v**2 for v in b.values()))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


class TFIDFContextStore:
    def __init__(self, threshold: float = 0.80) -> None:
        self.threshold = threshold
        self.entries: list[str] = []
        self._token_lists: list[list[str]] = []
        self._df: Counter = Counter()
        self.rejected = 0

    def _compute_idf(self) -> dict[str, float]:
        n = len(self._token_lists) + 1
        return {term: math.log(n / (1 + df)) for term, df in self._df.items()}

    def try_insert(self, text: str) -> bool:
        tokens = tokenize(text)
        idf = self._compute_idf()
        vec_new = tfidf_vector(tokens, idf)

        for existing_tokens in self._token_lists:
            vec_ex = tfidf_vector(existing_tokens, idf)
            if cosine_sim(vec_new, vec_ex) >= self.threshold:
                self.rejected += 1
                return False

        self.entries.append(text)
        self._token_lists.append(tokens)
        for t in set(tokens):
            self._df[t] += 1
        return True

    def build_context(self) -> str:
        return "\n\n".join(self.entries)

    def stats(self) -> dict:
        return {
            "unique_entries": len(self.entries),
            "rejected": self.rejected,
            "estimated_tokens": sum(len(e) // 4 for e in self.entries),
        }


def build_context_and_ask(snippets: list[str], question: str) -> str:
    store = TFIDFContextStore(threshold=0.78)
    for snippet in snippets:
        added = store.try_insert(snippet)
        print(f"  [{'ADD' if added else 'DUP'}] {snippet[:70]}")

    print(f"\n  Stats: {store.stats()}")
    context = store.build_context()

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=f"Context:\n{context}",
        messages=[{"role": "user", "content": question}],
    )
    return resp.content[0].text


if __name__ == "__main__":
    snippets = [
        "The transformer architecture uses self-attention mechanisms.",
        "Transformers rely on self-attention to process sequences.",        # semantic duplicate
        "BERT is a bidirectional transformer model for NLP.",
        "GPT uses a decoder-only transformer architecture.",
        "The attention mechanism allows models to weigh token importance.",
        "Self-attention in transformers enables tokens to attend to each other.",  # semantic duplicate
        "Large language models are based on the transformer architecture.",
    ]
    answer = build_context_and_ask(snippets, "What architecture do large language models use?")
    print(f"\nAnswer: {answer[:150]}")

# Expected Token Savings: 25–45% context reduction; no API calls needed for similarity
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 3: LLM-Based Semantic Equivalence Check

Use a small, cheap model to explicitly check whether two statements are semantically equivalent before inserting.

```python
import anthropic
import re

client = anthropic.Anthropic()

EQUIVALENCE_CACHE: dict[tuple[str, str], bool] = {}


def are_semantically_equivalent(a: str, b: str) -> bool:
    key = (min(a, b), max(a, b))
    if key in EQUIVALENCE_CACHE:
        return EQUIVALENCE_CACHE[key]

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=16,
        messages=[{
            "role": "user",
            "content": (
                f'Do these two statements convey the same core information?\n'
                f'A: "{a}"\n'
                f'B: "{b}"\n'
                'Reply with only YES or NO.'
            ),
        }],
    )
    result = "YES" in resp.content[0].text.upper()
    EQUIVALENCE_CACHE[key] = result
    return result


class SemanticContextStore:
    def __init__(self, max_compare: int = 5) -> None:
        """Only compare against the most recent max_compare entries for efficiency."""
        self.entries: list[str] = []
        self.max_compare = max_compare
        self.rejected = 0
        self.llm_calls = 0

    def try_insert(self, text: str) -> bool:
        # compare against recent entries (most likely to be similar)
        recent = self.entries[-self.max_compare:]
        for existing in recent:
            self.llm_calls += 1
            if are_semantically_equivalent(text, existing):
                self.rejected += 1
                return False
        self.entries.append(text)
        return True

    def build_context(self) -> str:
        return "\n\n".join(self.entries)

    def stats(self) -> dict:
        return {
            "unique": len(self.entries),
            "rejected": self.rejected,
            "llm_calls": self.llm_calls,
            "cache_hits": len(EQUIVALENCE_CACHE) - self.llm_calls,
        }


def deduplicated_rag(retrieved_chunks: list[str], question: str) -> str:
    store = SemanticContextStore(max_compare=5)
    for chunk in retrieved_chunks:
        inserted = store.try_insert(chunk)
        print(f"  [{'ADD' if inserted else 'DUP'}] {chunk[:70]}")

    print(f"\n  Stats: {store.stats()}")
    context = store.build_context()

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        system=f"Use the provided context to answer accurately:\n\n{context}",
        messages=[{"role": "user", "content": question}],
    )
    return resp.content[0].text


if __name__ == "__main__":
    chunks = [
        "Einstein developed the theory of general relativity in 1915.",
        "Albert Einstein published his general theory of relativity in 1915.",  # equivalent
        "General relativity describes gravity as spacetime curvature.",
        "According to Einstein's general relativity, gravity is the curvature of spacetime.",  # equivalent
        "Einstein won the Nobel Prize in Physics in 1921 for the photoelectric effect.",
        "The photoelectric effect, explained by Einstein, won him the 1921 Nobel Prize.",    # equivalent
    ]
    answer = deduplicated_rag(chunks, "What is Einstein famous for in physics?")
    print(f"\nAnswer: {answer[:200]}")

# Expected Token Savings: 30–50% on RAG pipelines with overlapping retrieval chunks
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 4: Hash-Based Exact + Fuzzy Two-Pass Deduplication

First check exact hash match (free), then fall back to fuzzy similarity only for near-misses, minimizing comparison work.

```python
import anthropic
import hashlib
import re
from difflib import SequenceMatcher

client = anthropic.Anthropic()


def normalize(text: str) -> str:
    """Normalize for comparison: lowercase, collapse whitespace, strip punctuation."""
    text = text.lower().strip()
    text = re.sub(r'[^\w\s]', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text


def text_hash(text: str) -> str:
    return hashlib.md5(normalize(text).encode()).hexdigest()


def fuzzy_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


class TwoPassDedupStore:
    def __init__(self, fuzzy_threshold: float = 0.80) -> None:
        self.fuzzy_threshold = fuzzy_threshold
        self.entries: list[str] = []
        self._hashes: set[str] = set()
        self.exact_hits  = 0
        self.fuzzy_hits  = 0
        self.insertions  = 0

    def try_insert(self, text: str) -> tuple[bool, str]:
        """Returns (inserted, reason)."""
        # Pass 1: exact hash check (O(1))
        h = text_hash(text)
        if h in self._hashes:
            self.exact_hits += 1
            return False, "exact_duplicate"

        # Pass 2: fuzzy check against recent entries (limit to last 20)
        for existing in self.entries[-20:]:
            if fuzzy_ratio(text, existing) >= self.fuzzy_threshold:
                self.fuzzy_hits += 1
                return False, "fuzzy_duplicate"

        self._hashes.add(h)
        self.entries.append(text)
        self.insertions += 1
        return True, "inserted"

    def build_context(self) -> str:
        return "\n\n".join(self.entries)

    def stats(self) -> dict:
        total = self.insertions + self.exact_hits + self.fuzzy_hits
        return {
            "total_attempted": total,
            "inserted": self.insertions,
            "exact_dups": self.exact_hits,
            "fuzzy_dups": self.fuzzy_hits,
            "dedup_rate": f"{(total - self.insertions) / max(total, 1):.1%}",
        }


def process_retrieved_context(chunks: list[str], question: str) -> str:
    store = TwoPassDedupStore(fuzzy_threshold=0.78)
    for chunk in chunks:
        inserted, reason = store.try_insert(chunk)
        icon = "✓" if inserted else "✗"
        print(f"  [{icon}] ({reason:16s}) {chunk[:65]}")

    print(f"\n  Stats: {store.stats()}")
    context = store.build_context()

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=f"Answer based on context:\n\n{context}",
        messages=[{"role": "user", "content": question}],
    )
    return resp.content[0].text


if __name__ == "__main__":
    chunks = [
        "The speed of light in vacuum is approximately 299,792,458 m/s.",
        "The speed of light in vacuum is approximately 299,792,458 m/s.",   # exact duplicate
        "Light travels at roughly 3 × 10^8 meters per second in vacuum.",   # fuzzy duplicate
        "Nothing can travel faster than the speed of light.",
        "The speed of light is a fundamental constant in physics.",
        "According to physics, nothing can exceed light speed.",             # fuzzy duplicate
        "Einstein's special relativity places light speed as the universal speed limit.",
    ]
    answer = process_retrieved_context(chunks, "What is the speed of light?")
    print(f"\nAnswer: {answer[:150]}")

# Expected Token Savings: Exact check is free; fuzzy only runs when needed — fast and efficient
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 5: Async Embedding-Based Semantic Deduplication

Use the Anthropic messages API to generate mini-embeddings via response comparison, or integrate with a vector store for high-quality semantic matching.

```python
import anthropic
import asyncio
import math
import re
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

SIMILARITY_THRESHOLD = 0.80


def simple_bow_vector(text: str) -> dict[str, float]:
    """Bag-of-words vector (substitute with real embeddings in production)."""
    words = re.findall(r'\b[a-z]{3,}\b', text.lower())
    if not words:
        return {}
    total = len(words)
    freq: dict[str, int] = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1
    return {w: c / total for w, c in freq.items()}


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    keys = set(a) & set(b)
    if not keys:
        return 0.0
    dot   = sum(a[k] * b[k] for k in keys)
    mag_a = math.sqrt(sum(v**2 for v in a.values()))
    mag_b = math.sqrt(sum(v**2 for v in b.values()))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


@dataclass
class AsyncSemanticStore:
    threshold: float = SIMILARITY_THRESHOLD
    entries: list[str] = field(default_factory=list)
    vectors: list[dict[str, float]] = field(default_factory=list)
    rejected: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def try_insert(self, text: str) -> bool:
        vec = simple_bow_vector(text)
        async with self._lock:
            for existing_vec in self.vectors:
                if cosine(vec, existing_vec) >= self.threshold:
                    self.rejected += 1
                    return False
            self.entries.append(text)
            self.vectors.append(vec)
            return True

    def build_context(self, max_entries: int = 20) -> str:
        return "\n\n".join(self.entries[:max_entries])

    def stats(self) -> dict:
        return {
            "unique":   len(self.entries),
            "rejected": self.rejected,
            "tokens":   sum(len(e) // 4 for e in self.entries),
        }


async def parallel_ingest_and_ask(
    chunks: list[str],
    question: str,
    concurrency: int = 4,
) -> str:
    store = AsyncSemanticStore()
    sem = asyncio.Semaphore(concurrency)

    async def ingest(chunk: str) -> None:
        async with sem:
            inserted = await store.try_insert(chunk)
            print(f"  [{'ADD' if inserted else 'DUP'}] {chunk[:65]}")

    await asyncio.gather(*[ingest(c) for c in chunks])
    print(f"\n  Stats: {store.stats()}")

    context = store.build_context()
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=f"Context:\n{context}",
        messages=[{"role": "user", "content": question}],
    )
    return resp.content[0].text


if __name__ == "__main__":
    async def main() -> None:
        chunks = [
            "Machine learning algorithms learn from data without explicit programming.",
            "ML algorithms learn patterns from data automatically.",               # duplicate
            "Deep learning uses neural networks with multiple layers.",
            "Deep neural networks with many layers form the basis of deep learning.", # duplicate
            "Supervised learning requires labeled training data.",
            "In supervised ML, the model trains on labeled examples.",             # duplicate
            "Reinforcement learning trains agents through reward signals.",
            "Unsupervised learning finds patterns in unlabeled data.",
        ]
        answer = await parallel_ingest_and_ask(chunks, "What are the types of machine learning?")
        print(f"\nAnswer: {answer[:200]}")

    asyncio.run(main())

# Expected Token Savings: Async ingestion is fast; semantic matching removes 30–50% of redundant chunks
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 6: Sliding Window Deduplication with Importance-Weighted Retention

When a near-duplicate is detected, keep the higher-importance version (longer, more specific, or more recent) rather than always keeping the first.

```python
import anthropic
import re
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher

client = anthropic.Anthropic()

SIMILARITY_THRESHOLD = 0.75


@dataclass
class ContextEntry:
    text: str
    importance: float   # 0–1, higher = more important
    added_at: float = field(default_factory=time.time)
    source: str = ""

    def token_estimate(self) -> int:
        return len(self.text) // 4

    def specificity_score(self) -> float:
        """Longer, more specific entries score higher."""
        words = len(self.text.split())
        numbers = len(re.findall(r'\d+', self.text))
        return min(1.0, (words / 50) + (numbers * 0.1))


def fuzzy_sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


class ImportanceAwareStore:
    def __init__(self, threshold: float = SIMILARITY_THRESHOLD, max_entries: int = 50) -> None:
        self.threshold = threshold
        self.max_entries = max_entries
        self.entries: list[ContextEntry] = []
        self.replaced = 0
        self.rejected = 0

    def _find_similar(self, text: str) -> int | None:
        """Return index of most similar existing entry, or None."""
        best_idx, best_sim = None, 0.0
        for i, entry in enumerate(self.entries):
            sim = fuzzy_sim(text, entry.text)
            if sim >= self.threshold and sim > best_sim:
                best_sim = sim
                best_idx = i
        return best_idx

    def try_insert(self, entry: ContextEntry) -> tuple[bool, str]:
        idx = self._find_similar(entry.text)
        if idx is not None:
            existing = self.entries[idx]
            new_score = entry.importance + entry.specificity_score()
            old_score = existing.importance + existing.specificity_score()
            if new_score > old_score:
                self.entries[idx] = entry
                self.replaced += 1
                return True, "replaced_inferior"
            else:
                self.rejected += 1
                return False, "kept_superior"

        if len(self.entries) >= self.max_entries:
            # evict lowest importance entry
            self.entries.sort(key=lambda e: e.importance + e.specificity_score())
            self.entries.pop(0)

        self.entries.append(entry)
        return True, "inserted"

    def build_context(self) -> str:
        # sort by importance for best context ordering
        sorted_entries = sorted(self.entries, key=lambda e: e.importance, reverse=True)
        return "\n\n".join(e.text for e in sorted_entries)

    def stats(self) -> dict:
        total_tokens = sum(e.token_estimate() for e in self.entries)
        return {
            "unique_entries":  len(self.entries),
            "rejected":        self.rejected,
            "replaced":        self.replaced,
            "total_tokens":    total_tokens,
        }


def smart_context_build(candidates: list[dict], question: str) -> str:
    store = ImportanceAwareStore(threshold=0.72)
    for item in candidates:
        entry = ContextEntry(
            text=item["text"],
            importance=item.get("importance", 0.5),
            source=item.get("source", ""),
        )
        inserted, reason = store.try_insert(entry)
        icon = "✓" if inserted else "✗"
        print(f"  [{icon}] ({reason:20s}) imp={entry.importance:.1f} | {entry.text[:55]}")

    print(f"\n  Stats: {store.stats()}")
    context = store.build_context()

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=f"Context:\n{context}",
        messages=[{"role": "user", "content": question}],
    )
    return resp.content[0].text


if __name__ == "__main__":
    candidates = [
        {"text": "Python was released in 1991.",                       "importance": 0.5, "source": "wiki"},
        {"text": "Python 1.0 was released on February 20, 1991, by Guido van Rossum.", "importance": 0.9, "source": "docs"},  # more specific
        {"text": "Python supports OOP and functional programming.",     "importance": 0.6, "source": "wiki"},
        {"text": "Python supports object-oriented programming.",        "importance": 0.4, "source": "blog"},  # less specific
        {"text": "Python is used in web, data science, and AI.",        "importance": 0.7, "source": "docs"},
        {"text": "Python is popular in AI and machine learning.",       "importance": 0.5, "source": "blog"},  # duplicate
    ]
    answer = smart_context_build(candidates, "Tell me about Python.")
    print(f"\nAnswer: {answer[:200]}")

# Expected Token Savings: Keeps highest-value version of each fact; replaces low-quality duplicates
# Environment: ANTHROPIC_API_KEY must be set
```

---

## Comparison

| Option | Method | API Cost | Accuracy | Speed | Best For |
|--------|--------|----------|----------|-------|----------|
| 1 | Edit distance | None | Low–Medium | Fast | Short snippets, exact near-duplicates |
| 2 | TF-IDF cosine | None | Medium | Fast | Keyword-rich technical content |
| 3 | LLM equivalence judge | Low (haiku) | High | Slow | High-precision deduplication |
| 4 | Hash + fuzzy two-pass | None | Medium | Very Fast | High-volume RAG pipelines |
| 5 | Async BOW similarity | None | Medium | Fast (parallel) | Concurrent ingestion pipelines |
| 6 | Importance-weighted replacement | None | Medium | Fast | Multi-source RAG with quality signals |
