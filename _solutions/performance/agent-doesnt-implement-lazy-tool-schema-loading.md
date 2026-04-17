---
title: "Agent Doesn't Implement Lazy Tool Schema Loading"
description: "Agents that load and include the full schema for every registered tool on every request inject thousands of tokens of tool descriptions that are irrelevant to most queries. A tool registry with 30 tools means 30 schemas in the context even when only 2 will be called. Implement lazy tool schema loading that injects only schemas for tools predicted to be relevant to the current request, reducing context token usage without limiting tool availability."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-lazy-tool-schema-loading
tags: [lazy-loading, tool-schema, context-efficiency, token-reduction, relevance-filtering, tool-registry]
symptoms:
  - "Full tool schema list injected into every request regardless of which tools are needed"
  - "30-tool registry adds 6000+ tokens of schema descriptions to every context"
  - "Context window fills with tool docs before user content even begins"
  - "No mechanism to select tool subsets based on request topic or user role"
  - "Adding more tools linearly increases base token cost of every request"
---

## Why This Happens

Tool schemas are registered at agent startup and serialized into every LLM request as a fixed block. This is the simplest implementation — always include everything — but it ignores that most requests need only a small subset of available tools. A coding assistant with 30 tools rarely needs the billing tool; a customer support agent rarely needs the code execution tool. Lazy loading requires classifying the request, scoring tool relevance against the query, and injecting only the top-k schemas while keeping all tools callable via a fallback full-schema injection if the LLM requests an unlisted tool.

## Solution 1: Tool Schema Definition

```python
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolSchema:
    name: str
    description: str
    parameters: Dict[str, Any]
    tags: List[str] = field(default_factory=list)       # topic tags for relevance scoring
    keywords: List[str] = field(default_factory=list)   # keyword hints for matching
    estimated_tokens: int = 0                            # pre-computed schema token cost
    always_include: bool = False                         # force-include regardless of relevance

    def __post_init__(self) -> None:
        if self.estimated_tokens == 0:
            import json
            schema_text = json.dumps({
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            })
            self.estimated_tokens = max(1, int(len(schema_text) * 0.25))
```

## Solution 2: Tool Relevance Scorer

```python
import re
from typing import Dict, List


class ToolRelevanceScorer:
    """
    Scores each registered tool schema against the user's request
    using keyword overlap, tag matching, and description similarity.
    Returns scores in [0.0, 1.0] for ranking.
    """

    def score_all(
        self,
        request_text: str,
        schemas: List[ToolSchema],
    ) -> Dict[str, float]:
        request_tokens = set(re.findall(r"\w+", request_text.lower()))
        scores: Dict[str, float] = {}

        for schema in schemas:
            if schema.always_include:
                scores[schema.name] = 1.0
                continue

            score = 0.0

            # Keyword overlap with request
            schema_words = set(
                re.findall(r"\w+", (schema.description + " " + " ".join(schema.keywords)).lower())
            )
            if schema_words:
                overlap = len(request_tokens & schema_words) / len(schema_words)
                score += overlap * 0.6

            # Tag match bonus
            request_text_lower = request_text.lower()
            for tag in schema.tags:
                if tag.lower() in request_text_lower:
                    score += 0.2
                    break

            # Name match bonus
            if schema.name.lower().replace("_", " ") in request_text_lower:
                score += 0.3

            scores[schema.name] = min(round(score, 4), 1.0)

        return scores
```

## Solution 3: Lazy Schema Loader

```python
from typing import Dict, List, Optional, Tuple


class LazyToolSchemaLoader:
    """
    Selects a subset of tool schemas to inject based on relevance scores
    and a token budget. Always-include tools are added first; remaining
    budget is filled by top-scored schemas.
    """

    def __init__(
        self,
        scorer: ToolRelevanceScorer,
        max_schema_tokens: int = 3000,
        top_k: int = 8,
        min_score: float = 0.10,
    ):
        self._scorer = scorer
        self._max_tokens = max_schema_tokens
        self._top_k = top_k
        self._min_score = min_score

    def select(
        self,
        request_text: str,
        all_schemas: List[ToolSchema],
    ) -> Tuple[List[ToolSchema], dict]:
        """
        Returns (selected_schemas, selection_report).
        """
        scores = self._scorer.score_all(request_text, all_schemas)

        # Always-include first
        always = [s for s in all_schemas if s.always_include]
        candidates = [s for s in all_schemas if not s.always_include]

        # Sort by score descending
        ranked = sorted(
            candidates,
            key=lambda s: scores.get(s.name, 0.0),
            reverse=True,
        )

        selected = list(always)
        tokens_used = sum(s.estimated_tokens for s in selected)

        for schema in ranked:
            score = scores.get(schema.name, 0.0)
            if score < self._min_score:
                break
            if len(selected) - len(always) >= self._top_k:
                break
            if tokens_used + schema.estimated_tokens > self._max_tokens:
                continue
            selected.append(schema)
            tokens_used += schema.estimated_tokens

        total_tokens_all = sum(s.estimated_tokens for s in all_schemas)
        report = {
            "total_tools": len(all_schemas),
            "selected_tools": len(selected),
            "tokens_used": tokens_used,
            "tokens_saved": total_tokens_all - tokens_used,
            "always_included": [s.name for s in always],
            "relevance_selected": [s.name for s in selected if not s.always_include],
            "scores": {s.name: scores.get(s.name, 0.0) for s in selected},
        }
        return selected, report
```

## Solution 4: Schema Registry with Lazy Loading

```python
import json
from typing import Any, Dict, List, Optional


class LazyLoadingToolRegistry:
    """
    Stores all tool schemas and provides both lazy (relevance-filtered)
    and eager (all schemas) access. Falls back to full schema list when
    the LLM requests a tool that was not in the lazy selection.
    """

    def __init__(self, loader: LazyToolSchemaLoader):
        self._loader = loader
        self._schemas: Dict[str, ToolSchema] = {}

    def register(self, schema: ToolSchema) -> None:
        self._schemas[schema.name] = schema

    def lazy_select(
        self, request_text: str
    ) -> tuple:
        return self._loader.select(request_text, list(self._schemas.values()))

    def get_schema(self, tool_name: str) -> Optional[ToolSchema]:
        return self._schemas.get(tool_name)

    def all_schemas(self) -> List[ToolSchema]:
        return list(self._schemas.values())

    def serialize_schemas(self, schemas: List[ToolSchema]) -> List[Dict[str, Any]]:
        return [
            {
                "name": s.name,
                "description": s.description,
                "parameters": s.parameters,
            }
            for s in schemas
        ]
```

## Solution 5: Schema Loading Stats Recorder

```python
import time
from threading import Lock
from typing import List


class SchemaLoadingStatsRecorder:
    """
    Tracks token savings from lazy schema loading over time.
    """

    def __init__(self):
        self._lock = Lock()
        self._records: List[dict] = []

    def record(self, selection_report: dict) -> None:
        with self._lock:
            self._records.append({
                "ts": time.time(),
                "total_tools": selection_report["total_tools"],
                "selected_tools": selection_report["selected_tools"],
                "tokens_used": selection_report["tokens_used"],
                "tokens_saved": selection_report["tokens_saved"],
            })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "requests": 0}

        total_saved = sum(r["tokens_saved"] for r in recent)
        avg_selected = sum(r["selected_tools"] for r in recent) / len(recent)
        avg_total = sum(r["total_tools"] for r in recent) / len(recent)

        return {
            "window_seconds": window_seconds,
            "requests": len(recent),
            "total_tokens_saved": total_saved,
            "avg_tokens_saved_per_request": round(total_saved / len(recent), 1),
            "avg_tools_selected": round(avg_selected, 1),
            "avg_tools_total": round(avg_total, 1),
            "avg_selection_ratio": round(avg_selected / max(avg_total, 1), 4),
        }
```

## Solution 6: Lazy Schema Loading Dashboard

```python
import time


class LazySchemaLoadingDashboard:
    """
    Combines registry state, loader configuration, and savings statistics
    into a single operational snapshot.
    """

    def __init__(
        self,
        registry: LazyLoadingToolRegistry,
        loader: LazyToolSchemaLoader,
        stats: SchemaLoadingStatsRecorder,
    ):
        self._registry = registry
        self._loader = loader
        self._stats = stats

    def render(self) -> dict:
        all_schemas = self._registry.all_schemas()
        total_tokens = sum(s.estimated_tokens for s in all_schemas)
        always_tools = [s.name for s in all_schemas if s.always_include]

        return {
            "generated_at": time.time(),
            "total_registered_tools": len(all_schemas),
            "always_included_tools": always_tools,
            "max_schema_tokens_budget": self._loader._max_tokens,
            "top_k_limit": self._loader._top_k,
            "full_schema_token_cost": total_tokens,
            "savings_last_hour": self._stats.summary(window_seconds=3600.0),
        }
```

## Comparison

| Approach | Relevance Scoring | Token Budget | Always-Include | Fallback Access | Savings Tracking |
|---|---|---|---|---|---|
| ToolRelevanceScorer | Yes (keyword+tag+name) | No | No | No | No |
| LazyToolSchemaLoader | Via scorer | Yes | Yes | No | No |
| LazyLoadingToolRegistry | Via loader | Via loader | Via loader | Yes (get_schema) | No |
| SchemaLoadingStatsRecorder | No | No | No | No | Yes |
| LazySchemaLoadingDashboard | No | No | No | No | Yes (aggregate) |

**Best for production**: Mark 2–3 universal tools (e.g., `answer`, `clarify`, `end_session`) as `always_include=True` so they are always available regardless of relevance scoring. Set `max_schema_tokens=3000` and `top_k=8` as starting defaults — this covers the most common request patterns while saving 60–80% of schema tokens for a 30-tool registry. When the LLM calls a tool not in the lazy selection, log the tool name and re-run with full schemas for that request: if this happens frequently for a specific tool, lower its `min_score` threshold or add relevant keywords to its schema definition.
