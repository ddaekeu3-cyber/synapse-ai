---
title: "Agent Doesn't Implement Speculative Prefetching for Predictable Tool Calls"
description: "Agents that only invoke tools after the LLM explicitly requests them pay full latency for every call in sequence. Many tool calls are predictable from context: a user asking about a company will almost certainly need its profile, recent news, and stock price. Implement speculative prefetching that predicts likely tool calls from the incoming query, fires them in the background before the LLM requests them, and serves results from cache when the LLM catches up."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-speculative-prefetching-for-predictable-tool-calls
tags: [speculative-prefetch, tool-prefetch, latency-reduction, predictive-execution, background-fetch, cache-warmup]
symptoms:
  - "Tool calls fire sequentially even though their inputs are known from the original query"
  - "First tool result takes 800ms; the LLM could have had it ready before it asked"
  - "No mechanism to start background data fetches while the LLM is generating its plan"
  - "Predictable follow-up queries always pay full round-trip latency"
  - "Cache is cold at the start of every session even for common query patterns"
---

## Why This Happens

Standard tool-calling loops wait for the LLM to emit a tool-call token, then execute the tool, then feed the result back. The LLM's planning phase (generating the tool call) and the tool's execution phase are fully serialized. Speculative prefetching breaks this dependency: a lightweight prediction model identifies which tools are likely to be called given the query, fires them immediately in the background, and stores results in a short-lived prefetch cache. When the LLM later requests the same tool with the same arguments, the result is already available — zero additional latency.

## Solution 1: Prefetch Prediction Rule

```python
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass
class PrefetchPrediction:
    tool_name: str
    predicted_args: Dict[str, Any]
    confidence: float   # 0.0–1.0
    trigger_pattern: str = ""


@dataclass
class PrefetchRule:
    """
    A rule that fires when a query matches `trigger_pattern` and
    predicts one or more tool calls with argument extraction from the query.
    """
    rule_id: str
    trigger_pattern: str                        # regex against the user query
    tool_name: str
    arg_extractor: Callable[[re.Match], Dict[str, Any]]
    base_confidence: float = 0.80
    min_confidence_to_fire: float = 0.60

    def match(self, query: str) -> Optional[PrefetchPrediction]:
        m = re.search(self.trigger_pattern, query, re.IGNORECASE)
        if not m:
            return None
        args = self.arg_extractor(m)
        return PrefetchPrediction(
            tool_name=self.tool_name,
            predicted_args=args,
            confidence=self.base_confidence,
            trigger_pattern=self.trigger_pattern,
        )
```

## Solution 2: Prefetch Cache

```python
import asyncio
import hashlib
import json
import time
from typing import Any, Dict, Optional


class PrefetchCache:
    """
    Short-lived in-process cache for speculative prefetch results.
    Entries expire after ttl_seconds to prevent stale data from being served.
    Key: (tool_name, sorted JSON of args).
    """

    def __init__(self, ttl_seconds: float = 30.0, max_entries: int = 256):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._store: Dict[str, tuple] = {}   # key -> (value, stored_at, future_or_none)

    def _key(self, tool_name: str, args: Dict[str, Any]) -> str:
        payload = json.dumps({"tool": tool_name, "args": args}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def put(self, tool_name: str, args: Dict[str, Any], value: Any) -> None:
        if len(self._store) >= self._max:
            self._evict_oldest()
        key = self._key(tool_name, args)
        self._store[key] = (value, time.time(), None)

    def put_future(
        self,
        tool_name: str,
        args: Dict[str, Any],
        future: asyncio.Future,
    ) -> None:
        if len(self._store) >= self._max:
            self._evict_oldest()
        key = self._key(tool_name, args)
        self._store[key] = (None, time.time(), future)

    async def get(
        self,
        tool_name: str,
        args: Dict[str, Any],
    ) -> Optional[Any]:
        key = self._key(tool_name, args)
        entry = self._store.get(key)
        if not entry:
            return None
        value, stored_at, future = entry
        if time.time() - stored_at > self._ttl:
            del self._store[key]
            return None
        if future is not None:
            try:
                value = await asyncio.wait_for(asyncio.shield(future), timeout=self._ttl)
                self._store[key] = (value, stored_at, None)
            except Exception:
                del self._store[key]
                return None
        return value

    def _evict_oldest(self) -> None:
        if not self._store:
            return
        oldest_key = min(self._store, key=lambda k: self._store[k][1])
        del self._store[oldest_key]

    def stats(self) -> dict:
        return {
            "entries": len(self._store),
            "max_entries": self._max,
            "ttl_seconds": self._ttl,
        }
```

## Solution 3: Speculative Prefetcher

```python
import asyncio
from typing import Any, Callable, Dict, List


class SpeculativePrefetcher:
    """
    Evaluates prefetch rules against an incoming query and fires
    background tasks for all predictions above the confidence threshold.
    Results are stored in PrefetchCache for later retrieval.
    """

    def __init__(
        self,
        rules: List[PrefetchRule],
        cache: PrefetchCache,
        max_concurrent_prefetches: int = 4,
    ):
        self._rules = rules
        self._cache = cache
        self._semaphore = asyncio.Semaphore(max_concurrent_prefetches)
        self._prefetch_hits = 0
        self._prefetch_misses = 0
        self._prefetch_fired = 0

    def predict(self, query: str) -> List[PrefetchPrediction]:
        predictions = []
        for rule in self._rules:
            prediction = rule.match(query)
            if prediction and prediction.confidence >= rule.min_confidence_to_fire:
                predictions.append(prediction)
        return predictions

    def fire_prefetches(
        self,
        query: str,
        tool_registry: Dict[str, Callable],
    ) -> List[asyncio.Task]:
        predictions = self.predict(query)
        tasks = []
        for pred in predictions:
            tool_fn = tool_registry.get(pred.tool_name)
            if tool_fn is None:
                continue
            future: asyncio.Future = asyncio.get_event_loop().create_future()
            self._cache.put_future(pred.tool_name, pred.predicted_args, future)
            task = asyncio.create_task(
                self._execute_prefetch(pred, tool_fn, future)
            )
            tasks.append(task)
            self._prefetch_fired += 1
        return tasks

    async def _execute_prefetch(
        self,
        pred: PrefetchPrediction,
        tool_fn: Callable,
        future: asyncio.Future,
    ) -> None:
        async with self._semaphore:
            try:
                result = await tool_fn(**pred.predicted_args)
                if not future.done():
                    future.set_result(result)
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)

    async def get_or_execute(
        self,
        tool_name: str,
        args: Dict[str, Any],
        tool_fn: Callable,
    ) -> Any:
        """
        Returns prefetch cache result if available; otherwise executes the tool.
        """
        cached = await self._cache.get(tool_name, args)
        if cached is not None:
            self._prefetch_hits += 1
            return cached
        self._prefetch_misses += 1
        return await tool_fn(**args)

    def stats(self) -> dict:
        total = self._prefetch_hits + self._prefetch_misses
        return {
            "prefetches_fired": self._prefetch_fired,
            "cache_hits": self._prefetch_hits,
            "cache_misses": self._prefetch_misses,
            "hit_rate": round(self._prefetch_hits / max(total, 1), 4),
        }
```

## Solution 4: Prefetch-Aware Tool Dispatcher

```python
import asyncio
from typing import Any, Callable, Dict, List, Optional


class PrefetchAwareToolDispatcher:
    """
    Drop-in replacement for direct tool invocation.
    On receiving a query, fires speculative prefetches immediately.
    On tool call dispatch, serves from prefetch cache if available.
    """

    def __init__(
        self,
        prefetcher: SpeculativePrefetcher,
        tool_registry: Dict[str, Callable],
    ):
        self._prefetcher = prefetcher
        self._registry = tool_registry
        self._prefetch_tasks: List[asyncio.Task] = []

    def on_query_received(self, query: str) -> None:
        """Call this as soon as the user query arrives — before LLM processing."""
        self._prefetch_tasks = self._prefetcher.fire_prefetches(
            query, self._registry
        )

    async def call_tool(self, tool_name: str, args: Dict[str, Any]) -> Any:
        """Called by the tool-call loop when the LLM requests a tool."""
        tool_fn = self._registry.get(tool_name)
        if tool_fn is None:
            raise KeyError(f"tool '{tool_name}' not registered")
        return await self._prefetcher.get_or_execute(tool_name, args, tool_fn)

    async def await_all_prefetches(self) -> None:
        """Optionally await all background prefetches before session ends."""
        if self._prefetch_tasks:
            await asyncio.gather(*self._prefetch_tasks, return_exceptions=True)
            self._prefetch_tasks = []
```

## Solution 5: Prefetch Rule Builder

```python
import re
from typing import Any, Dict


class PrefetchRuleBuilder:
    """
    Fluent builder for common prefetch rule patterns.
    """

    @staticmethod
    def entity_lookup(
        rule_id: str,
        entity_pattern: str,
        tool_name: str,
        arg_name: str = "entity_id",
        confidence: float = 0.85,
    ) -> PrefetchRule:
        """Prefetch a lookup tool whenever a named entity appears in the query."""
        def extractor(m: re.Match) -> Dict[str, Any]:
            return {arg_name: m.group(1).strip()}

        return PrefetchRule(
            rule_id=rule_id,
            trigger_pattern=entity_pattern,
            tool_name=tool_name,
            arg_extractor=extractor,
            base_confidence=confidence,
        )

    @staticmethod
    def keyword_prefetch(
        rule_id: str,
        keyword: str,
        tool_name: str,
        static_args: Dict[str, Any],
        confidence: float = 0.70,
    ) -> PrefetchRule:
        """Prefetch a tool with fixed args whenever a keyword appears."""
        def extractor(m: re.Match) -> Dict[str, Any]:
            return static_args

        return PrefetchRule(
            rule_id=rule_id,
            trigger_pattern=re.escape(keyword),
            tool_name=tool_name,
            arg_extractor=extractor,
            base_confidence=confidence,
        )
```

## Solution 6: Prefetch Effectiveness Monitor

```python
import time
from typing import List


class PrefetchEffectivenessMonitor:
    """
    Tracks whether prefetching is providing real latency savings.
    Compares prefetch hit rate against the prediction firing rate
    to detect rules that fire frequently but miss the actual tool calls.
    """

    def __init__(
        self,
        prefetcher: SpeculativePrefetcher,
        min_hit_rate_to_keep_rule: float = 0.30,
    ):
        self._prefetcher = prefetcher
        self._min_hit_rate = min_hit_rate_to_keep_rule

    def report(self) -> dict:
        stats = self._prefetcher.stats()
        alerts = []

        if stats["prefetches_fired"] > 20 and stats["hit_rate"] < self._min_hit_rate:
            alerts.append({
                "type": "low_prefetch_hit_rate",
                "hit_rate": stats["hit_rate"],
                "target": self._min_hit_rate,
                "recommendation": (
                    "Prefetch rules are firing but LLM is not calling the predicted tools. "
                    "Review trigger patterns or reduce base_confidence."
                ),
            })

        waste_rate = (
            (stats["prefetches_fired"] - stats["cache_hits"]) / max(stats["prefetches_fired"], 1)
        )
        if waste_rate > 0.80 and stats["prefetches_fired"] > 50:
            alerts.append({
                "type": "high_prefetch_waste",
                "waste_rate": round(waste_rate, 4),
                "recommendation": "Most prefetches are wasted; increase min_confidence_to_fire.",
            })

        return {
            "generated_at": time.time(),
            "stats": stats,
            "healthy": len(alerts) == 0,
            "alerts": alerts,
        }
```

## Comparison

| Approach | Pattern Matching | Background Execution | Cache Lookup | Dispatcher Integration | Monitoring |
|---|---|---|---|---|---|
| PrefetchRule | Yes (regex) | No | No | No | No |
| PrefetchCache | No | No (stores futures) | Yes (await future) | No | No |
| SpeculativePrefetcher | Via rules | Yes (asyncio tasks) | Via cache | No | No |
| PrefetchAwareToolDispatcher | No | Via prefetcher | Via prefetcher | Yes | No |
| PrefetchEffectivenessMonitor | No | No | No | No | Yes |

**Best for production**: Call `PrefetchAwareToolDispatcher.on_query_received()` immediately after receiving the user message — before passing it to the LLM. This gives prefetches the full LLM planning time (typically 200–800ms) to complete in the background. Set `ttl_seconds=30` on the cache — long enough to cover the planning phase, short enough to prevent stale data. Start with high-confidence rules (0.85+) for entity lookups where the tool argument can be extracted directly from the query text. Monitor `hit_rate`: if it is below 30% after 50+ fired prefetches, the rules are misfiring and should be revised rather than kept.
