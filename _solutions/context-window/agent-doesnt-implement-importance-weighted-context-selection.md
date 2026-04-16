---
title: "Agent Doesn't Implement Importance-Weighted Context Selection"
description: "Score each candidate context chunk by relevance, recency, and type — then fill the context window with the highest-scoring chunks rather than naively truncating."
category: context-window
difficulty: intermediate
tags: [context-window, relevance, scoring, retrieval, token-efficiency, ranking]
---

# Agent Doesn't Implement Importance-Weighted Context Selection

## Problem

Agents that simply concatenate all available context until the window fills up waste tokens on low-value information and drop high-value information that doesn't fit. Importance-weighted selection scores every candidate chunk across multiple dimensions and selects the highest-value subset that fits within the token budget.

---

## Option 1: Multi-Signal Scoring with Budget Allocation

```python
import asyncio
import anthropic
import math
import time
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class ContextChunk:
    id: str
    content: str
    chunk_type: str  # "tool_result", "user_message", "assistant_message", "document", "system_note"
    created_at: float = field(default_factory=time.time)
    relevance_score: float = 0.5  # 0-1, set externally
    importance_override: float | None = None  # force include if set high enough

    def token_estimate(self) -> int:
        return len(self.content.split()) * 1.3  # rough tokens

    def age_seconds(self) -> float:
        return time.time() - self.created_at

TYPE_WEIGHTS = {
    "system_note": 1.0,
    "tool_result": 0.85,
    "user_message": 0.80,
    "document": 0.70,
    "assistant_message": 0.60,
}

def score_chunk(chunk: ContextChunk, query: str) -> float:
    if chunk.importance_override is not None:
        return chunk.importance_override

    # Relevance component (externally set, 0-1)
    relevance = chunk.relevance_score

    # Recency decay: half-life of 5 minutes
    age_minutes = chunk.age_seconds() / 60.0
    recency = math.exp(-0.693 * age_minutes / 5.0)

    # Type weight
    type_weight = TYPE_WEIGHTS.get(chunk.chunk_type, 0.5)

    # Simple keyword overlap with query
    query_words = set(query.lower().split())
    chunk_words = set(chunk.content.lower().split())
    overlap = len(query_words & chunk_words) / max(len(query_words), 1)

    return (relevance * 0.40 + recency * 0.25 + type_weight * 0.20 + overlap * 0.15)

def select_context(chunks: list[ContextChunk], query: str, token_budget: int) -> list[ContextChunk]:
    scored = sorted(chunks, key=lambda c: score_chunk(c, query), reverse=True)
    selected: list[ContextChunk] = []
    used_tokens = 0
    for chunk in scored:
        est = chunk.token_estimate()
        if used_tokens + est <= token_budget:
            selected.append(chunk)
            used_tokens += est
    # Re-order chronologically for coherence
    return sorted(selected, key=lambda c: c.created_at)

async def answer_with_weighted_context(query: str, chunks: list[ContextChunk], token_budget: int = 3000) -> str:
    selected = select_context(chunks, query, token_budget)
    context_text = "\n\n".join([f"[{c.chunk_type}|score={score_chunk(c, query):.2f}]\n{c.content}" for c in selected])
    print(f"[CTX SELECT] {len(selected)}/{len(chunks)} chunks, ~{sum(c.token_estimate() for c in selected):.0f} tokens")
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=f"Selected context:\n{context_text}",
        messages=[{"role": "user", "content": query}]
    )
    return resp.content[0].text

async def main():
    chunks = [
        ContextChunk("c1", "Python asyncio uses an event loop to schedule coroutines.", "document", relevance_score=0.9),
        ContextChunk("c2", "The weather today is sunny.", "document", relevance_score=0.1),
        ContextChunk("c3", "asyncio.gather() runs coroutines concurrently.", "tool_result", relevance_score=0.95),
        ContextChunk("c4", "Breakfast was good this morning.", "assistant_message", relevance_score=0.05),
        ContextChunk("c5", "Use asyncio.wait_for() to add timeouts.", "document", relevance_score=0.88),
    ]
    result = await answer_with_weighted_context("How do I run async tasks concurrently?", chunks, token_budget=500)
    print(result[:200])

asyncio.run(main())
```

---

## Option 2: LLM-Scored Relevance Ranking

```python
import asyncio
import anthropic
import json
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class RankedChunk:
    id: str
    content: str
    relevance: float = 0.0  # set by LLM

async def llm_rank_chunks(query: str, chunks: list[RankedChunk]) -> list[RankedChunk]:
    """Use Haiku to score each chunk's relevance to the query."""
    chunk_list = "\n\n".join([f'[{c.id}]: "{c.content[:200]}"' for c in chunks])
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=f'Score each chunk 0.0-1.0 for relevance to the query. Return JSON: {{"scores": {{"id": 0.0, ...}}}}',
        messages=[{"role": "user", "content": f"Query: {query}\n\nChunks:\n{chunk_list}"}]
    )
    try:
        data = json.loads(resp.content[0].text)
        scores = data.get("scores", {})
        for chunk in chunks:
            chunk.relevance = float(scores.get(chunk.id, 0.5))
    except Exception:
        pass
    return sorted(chunks, key=lambda c: -c.relevance)

async def select_and_answer(query: str, chunks: list[RankedChunk], token_budget: int = 2000) -> str:
    ranked = await llm_rank_chunks(query, chunks)
    selected: list[RankedChunk] = []
    used = 0
    for chunk in ranked:
        est = len(chunk.content.split()) * 1.3
        if used + est <= token_budget:
            selected.append(chunk)
            used += est

    for c in selected:
        print(f"[LLM RANK] {c.id}: relevance={c.relevance:.2f}")

    context = "\n\n".join([c.content for c in selected])
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        system=f"Context:\n{context}",
        messages=[{"role": "user", "content": query}]
    )
    return resp.content[0].text
```

---

## Option 3: Marginal Value Selection (Maximum Coverage)

```python
import asyncio
import anthropic
import hashlib
import math
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

def embed_simple(text: str, dim: int = 64) -> list[float]:
    vec = [0.0] * dim
    for word in text.lower().split():
        h = int(hashlib.md5(word.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(x*x for x in vec)) or 1.0
    return [x/norm for x in vec]

def cosine(a: list[float], b: list[float]) -> float:
    return sum(x*y for x,y in zip(a,b))

@dataclass
class CoverageChunk:
    id: str
    content: str
    base_score: float = 0.5  # relevance to query

    def token_est(self) -> int:
        return max(1, int(len(self.content.split()) * 1.3))

def marginal_value_select(chunks: list[CoverageChunk], token_budget: int) -> list[CoverageChunk]:
    """
    Greedy: at each step, pick the chunk with highest marginal value
    given what's already been selected (penalize redundancy).
    """
    selected: list[CoverageChunk] = []
    selected_embs: list[list[float]] = []
    used_tokens = 0
    remaining = list(chunks)

    while remaining and used_tokens < token_budget:
        best_chunk: CoverageChunk | None = None
        best_score = -1.0

        for chunk in remaining:
            if used_tokens + chunk.token_est() > token_budget:
                continue
            emb = embed_simple(chunk.content)
            # Penalty for overlap with already-selected chunks
            if selected_embs:
                max_sim = max(cosine(emb, se) for se in selected_embs)
                novelty = 1.0 - max_sim
            else:
                novelty = 1.0
            marginal = chunk.base_score * 0.6 + novelty * 0.4
            if marginal > best_score:
                best_score = marginal
                best_chunk = chunk

        if best_chunk is None:
            break
        selected.append(best_chunk)
        selected_embs.append(embed_simple(best_chunk.content))
        used_tokens += best_chunk.token_est()
        remaining.remove(best_chunk)

    return selected

async def answer_with_coverage(query: str, chunks: list[CoverageChunk], token_budget: int = 2500) -> str:
    selected = marginal_value_select(chunks, token_budget)
    context = "\n\n".join([c.content for c in selected])
    print(f"[COVERAGE] Selected {len(selected)}/{len(chunks)} chunks (no redundancy)")
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        system=f"Context:\n{context}",
        messages=[{"role": "user", "content": query}]
    )
    return resp.content[0].text
```

---

## Option 4: Type-Budgeted Context Allocation

```python
import asyncio
import anthropic
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

# Allocate specific token budgets per context type
TYPE_BUDGETS = {
    "system_instruction": 500,
    "tool_result": 1500,
    "conversation_history": 800,
    "retrieved_document": 1200,
}

@dataclass
class TypedChunk:
    content: str
    chunk_type: str
    score: float = 0.5  # relevance within type

    def token_est(self) -> int:
        return max(1, int(len(self.content.split()) * 1.3))

def fill_type_bucket(chunks: list[TypedChunk], budget: int) -> list[TypedChunk]:
    sorted_chunks = sorted(chunks, key=lambda c: -c.score)
    selected: list[TypedChunk] = []
    used = 0
    for chunk in sorted_chunks:
        est = chunk.token_est()
        if used + est <= budget:
            selected.append(chunk)
            used += est
    return selected

async def type_budgeted_answer(query: str, all_chunks: list[TypedChunk]) -> str:
    # Group by type
    by_type: dict[str, list[TypedChunk]] = {}
    for chunk in all_chunks:
        by_type.setdefault(chunk.chunk_type, []).append(chunk)

    selected_all: list[TypedChunk] = []
    for ctype, budget in TYPE_BUDGETS.items():
        chunks_of_type = by_type.get(ctype, [])
        selected = fill_type_bucket(chunks_of_type, budget)
        selected_all.extend(selected)
        if selected:
            print(f"[TYPE BUDGET] {ctype}: {len(selected)}/{len(chunks_of_type)} chunks, ~{sum(c.token_est() for c in selected)} tokens")

    context_parts = []
    for ctype in TYPE_BUDGETS:
        chunks = [c for c in selected_all if c.chunk_type == ctype]
        if chunks:
            context_parts.append(f"## {ctype.upper()}\n" + "\n".join(c.content for c in chunks))

    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="\n\n".join(context_parts),
        messages=[{"role": "user", "content": query}]
    )
    return resp.content[0].text
```

---

## Option 5: Recency-Weighted Sliding Window with Pinned Chunks

```python
import asyncio
import anthropic
import time
import math
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class WindowChunk:
    content: str
    created_at: float = field(default_factory=time.time)
    pinned: bool = False   # always include if True
    chunk_type: str = "message"

    def token_est(self) -> int:
        return max(1, int(len(self.content.split()) * 1.3))

    def recency_score(self, half_life_minutes: float = 10.0) -> float:
        age = (time.time() - self.created_at) / 60.0
        return math.exp(-0.693 * age / half_life_minutes)

def recency_window_select(chunks: list[WindowChunk], token_budget: int, half_life_min: float = 10.0) -> list[WindowChunk]:
    pinned = [c for c in chunks if c.pinned]
    unpinned = [c for c in chunks if not c.pinned]

    pinned_tokens = sum(c.token_est() for c in pinned)
    remaining_budget = token_budget - pinned_tokens

    # Sort unpinned by recency score
    scored = sorted(unpinned, key=lambda c: c.recency_score(half_life_min), reverse=True)

    selected: list[WindowChunk] = list(pinned)
    used = pinned_tokens
    for chunk in scored:
        est = chunk.token_est()
        if used + est <= token_budget:
            selected.append(chunk)
            used += est

    return sorted(selected, key=lambda c: c.created_at)  # chronological order

async def windowed_answer(query: str, chunks: list[WindowChunk], token_budget: int = 3000) -> str:
    selected = recency_window_select(chunks, token_budget)
    pinned_count = sum(1 for c in selected if c.pinned)
    print(f"[WINDOW] {len(selected)} chunks ({pinned_count} pinned), ~{sum(c.token_est() for c in selected)} tokens")
    context = "\n\n".join(c.content for c in selected)
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=f"Context:\n{context}",
        messages=[{"role": "user", "content": query}]
    )
    return resp.content[0].text

async def main():
    now = time.time()
    chunks = [
        WindowChunk("SYSTEM: Always respond in English.", time.time(), pinned=True, chunk_type="system"),
        WindowChunk("User asked about Python last session.", now - 3600, chunk_type="history"),
        WindowChunk("asyncio event loop documentation.", now - 300, chunk_type="document"),
        WindowChunk("User: How does asyncio work?", now - 60, chunk_type="message"),
        WindowChunk("Tool result: asyncio.run() starts the event loop.", now - 30, chunk_type="tool_result"),
        WindowChunk("Old note from yesterday about weather.", now - 86400, chunk_type="note"),
    ]
    result = await windowed_answer("What is the asyncio event loop?", chunks, token_budget=400)
    print(result[:200])

asyncio.run(main())
```

---

## Option 6: Dynamic Re-Scoring on Query Change

```python
import asyncio
import anthropic
import hashlib
import math
import time
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

def embed(text: str, dim: int = 80) -> list[float]:
    vec = [0.0] * dim
    for word in text.lower().split():
        h = int(hashlib.sha256(word.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(x*x for x in vec)) or 1.0
    return [x/norm for x in vec]

def cosine(a: list[float], b: list[float]) -> float:
    return sum(x*y for x,y in zip(a,b))

@dataclass
class DynamicChunk:
    id: str
    content: str
    content_emb: list[float] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    access_count: int = 0

    def __post_init__(self):
        if not self.content_emb:
            self.content_emb = embed(self.content)

    def token_est(self) -> int:
        return max(1, int(len(self.content.split()) * 1.3))

class DynamicContextManager:
    def __init__(self, token_budget: int = 3000, recency_weight: float = 0.3, relevance_weight: float = 0.7):
        self.chunks: list[DynamicChunk] = []
        self.token_budget = token_budget
        self.rw = recency_weight
        self.rv = relevance_weight
        self._last_query_emb: list[float] = []

    def add(self, chunk: DynamicChunk):
        self.chunks.append(chunk)

    def select(self, query: str) -> list[DynamicChunk]:
        query_emb = embed(query)
        self._last_query_emb = query_emb

        scored: list[tuple[float, DynamicChunk]] = []
        for chunk in self.chunks:
            relevance = cosine(query_emb, chunk.content_emb)
            age_min = (time.time() - chunk.created_at) / 60.0
            recency = math.exp(-0.693 * age_min / 15.0)
            score = self.rv * relevance + self.rw * recency
            scored.append((score, chunk))

        scored.sort(key=lambda x: -x[0])
        selected: list[DynamicChunk] = []
        used = 0
        for score, chunk in scored:
            if used + chunk.token_est() <= self.token_budget:
                chunk.access_count += 1
                selected.append(chunk)
                used += chunk.token_est()

        return sorted(selected, key=lambda c: c.created_at)

    async def answer(self, query: str) -> str:
        selected = self.select(query)
        print(f"[DYNAMIC] Selected {len(selected)}/{len(self.chunks)} chunks for: {query[:50]}")
        context = "\n\n".join(c.content for c in selected)
        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=f"Context:\n{context}",
            messages=[{"role": "user", "content": query}]
        )
        return resp.content[0].text

async def main():
    mgr = DynamicContextManager(token_budget=500)
    for content in [
        "Python asyncio provides single-threaded concurrency via coroutines.",
        "The event loop schedules and runs coroutines.",
        "JavaScript is also event-loop based but uses callbacks.",
        "asyncio.gather() runs multiple coroutines concurrently.",
        "Rust's async/await uses Futures, not coroutines.",
        "asyncio.Queue is useful for producer-consumer patterns.",
    ]:
        mgr.add(DynamicChunk(id=hashlib.md5(content.encode()).hexdigest()[:6], content=content))

    # Different queries → different context selections
    for query in ["How do I run async tasks concurrently?", "How does asyncio compare to JavaScript?"]:
        result = await mgr.answer(query)
        print(f"Q: {query}\nA: {result[:120]}\n")

asyncio.run(main())
```

---

## Comparison

| Option | Scoring Method | Redundancy Handling | Dynamic Re-scoring | Best For |
|--------|--------------|--------------------|--------------------|----------|
| 1 – Multi-Signal | Weighted formula | None | Per-query keyword overlap | General agents |
| 2 – LLM-Scored | Haiku relevance scorer | None | Yes (per query) | High-accuracy retrieval |
| 3 – Marginal Value | Greedy coverage | Cosine penalty | No | Diverse document sets |
| 4 – Type-Budgeted | Per-type score | None | No | Multi-source context agents |
| 5 – Recency Window | Recency + pinned | None | No | Conversational agents |
| 6 – Dynamic Re-score | Cosine + recency | None | Yes (every query) | Long-lived knowledge bases |

**Recommendation:** Use Option 1 (multi-signal scoring) as your baseline — it requires no extra API calls and handles the most important factors. Add Option 4's type budgeting when you have distinct context sources (tools, documents, history) that should each have a guaranteed allocation. Use Option 3's marginal value approach when your chunks are highly redundant and you need to maximize information coverage.
