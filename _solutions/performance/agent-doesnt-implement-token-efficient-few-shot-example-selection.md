---
title: "Agent Doesn't Implement Token-Efficient Few-Shot Example Selection"
description: "Agents that include a fixed set of few-shot examples in every prompt waste tokens on irrelevant demonstrations: a customer support agent appending the same 10 examples to every request regardless of the query type consumes hundreds of tokens that could be used for actual context. Implement dynamic few-shot selection that retrieves only the most relevant examples for each query using embedding similarity, fitting the maximum number within a token budget."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-token-efficient-few-shot-example-selection
tags: [few-shot, token-efficiency, example-selection, embedding-similarity, dynamic-prompting, context-budget]
symptoms:
  - "Same static few-shot examples appear in every prompt regardless of query type"
  - "Few-shot block consumes 20–40% of total context budget on every request"
  - "Irrelevant examples confuse the model and degrade output quality"
  - "No mechanism to add new examples without increasing token cost for all requests"
  - "Token limit errors caused by examples crowding out actual user context"
---

## Why This Happens

Static few-shot selection is the path of least resistance: write the examples once, prepend them always. The cost compounds at scale — 500 tokens of static examples on 10,000 requests/day is 5 million tokens that could have served actual user context. Dynamic selection requires an example store with precomputed embeddings and a retrieval mechanism that finds the closest examples to the current query. The token savings compound further when examples are ranked by relevance and truncated to fit a budget rather than included wholesale.

## Solution 1: Few-Shot Example Store

```python
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class FewShotExample:
    input_text: str
    output_text: str
    category: str = ""
    quality_score: float = 1.0      # higher = prefer this example
    token_count: Optional[int] = None
    embedding: Optional[List[float]] = None
    example_id: str = ""
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.example_id:
            content = f"{self.input_text}||{self.output_text}"
            self.example_id = hashlib.sha256(content.encode()).hexdigest()[:16]

    def formatted(self, input_prefix: str = "Input", output_prefix: str = "Output") -> str:
        return f"{input_prefix}: {self.input_text}\n{output_prefix}: {self.output_text}"

    def estimated_tokens(self, chars_per_token: float = 4.0) -> int:
        if self.token_count is not None:
            return self.token_count
        return int(len(self.formatted()) / chars_per_token)


class FewShotExampleStore:
    """
    Holds a library of few-shot examples with precomputed embeddings.
    Supports category filtering and relevance-based retrieval.
    """

    def __init__(self):
        self._examples: Dict[str, FewShotExample] = {}

    def add(self, example: FewShotExample) -> None:
        self._examples[example.example_id] = example

    def add_many(self, examples: List[FewShotExample]) -> None:
        for ex in examples:
            self.add(ex)

    def get(self, example_id: str) -> Optional[FewShotExample]:
        return self._examples.get(example_id)

    def by_category(self, category: str) -> List[FewShotExample]:
        return [ex for ex in self._examples.values() if ex.category == category]

    def all(self) -> List[FewShotExample]:
        return list(self._examples.values())

    def count(self) -> int:
        return len(self._examples)
```

## Solution 2: Embedding-Based Example Retriever

```python
import math
from typing import Callable, List, Optional, Tuple


class EmbeddingExampleRetriever:
    """
    Retrieves the most relevant few-shot examples for a query using
    cosine similarity between query embedding and example embeddings.
    Falls back to quality-score ordering when embeddings are unavailable.
    """

    def __init__(self, store: FewShotExampleStore):
        self._store = store

    @staticmethod
    def _cosine(a: List[float], b: List[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def retrieve(
        self,
        query_embedding: Optional[List[float]],
        top_k: int = 5,
        category: Optional[str] = None,
        min_quality: float = 0.0,
    ) -> List[Tuple[FewShotExample, float]]:
        """
        Returns list of (example, score) sorted by descending relevance.
        Score is cosine similarity when embeddings are available, else quality_score.
        """
        candidates = (
            self._store.by_category(category)
            if category
            else self._store.all()
        )
        candidates = [ex for ex in candidates if ex.quality_score >= min_quality]

        if query_embedding is not None:
            scored = [
                (ex, self._cosine(query_embedding, ex.embedding))
                for ex in candidates
                if ex.embedding is not None
            ]
            unembedded = [
                (ex, ex.quality_score * 0.5)  # penalize unembedded
                for ex in candidates
                if ex.embedding is None
            ]
            scored = sorted(scored + unembedded, key=lambda x: x[1], reverse=True)
        else:
            scored = sorted(candidates, key=lambda ex: ex.quality_score, reverse=True)
            scored = [(ex, ex.quality_score) for ex in scored]

        return scored[:top_k]
```

## Solution 3: Token-Budget-Aware Example Selector

```python
from typing import List, Optional, Tuple


class TokenBudgetExampleSelector:
    """
    Selects the maximum number of examples that fit within a token budget,
    prioritized by relevance score. Guarantees the budget is never exceeded.
    """

    def __init__(
        self,
        retriever: EmbeddingExampleRetriever,
        token_budget: int = 800,
        min_examples: int = 1,
        max_examples: int = 8,
        overhead_tokens: int = 20,   # prompt formatting overhead per example
    ):
        self._retriever = retriever
        self._budget = token_budget
        self._min = min_examples
        self._max = max_examples
        self._overhead = overhead_tokens

    def select(
        self,
        query_embedding: Optional[List[float]],
        category: Optional[str] = None,
        available_budget: Optional[int] = None,
    ) -> List[Tuple[FewShotExample, float]]:
        budget = available_budget if available_budget is not None else self._budget
        candidates = self._retriever.retrieve(
            query_embedding=query_embedding,
            top_k=self._max * 2,   # over-fetch, then trim to budget
            category=category,
        )

        selected = []
        tokens_used = 0
        for example, score in candidates:
            cost = example.estimated_tokens() + self._overhead
            if tokens_used + cost <= budget:
                selected.append((example, score))
                tokens_used += cost
            if len(selected) >= self._max:
                break

        # Ensure minimum even if it slightly exceeds budget
        if len(selected) < self._min and candidates:
            for example, score in candidates:
                if example not in [e for e, _ in selected]:
                    selected.append((example, score))
                if len(selected) >= self._min:
                    break

        return selected

    def tokens_used(self, selected: List[Tuple[FewShotExample, float]]) -> int:
        return sum(ex.estimated_tokens() + self._overhead for ex, _ in selected)
```

## Solution 4: Dynamic Few-Shot Prompt Builder

```python
from typing import Callable, List, Optional


class DynamicFewShotPromptBuilder:
    """
    Builds a prompt section from dynamically selected few-shot examples.
    Supports async embedding of the query and custom formatting templates.
    """

    def __init__(
        self,
        selector: TokenBudgetExampleSelector,
        input_prefix: str = "Input",
        output_prefix: str = "Output",
        section_header: str = "Examples:",
    ):
        self._selector = selector
        self._input_prefix = input_prefix
        self._output_prefix = output_prefix
        self._header = section_header

    async def build(
        self,
        query: str,
        embed_fn: Callable[[str], List[float]],
        category: Optional[str] = None,
        available_budget: Optional[int] = None,
    ) -> dict:
        query_embedding = await embed_fn(query)
        selected = self._selector.select(
            query_embedding=query_embedding,
            category=category,
            available_budget=available_budget,
        )

        if not selected:
            return {
                "prompt_block": "",
                "example_count": 0,
                "tokens_used": 0,
                "scores": [],
            }

        lines = [self._header]
        for example, _ in selected:
            lines.append(example.formatted(self._input_prefix, self._output_prefix))
        prompt_block = "\n\n".join(lines)

        return {
            "prompt_block": prompt_block,
            "example_count": len(selected),
            "tokens_used": self._selector.tokens_used(selected),
            "scores": [round(score, 4) for _, score in selected],
        }
```

## Solution 5: Example Store Embedding Indexer

```python
import asyncio
from typing import Callable, List


class ExampleStoreEmbeddingIndexer:
    """
    Pre-computes embeddings for all examples in the store that lack them.
    Run at startup or when new examples are added to keep the store current.
    """

    def __init__(
        self,
        store: FewShotExampleStore,
        embed_fn: Callable[[str], List[float]],
        batch_size: int = 20,
    ):
        self._store = store
        self._embed_fn = embed_fn
        self._batch_size = batch_size
        self._indexed_count = 0

    async def index_missing(self) -> dict:
        unindexed = [ex for ex in self._store.all() if ex.embedding is None]
        if not unindexed:
            return {"indexed": 0, "total": self._store.count()}

        batches = [
            unindexed[i:i + self._batch_size]
            for i in range(0, len(unindexed), self._batch_size)
        ]

        for batch in batches:
            tasks = [self._embed_fn(ex.input_text) for ex in batch]
            embeddings = await asyncio.gather(*tasks)
            for ex, emb in zip(batch, embeddings):
                ex.embedding = emb
                self._indexed_count += 1

        return {
            "indexed": len(unindexed),
            "total": self._store.count(),
            "total_indexed_ever": self._indexed_count,
        }
```

## Solution 6: Few-Shot Selection Dashboard

```python
import time
from typing import List


class FewShotSelectionDashboard:
    """
    Tracks selection statistics over time to measure token savings
    versus a static baseline and identify the most frequently selected examples.
    """

    def __init__(
        self,
        static_baseline_tokens: int,
        store: FewShotExampleStore,
    ):
        self._baseline = static_baseline_tokens
        self._store = store
        self._selection_records: List[dict] = []

    def record(self, build_result: dict) -> None:
        self._selection_records.append({
            "ts": time.time(),
            "example_count": build_result["example_count"],
            "tokens_used": build_result["tokens_used"],
            "tokens_saved": self._baseline - build_result["tokens_used"],
            "top_score": build_result["scores"][0] if build_result["scores"] else 0.0,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._selection_records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "requests": 0}

        total_saved = sum(r["tokens_saved"] for r in recent)
        avg_tokens = sum(r["tokens_used"] for r in recent) / len(recent)
        avg_examples = sum(r["example_count"] for r in recent) / len(recent)

        return {
            "window_seconds": window_seconds,
            "requests": len(recent),
            "total_tokens_saved": total_saved,
            "avg_tokens_per_request": round(avg_tokens, 1),
            "avg_examples_selected": round(avg_examples, 2),
            "avg_top_similarity": round(
                sum(r["top_score"] for r in recent) / len(recent), 4
            ),
            "store_size": self._store.count(),
        }
```

## Comparison

| Approach | Embedding Retrieval | Budget Enforcement | Dynamic Selection | Token Savings | Indexing |
|---|---|---|---|---|---|
| FewShotExampleStore | No | No | No | No | No |
| EmbeddingExampleRetriever | Yes (cosine) | No | Yes | No | No |
| TokenBudgetExampleSelector | Via retriever | Yes | Yes | Yes | No |
| DynamicFewShotPromptBuilder | Via selector | Via selector | Yes | Via selector | No |
| ExampleStoreEmbeddingIndexer | No | No | No | No | Yes |
| FewShotSelectionDashboard | No | No | No | Yes (tracked) | No |

**Best for production**: Set `token_budget` to 15–20% of the model's context window — few-shot examples should never dominate the context. Use `ExampleStoreEmbeddingIndexer` at startup to pre-index all examples so query-time latency is only one embed call (the query) not N+1. Set `min_quality=0.7` in `EmbeddingExampleRetriever` to exclude low-quality examples from selection regardless of similarity — a highly similar but poorly written example is worse than a moderately similar excellent one. Monitor `avg_top_similarity` in the dashboard: if it drops below 0.6 consistently, the example library needs more coverage for the query distribution being served.
