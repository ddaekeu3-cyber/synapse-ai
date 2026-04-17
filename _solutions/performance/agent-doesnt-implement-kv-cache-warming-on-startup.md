---
title: "Agent Doesn't Implement KV Cache Warming on Startup"
description: "Agents that initialize without pre-warming their key-value caches (system prompt cache, tool definition cache, embedding lookup cache) pay full compute cost on every first request after startup — inflating cold-start latency and time-to-first-token for early users. Implement KV cache warming that pre-populates the most expensive cache entries during initialization, before the agent begins serving traffic."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-kv-cache-warming-on-startup
tags: [kv-cache, cache-warming, startup-optimization, time-to-first-token, prompt-caching, initialization]
symptoms:
  - "First few requests after deployment are significantly slower than steady-state"
  - "System prompt prefix must be re-tokenized and re-processed on every cold start"
  - "Tool definition embeddings recomputed from scratch after each restart"
  - "No mechanism to pre-populate frequently-used cache entries before serving begins"
  - "P99 latency spikes on every deployment window when caches are cold"
---

## Why This Happens

LLM inference with prompt caching (e.g., Anthropic's cache_control, OpenAI's prompt caching) requires the cached prefix to have been processed at least once before the cache hit occurs. After a restart, all cache entries are cold — the first request that includes the system prompt pays the full prompt-processing cost. For a 2000-token system prompt, this can add 200–400ms to the first response. KV cache warming pre-sends the cacheable prefixes during initialization as dummy requests, so that by the time real user traffic arrives, the cache is warm and subsequent requests hit the cached prefix immediately.

## Solution 1: Cache Warm-Up Target

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class WarmUpTargetType(str, Enum):
    SYSTEM_PROMPT = "system_prompt"
    TOOL_DEFINITIONS = "tool_definitions"
    EMBEDDING = "embedding"
    CUSTOM = "custom"


@dataclass
class CacheWarmUpTarget:
    name: str
    target_type: WarmUpTargetType
    warm_up_fn: Callable         # async callable that executes the warm-up
    priority: int = 1            # lower = warmed up first
    timeout_seconds: float = 30.0
    required: bool = True        # if True, failure blocks startup readiness
    metadata: Dict[str, Any] = field(default_factory=dict)
```

## Solution 2: Warm-Up Result

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class WarmUpStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class WarmUpResult:
    target_name: str
    status: WarmUpStatus
    duration_ms: Optional[float] = None
    error: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    def latency_ms(self) -> Optional[float]:
        if self.started_at and self.finished_at:
            return round((self.finished_at - self.started_at) * 1000, 2)
        return None
```

## Solution 3: KV Cache Warmer

```python
import asyncio
import time
from typing import Callable, List, Optional


class KVCacheWarmer:
    """
    Executes a list of cache warm-up targets in priority order,
    with per-target timeouts and failure handling.
    """

    def __init__(self, targets: List[CacheWarmUpTarget]):
        self._targets = sorted(targets, key=lambda t: t.priority)
        self._results: List[WarmUpResult] = []

    async def warm_up(self) -> dict:
        self._results = []
        total_start = time.time()
        has_required_failure = False

        for target in self._targets:
            result = WarmUpResult(
                target_name=target.name,
                status=WarmUpStatus.RUNNING,
            )
            try:
                await asyncio.wait_for(
                    target.warm_up_fn(),
                    timeout=target.timeout_seconds,
                )
                result.status = WarmUpStatus.SUCCESS
                result.finished_at = time.time()
            except asyncio.TimeoutError:
                result.status = WarmUpStatus.FAILED
                result.finished_at = time.time()
                result.error = f"timed_out_after_{target.timeout_seconds}s"
                if target.required:
                    has_required_failure = True
            except Exception as exc:
                result.status = WarmUpStatus.FAILED
                result.finished_at = time.time()
                result.error = str(exc)
                if target.required:
                    has_required_failure = True

            self._results.append(result)

        total_ms = round((time.time() - total_start) * 1000, 2)
        return {
            "completed_at": time.time(),
            "total_duration_ms": total_ms,
            "passed": not has_required_failure,
            "targets_warmed": sum(1 for r in self._results if r.status == WarmUpStatus.SUCCESS),
            "targets_failed": sum(1 for r in self._results if r.status == WarmUpStatus.FAILED),
            "results": self._results,
        }

    def results(self) -> List[WarmUpResult]:
        return list(self._results)
```

## Solution 4: System Prompt Cache Primer

```python
from typing import Any, Callable


class SystemPromptCachePrimer:
    """
    Pre-warms the LLM provider's prompt cache by sending a minimal
    completion request with the system prompt marked as cacheable.
    The response is discarded — only the cache population matters.
    """

    def __init__(
        self,
        system_prompt: str,
        llm_fn: Callable[[str, str], Any],   # (system, user) -> response
        primer_user_message: str = "Hello.",
    ):
        self._system = system_prompt
        self._llm = llm_fn
        self._primer_msg = primer_user_message

    async def warm(self) -> None:
        """Send a minimal request to populate the system prompt cache."""
        await self._llm(self._system, self._primer_msg)

    def as_target(self) -> CacheWarmUpTarget:
        return CacheWarmUpTarget(
            name="system_prompt_cache",
            target_type=WarmUpTargetType.SYSTEM_PROMPT,
            warm_up_fn=self.warm,
            priority=1,
            timeout_seconds=15.0,
            required=False,   # cache miss on first request is degraded, not fatal
        )
```

## Solution 5: Embedding Cache Pre-Loader

```python
from typing import Any, Callable, List


class EmbeddingCachePreLoader:
    """
    Pre-computes embeddings for a list of seed texts (common queries,
    tool descriptions, few-shot examples) and stores them in the
    embedding cache before the agent begins serving traffic.
    """

    def __init__(
        self,
        seed_texts: List[str],
        embed_fn: Callable[[str], Any],
        cache_store: Any,        # any object with a put(text, embedding) method
    ):
        self._seeds = seed_texts
        self._embed = embed_fn
        self._cache = cache_store

    async def warm(self) -> None:
        for text in self._seeds:
            embedding = await self._embed(text)
            self._cache.put(text, embedding)

    def as_target(self, priority: int = 2) -> CacheWarmUpTarget:
        return CacheWarmUpTarget(
            name="embedding_cache",
            target_type=WarmUpTargetType.EMBEDDING,
            warm_up_fn=self.warm,
            priority=priority,
            timeout_seconds=60.0,
            required=False,
            metadata={"seed_count": len(self._seeds)},
        )
```

## Solution 6: Warm-Up Dashboard

```python
import time
from typing import List, Optional


class CacheWarmUpDashboard:
    """
    Reports cache warm-up results for startup observability
    and SLO tracking of initialization completeness.
    """

    def __init__(self, warmer: KVCacheWarmer):
        self._warmer = warmer

    def render(self) -> dict:
        results = self._warmer.results()
        by_status: dict = {}
        for r in results:
            by_status[r.status.value] = by_status.get(r.status.value, 0) + 1

        slowest = max(results, key=lambda r: r.latency_ms() or 0, default=None)

        return {
            "generated_at": time.time(),
            "total_targets": len(results),
            "by_status": by_status,
            "slowest_target": {
                "name": slowest.target_name,
                "duration_ms": slowest.latency_ms(),
            } if slowest else None,
            "per_target": [
                {
                    "name": r.target_name,
                    "status": r.status.value,
                    "duration_ms": r.latency_ms(),
                    "error": r.error,
                }
                for r in results
            ],
        }
```

## Comparison

| Approach | Priority Ordering | Per-Target Timeout | System Prompt Priming | Embedding Pre-load | Dashboard |
|---|---|---|---|---|---|
| KVCacheWarmer | Yes | Yes | Via targets | Via targets | No |
| SystemPromptCachePrimer | No | Via warmer | Yes | No | No |
| EmbeddingCachePreLoader | No | Via warmer | No | Yes | No |
| CacheWarmUpDashboard | No | No | No | No | Yes |

**Best for production**: Mark system prompt priming as `required=False` — a cache miss on the first request is a latency degradation, not a correctness failure; blocking startup on a non-critical cache miss is worse than serving slightly slower. Run warm-up targets in parallel when they are independent (system prompt and embeddings can be warmed concurrently). Set `timeout_seconds=15.0` for LLM warm-up calls — if the model is unresponsive for 15 seconds during initialization, the healthcheck should fail and the instance should not receive traffic anyway. Log `CacheWarmUpDashboard.render()` as a structured event at the end of initialization so deployment pipelines can verify that all warm-up targets succeeded before routing traffic to the new instance.
