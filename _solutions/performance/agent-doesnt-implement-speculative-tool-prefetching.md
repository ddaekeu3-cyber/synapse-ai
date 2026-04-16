---
title: "Agent Doesn't Implement Speculative Tool Prefetching"
description: "Agents that execute tool calls strictly in response to LLM output wait for each round-trip before starting the next fetch — even when downstream tool calls are highly predictable from context. Implement speculative tool prefetching that identifies high-confidence follow-on calls from earlier results and starts them in the background before the LLM explicitly requests them, reducing total wall-clock latency for predictable multi-step workflows."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-speculative-tool-prefetching
tags: [speculative-prefetch, latency-reduction, parallel-tool-calls, predictive-execution, prefetch-cache, multi-step-workflow]
symptoms:
  - "Sequential tool call chains take N × round-trip time even when calls are independent"
  - "User lookup is always followed by preference fetch, but preference fetch waits for user lookup to complete"
  - "No prefetching despite highly predictable tool call sequences in structured workflows"
  - "Tool call waterfall shows long idle gaps between logically sequential but predictable calls"
  - "P50 latency could be cut significantly if predictable follow-on calls ran concurrently"
---

## Why This Happens

LLM-driven tool call execution is inherently sequential: the model receives tool results, reasons about them, and then requests the next tool. In workflows with predictable structure — fetch user → fetch preferences → fetch history — each step waits for the prior result even though later calls could start immediately using the result of the first. Speculative prefetching breaks this chain: when a trigger result arrives (e.g., a user ID is returned), the prefetcher starts fetching predicted downstream resources in the background. If the LLM then requests those resources, the results are already cached and returned immediately. If the LLM does not request them, the prefetched results are discarded.

## Solution 1: Prefetch Rule

```python
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class PrefetchRule:
    """
    Defines when to speculatively prefetch a follow-on tool call.
    trigger_tool: tool whose result activates this rule
    extract_args: callable that extracts args for the prefetch call from the trigger result
    prefetch_tool: tool to call speculatively
    confidence: 0.0–1.0; only prefetch when above min_confidence threshold
    """
    trigger_tool: str
    prefetch_tool: str
    extract_args: Callable[[Any], Optional[Dict[str, Any]]]
    confidence: float = 0.9
    ttl_seconds: float = 30.0
    tags: List[str] = field(default_factory=list)
```

## Solution 2: Prefetch Rule Registry

```python
from typing import Dict, List


class PrefetchRuleRegistry:
    """
    Stores and retrieves prefetch rules indexed by trigger tool name.
    """

    def __init__(self, min_confidence: float = 0.8):
        self._rules: Dict[str, List[PrefetchRule]] = {}
        self._min_confidence = min_confidence

    def register(self, rule: PrefetchRule) -> None:
        if rule.confidence < self._min_confidence:
            return
        self._rules.setdefault(rule.trigger_tool, []).append(rule)

    def rules_for(self, trigger_tool: str) -> List[PrefetchRule]:
        return self._rules.get(trigger_tool, [])

    def all_rules(self) -> Dict[str, List[PrefetchRule]]:
        return dict(self._rules)
```

## Solution 3: Speculative Prefetch Cache

```python
import asyncio
import time
from typing import Any, Dict, Optional, Tuple


class SpeculativePrefetchCache:
    """
    Holds in-progress and completed prefetch futures keyed by
    (tool_name, canonical_args_hash). Returns results immediately
    if ready; awaits the future if still in-flight.
    """

    def __init__(self):
        self._entries: Dict[str, Tuple[asyncio.Future, float]] = {}
        # key → (future, expires_at)
        self._hits = 0
        self._misses = 0
        self._discards = 0

    def _key(self, tool_name: str, args: Dict[str, Any]) -> str:
        import hashlib, json
        return hashlib.sha256(
            json.dumps({"t": tool_name, "a": args}, sort_keys=True).encode()
        ).hexdigest()[:24]

    def store(self, tool_name: str, args: Dict[str, Any], future: asyncio.Future, ttl: float) -> None:
        key = self._key(tool_name, args)
        self._entries[key] = (future, time.time() + ttl)

    async def get(self, tool_name: str, args: Dict[str, Any]) -> Optional[Any]:
        key = self._key(tool_name, args)
        entry = self._entries.pop(key, None)
        if entry is None:
            self._misses += 1
            return None
        future, expires_at = entry
        if time.time() > expires_at:
            self._discards += 1
            return None
        self._hits += 1
        return await future

    def evict_expired(self) -> int:
        now = time.time()
        expired = [k for k, (_, exp) in self._entries.items() if now > exp]
        for k in expired:
            del self._entries[k]
            self._discards += 1
        return len(expired)

    def stats(self) -> dict:
        return {
            "hits": self._hits,
            "misses": self._misses,
            "discards": self._discards,
            "pending": len(self._entries),
            "hit_rate": round(self._hits / max(self._hits + self._misses, 1), 4),
        }
```

## Solution 4: Speculative Prefetcher

```python
import asyncio
from typing import Any, Callable, Dict


class SpeculativePrefetcher:
    """
    After a trigger tool result arrives, evaluates applicable prefetch
    rules and starts background fetches for predicted follow-on calls.
    Results land in the prefetch cache for immediate retrieval.
    """

    def __init__(
        self,
        registry: PrefetchRuleRegistry,
        cache: SpeculativePrefetchCache,
        tool_dispatch_fn: Callable[[str, Dict[str, Any]], Any],
    ):
        self._registry = registry
        self._cache = cache
        self._dispatch = tool_dispatch_fn
        self._prefetches_started = 0
        self._prefetches_used = 0

    def on_tool_result(
        self,
        trigger_tool: str,
        trigger_result: Any,
    ) -> int:
        """Call after any tool result arrives. Returns number of prefetches started."""
        rules = self._registry.rules_for(trigger_tool)
        started = 0
        for rule in rules:
            args = rule.extract_args(trigger_result)
            if args is None:
                continue
            future = asyncio.ensure_future(self._dispatch(rule.prefetch_tool, args))
            self._cache.store(rule.prefetch_tool, args, future, rule.ttl_seconds)
            self._prefetches_started += 1
            started += 1
        return started

    async def get_prefetched(
        self,
        tool_name: str,
        args: Dict[str, Any],
    ) -> tuple[Any, bool]:
        """Returns (result, was_prefetched). was_prefetched=False means cache miss."""
        result = await self._cache.get(tool_name, args)
        if result is not None:
            self._prefetches_used += 1
            return result, True
        return None, False

    def stats(self) -> dict:
        return {
            "prefetches_started": self._prefetches_started,
            "prefetches_used": self._prefetches_used,
            "utilization_rate": round(
                self._prefetches_used / max(self._prefetches_started, 1), 4
            ),
            "cache": self._cache.stats(),
        }
```

## Solution 5: Prefetch-Aware Tool Executor

```python
import asyncio
from typing import Any, Callable, Dict


class PrefetchAwareToolExecutor:
    """
    Wraps tool dispatch to check the prefetch cache before calling upstream.
    On a cache hit the prefetched result is returned immediately.
    On a miss the call is dispatched normally and prefetch rules fire on the result.
    """

    def __init__(
        self,
        prefetcher: SpeculativePrefetcher,
        tool_dispatch_fn: Callable[[str, Dict[str, Any]], Any],
    ):
        self._prefetcher = prefetcher
        self._dispatch = tool_dispatch_fn
        self._total_calls = 0
        self._prefetch_saves = 0

    async def execute(self, tool_name: str, args: Dict[str, Any]) -> Any:
        self._total_calls += 1

        prefetched_result, was_prefetched = await self._prefetcher.get_prefetched(
            tool_name, args
        )
        if was_prefetched:
            self._prefetch_saves += 1
            self._prefetcher.on_tool_result(tool_name, prefetched_result)
            return prefetched_result

        result = await self._dispatch(tool_name, args)
        self._prefetcher.on_tool_result(tool_name, result)
        return result

    def stats(self) -> dict:
        return {
            "total_calls": self._total_calls,
            "prefetch_saves": self._prefetch_saves,
            "save_rate": round(self._prefetch_saves / max(self._total_calls, 1), 4),
            "prefetcher": self._prefetcher.stats(),
        }
```

## Solution 6: Speculative Prefetch Dashboard

```python
import time


class SpeculativePrefetchDashboard:
    """
    Combines executor stats, prefetcher stats, and cache stats
    into a single operational report for prefetch tuning.
    """

    def __init__(
        self,
        executor: PrefetchAwareToolExecutor,
        registry: PrefetchRuleRegistry,
    ):
        self._executor = executor
        self._registry = registry

    def render(self) -> dict:
        exec_stats = self._executor.stats()
        return {
            "generated_at": time.time(),
            "executor": exec_stats,
            "registered_rules": {
                tool: len(rules)
                for tool, rules in self._registry.all_rules().items()
            },
            "efficiency": {
                "prefetch_save_rate": exec_stats["save_rate"],
                "prefetch_utilization": exec_stats["prefetcher"]["utilization_rate"],
                "cache_hit_rate": exec_stats["prefetcher"]["cache"]["hit_rate"],
            },
        }
```

## Comparison

| Approach | Rule-Based Trigger | Background Fetch | Cache Lookup | Chain Propagation | Dashboard |
|---|---|---|---|---|---|
| PrefetchRuleRegistry | Yes | No | No | No | No |
| SpeculativePrefetchCache | No | No | Yes (future-backed) | No | No |
| SpeculativePrefetcher | Via registry | Yes (asyncio.ensure_future) | Via cache | Partial | No |
| PrefetchAwareToolExecutor | No | Via prefetcher | Via prefetcher | Yes | No |
| SpeculativePrefetchDashboard | No | No | No | No | Yes |

**Best for production**: Only register prefetch rules for tool pairs where the transition probability exceeds 0.85 — measure this from production traces before adding rules. Set `ttl_seconds=30` to prevent stale prefetch results from being served in slow-path sessions. Monitor `utilization_rate`: if below 0.50, the rules are too speculative and are wasting upstream capacity on fetches that the LLM rarely requests; raise the confidence threshold or remove low-value rules. Apply prefetching only to idempotent read calls — never speculatively execute tools that write state, send messages, or consume rate-limited quotas.
