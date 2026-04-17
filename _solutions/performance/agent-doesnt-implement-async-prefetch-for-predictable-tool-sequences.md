---
title: "Agent Doesn't Implement Async Prefetch for Predictable Tool Sequences"
description: "Agents that execute tool calls strictly sequentially waste wall-clock time when subsequent calls are predictable from earlier results. Implement async prefetch that detects high-confidence next-tool predictions from partial results and fires those calls speculatively in the background, so results are ready when the LLM confirms the next step."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-async-prefetch-for-predictable-tool-sequences
tags: [prefetch, speculative-execution, async-tools, latency-reduction, tool-sequencing, predictive-loading]
symptoms:
  - "Every tool call waits for the previous one to fully complete before starting"
  - "Total latency is the sum of individual tool latencies even when calls are independent"
  - "Predictable patterns like lookup-then-enrich always serialize unnecessarily"
  - "No mechanism to fire a likely next tool call while the LLM is still processing current results"
  - "P99 latency scales linearly with tool chain length"
---

## Why This Happens

LLM-driven agents are typically implemented as a request-response loop: call tool, wait for result, send to LLM, wait for next tool decision. Even when the next tool call is highly predictable from intermediate results — for example, after fetching a user record the agent almost always fetches their recent orders — the sequential model prevents overlap. Speculative prefetch breaks this by tracking historical tool sequences, computing transition probabilities, and firing likely next calls in the background while the LLM processes the current result. If the prediction is correct, the result is already waiting; if wrong, the speculative result is discarded.

## Solution 1: Tool Sequence Transition Model

```python
import time
from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, List, Optional, Tuple


@dataclass
class TransitionRecord:
    from_tool: str
    to_tool: str
    count: int = 0
    last_seen: float = field(default_factory=time.time)


class ToolSequenceTransitionModel:
    """
    Tracks observed tool-to-tool transition frequencies and computes
    next-tool probabilities from a sliding observation window.
    """

    def __init__(self, min_observations: int = 3, max_history: int = 5000):
        self._transitions: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._history: List[Tuple[float, str, str]] = []
        self._min_obs = min_observations
        self._max_history = max_history
        self._lock = Lock()

    def record(self, from_tool: str, to_tool: str) -> None:
        with self._lock:
            self._transitions[from_tool][to_tool] += 1
            self._history.append((time.time(), from_tool, to_tool))
            if len(self._history) > self._max_history:
                self._history.pop(0)

    def top_predictions(
        self,
        from_tool: str,
        top_n: int = 3,
        min_confidence: float = 0.30,
    ) -> List[Tuple[str, float]]:
        with self._lock:
            counts = dict(self._transitions.get(from_tool, {}))

        total = sum(counts.values())
        if total < self._min_obs:
            return []

        predictions = [
            (tool, count / total)
            for tool, count in sorted(counts.items(), key=lambda x: -x[1])
            if count / total >= min_confidence
        ]
        return predictions[:top_n]
```

## Solution 2: Prefetch Request Builder

```python
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


@dataclass
class PrefetchRequest:
    tool_name: str
    args: Dict[str, Any]
    confidence: float
    triggered_by: str             # the tool that caused this prefetch
    arg_extractor_name: str = ""  # which extractor was used


class PrefetchArgExtractorRegistry:
    """
    Maps (from_tool, to_tool) pairs to functions that extract likely
    arguments for the prefetched tool from the current tool's result.
    """

    def __init__(self):
        self._extractors: Dict[Tuple[str, str], Callable] = {}

    def register(
        self,
        from_tool: str,
        to_tool: str,
        extractor: Callable[[Any], Optional[Dict[str, Any]]],
    ) -> None:
        self._extractors[(from_tool, to_tool)] = extractor

    def extract(
        self,
        from_tool: str,
        to_tool: str,
        result: Any,
    ) -> Optional[Dict[str, Any]]:
        extractor = self._extractors.get((from_tool, to_tool))
        if extractor is None:
            return None
        try:
            return extractor(result)
        except Exception:
            return None
```

## Solution 3: Async Prefetch Executor

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


@dataclass
class PrefetchResult:
    request: PrefetchRequest
    result: Optional[Any] = None
    error: Optional[Exception] = None
    completed_at: Optional[float] = None
    latency_ms: Optional[float] = None

    @property
    def succeeded(self) -> bool:
        return self.error is None and self.completed_at is not None


class AsyncPrefetchExecutor:
    """
    Fires speculative tool calls in the background as asyncio tasks.
    Results are stored by (tool_name, args_key) for retrieval.
    Speculative results are discarded if not consumed within the TTL.
    """

    def __init__(
        self,
        tool_dispatch_fn: Callable[[str, Dict[str, Any]], Any],
        result_ttl_seconds: float = 30.0,
    ):
        self._dispatch = tool_dispatch_fn
        self._ttl = result_ttl_seconds
        self._cache: Dict[str, PrefetchResult] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._hits = 0
        self._misses = 0
        self._speculative_calls = 0

    def _cache_key(self, tool_name: str, args: Dict[str, Any]) -> str:
        import json
        return f"{tool_name}:{json.dumps(args, sort_keys=True)}"

    async def prefetch(self, request: PrefetchRequest) -> None:
        key = self._cache_key(request.tool_name, request.args)
        if key in self._tasks or key in self._cache:
            return

        self._speculative_calls += 1

        async def _run() -> None:
            start = time.time()
            try:
                result = await self._dispatch(request.tool_name, request.args)
                self._cache[key] = PrefetchResult(
                    request=request,
                    result=result,
                    completed_at=time.time(),
                    latency_ms=round((time.time() - start) * 1000, 2),
                )
            except Exception as exc:
                self._cache[key] = PrefetchResult(
                    request=request,
                    error=exc,
                    completed_at=time.time(),
                )
            finally:
                self._tasks.pop(key, None)

        self._tasks[key] = asyncio.create_task(_run())

    def consume(self, tool_name: str, args: Dict[str, Any]) -> Optional[PrefetchResult]:
        key = self._cache_key(tool_name, args)
        result = self._cache.pop(key, None)
        if result is None:
            self._misses += 1
            return None
        if result.completed_at and time.time() - result.completed_at > self._ttl:
            self._misses += 1
            return None
        self._hits += 1
        return result

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "speculative_calls": self._speculative_calls,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 3) if total > 0 else 0.0,
            "pending_prefetches": len(self._tasks),
        }
```

## Solution 4: Prefetch-Aware Tool Dispatcher

```python
import asyncio
import time
from typing import Any, Callable, Dict, Optional


class PrefetchAwareToolDispatcher:
    """
    Wraps the standard tool dispatcher with prefetch integration.
    After each tool call, fires speculative prefetches for likely next tools.
    Before each tool call, checks if a prefetch result is available.
    """

    def __init__(
        self,
        base_dispatch_fn: Callable[[str, Dict[str, Any]], Any],
        transition_model: ToolSequenceTransitionModel,
        arg_extractor_registry: PrefetchArgExtractorRegistry,
        prefetch_executor: AsyncPrefetchExecutor,
        prefetch_confidence_threshold: float = 0.40,
    ):
        self._dispatch = base_dispatch_fn
        self._model = transition_model
        self._extractors = arg_extractor_registry
        self._prefetcher = prefetch_executor
        self._threshold = prefetch_confidence_threshold
        self._prefetch_saves_ms: list = []

    async def call(self, tool_name: str, args: Dict[str, Any]) -> Any:
        # Check for a waiting prefetch result
        prefetch_hit = self._prefetcher.consume(tool_name, args)
        if prefetch_hit and prefetch_hit.succeeded:
            self._prefetch_saves_ms.append(prefetch_hit.latency_ms or 0)
            return prefetch_hit.result

        # Execute normally
        start = time.time()
        result = await self._dispatch(tool_name, args)
        actual_ms = (time.time() - start) * 1000

        # Record transition for model training (caller must record from-tool)
        # Fire speculative prefetches for likely next tools
        predictions = self._model.top_predictions(tool_name, min_confidence=self._threshold)
        for next_tool, confidence in predictions:
            extracted_args = self._extractors.extract(tool_name, next_tool, result)
            if extracted_args is not None:
                request = PrefetchRequest(
                    tool_name=next_tool,
                    args=extracted_args,
                    confidence=confidence,
                    triggered_by=tool_name,
                )
                asyncio.create_task(self._prefetcher.prefetch(request))

        return result

    def stats(self) -> dict:
        saves = self._prefetch_saves_ms
        return {
            "prefetch_hits_saved_ms": round(sum(saves), 1),
            "avg_save_per_hit_ms": round(sum(saves) / len(saves), 1) if saves else 0.0,
            "prefetcher": self._prefetcher.stats(),
        }
```

## Solution 5: Sequence Pattern Recorder

```python
import time
from typing import List, Optional


class SequencePatternRecorder:
    """
    Records observed tool sequences during actual agent runs and feeds
    them into the transition model for probability training.
    """

    def __init__(self, model: ToolSequenceTransitionModel):
        self._model = model
        self._current_sequence: List[str] = []
        self._last_tool_time: Optional[float] = None
        self._sequence_gap_seconds: float = 60.0  # reset sequence after idle gap

    def on_tool_called(self, tool_name: str) -> None:
        now = time.time()
        if (
            self._last_tool_time is not None
            and now - self._last_tool_time > self._sequence_gap_seconds
        ):
            self._current_sequence = []

        if self._current_sequence:
            self._model.record(self._current_sequence[-1], tool_name)

        self._current_sequence.append(tool_name)
        self._last_tool_time = now

    def reset_sequence(self) -> None:
        self._current_sequence = []
        self._last_tool_time = None

    def current_sequence(self) -> List[str]:
        return list(self._current_sequence)
```

## Solution 6: Prefetch Performance Dashboard

```python
import time


class PrefetchPerformanceDashboard:
    """
    Reports prefetch effectiveness: hit rate, latency savings,
    and top prefetch-eligible tool transitions.
    """

    def __init__(
        self,
        dispatcher: PrefetchAwareToolDispatcher,
        transition_model: ToolSequenceTransitionModel,
    ):
        self._dispatcher = dispatcher
        self._model = transition_model

    def render(self, top_tools: List[str] = None) -> dict:
        dispatcher_stats = self._dispatcher.stats()
        prefetcher_stats = dispatcher_stats["prefetcher"]

        tool_predictions = {}
        if top_tools:
            for tool in top_tools:
                preds = self._model.top_predictions(tool, top_n=3, min_confidence=0.20)
                if preds:
                    tool_predictions[tool] = [
                        {"next": t, "confidence": round(c, 3)} for t, c in preds
                    ]

        return {
            "generated_at": time.time(),
            "prefetch_hit_rate": prefetcher_stats["hit_rate"],
            "speculative_calls_fired": prefetcher_stats["speculative_calls"],
            "total_latency_saved_ms": dispatcher_stats["prefetch_hits_saved_ms"],
            "avg_latency_saved_per_hit_ms": dispatcher_stats["avg_save_per_hit_ms"],
            "pending_prefetches": prefetcher_stats["pending_prefetches"],
            "top_tool_predictions": tool_predictions,
        }
```

## Comparison

| Approach | Transition Learning | Arg Extraction | Background Execution | Hit/Miss Tracking | Sequence Recording |
|---|---|---|---|---|---|
| ToolSequenceTransitionModel | Yes (frequency) | No | No | No | No |
| PrefetchArgExtractorRegistry | No | Yes (per-pair) | No | No | No |
| AsyncPrefetchExecutor | No | No | Yes (asyncio tasks) | Yes | No |
| PrefetchAwareToolDispatcher | Via model | Via extractors | Via executor | Via executor | No |
| SequencePatternRecorder | Feeds model | No | No | No | Yes |
| PrefetchPerformanceDashboard | No | No | No | No | No |

**Best for production**: Only prefetch when the transition model has at least 10 observations for a given pair — predictions from sparse data fire speculative calls that almost never hit, wasting API quota. Set `prefetch_confidence_threshold=0.50` initially and lower it only after validating hit rates exceed 60%. Register explicit `PrefetchArgExtractorRegistry` entries for your most common tool chains (user_lookup → orders_fetch, entity_resolve → detail_fetch) rather than relying on generic extraction — type-safe extraction prevents wasted calls with malformed arguments. Cancel pending prefetch tasks on conversation end to avoid orphaned API calls.
