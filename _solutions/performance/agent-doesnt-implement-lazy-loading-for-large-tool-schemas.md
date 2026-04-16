---
title: "Agent Doesn't Implement Lazy Loading for Large Tool Schemas"
description: "Agents that include all tool schemas in every LLM request waste tokens on tools that are never relevant to the current query — an agent with 50 tools sends all 50 schemas regardless of context, consuming 2000–5000 prompt tokens per request. Implement lazy loading that selects only the tools relevant to the current query using embedding similarity, dynamically assembles the tool subset for each request, and caches schema embeddings to avoid recomputation."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-lazy-loading-for-large-tool-schemas
tags: [lazy-loading, tool-selection, dynamic-tools, token-efficiency, schema-pruning, relevant-tools]
symptoms:
  - "Every LLM request includes 50 tool schemas consuming 4000 tokens regardless of the query"
  - "Context window fills up with tool schemas leaving little room for conversation history"
  - "Simple queries that need one tool still pay for 49 irrelevant tool schemas in the prompt"
  - "Adding more tools increases base cost per request even for queries that use none of them"
  - "No mechanism to dynamically select which tools are relevant to the current user intent"
---

## Why This Happens

Tool schemas are defined once and included in every LLM request. With 50 tools each averaging 80 tokens, every request pays 4000 tokens just for tool definitions — even when the user's query only requires one tool. Lazy loading selects a relevant subset by embedding the query and retrieving only the top-K most similar tool descriptions. The tool schema cache ensures embedding comparisons use pre-computed vectors rather than re-embedding all schemas on every request.

## Solution 1: Tool Schema Record

```python
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolSchemaRecord:
    tool_name: str
    description: str
    parameters_schema: Dict[str, Any]
    category: str = ""
    keywords: List[str] = field(default_factory=list)
    always_include: bool = False   # if True, always included regardless of relevance
    embedding: Optional[List[float]] = None
    token_count: int = 0

    def to_llm_format(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.tool_name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }

    def searchable_text(self) -> str:
        parts = [self.description]
        if self.keywords:
            parts.append(" ".join(self.keywords))
        if self.category:
            parts.append(self.category)
        return " ".join(parts)
```

## Solution 2: Schema Embedding Cache

```python
import asyncio
from typing import Callable, Dict, List, Optional


class SchemaEmbeddingCache:
    """
    Pre-computes and caches embeddings for all tool schema records.
    Embeddings are computed once on first access and reused for all queries.
    """

    def __init__(self, embed_fn: Callable):
        self._embed_fn = embed_fn
        self._embeddings: Dict[str, List[float]] = {}
        self._lock = asyncio.Lock()

    async def get_embedding(self, record: ToolSchemaRecord) -> List[float]:
        if record.embedding is not None:
            return record.embedding

        async with self._lock:
            if record.tool_name not in self._embeddings:
                embedding = await self._embed_fn(record.searchable_text())
                self._embeddings[record.tool_name] = embedding
                record.embedding = embedding

        return self._embeddings[record.tool_name]

    async def warm_all(self, records: List[ToolSchemaRecord]) -> None:
        """Pre-compute embeddings for all records in parallel."""
        await asyncio.gather(*[self.get_embedding(r) for r in records])

    def is_warm(self, tool_name: str) -> bool:
        return tool_name in self._embeddings

    def stats(self) -> dict:
        return {
            "cached_schemas": len(self._embeddings),
        }
```

## Solution 3: Relevant Tool Selector

```python
import math
from typing import Callable, List, Optional


def cosine_similarity(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    return dot / (mag_a * mag_b + 1e-9)


class RelevantToolSelector:
    """
    Selects the top-K most relevant tools for a query using embedding similarity.
    Always-include tools are added unconditionally.
    """

    def __init__(
        self,
        embedding_cache: SchemaEmbeddingCache,
        embed_fn: Callable,
        top_k: int = 8,
        min_similarity: float = 0.30,
    ):
        self._cache = embedding_cache
        self._embed_fn = embed_fn
        self._top_k = top_k
        self._min_sim = min_similarity

    async def select(
        self,
        query: str,
        all_records: List[ToolSchemaRecord],
    ) -> List[ToolSchemaRecord]:
        # Always-include tools
        always = [r for r in all_records if r.always_include]
        candidates = [r for r in all_records if not r.always_include]

        if not candidates:
            return always

        query_embedding = await self._embed_fn(query)

        # Score each candidate
        scored = []
        for record in candidates:
            schema_embedding = await self._cache.get_embedding(record)
            sim = cosine_similarity(query_embedding, schema_embedding)
            if sim >= self._min_sim:
                scored.append((record, sim))

        scored.sort(key=lambda x: -x[1])
        selected = [r for r, _ in scored[:self._top_k]]

        # Deduplicate with always-include
        always_names = {r.tool_name for r in always}
        selected = [r for r in selected if r.tool_name not in always_names]

        return always + selected
```

## Solution 4: Lazy Tool Schema Assembler

```python
from typing import Any, Callable, Dict, List, Optional


class LazyToolSchemaAssembler:
    """
    Assembles the tool list for an LLM request by lazily selecting
    only relevant schemas. Tracks token savings vs. full schema inclusion.
    """

    def __init__(
        self,
        selector: RelevantToolSelector,
        all_records: List[ToolSchemaRecord],
    ):
        self._selector = selector
        self._all_records = all_records
        self._total_full_tokens = sum(r.token_count for r in all_records)
        self._tokens_saved = 0
        self._requests_served = 0

    async def assemble(self, query: str) -> List[dict]:
        selected = await self._selector.select(query, self._all_records)
        self._requests_served += 1
        selected_tokens = sum(r.token_count for r in selected)
        self._tokens_saved += (self._total_full_tokens - selected_tokens)
        return [r.to_llm_format() for r in selected]

    def stats(self) -> dict:
        return {
            "total_tools": len(self._all_records),
            "full_schema_tokens": self._total_full_tokens,
            "total_tokens_saved": self._tokens_saved,
            "requests_served": self._requests_served,
            "avg_tokens_saved_per_request": round(
                self._tokens_saved / max(self._requests_served, 1), 1
            ),
        }
```

## Solution 5: Category-Based Fallback Selector

```python
from typing import List


class CategoryBasedFallbackSelector:
    """
    Fallback selector when embeddings are unavailable.
    Groups tools by category and selects from the most relevant category
    based on keyword matching.
    """

    def select(
        self,
        query: str,
        all_records: List[ToolSchemaRecord],
        max_tools: int = 8,
    ) -> List[ToolSchemaRecord]:
        query_lower = query.lower()

        # Score each record by keyword overlap
        scored = []
        for record in all_records:
            if record.always_include:
                continue
            keywords = [record.category.lower()] + [k.lower() for k in record.keywords]
            matches = sum(1 for kw in keywords if kw and kw in query_lower)
            desc_words = set(record.description.lower().split())
            query_words = set(query_lower.split())
            overlap = len(desc_words & query_words)
            score = matches * 3 + overlap
            scored.append((record, score))

        scored.sort(key=lambda x: -x[1])
        always = [r for r in all_records if r.always_include]
        selected = [r for r, _ in scored[:max_tools]]
        return always + selected
```

## Solution 6: Schema Loading Dashboard

```python
import time


class LazySchemaLoadingDashboard:
    """Monitors lazy loading efficiency and token savings."""

    def __init__(
        self,
        assembler: LazyToolSchemaAssembler,
        embedding_cache: SchemaEmbeddingCache,
    ):
        self._assembler = assembler
        self._cache = embedding_cache

    def render(self) -> dict:
        assembler_stats = self._assembler.stats()
        cache_stats = self._cache.stats()

        cost_savings_pct = round(
            assembler_stats["avg_tokens_saved_per_request"]
            / max(assembler_stats["full_schema_tokens"], 1)
            * 100,
            1,
        )

        return {
            "generated_at": time.time(),
            "total_tools_registered": assembler_stats["total_tools"],
            "full_schema_tokens": assembler_stats["full_schema_tokens"],
            "avg_tokens_saved_per_request": assembler_stats["avg_tokens_saved_per_request"],
            "token_cost_reduction_pct": cost_savings_pct,
            "cache_warmed_schemas": cache_stats["cached_schemas"],
            "requests_served": assembler_stats["requests_served"],
        }
```

## Comparison

| Approach | Embedding-Based Selection | Always-Include Support | Token Tracking | Fallback (no embeddings) | Dashboard |
|---|---|---|---|---|---|
| RelevantToolSelector | Yes (cosine similarity) | Yes | No | No | No |
| LazyToolSchemaAssembler | Via selector | Via selector | Yes | No | No |
| SchemaEmbeddingCache | No | No | No | No | No |
| CategoryBasedFallbackSelector | No | Yes | No | Yes | No |
| LazySchemaLoadingDashboard | No | No | Via assembler | No | Yes |

**Best for production**: Call `SchemaEmbeddingCache.warm_all()` at agent startup to pre-compute all schema embeddings — this takes 1–2 seconds and makes first-request latency identical to subsequent requests. Set `top_k=8` as the starting point: most queries need fewer than 8 tools, and 8 schemas fit comfortably in the prompt. Mark tools like `search`, `get_current_time`, and `send_message` as `always_include=True` if they are universally useful. Monitor `token_cost_reduction_pct` — with 50 tools and top_k=8, you should see 80%+ reduction in schema token overhead.
