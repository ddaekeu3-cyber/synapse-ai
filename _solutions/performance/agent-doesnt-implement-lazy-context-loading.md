---
title: "Agent Doesn't Implement Lazy Context Loading"
description: "Agents that eagerly load all available context — full conversation history, complete tool schemas, entire knowledge base excerpts — into every prompt pay token costs for content that may be irrelevant to the current turn. Implement lazy context loading that defers expensive context retrieval until the turn's intent is known, loads only the sections the current query actually needs, and tracks which context was used versus ignored."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-lazy-context-loading
tags: [lazy-loading, context-efficiency, deferred-retrieval, token-reduction, intent-based-loading, rag-optimization]
symptoms:
  - "Full conversation history injected on every turn even for simple one-shot queries"
  - "All tool schemas included in every prompt regardless of which tools are relevant"
  - "Knowledge base retrieval runs before the user's intent is understood"
  - "Token usage is constant regardless of query complexity"
  - "Context loading takes 200ms per turn even for trivial lookups"
---

## Why This Happens

Agents built with a static context assembly pattern load all context sections on every turn: history, tool schemas, retrieved documents, system instructions. This is simple to implement but wasteful — a query like "what time is it?" does not need the last 20 conversation turns, the schemas for database tools, or a knowledge base excerpt. Lazy loading requires analyzing the query intent first, then loading only the context sections that are relevant to that intent. Sections are represented as deferred callables that are only invoked if a relevance check passes.

## Solution 1: Context Section Descriptor

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, List, Optional


class ContextSectionPriority(int, Enum):
    CRITICAL = 0      # always loaded (system prompt, safety rules)
    HIGH = 1          # loaded if intent matches
    MEDIUM = 2        # loaded if tokens allow
    LOW = 3           # loaded only if explicitly needed


@dataclass
class ContextSectionDescriptor:
    section_id: str
    description: str
    priority: ContextSectionPriority
    intent_tags: List[str]           # intent keywords that trigger this section
    loader: Callable[[], Any]        # sync or async callable returning content
    estimated_tokens: int = 500      # estimate before loading
    max_tokens: Optional[int] = None # hard limit after loading
```

## Solution 2: Turn Intent Analyzer

```python
import re
from typing import Dict, List, Set


INTENT_PATTERNS: Dict[str, List[str]] = {
    "history": [r"\bprevious\b", r"\bearlier\b", r"\blast time\b", r"\bremember\b", r"\bbefore\b"],
    "database": [r"\bquery\b", r"\brecord\b", r"\brow\b", r"\btable\b", r"\bsql\b", r"\bfetch\b"],
    "file": [r"\bfile\b", r"\bread\b", r"\bwrite\b", r"\bpath\b", r"\bdirectory\b"],
    "web": [r"\bsearch\b", r"\blook up\b", r"\bfind\b", r"\burl\b", r"\bwebsite\b"],
    "calculation": [r"\bcalculate\b", r"\bcompute\b", r"\bmath\b", r"\bsum\b", r"\baverage\b"],
    "code": [r"\bcode\b", r"\bfunction\b", r"\bscript\b", r"\bimplement\b", r"\bclass\b"],
    "knowledge": [r"\bexplain\b", r"\bwhat is\b", r"\bdefinition\b", r"\bhow does\b"],
}


class TurnIntentAnalyzer:
    """
    Analyzes the current user message to determine which context
    sections are likely to be relevant for the turn.
    Returns a set of intent tags that can be matched against
    ContextSectionDescriptor.intent_tags.
    """

    def analyze(self, user_message: str) -> Set[str]:
        detected: Set[str] = set()
        for tag, patterns in INTENT_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, user_message, re.IGNORECASE):
                    detected.add(tag)
                    break
        return detected

    def relevance_score(
        self,
        section: ContextSectionDescriptor,
        detected_intents: Set[str],
    ) -> float:
        if not section.intent_tags:
            return 1.0  # no filter = always relevant
        matches = len(set(section.intent_tags) & detected_intents)
        return matches / len(section.intent_tags)
```

## Solution 3: Lazy Context Loader

```python
import asyncio
import inspect
import time
from typing import Any, Dict, List, Optional, Set, Tuple


class LazyContextLoader:
    """
    Loads context sections lazily based on intent relevance and
    token budget. Critical sections are always loaded first;
    lower-priority sections are deferred and loaded only if
    the intent analysis indicates they are needed.
    """

    def __init__(
        self,
        intent_analyzer: TurnIntentAnalyzer,
        token_budget: int = 16000,
        relevance_threshold: float = 0.5,
    ):
        self._intent = intent_analyzer
        self._budget = token_budget
        self._relevance_threshold = relevance_threshold

    async def _load_section(self, section: ContextSectionDescriptor) -> Tuple[str, int, float]:
        start = time.time()
        content = section.loader()
        if inspect.isawaitable(content):
            content = await content
        text = str(content) if not isinstance(content, str) else content
        if section.max_tokens:
            # rough truncation at char level
            max_chars = section.max_tokens * 4
            if len(text) > max_chars:
                text = text[:max_chars] + "\n[TRUNCATED]"
        load_ms = round((time.time() - start) * 1000, 2)
        token_estimate = len(text) // 4
        return text, token_estimate, load_ms

    async def load(
        self,
        sections: List[ContextSectionDescriptor],
        user_message: str,
    ) -> dict:
        intents = self._intent.analyze(user_message)
        sorted_sections = sorted(sections, key=lambda s: s.priority.value)

        loaded: List[Dict[str, Any]] = []
        skipped: List[str] = []
        tokens_used = 0
        total_load_ms = 0.0

        for section in sorted_sections:
            # Always load critical sections
            if section.priority == ContextSectionPriority.CRITICAL:
                text, tokens, ms = await self._load_section(section)
                loaded.append({"section_id": section.section_id, "content": text, "tokens": tokens})
                tokens_used += tokens
                total_load_ms += ms
                continue

            # Check relevance
            relevance = self._intent.relevance_score(section, intents)
            if relevance < self._relevance_threshold:
                skipped.append(section.section_id)
                continue

            # Check token budget
            if tokens_used + section.estimated_tokens > self._budget:
                skipped.append(section.section_id)
                continue

            text, tokens, ms = await self._load_section(section)
            loaded.append({"section_id": section.section_id, "content": text, "tokens": tokens})
            tokens_used += tokens
            total_load_ms += ms

        return {
            "sections": loaded,
            "detected_intents": list(intents),
            "loaded_count": len(loaded),
            "skipped_count": len(skipped),
            "skipped_sections": skipped,
            "tokens_used": tokens_used,
            "load_ms": round(total_load_ms, 2),
        }
```

## Solution 4: Context Usage Tracker

```python
import time
from collections import Counter
from typing import List


class ContextUsageTracker:
    """
    Tracks which context sections were loaded versus skipped per turn
    to identify sections that are rarely relevant and could be demoted
    to lower priority or removed entirely.
    """

    def __init__(self):
        self._loaded_counts: Counter = Counter()
        self._skipped_counts: Counter = Counter()
        self._tokens_saved: List[int] = []
        self._turns = 0

    def record(
        self,
        load_result: dict,
        sections: List[ContextSectionDescriptor],
    ) -> None:
        self._turns += 1
        loaded_ids = {s["section_id"] for s in load_result["sections"]}
        for s in sections:
            if s.section_id in loaded_ids:
                self._loaded_counts[s.section_id] += 1
            else:
                self._skipped_counts[s.section_id] += 1

        skipped_tokens = sum(
            s.estimated_tokens for s in sections
            if s.section_id in load_result["skipped_sections"]
        )
        self._tokens_saved.append(skipped_tokens)

    def stats(self) -> dict:
        total_saved = sum(self._tokens_saved)
        avg_saved = total_saved / max(self._turns, 1)
        rarely_loaded = [
            sid for sid, count in self._loaded_counts.items()
            if count / max(self._turns, 1) < 0.1
        ]
        return {
            "turns": self._turns,
            "total_tokens_saved_est": total_saved,
            "avg_tokens_saved_per_turn": round(avg_saved, 1),
            "rarely_loaded_sections": rarely_loaded,
            "load_rates": {
                sid: round(cnt / max(self._turns, 1), 3)
                for sid, cnt in self._loaded_counts.items()
            },
        }
```

## Solution 5: Lazy Context Assembly Pipeline

```python
from typing import Any, Dict, List, Optional


class LazyContextAssemblyPipeline:
    """
    Orchestrates intent analysis, lazy loading, and usage tracking
    into a single pipeline called once per turn.
    """

    def __init__(
        self,
        loader: LazyContextLoader,
        tracker: ContextUsageTracker,
        sections: List[ContextSectionDescriptor],
    ):
        self._loader = loader
        self._tracker = tracker
        self._sections = sections

    async def assemble(self, user_message: str) -> dict:
        result = await self._loader.load(self._sections, user_message)
        self._tracker.record(result, self._sections)
        assembled = "\n\n".join(
            s["content"] for s in result["sections"] if s["content"]
        )
        return {
            "context": assembled,
            "tokens_used": result["tokens_used"],
            "load_ms": result["load_ms"],
            "loaded_sections": [s["section_id"] for s in result["sections"]],
            "skipped_sections": result["skipped_sections"],
            "detected_intents": result["detected_intents"],
        }
```

## Solution 6: Lazy Loading Savings Dashboard

```python
import time


class LazyLoadingSavingsDashboard:
    """
    Combines usage tracking statistics and current section registry
    into an operational report for tuning section priorities.
    """

    def __init__(
        self,
        tracker: ContextUsageTracker,
        sections: List[ContextSectionDescriptor],
    ):
        self._tracker = tracker
        self._sections = sections

    def render(self) -> dict:
        stats = self._tracker.stats()
        section_catalog = [
            {
                "section_id": s.section_id,
                "priority": s.priority.name,
                "estimated_tokens": s.estimated_tokens,
                "intent_tags": s.intent_tags,
                "load_rate": stats["load_rates"].get(s.section_id, 0.0),
            }
            for s in self._sections
        ]
        return {
            "generated_at": time.time(),
            "usage_stats": stats,
            "section_catalog": section_catalog,
            "recommendations": [
                f"Consider demoting '{sid}' to LOW priority (load rate < 10%)"
                for sid in stats["rarely_loaded_sections"]
            ],
        }
```

## Comparison

| Approach | Intent Analysis | Lazy Loading | Token Budget | Usage Tracking | Dashboard |
|---|---|---|---|---|---|
| TurnIntentAnalyzer | Yes (regex patterns) | No | No | No | No |
| LazyContextLoader | Via analyzer | Yes (deferred) | Yes | No | No |
| ContextUsageTracker | No | No | No | Yes | No |
| LazyContextAssemblyPipeline | Via loader | Via loader | Via loader | Via tracker | No |
| LazyLoadingSavingsDashboard | No | No | No | Via tracker | Yes |

**Best for production**: Mark only the system prompt and safety rules as `CRITICAL` — everything else should require intent tags. A section with `intent_tags=["database"]` will only be loaded when the user's message contains database-related vocabulary, reducing average token usage by 40-60% for mixed workloads. Monitor `avg_tokens_saved_per_turn` from `ContextUsageTracker`: if it is under 200 tokens, the sections are too coarse-grained and should be split into smaller, more targeted units. Use `rarely_loaded_sections` to identify dead weight that can be removed entirely.
