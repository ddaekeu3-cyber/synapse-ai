---
title: "Agent Doesn't Implement Prompt Caching with Anthropic Cache Control"
description: "Agents built on Claude that send the same large system prompt, tool definitions, or retrieved documents on every request pay full input token costs for content that Anthropic can cache server-side. Implementing cache_control breakpoints on stable prompt segments reduces input token costs by up to 90% on repeated requests and significantly lowers latency for cache hits. This solution covers marking cacheable segments, measuring hit rates, and managing cache invalidation."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-prompt-caching-with-anthropic-cache-control
tags: [prompt-caching, cache-control, anthropic, token-cost, cache-hit-rate, input-tokens]
symptoms:
  - "Full system prompt and tool definitions re-sent and billed on every API call"
  - "No cache_control breakpoints in message construction — caching never activates"
  - "Input token costs scale linearly with request volume despite stable prompt content"
  - "Large RAG document chunks injected without caching despite being reused across queries"
  - "No measurement of cache hit rates — cannot verify caching is working"
---

## Why This Happens

Anthropic's prompt caching requires explicit `cache_control` markers on message blocks — the API does not automatically cache any content. Developers unfamiliar with the feature omit these markers entirely, paying full input token costs on every request. The most impactful segments to cache are those that are large and stable across requests: system prompts (hundreds to thousands of tokens), tool definitions (often 2,000+ tokens for complex agents), and retrieved reference documents that are reused across multiple queries in the same session. Without instrumentation, developers cannot even verify whether caching is active.

## Solution 1: Cacheable Block Builder

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class CacheControlType(str, Enum):
    EPHEMERAL = "ephemeral"    # current Anthropic cache type


@dataclass
class ContentBlock:
    type: str                  # "text" | "tool_result" | "image"
    text: Optional[str] = None
    cache_control: Optional[Dict[str, str]] = None
    # Additional fields for non-text blocks omitted for brevity

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"type": self.type}
        if self.text is not None:
            d["text"] = self.text
        if self.cache_control is not None:
            d["cache_control"] = self.cache_control
        return d


class CacheableBlockBuilder:
    """
    Constructs Anthropic API message blocks with cache_control markers.
    Applies caching to the last content block of a cacheable segment
    (Anthropic caches everything up to and including the marked block).
    """

    @staticmethod
    def text_block(text: str, cacheable: bool = False) -> ContentBlock:
        cache_control = {"type": CacheControlType.EPHEMERAL.value} if cacheable else None
        return ContentBlock(type="text", text=text, cache_control=cache_control)

    @staticmethod
    def system_message(
        system_prompt: str,
        cacheable: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Returns a system message with cache_control on the final block.
        The entire system prompt is cached up to this breakpoint.
        """
        block = CacheableBlockBuilder.text_block(system_prompt, cacheable=cacheable)
        return [block.to_dict()]

    @staticmethod
    def tool_definitions_with_cache(
        tools: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Appends cache_control to the last tool definition block.
        Tool definitions are stable and large — ideal for caching.
        """
        if not tools:
            return tools
        result = [dict(t) for t in tools]
        result[-1] = {**result[-1], "cache_control": {"type": "ephemeral"}}
        return result
```

## Solution 2: Cache Breakpoint Planner

```python
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class PromptSegment:
    name: str
    content: str
    stable: bool         # True if content is the same across many requests
    estimated_tokens: int
    cache_priority: int  # lower = cache first


class CacheBreakpointPlanner:
    """
    Analyzes prompt segments and decides where to place cache_control breakpoints.
    Anthropic supports up to 4 cache breakpoints per request; this planner
    selects the highest-value positions.
    """

    MAX_BREAKPOINTS = 4

    def __init__(self, tokens_per_char: float = 0.25):
        self._tpc = tokens_per_char

    def plan(self, segments: List[PromptSegment]) -> List[Tuple[str, bool]]:
        """
        Returns list of (segment_name, should_cache) tuples.
        Selects up to MAX_BREAKPOINTS stable segments with highest token counts.
        """
        stable = [s for s in segments if s.stable]
        # Sort by token count descending, then cache_priority ascending
        stable_sorted = sorted(stable, key=lambda s: (-s.estimated_tokens, s.cache_priority))
        cacheable_names = {s.name for s in stable_sorted[: self.MAX_BREAKPOINTS]}

        return [(s.name, s.name in cacheable_names) for s in segments]

    def estimate_savings(
        self,
        segments: List[PromptSegment],
        requests_per_hour: int,
        cache_write_multiplier: float = 1.25,
        cache_read_discount: float = 0.10,
    ) -> dict:
        """
        Estimates hourly token savings from caching planned breakpoints.
        cache_write_multiplier: cached writes cost 25% more than normal.
        cache_read_discount: cached reads cost 10% of normal input price.
        """
        plan = self.plan(segments)
        cached = {name for name, should_cache in plan if should_cache}

        normal_tokens = sum(s.estimated_tokens for s in segments)
        cacheable_tokens = sum(s.estimated_tokens for s in segments if s.name in cached)

        # First request: pay write premium on cached segments
        first_cost_tokens = (
            (normal_tokens - cacheable_tokens)
            + cacheable_tokens * cache_write_multiplier
        )
        # Subsequent requests: cached segments at 10%
        repeat_cost_tokens = (
            (normal_tokens - cacheable_tokens)
            + cacheable_tokens * cache_read_discount
        )
        hourly_savings = (normal_tokens - repeat_cost_tokens) * max(0, requests_per_hour - 1)

        return {
            "normal_tokens_per_request": normal_tokens,
            "cacheable_tokens": cacheable_tokens,
            "cache_coverage_pct": round(cacheable_tokens / max(normal_tokens, 1) * 100, 1),
            "repeat_cost_tokens": round(repeat_cost_tokens, 0),
            "hourly_token_savings_est": round(hourly_savings, 0),
        }
```

## Solution 3: Cache Usage Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Optional, Tuple


class CacheUsageTracker:
    """
    Tracks cache hit and write token counts from Anthropic API response usage fields.
    Measures actual cache hit rates and computes cost savings.
    """

    def __init__(self, window_seconds: float = 3600.0, max_records: int = 10000):
        self._window = window_seconds
        self._records: Deque[Tuple[float, dict]] = deque(maxlen=max_records)
        self._lock = Lock()

    def record_usage(self, usage: dict) -> None:
        """
        usage: the 'usage' field from Anthropic API response.
        Relevant keys: input_tokens, output_tokens,
                       cache_creation_input_tokens, cache_read_input_tokens
        """
        with self._lock:
            self._records.append((time.time(), {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
                "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
            }))

    def summary(self, window_seconds: Optional[float] = None) -> dict:
        window = window_seconds or self._window
        cutoff = time.time() - window
        with self._lock:
            recent = [r for ts, r in self._records if ts >= cutoff]

        if not recent:
            return {"window_seconds": window, "requests": 0}

        total_input = sum(r["input_tokens"] for r in recent)
        total_cache_read = sum(r["cache_read_tokens"] for r in recent)
        total_cache_write = sum(r["cache_creation_tokens"] for r in recent)
        total_tokens_without_cache = total_input + total_cache_read

        hit_rate = total_cache_read / max(total_tokens_without_cache, 1)

        return {
            "window_seconds": window,
            "requests": len(recent),
            "total_input_tokens": total_input,
            "total_cache_read_tokens": total_cache_read,
            "total_cache_write_tokens": total_cache_write,
            "cache_hit_rate_pct": round(hit_rate * 100, 2),
            "effective_input_tokens": total_input + total_cache_write,
        }
```

## Solution 4: Session-Scoped Cache Manager

```python
from typing import Any, Dict, List, Optional


class SessionScopedCacheManager:
    """
    Manages which prompt segments should be cached for a given session.
    The system prompt and tool definitions are cached at session start;
    retrieved documents are cached if they appear in multiple turns.
    """

    def __init__(self, min_tokens_to_cache: int = 1024):
        self._min_tokens = min_tokens_to_cache
        self._session_segments: Dict[str, Dict[str, int]] = {}
        # session_id -> {content_hash: appearance_count}

    def should_cache_document(
        self, session_id: str, content: str, estimated_tokens: int
    ) -> bool:
        if estimated_tokens < self._min_tokens:
            return False
        import hashlib
        h = hashlib.sha256(content[:500].encode()).hexdigest()[:16]
        session = self._session_segments.setdefault(session_id, {})
        session[h] = session.get(h, 0) + 1
        # Cache if the document has appeared more than once in this session
        return session[h] > 1

    def build_rag_message(
        self,
        session_id: str,
        documents: List[Dict[str, Any]],
        tokens_per_char: float = 0.25,
    ) -> List[Dict[str, Any]]:
        """
        Constructs a user message containing retrieved documents,
        applying cache_control to documents that warrant caching.
        """
        blocks = []
        for doc in documents:
            content = str(doc.get("content", ""))
            estimated_tokens = int(len(content) * tokens_per_char)
            cacheable = self.should_cache_document(session_id, content, estimated_tokens)
            block: Dict[str, Any] = {"type": "text", "text": content}
            if cacheable:
                block["cache_control"] = {"type": "ephemeral"}
            blocks.append(block)
        return blocks
```

## Solution 5: Cache Invalidation Tracker

```python
import time
from typing import Dict, List


class CacheInvalidationTracker:
    """
    Tracks when cached segments change (system prompt updates, tool definition
    changes) so that cache write costs on the first post-change request
    are expected and do not appear as anomalies in cost metrics.
    """

    def __init__(self):
        self._versions: Dict[str, str] = {}   # segment_name -> content_hash
        self._invalidations: List[dict] = []

    def check_and_update(self, segment_name: str, content: str) -> bool:
        import hashlib
        h = hashlib.sha256(content.encode()).hexdigest()[:16]
        if self._versions.get(segment_name) != h:
            self._versions[segment_name] = h
            self._invalidations.append({
                "ts": time.time(),
                "segment": segment_name,
                "new_hash": h,
            })
            return True   # cache was invalidated
        return False

    def invalidation_history(self, limit: int = 20) -> List[dict]:
        return self._invalidations[-limit:]
```

## Solution 6: Prompt Cache Dashboard

```python
import time


class PromptCacheDashboard:
    """
    Combines cache hit rates, invalidation history, and savings
    estimates into a single cost optimization view.
    """

    def __init__(
        self,
        tracker: CacheUsageTracker,
        invalidation_tracker: CacheInvalidationTracker,
    ):
        self._tracker = tracker
        self._invalidations = invalidation_tracker

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "cache_stats_1h": self._tracker.summary(3600.0),
            "cache_stats_24h": self._tracker.summary(86400.0),
            "recent_invalidations": self._invalidations.invalidation_history(10),
        }
```

## Comparison

| Approach | Block Construction | Breakpoint Planning | Hit Rate Tracking | Session Caching | Invalidation |
|---|---|---|---|---|---|
| CacheableBlockBuilder | Yes | No | No | No | No |
| CacheBreakpointPlanner | No | Yes (token-ranked) | No | No | No |
| CacheUsageTracker | No | No | Yes (from API usage) | No | No |
| SessionScopedCacheManager | Via builder | No | No | Yes | No |
| CacheInvalidationTracker | No | No | No | No | Yes |
| PromptCacheDashboard | No | No | No | No | Yes (aggregate) |

**Best for production**: Mark the system prompt and tool definitions as cacheable on every request — these are the highest-value targets because they are large (2,000-10,000 tokens), completely stable within a deployment, and present in every API call. Use `CacheUsageTracker` to record `cache_creation_input_tokens` and `cache_read_input_tokens` from the API response on every call — Anthropic only provides these fields when caching is active, so a zero value means the cache_control markers are not being applied correctly. Target a cache hit rate above 80% for agents with frequent repeated queries; below 50% indicates that the cached segments are changing too often or that requests are not hitting the same cache partition. Set `min_tokens_to_cache=1024` in `SessionScopedCacheManager` — caching small documents adds the 25% write premium without meaningful read savings.
