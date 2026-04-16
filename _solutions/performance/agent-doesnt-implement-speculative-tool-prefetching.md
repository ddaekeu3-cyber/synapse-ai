---
title: "Agent Doesn't Implement Speculative Tool Prefetching"
description: "Agents that wait for one tool call to complete before dispatching the next pay full sequential latency even when subsequent tool calls are predictable from the current context — a user asking for a stock price almost always follows up with financial ratios, a city lookup almost always triggers a weather call. Implement speculative prefetching that predicts and pre-executes likely next tool calls while the current response is being processed."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-speculative-tool-prefetching
tags: [speculative-prefetch, tool-prefetching, latency-hiding, predictive-execution, pipeline-optimization, prefetch-cache]
symptoms:
  - "Sequential tool calls each add full round-trip latency even when the sequence is predictable"
  - "User waits 3 seconds for tool A, then 3 more for tool B that was clearly needed after A"
  - "No prediction mechanism — every tool call is reactive, never proactive"
  - "Tool result caches are cold at the start of common multi-step workflows"
  - "Agent pipeline latency is sum of all tool latencies rather than critical-path latency"
---

## Why This Happens

Sequential tool dispatch is safe but slow. When a user asks "what's the weather and flight status for my trip to Tokyo?", the agent dispatches the weather tool, waits, then dispatches the flights tool. If both tools could have run in parallel — or if the second call was predictable enough to start speculatively — the user experiences sum-of-latencies instead of max-of-latencies. Speculative prefetching works by maintaining a prediction model (even a simple rule-based one) that maps tool call outcomes to likely follow-up calls, fires those calls in the background with low priority, and serves from the prefetch cache if the prediction was correct.

## Solution 1: Tool Sequence Predictor

```python
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class PrefetchPrediction:
    trigger_tool: str
    predicted_tool: str
    confidence: float
    suggested_args_template: Optional[Dict] = None


class ToolSequencePredictor:
    """
    Learns tool call sequences from history and predicts which tool
    will be called next given the current tool that just completed.
    """

    def __init__(self, min_confidence: float = 0.60, min_observations: int = 5):
        self._sequences: Dict[str, Counter] = defaultdict(Counter)
        self._min_confidence = min_confidence
        self._min_observations = min_observations

    def record_sequence(self, tool_a: str, tool_b: str) -> None:
        """Record that tool_b was called after tool_a."""
        self._sequences[tool_a][tool_b] += 1

    def predict(self, current_tool: str) -> List[PrefetchPrediction]:
        counts = self._sequences.get(current_tool)
        if not counts:
            return []
        total = sum(counts.values())
        if total < self._min_observations:
            return []
        predictions = []
        for next_tool, count in counts.most_common(3):
            confidence = count / total
            if confidence >= self._min_confidence:
                predictions.append(PrefetchPrediction(
                    trigger_tool=current_tool,
                    predicted_tool=next_tool,
                    confidence=round(confidence, 4),
                ))
        return predictions

    def top_sequences(self) -> List[Tuple[str, str, float]]:
        result = []
        for trigger, counts in self._sequences.items():
            total = sum(counts.values())
            for next_tool, count in counts.most_common(1):
                result.append((trigger, next_tool, round(count / max(total, 1), 4)))
        return sorted(result, key=lambda x: -x[2])
```

## Solution 2: Prefetch Result Cache

```python
import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class PrefetchEntry:
    key: str
    tool_name: str
    args: Dict[str, Any]
    future: asyncio.Future
    started_at: float = field(default_factory=time.time)
    result: Optional[Any] = None
    error: Optional[Exception] = None
    used: bool = False
    ttl_seconds: float = 30.0

    def is_expired(self) -> bool:
        return time.time() - self.started_at > self.ttl_seconds


class PrefetchResultCache:
    """
    Stores in-flight and completed speculative prefetch results.
    Entries expire after TTL to avoid serving stale prefetched data.
    """

    def __init__(self, ttl_seconds: float = 30.0):
        self._ttl = ttl_seconds
        self._entries: Dict[str, PrefetchEntry] = {}
        self._lock = asyncio.Lock()
        self._hits = 0
        self._misses = 0
        self._wasted = 0  # prefetched but expired before use

    @staticmethod
    def _key(tool_name: str, args: Dict[str, Any]) -> str:
        payload = json.dumps({"tool": tool_name, "args": args}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    async def store(
        self,
        tool_name: str,
        args: Dict[str, Any],
        future: asyncio.Future,
    ) -> str:
        key = self._key(tool_name, args)
        async with self._lock:
            self._entries[key] = PrefetchEntry(
                key=key,
                tool_name=tool_name,
                args=args,
                future=future,
                ttl_seconds=self._ttl,
            )
        return key

    async def consume(
        self,
        tool_name: str,
        args: Dict[str, Any],
    ) -> Optional[Any]:
        key = self._key(tool_name, args)
        async with self._lock:
            entry = self._entries.pop(key, None)

        if entry is None:
            self._misses += 1
            return None

        if entry.is_expired():
            self._wasted += 1
            return None

        try:
            result = await asyncio.wait_for(asyncio.shield(entry.future), timeout=0.1)
            entry.used = True
            self._hits += 1
            return result
        except (asyncio.TimeoutError, Exception):
            self._misses += 1
            return None

    def hit_rate(self) -> float:
        total = self._hits + self._misses
        return round(self._hits / max(total, 1), 4)

    def stats(self) -> dict:
        return {
            "hits": self._hits,
            "misses": self._misses,
            "wasted_prefetches": self._wasted,
            "hit_rate": self.hit_rate(),
            "pending_entries": len(self._entries),
        }
```

## Solution 3: Speculative Prefetch Dispatcher

```python
import asyncio
from typing import Any, Callable, Dict, List, Optional


class SpeculativePrefetchDispatcher:
    """
    Given a completed tool call, predicts likely follow-up tools and
    pre-executes them at low priority using asyncio background tasks.
    """

    def __init__(
        self,
        predictor: ToolSequencePredictor,
        cache: PrefetchResultCache,
        max_concurrent_prefetches: int = 3,
    ):
        self._predictor = predictor
        self._cache = cache
        self._semaphore = asyncio.Semaphore(max_concurrent_prefetches)
        self._prefetch_count = 0
        self._cancelled_count = 0

    async def on_tool_completed(
        self,
        completed_tool: str,
        completed_args: Dict[str, Any],
        completed_result: Any,
        tool_fn: Callable,
        args_resolver: Optional[Callable] = None,
    ) -> List[str]:
        """
        Called after a tool completes. Returns list of prefetch keys started.
        args_resolver(prediction, completed_args, completed_result) -> dict | None
        """
        predictions = self._predictor.predict(completed_tool)
        started_keys = []

        for prediction in predictions:
            args = {}
            if args_resolver:
                resolved = args_resolver(prediction, completed_args, completed_result)
                if resolved is None:
                    continue
                args = resolved

            loop = asyncio.get_event_loop()
            future: asyncio.Future = loop.create_future()
            key = await self._cache.store(prediction.predicted_tool, args, future)

            asyncio.create_task(
                self._execute_prefetch(prediction.predicted_tool, args, tool_fn, future)
            )
            self._prefetch_count += 1
            started_keys.append(key)

        return started_keys

    async def _execute_prefetch(
        self,
        tool_name: str,
        args: Dict[str, Any],
        tool_fn: Callable,
        future: asyncio.Future,
    ) -> None:
        async with self._semaphore:
            try:
                result = await tool_fn(tool_name, args)
                if not future.done():
                    future.set_result(result)
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)

    def stats(self) -> dict:
        return {
            "prefetch_tasks_started": self._prefetch_count,
            **self._cache.stats(),
        }
```

## Solution 4: Prefetch-Aware Tool Caller

```python
import asyncio
import time
from typing import Any, Callable, Dict, Optional


class PrefetchAwareToolCaller:
    """
    Checks the prefetch cache before executing a tool call.
    Falls back to direct execution on cache miss.
    Records sequence data to improve future predictions.
    """

    def __init__(
        self,
        cache: PrefetchResultCache,
        predictor: ToolSequencePredictor,
        prefetch_dispatcher: SpeculativePrefetchDispatcher,
    ):
        self._cache = cache
        self._predictor = predictor
        self._dispatcher = prefetch_dispatcher
        self._last_tool: Optional[str] = None

    async def call(
        self,
        tool_name: str,
        args: Dict[str, Any],
        tool_fn: Callable,
        args_resolver: Optional[Callable] = None,
    ) -> dict:
        start = time.time()

        # Record sequence
        if self._last_tool:
            self._predictor.record_sequence(self._last_tool, tool_name)

        # Try prefetch cache first
        cached = await self._cache.consume(tool_name, args)
        if cached is not None:
            latency_ms = round((time.time() - start) * 1000, 2)
            self._last_tool = tool_name
            result = cached
            source = "prefetch_cache"
        else:
            result = await tool_fn(tool_name, args)
            latency_ms = round((time.time() - start) * 1000, 2)
            self._last_tool = tool_name
            source = "direct_execution"

        # Trigger prefetching for next predicted tools
        await self._dispatcher.on_tool_completed(
            tool_name, args, result, tool_fn, args_resolver
        )

        return {
            "result": result,
            "tool_name": tool_name,
            "source": source,
            "latency_ms": latency_ms,
        }
```

## Solution 5: Prefetch Accuracy Tracker

```python
import time
from typing import List, Tuple


class PrefetchAccuracyTracker:
    """
    Tracks prediction accuracy over time — what fraction of prefetches
    were actually consumed vs. wasted (prefetched but never requested).
    """

    def __init__(self):
        self._log: List[Tuple[float, str, str, bool]] = []
        # (ts, trigger_tool, predicted_tool, was_hit)

    def record(self, trigger: str, predicted: str, was_hit: bool) -> None:
        self._log.append((time.time(), trigger, predicted, was_hit))

    def accuracy(self, window_seconds: float = 3600.0) -> float:
        cutoff = time.time() - window_seconds
        recent = [(t, p, h) for ts, t, p, h in self._log if ts >= cutoff]
        if not recent:
            return 0.0
        return round(sum(1 for _, _, h in recent if h) / len(recent), 4)

    def top_accurate_predictions(self, window_seconds: float = 3600.0) -> List[dict]:
        from collections import Counter
        cutoff = time.time() - window_seconds
        hits: Counter = Counter()
        total: Counter = Counter()
        for ts, trigger, predicted, hit in self._log:
            if ts < cutoff:
                continue
            key = f"{trigger}->{predicted}"
            total[key] += 1
            if hit:
                hits[key] += 1
        return [
            {"sequence": k, "hit_rate": round(hits[k] / total[k], 4), "count": total[k]}
            for k in sorted(total, key=lambda k: -hits.get(k, 0) / total[k])
            if total[k] >= 5
        ][:10]
```

## Solution 6: Speculative Prefetch Dashboard

```python
import time


class SpeculativePrefetchDashboard:
    """
    Combines predictor sequences, cache stats, dispatcher stats,
    and accuracy tracking into an operational prefetch health report.
    """

    def __init__(
        self,
        predictor: ToolSequencePredictor,
        cache: PrefetchResultCache,
        dispatcher: SpeculativePrefetchDispatcher,
        accuracy_tracker: PrefetchAccuracyTracker,
    ):
        self._predictor = predictor
        self._cache = cache
        self._dispatcher = dispatcher
        self._accuracy = accuracy_tracker

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "top_predicted_sequences": self._predictor.top_sequences()[:5],
            "cache_stats": self._cache.stats(),
            "dispatcher_stats": self._dispatcher.stats(),
            "accuracy_1h": self._accuracy.accuracy(window_seconds=3600.0),
            "top_accurate_predictions": self._accuracy.top_accurate_predictions(),
        }
```

## Comparison

| Approach | Sequence Learning | Prefetch Execution | Cache Serving | Accuracy Tracking | Dashboard |
|---|---|---|---|---|---|
| ToolSequencePredictor | Yes (Counter) | No | No | No | No |
| PrefetchResultCache | No | No | Yes (TTL) | No | No |
| SpeculativePrefetchDispatcher | Via predictor | Yes (background) | Via cache | No | No |
| PrefetchAwareToolCaller | Via predictor | Via dispatcher | Via cache | No | No |
| PrefetchAccuracyTracker | No | No | No | Yes | No |
| SpeculativePrefetchDashboard | No | No | No | No | Yes |

**Best for production**: Start prefetching only for sequences with confidence >= 0.70 observed over at least 5 sessions — lower thresholds waste downstream API quota on incorrect predictions. Set `ttl_seconds=30` so a prefetched result that goes unused does not serve stale data if eventually consumed late. Monitor `wasted_prefetches`: if it exceeds 40% of total prefetches, the predictor is over-eager and the confidence threshold should be raised. Use `args_resolver` to propagate result fields from the completed tool into the prefetch args — e.g., pass the returned city name from a geocoder into the weather tool's `location` argument.
