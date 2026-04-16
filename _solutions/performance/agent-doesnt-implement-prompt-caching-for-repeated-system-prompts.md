---
title: "Agent Doesn't Implement Prompt Caching for Repeated System Prompts"
description: "Agents that send the full system prompt on every request pay input token costs for content the provider has already processed. Anthropic's prompt caching allows marking prompt prefixes as cacheable so subsequent requests with the same prefix skip re-processing. Implement prompt cache tagging, track cache hit rates and cost savings, and alert when cache hit rate drops below threshold — indicating that prompt changes are thrashing the cache."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-prompt-caching-for-repeated-system-prompts
tags: [prompt-caching, cache-control, anthropic-cache, input-token-cost, system-prompt, cache-hit-rate]
symptoms:
  - "Every request re-processes the same 2000-token system prompt even though it never changes"
  - "Input token costs are linear with request volume even for identical system prompts"
  - "No cache_control tags in API requests — provider cannot cache any prefix"
  - "System prompt is rebuilt from scratch on every session initialization"
  - "Cannot tell from logs whether prompt caching is active or saving any tokens"
---

## Why This Happens

Prompt caching is opt-in: the provider only caches a prefix when the request explicitly marks it with a `cache_control` block. Agents that assemble prompts as plain strings never produce the structured message format that enables caching. Even agents using the messages API often append tools, examples, or dynamic context after the system prompt, which shifts the cached prefix boundary and invalidates the cache. Effective prompt caching requires understanding which prefix is stable (system prompt + static instructions), marking only that prefix, and keeping dynamic content (conversation history, user input) after the cache boundary.

## Solution 1: Cacheable Prompt Block

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class CacheControlType(str, Enum):
    EPHEMERAL = "ephemeral"    # Anthropic's only current type; caches for ~5 min


@dataclass
class CacheControlBlock:
    type: CacheControlType = CacheControlType.EPHEMERAL

    def to_dict(self) -> dict:
        return {"type": self.type.value}


@dataclass
class PromptBlock:
    """
    Represents one block in a structured prompt (text, tool_result, etc.).
    mark_cacheable=True adds cache_control to signal the provider to cache
    everything up to and including this block.
    """
    block_type: str       # "text" | "tool_result" | "image" | etc.
    content: Any
    mark_cacheable: bool = False

    def to_api_dict(self) -> dict:
        block: Dict[str, Any] = {"type": self.block_type}
        if self.block_type == "text":
            block["text"] = self.content
        else:
            block["content"] = self.content
        if self.mark_cacheable:
            block["cache_control"] = CacheControlBlock().to_dict()
        return block
```

## Solution 2: System Prompt Cache Assembler

```python
from typing import List, Optional


class SystemPromptCacheAssembler:
    """
    Builds a system prompt message with cache_control on the stable prefix.
    The stable prefix (base instructions + static examples) is marked cacheable.
    Dynamic additions (session context, user-specific rules) are appended
    WITHOUT cache_control so they don't thrash the cache boundary.
    """

    def __init__(self, stable_instructions: str):
        self._stable = stable_instructions
        self._dynamic_parts: List[str] = []

    def add_dynamic(self, text: str) -> None:
        """Add session-specific or user-specific context (not cached)."""
        self._dynamic_parts.append(text)

    def clear_dynamic(self) -> None:
        self._dynamic_parts.clear()

    def build_system_message(self) -> dict:
        """Returns the system message in Anthropic messages API format."""
        content: List[dict] = [
            PromptBlock(
                block_type="text",
                content=self._stable,
                mark_cacheable=True,
            ).to_api_dict()
        ]
        for part in self._dynamic_parts:
            content.append(
                PromptBlock(block_type="text", content=part).to_api_dict()
            )
        return {"role": "system", "content": content}

    def stable_token_estimate(self) -> int:
        """Rough estimate of tokens in the stable (cached) prefix."""
        return max(1, len(self._stable) // 4)

    def total_token_estimate(self) -> int:
        dynamic_chars = sum(len(p) for p in self._dynamic_parts)
        return max(1, (len(self._stable) + dynamic_chars) // 4)
```

## Solution 3: Cache Usage Tracker

```python
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional


@dataclass
class CacheUsageRecord:
    input_tokens: int
    cache_creation_tokens: int    # tokens written to cache (billed at 1.25×)
    cache_read_tokens: int        # tokens read from cache (billed at 0.1×)
    recorded_at: float = field(default_factory=time.time)
    session_id: str = ""

    def was_cache_hit(self) -> bool:
        return self.cache_read_tokens > 0

    def effective_input_tokens(self) -> int:
        """Tokens actually billed at full input price."""
        return self.input_tokens - self.cache_read_tokens


class PromptCacheUsageTracker:
    """
    Accumulates cache usage across requests.
    Computes hit rate, token savings, and cost savings estimate.
    """

    def __init__(
        self,
        window_seconds: float = 3600.0,
        full_input_cost_per_1k: float = 3.0,      # $/1k tokens
        cache_read_cost_per_1k: float = 0.30,      # $/1k tokens (0.1×)
        cache_write_cost_per_1k: float = 3.75,     # $/1k tokens (1.25×)
    ):
        self._window = window_seconds
        self._full_cost = full_input_cost_per_1k / 1000
        self._read_cost = cache_read_cost_per_1k / 1000
        self._write_cost = cache_write_cost_per_1k / 1000
        self._records: Deque[CacheUsageRecord] = deque()

    def record(self, rec: CacheUsageRecord) -> None:
        self._records.append(rec)
        self._trim()

    def _trim(self) -> None:
        cutoff = time.time() - self._window
        while self._records and self._records[0].recorded_at < cutoff:
            self._records.popleft()

    def stats(self) -> dict:
        self._trim()
        if not self._records:
            return {"requests": 0}

        total = len(self._records)
        hits = sum(1 for r in self._records if r.was_cache_hit())
        total_input = sum(r.input_tokens for r in self._records)
        total_read = sum(r.cache_read_tokens for r in self._records)
        total_write = sum(r.cache_creation_tokens for r in self._records)

        # Cost without caching
        cost_without_cache = total_input * self._full_cost
        # Cost with caching
        non_cached_input = sum(r.effective_input_tokens() for r in self._records)
        cost_with_cache = (
            non_cached_input * self._full_cost
            + total_read * self._read_cost
            + total_write * self._write_cost
        )
        savings = cost_without_cache - cost_with_cache

        return {
            "requests": total,
            "cache_hits": hits,
            "hit_rate": round(hits / total, 4),
            "total_input_tokens": total_input,
            "cache_read_tokens": total_read,
            "cache_write_tokens": total_write,
            "tokens_saved": total_read,
            "cost_without_cache_usd": round(cost_without_cache, 4),
            "cost_with_cache_usd": round(cost_with_cache, 4),
            "savings_usd": round(savings, 4),
            "savings_pct": round(savings / max(cost_without_cache, 0.0001) * 100, 1),
        }
```

## Solution 4: Cache Thrash Detector

```python
import time
from collections import deque
from typing import Deque


class CacheThrashDetector:
    """
    Detects when the prompt cache is being invalidated too frequently.
    Cache thrashing happens when the stable prefix changes between requests,
    forcing re-creation on every call and eliminating savings.
    Alerts when hit rate drops below threshold for a sustained window.
    """

    def __init__(
        self,
        tracker: PromptCacheUsageTracker,
        min_hit_rate: float = 0.70,
        evaluation_window_requests: int = 20,
    ):
        self._tracker = tracker
        self._min_hit_rate = min_hit_rate
        self._eval_window = evaluation_window_requests
        self._recent_hits: Deque[bool] = deque(maxlen=evaluation_window_requests)

    def record_request(self, was_hit: bool) -> None:
        self._recent_hits.append(was_hit)

    def is_thrashing(self) -> bool:
        if len(self._recent_hits) < self._eval_window // 2:
            return False
        recent_rate = sum(self._recent_hits) / len(self._recent_hits)
        return recent_rate < self._min_hit_rate

    def diagnosis(self) -> dict:
        if len(self._recent_hits) == 0:
            return {"status": "no_data"}
        recent_rate = sum(self._recent_hits) / len(self._recent_hits)
        thrashing = self.is_thrashing()
        return {
            "recent_hit_rate": round(recent_rate, 4),
            "min_hit_rate": self._min_hit_rate,
            "thrashing": thrashing,
            "sample_count": len(self._recent_hits),
            "recommendation": (
                "Stable prefix is changing between requests. "
                "Ensure dynamic content is appended AFTER the cached block, "
                "not inserted into the middle of the system prompt."
            ) if thrashing else None,
        }
```

## Solution 5: Cache-Aware Request Builder

```python
from typing import Any, Dict, List, Optional


class CacheAwareRequestBuilder:
    """
    Assembles Anthropic API request payloads with correct cache_control placement.
    Ensures the stable system prefix is always the first content block
    and that user messages do not contain cache_control (which would
    shift and invalidate the stable prefix cache).
    """

    def __init__(self, assembler: SystemPromptCacheAssembler):
        self._assembler = assembler

    def build(
        self,
        user_message: str,
        conversation_history: Optional[List[Dict[str, Any]]] = None,
        model: str = "claude-opus-4-6",
        max_tokens: int = 1024,
        tools: Optional[List[dict]] = None,
    ) -> dict:
        system_msg = self._assembler.build_system_message()

        messages = list(conversation_history or [])
        messages.append({"role": "user", "content": user_message})

        payload: Dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_msg["content"],
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools

        return payload

    def estimated_cache_savings_tokens(self) -> int:
        """Tokens saved per request when the cache prefix is hit."""
        return self._assembler.stable_token_estimate()
```

## Solution 6: Prompt Cache Dashboard

```python
import time


class PromptCacheDashboard:
    """
    Combines cache usage stats, thrash detection, and savings estimates
    into a single observability report.
    """

    def __init__(
        self,
        tracker: PromptCacheUsageTracker,
        thrash_detector: CacheThrashDetector,
        assembler: SystemPromptCacheAssembler,
    ):
        self._tracker = tracker
        self._thrash = thrash_detector
        self._assembler = assembler

    def render(self) -> dict:
        stats = self._tracker.stats()
        thrash = self._thrash.diagnosis()

        alerts = []
        if thrash.get("thrashing"):
            alerts.append({
                "type": "cache_thrashing",
                "severity": "warning",
                "message": thrash["recommendation"],
                "recent_hit_rate": thrash["recent_hit_rate"],
            })
        if stats.get("hit_rate", 1.0) < 0.5 and stats.get("requests", 0) > 50:
            alerts.append({
                "type": "low_cache_hit_rate",
                "severity": "warning",
                "hit_rate": stats["hit_rate"],
                "message": "Prompt cache hit rate below 50% over 1 hour.",
            })

        return {
            "generated_at": time.time(),
            "cache_stats": stats,
            "thrash_diagnosis": thrash,
            "stable_prefix_tokens": self._assembler.stable_token_estimate(),
            "alerts": alerts,
            "healthy": len(alerts) == 0,
        }
```

## Comparison

| Approach | Cache Tag Placement | Usage Tracking | Thrash Detection | Request Assembly | Dashboard |
|---|---|---|---|---|---|
| SystemPromptCacheAssembler | Yes (stable prefix) | No | No | No | No |
| PromptCacheUsageTracker | No | Yes (cost savings) | No | No | No |
| CacheThrashDetector | No | Via tracker | Yes (hit rate window) | No | No |
| CacheAwareRequestBuilder | Via assembler | No | No | Yes | No |
| PromptCacheDashboard | No | No | No | No | Yes |

**Best for production**: Mark only the truly static portion of the system prompt as cacheable — base persona, capability description, output format rules. Do NOT include session ID, user name, or current date in the cached block; put those in a separate uncached block appended after. Monitor `hit_rate` in `PromptCacheUsageTracker.stats()`: a rate above 90% with a 2000-token stable prefix saves roughly 90% of input token costs at Anthropic's cache pricing. If hit rate drops below 70% after previously being high, run `CacheThrashDetector.diagnosis()` to identify whether dynamic content is being inserted into the wrong position.
