---
title: "Agent Doesn't Implement Prompt Caching for Repeated System Prompts"
description: "Agents that send the full system prompt with every LLM request pay the prompt token cost on every call — even when the system prompt is identical across thousands of requests. Implement prompt caching that detects stable prefix content, uses provider-side prompt caching APIs (Anthropic cache_control, OpenAI cached tokens) where available, and falls back to manual prefix deduplication to reduce redundant token processing."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-prompt-caching-for-repeated-system-prompts
tags: [prompt-caching, system-prompt, token-cost, cache-control, prefix-caching, cost-reduction]
symptoms:
  - "System prompt is 2000 tokens and sent on every request — 100% redundant prefix cost"
  - "No use of Anthropic cache_control or OpenAI prompt caching headers"
  - "Cost per session is dominated by system prompt tokens, not conversation tokens"
  - "Identical tool definitions re-sent on every tool-calling request"
  - "No measurement of how many tokens per request are stable vs dynamic"
---

## Why This Happens

LLM APIs charge for every input token on every request. When a system prompt is static — the same instructions, tool definitions, and context for every user — its tokens are re-processed on every call at full price. Provider-side prompt caching (Anthropic's `cache_control` blocks, OpenAI's automatic prefix caching) can reduce the cost of repeated prefixes by 80–90%. Without explicit caching headers, these savings are not applied. Beyond provider APIs, agents can reduce prompt bloat by separating the static prefix from the dynamic per-request content and only re-sending what changed.

## Solution 1: Prompt Segment Classifier

```python
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional


class SegmentStability(str, Enum):
    STATIC = "static"         # same across all requests — ideal cache candidate
    SEMI_STATIC = "semi_static"  # changes per session, not per request
    DYNAMIC = "dynamic"       # changes every request


@dataclass
class PromptSegment:
    name: str
    content: str
    stability: SegmentStability
    token_estimate: int = 0
    cache_eligible: bool = False

    def __post_init__(self) -> None:
        if self.token_estimate == 0:
            self.token_estimate = max(1, len(self.content) // 4)
        self.cache_eligible = self.stability in (
            SegmentStability.STATIC,
            SegmentStability.SEMI_STATIC,
        )
```

## Solution 2: Cache-Annotated Message Builder

```python
from typing import Any, Dict, List, Optional


class CacheAnnotatedMessageBuilder:
    """
    Builds an Anthropic-style messages array with cache_control annotations
    on stable content blocks. The last static block in the sequence gets
    cache_control={"type": "ephemeral"} to mark it as a cache breakpoint.
    """

    def build_system_blocks(
        self,
        segments: List[PromptSegment],
        provider: str = "anthropic",
    ) -> List[Dict[str, Any]]:
        blocks = []
        last_cacheable = max(
            (i for i, s in enumerate(segments) if s.cache_eligible),
            default=-1,
        )

        for i, segment in enumerate(segments):
            block: Dict[str, Any] = {
                "type": "text",
                "text": segment.content,
            }
            if provider == "anthropic" and i == last_cacheable:
                block["cache_control"] = {"type": "ephemeral"}
            blocks.append(block)

        return blocks

    def build_messages(
        self,
        conversation_turns: List[Dict[str, Any]],
        static_tool_definitions: Optional[List[Dict]] = None,
    ) -> List[Dict[str, Any]]:
        return list(conversation_turns)
```

## Solution 3: Prompt Cache Key Generator

```python
import hashlib
import json
from typing import Any, List


class PromptCacheKeyGenerator:
    """
    Generates stable cache keys for prompt segments so that
    in-process and external caches can be keyed consistently.
    """

    @staticmethod
    def segment_key(segment: PromptSegment) -> str:
        return hashlib.sha256(segment.content.encode()).hexdigest()[:16]

    @staticmethod
    def prefix_key(segments: List[PromptSegment]) -> str:
        combined = "|".join(
            s.content for s in segments if s.stability == SegmentStability.STATIC
        )
        return hashlib.sha256(combined.encode()).hexdigest()[:16]

    @staticmethod
    def request_key(
        prefix_key: str,
        dynamic_content: str,
        model: str,
    ) -> str:
        payload = json.dumps({
            "prefix": prefix_key,
            "dynamic": dynamic_content,
            "model": model,
        }, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:24]
```

## Solution 4: Prompt Cache Savings Tracker

```python
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import List


@dataclass
class CacheUsageRecord:
    request_id: str
    model: str
    total_input_tokens: int
    cached_tokens: int
    cache_hit: bool
    cost_saved_usd: float
    recorded_at: float = field(default_factory=time.time)


class PromptCacheSavingsTracker:
    """
    Accumulates cache hit/miss records from provider API responses.
    Anthropic returns cache_creation_input_tokens and cache_read_input_tokens
    in the usage object; this tracker normalizes those fields.
    """

    ANTHROPIC_CACHE_READ_COST_RATIO = 0.10
    DEFAULT_COST_PER_1K = 0.003

    def __init__(self):
        self._records: List[CacheUsageRecord] = []
        self._lock = Lock()

    def record_anthropic_response(
        self,
        request_id: str,
        model: str,
        usage: dict,
        cost_per_1k_input: float = DEFAULT_COST_PER_1K,
    ) -> CacheUsageRecord:
        input_tokens = usage.get("input_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)

        cache_hit = cache_read > 0
        cost_saved = (
            cache_read * cost_per_1k_input / 1000.0 * (1 - self.ANTHROPIC_CACHE_READ_COST_RATIO)
        )

        record = CacheUsageRecord(
            request_id=request_id,
            model=model,
            total_input_tokens=input_tokens + cache_read,
            cached_tokens=cache_read,
            cache_hit=cache_hit,
            cost_saved_usd=round(cost_saved, 6),
        )
        with self._lock:
            self._records.append(record)
        return record

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r.recorded_at >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "requests": 0}

        total_input = sum(r.total_input_tokens for r in recent)
        total_cached = sum(r.cached_tokens for r in recent)
        total_saved = sum(r.cost_saved_usd for r in recent)
        hits = sum(1 for r in recent if r.cache_hit)

        return {
            "window_seconds": window_seconds,
            "requests": len(recent),
            "cache_hit_rate": round(hits / len(recent), 4),
            "total_input_tokens": total_input,
            "total_cached_tokens": total_cached,
            "cache_ratio": round(total_cached / max(total_input, 1), 4),
            "cost_saved_usd": round(total_saved, 4),
        }
```

## Solution 5: Static Prefix Extractor

```python
from typing import List, Optional, Tuple


class StaticPrefixExtractor:
    """
    Given a list of past prompt strings, identifies the longest common
    prefix that can be marked as cacheable.
    """

    @staticmethod
    def longest_common_prefix(texts: List[str]) -> str:
        if not texts:
            return ""
        prefix = texts[0]
        for text in texts[1:]:
            while not text.startswith(prefix):
                prefix = prefix[:-1]
                if not prefix:
                    return ""
        return prefix

    def extract_segments(
        self,
        full_prompt: str,
        stable_prefix: str,
    ) -> Tuple[PromptSegment, Optional[PromptSegment]]:
        static_seg = PromptSegment(
            name="static_prefix",
            content=stable_prefix,
            stability=SegmentStability.STATIC,
        )
        dynamic_content = full_prompt[len(stable_prefix):]
        if not dynamic_content.strip():
            return static_seg, None

        dynamic_seg = PromptSegment(
            name="dynamic_suffix",
            content=dynamic_content,
            stability=SegmentStability.DYNAMIC,
        )
        return static_seg, dynamic_seg
```

## Solution 6: Prompt Caching Dashboard

```python
import time
from typing import List


class PromptCachingDashboard:
    """
    Combines savings tracker stats with segment analysis into
    a single report for cost and cache effectiveness monitoring.
    """

    def __init__(
        self,
        savings_tracker: PromptCacheSavingsTracker,
        segments: List[PromptSegment],
    ):
        self._tracker = savings_tracker
        self._segments = segments

    def render(self) -> dict:
        stats = self._tracker.summary(window_seconds=3600.0)
        total_tokens = sum(s.token_estimate for s in self._segments)
        static_tokens = sum(
            s.token_estimate for s in self._segments
            if s.stability == SegmentStability.STATIC
        )
        return {
            "generated_at": time.time(),
            "prompt_composition": {
                "total_segments": len(self._segments),
                "total_tokens_est": total_tokens,
                "static_tokens_est": static_tokens,
                "cacheable_pct": round(static_tokens / max(total_tokens, 1) * 100, 1),
            },
            "cache_performance": stats,
        }
```

## Comparison

| Approach | Segment Classification | Cache Annotations | Provider Integration | Savings Measurement | Prefix Detection |
|---|---|---|---|---|---|
| PromptSegment | Yes | No | No | No | No |
| CacheAnnotatedMessageBuilder | No | Yes (Anthropic) | Yes | No | No |
| PromptCacheSavingsTracker | No | No | Yes (usage fields) | Yes | No |
| StaticPrefixExtractor | Via segments | No | No | No | Yes |
| PromptCachingDashboard | No | No | No | Via tracker | No |

**Best for production**: Place all static content — system instructions, tool definitions, few-shot examples — at the top of the system prompt and mark the final static block with `cache_control={"type": "ephemeral"}` for Anthropic models. Ensure the static prefix is at least 1024 tokens — Anthropic's minimum cacheable size — otherwise the cache overhead exceeds the savings. Monitor `cache_ratio` from `PromptCacheSavingsTracker.summary()`: below 0.50 means less than half of input tokens are being served from cache; above 0.80 is excellent. Re-evaluate caching strategy after any system prompt change, as the cache is invalidated and must be rebuilt on the next request.
