---
title: "Agent Doesn't Implement Prompt Prefix Caching with Cache Warming"
description: "Agents that send full prompts on every request pay the input token cost for the stable system prompt and tool definitions on every call, even though this content never changes between turns. Providers like Anthropic offer prompt prefix caching that eliminates input token costs for repeated prefixes. Without proactive cache warming, the first request after a cold cache pays full cost and experiences higher latency before the prefix is cached. Implement prompt prefix caching with startup warming to guarantee cache hits from the first user request."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-prompt-prefix-caching-with-cache-warming
tags: [prompt-caching, prefix-caching, cache-warming, token-cost-reduction, anthropic-caching, input-token-optimization]
symptoms:
  - "System prompt and tool definitions billed as input tokens on every API call"
  - "No cache_control markers on stable prompt sections"
  - "First request after deployment pays full input token cost and is slower"
  - "Token costs scale linearly with request volume even though prompt prefix is constant"
  - "No tracking of cache hit vs cache miss rate across API calls"
---

## Why This Happens

Prompt prefix caching requires explicit opt-in: the caller must mark the cacheable portion of the prompt with a `cache_control` field and ensure the prefix is byte-identical across calls. Agents that build prompts dynamically — injecting session metadata, timestamps, or variable content early in the system prompt — break prefix stability and prevent caching. Cache warming requires sending a dummy request at startup specifically to prime the cache before real user traffic arrives, so that the first user request hits a warm cache rather than a cold one.

## Solution 1: Prompt Segment Stability Classifier

```python
from dataclasses import dataclass
from enum import Enum
from typing import List


class SegmentStability(str, Enum):
    STATIC = "static"           # identical across all calls — cacheable
    SESSION_STABLE = "session_stable"  # stable within a session, varies across sessions
    DYNAMIC = "dynamic"         # changes every call — not cacheable


@dataclass
class PromptSegment:
    name: str
    content: str
    stability: SegmentStability
    cache_eligible: bool = False

    def __post_init__(self) -> None:
        self.cache_eligible = self.stability == SegmentStability.STATIC


class PromptSegmentStabilityClassifier:
    """
    Classifies prompt segments by stability. Static segments are candidates
    for prefix caching; dynamic segments must follow static ones to
    preserve prefix byte-identity.
    """

    @staticmethod
    def validate_prefix_order(segments: List[PromptSegment]) -> List[str]:
        """
        Returns a list of warnings if dynamic segments appear before static ones.
        A dynamic segment before a static one breaks prefix stability.
        """
        warnings = []
        seen_dynamic = False
        for seg in segments:
            if seg.stability == SegmentStability.DYNAMIC:
                seen_dynamic = True
            elif seen_dynamic and seg.stability == SegmentStability.STATIC:
                warnings.append(
                    f"static segment '{seg.name}' follows a dynamic segment — "
                    "prefix cache will be broken"
                )
        return warnings
```

## Solution 2: Cache Control Prompt Assembler

```python
from typing import Any, Dict, List, Optional


class CacheControlPromptAssembler:
    """
    Assembles a prompt with Anthropic-compatible cache_control markers
    on static segments. Ensures that the cached prefix is as large as
    possible by placing all static content before dynamic content.
    """

    def assemble_system_blocks(
        self,
        segments: List[PromptSegment],
    ) -> List[Dict[str, Any]]:
        """
        Returns a list of Anthropic system content blocks with
        cache_control on the last static segment (to mark the prefix boundary).
        """
        blocks = []
        static_indices = [i for i, s in enumerate(segments) if s.cache_eligible]

        for i, segment in enumerate(segments):
            block: Dict[str, Any] = {
                "type": "text",
                "text": segment.content,
            }
            # Mark the last static segment as the cache boundary
            if static_indices and i == static_indices[-1]:
                block["cache_control"] = {"type": "ephemeral"}
            blocks.append(block)

        return blocks

    def stable_prefix_content(self, segments: List[PromptSegment]) -> str:
        """Returns the concatenated content of all static segments."""
        return "\n\n".join(s.content for s in segments if s.cache_eligible)
```

## Solution 3: Cache Warmup Runner

```python
import asyncio
import time
from typing import Any, Callable, List, Optional


class CacheWarmupResult:
    def __init__(
        self,
        succeeded: bool,
        duration_ms: float,
        cache_creation_tokens: int = 0,
        error: Optional[str] = None,
    ):
        self.succeeded = succeeded
        self.duration_ms = duration_ms
        self.cache_creation_tokens = cache_creation_tokens
        self.error = error


class PromptPrefixCacheWarmupRunner:
    """
    Sends a minimal warmup request at startup to prime the prefix cache.
    The warmup request must use the exact same system prompt prefix as
    production requests to ensure cache hit on the first real call.
    """

    def __init__(
        self,
        assembler: CacheControlPromptAssembler,
        static_segments: List[PromptSegment],
        warmup_user_message: str = "warmup",
    ):
        self._assembler = assembler
        self._static_segments = static_segments
        self._warmup_message = warmup_user_message
        self._last_warmup: Optional[CacheWarmupResult] = None

    async def warm(self, llm_call_fn: Callable) -> CacheWarmupResult:
        system_blocks = self._assembler.assemble_system_blocks(self._static_segments)
        start = time.time()
        try:
            response = await llm_call_fn(
                system=system_blocks,
                messages=[{"role": "user", "content": self._warmup_message}],
                max_tokens=1,
            )
            duration_ms = round((time.time() - start) * 1000, 2)
            cache_tokens = getattr(
                getattr(response, "usage", None),
                "cache_creation_input_tokens",
                0,
            )
            result = CacheWarmupResult(
                succeeded=True,
                duration_ms=duration_ms,
                cache_creation_tokens=cache_tokens or 0,
            )
        except Exception as exc:
            duration_ms = round((time.time() - start) * 1000, 2)
            result = CacheWarmupResult(
                succeeded=False,
                duration_ms=duration_ms,
                error=str(exc)[:200],
            )
        self._last_warmup = result
        return result
```

## Solution 4: Cache Hit Rate Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Tuple


class CacheHitRateTracker:
    """
    Parses Anthropic usage metadata from API responses and tracks
    cache hit vs miss rates over a rolling window.
    """

    def __init__(self, window_seconds: float = 3600.0):
        self._window = window_seconds
        self._records: Deque[Tuple[float, bool, int, int]] = deque()
        # (ts, is_hit, cache_read_tokens, cache_creation_tokens)
        self._lock = Lock()

    def record_response(self, usage: Any) -> None:
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
        is_hit = cache_read > 0
        with self._lock:
            self._records.append((time.time(), is_hit, cache_read, cache_creation))

    def summary(self) -> dict:
        cutoff = time.time() - self._window
        with self._lock:
            recent = [(hit, read, create)
                      for ts, hit, read, create in self._records if ts >= cutoff]
        if not recent:
            return {"requests": 0}

        hits = sum(1 for hit, _, _ in recent if hit)
        total_read = sum(r for _, r, _ in recent)
        total_created = sum(c for _, _, c in recent)

        return {
            "requests": len(recent),
            "cache_hits": hits,
            "cache_misses": len(recent) - hits,
            "hit_rate": round(hits / len(recent), 4),
            "cache_read_tokens_total": total_read,
            "cache_creation_tokens_total": total_created,
            "estimated_tokens_saved": total_read,
        }
```

## Solution 5: Prefix Stability Monitor

```python
import hashlib
from typing import Dict, List, Optional


class PrefixStabilityMonitor:
    """
    Detects when the cached prefix content changes between calls,
    which would invalidate the cache and cause an unexpected cache miss.
    """

    def __init__(self):
        self._last_hash: Optional[str] = None
        self._invalidation_count = 0

    def _hash(self, content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def check(self, prefix_content: str) -> dict:
        current_hash = self._hash(prefix_content)
        if self._last_hash is None:
            self._last_hash = current_hash
            return {"stable": True, "first_observation": True, "hash": current_hash}

        stable = current_hash == self._last_hash
        if not stable:
            self._invalidation_count += 1
            self._last_hash = current_hash

        return {
            "stable": stable,
            "hash": current_hash,
            "previous_hash": self._last_hash if not stable else current_hash,
            "invalidation_count": self._invalidation_count,
        }
```

## Solution 6: Prefix Caching Dashboard

```python
import time
from typing import Optional


class PromptPrefixCachingDashboard:
    """
    Renders cache hit rates, warmup status, prefix stability,
    and token savings estimates for operational visibility.
    """

    def __init__(
        self,
        hit_tracker: CacheHitRateTracker,
        stability_monitor: PrefixStabilityMonitor,
        warmup_runner: PromptPrefixCacheWarmupRunner,
        prefix_tokens: int = 0,
        cost_per_million_input_tokens: float = 3.0,
    ):
        self._hit_tracker = hit_tracker
        self._stability = stability_monitor
        self._warmup = warmup_runner
        self._prefix_tokens = prefix_tokens
        self._cost_per_m = cost_per_million_input_tokens

    def render(self) -> dict:
        summary = self._hit_tracker.summary()
        tokens_saved = summary.get("cache_read_tokens_total", 0)
        cost_saved = round(tokens_saved / 1_000_000 * self._cost_per_m, 6)
        last_warmup = self._warmup._last_warmup

        return {
            "generated_at": time.time(),
            "cache_performance": {
                **summary,
                "estimated_cost_saved_usd": cost_saved,
            },
            "prefix_stability": {
                "invalidations": self._stability._invalidation_count,
            },
            "warmup": {
                "last_succeeded": last_warmup.succeeded if last_warmup else None,
                "last_duration_ms": last_warmup.duration_ms if last_warmup else None,
                "cache_creation_tokens": last_warmup.cache_creation_tokens if last_warmup else None,
            },
        }
```

## Comparison

| Approach | Static Segment Marking | Cache Control Assembly | Cache Warming | Hit Rate Tracking | Stability Monitoring |
|---|---|---|---|---|---|
| PromptSegmentStabilityClassifier | Yes | No | No | No | No |
| CacheControlPromptAssembler | Via classifier | Yes | No | No | No |
| PromptPrefixCacheWarmupRunner | No | No | Yes | No | No |
| CacheHitRateTracker | No | No | No | Yes | No |
| PrefixStabilityMonitor | No | No | No | No | Yes |
| PromptPrefixCachingDashboard | No | No | No | No | No |

**Best for production**: Place all static content (system instructions, tool definitions, examples) before any session-specific or dynamic content — prefix stability is the single most important correctness requirement. Run `PromptPrefixCacheWarmupRunner.warm()` at agent startup and after every deployment so the cache is hot before the first real user request. Monitor `hit_rate` via `CacheHitRateTracker` — a hit rate below 0.80 in steady state indicates prefix instability; use `PrefixStabilityMonitor` to identify which calls are changing the prefix. At Anthropic's pricing, a 2,000-token system prompt cached across 1,000 requests saves ~\$6 — at scale, prefix caching is one of the highest-ROI optimizations available.
