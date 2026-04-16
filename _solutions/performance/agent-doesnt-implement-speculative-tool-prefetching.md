---
title: "Agent Doesn't Implement Speculative Tool Prefetching"
description: "Agents that execute tools strictly sequentially — waiting for the LLM to emit a tool call, dispatching it, returning the result, then asking the LLM for the next step — waste wall-clock time on round-trips that could be overlapped. When the agent's workflow has predictable tool sequences, prefetch the next likely tool result while the LLM is generating its response to the current result, hiding tool latency behind LLM latency."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-speculative-tool-prefetching
tags: [speculative-execution, tool-prefetching, latency-hiding, pipeline-parallelism, workflow-optimization, agentic-performance]
symptoms:
  - "Agent wall-clock time equals sum of all LLM + tool round-trips even when tools are independent"
  - "High-latency tools (web search, code execution) are always on the critical path"
  - "Tool call sequences that nearly always occur together are never parallelized"
  - "No measurement of how much time is spent waiting for tools vs. waiting for LLM"
  - "Sequential execution even when the agent's next tool call is highly predictable"
---

## Why This Happens

LLM inference and tool execution are both high-latency operations. A sequential agent waits for the LLM to emit a tool call, waits for the tool to execute, feeds the result back, and waits for the LLM again. When certain tool sequences are highly predictable — a web search almost always followed by a summarization lookup, or a code execution almost always followed by a lint check — the second tool can be prefetched while the LLM processes the first tool's result. The prefetch is discarded if the prediction was wrong; the result is served instantly if the prediction was correct. Prefetching is most valuable when tool latency exceeds LLM generation latency.

## Solution 1: Tool Sequence Predictor

```python
import time
from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, List, Optional, Tuple


@dataclass
class ToolTransitionRecord:
    from_tool: str
    to_tool: str
    count: int = 0
    last_seen: float = field(default_factory=time.time)


class ToolSequencePredictor:
    """
    Learns tool transition probabilities from observed sequences.
    Predicts which tool is most likely to follow the current one.
    """

    def __init__(self, min_confidence: float = 0.60, min_observations: int = 5):
        self._transitions: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._lock = Lock()
        self._min_confidence = min_confidence
        self._min_observations = min_observations

    def record_transition(self, from_tool: str, to_tool: str) -> None:
        with self._lock:
            self._transitions[from_tool][to_tool] += 1

    def predict_next(self, current_tool: str) -> Optional[Tuple[str, float]]:
        """Returns (predicted_tool, confidence) or None if prediction is uncertain."""
        with self._lock:
            following = self._transitions.get(current_tool, {})
            total = sum(following.values())
            if total < self._min_observations:
                return None
            best_tool = max(following, key=lambda t: following[t])
            confidence = following[best_tool] / total
            if confidence < self._min_confidence:
                return None
            return best_tool, round(confidence, 3)

    def top_sequences(self, top_n: int = 10) -> List[dict]:
        with self._lock:
            results = []
            for from_tool, successors in self._transitions.items():
                total = sum(successors.values())
                for to_tool, count in successors.items():
                    results.append({
                        "from_tool": from_tool,
                        "to_tool": to_tool,
                        "count": count,
                        "confidence": round(count / total, 3),
                    })
            return sorted(results, key=lambda r: r["count"], reverse=True)[:top_n]
```

## Solution 2: Prefetch Cache

```python
import asyncio
import time
from typing import Any, Dict, Optional, Tuple


class PrefetchEntry:
    def __init__(self, tool_name: str, args_key: str):
        self.tool_name = tool_name
        self.args_key = args_key
        self.future: asyncio.Future = asyncio.get_event_loop().create_future()
        self.created_at: float = time.time()
        self.hit: bool = False


class PrefetchCache:
    """
    Holds in-flight prefetch futures keyed by (tool_name, args_key).
    A cache hit returns the awaitable future immediately.
    Prefetches expire if not consumed within the TTL.
    """

    def __init__(self, ttl_seconds: float = 30.0):
        self._entries: Dict[Tuple[str, str], PrefetchEntry] = {}
        self._ttl = ttl_seconds

    def put(self, tool_name: str, args_key: str) -> PrefetchEntry:
        self._evict_stale()
        entry = PrefetchEntry(tool_name, args_key)
        self._entries[(tool_name, args_key)] = entry
        return entry

    def get(self, tool_name: str, args_key: str) -> Optional[PrefetchEntry]:
        self._evict_stale()
        entry = self._entries.get((tool_name, args_key))
        if entry:
            entry.hit = True
        return entry

    def discard(self, tool_name: str, args_key: str) -> None:
        self._entries.pop((tool_name, args_key), None)

    def _evict_stale(self) -> None:
        now = time.time()
        stale = [k for k, e in self._entries.items() if now - e.created_at > self._ttl]
        for k in stale:
            self._entries.pop(k, None)

    def stats(self) -> dict:
        return {"active_prefetches": len(self._entries)}
```

## Solution 3: Speculative Prefetch Executor

```python
import asyncio
import hashlib
import json
from typing import Any, Callable, Dict, Optional


class SpeculativePrefetchExecutor:
    """
    After each tool call completes, predicts the next tool and starts
    executing it speculatively. If the prediction is correct, the result
    is returned immediately from the prefetch cache.
    """

    def __init__(
        self,
        predictor: ToolSequencePredictor,
        cache: PrefetchCache,
    ):
        self._predictor = predictor
        self._cache = cache
        self._tool_registry: Dict[str, Callable] = {}

    def register_tool(self, tool_name: str, tool_fn: Callable) -> None:
        self._tool_registry[tool_name] = tool_fn

    @staticmethod
    def _args_key(kwargs: dict) -> str:
        try:
            return hashlib.sha256(
                json.dumps(kwargs, sort_keys=True, default=str).encode()
            ).hexdigest()[:12]
        except Exception:
            return "unknown"

    async def execute_with_prefetch(
        self,
        tool_name: str,
        tool_fn: Callable,
        **kwargs: Any,
    ) -> Any:
        args_key = self._args_key(kwargs)

        # Check if this call was prefetched
        entry = self._cache.get(tool_name, args_key)
        if entry is not None:
            try:
                result = await asyncio.wait_for(entry.future, timeout=0.001)
                return result
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass  # future not ready yet — fall through to real call

        # Execute normally
        result = await tool_fn(**kwargs)

        # Record transition and launch prefetch for predicted next tool
        self._predictor.record_transition(tool_name, "__end__")
        prediction = self._predictor.predict_next(tool_name)
        if prediction:
            next_tool, confidence = prediction
            next_fn = self._tool_registry.get(next_tool)
            if next_fn:
                self._launch_prefetch(next_tool, next_fn)

        return result

    def _launch_prefetch(self, tool_name: str, tool_fn: Callable) -> None:
        args_key = "speculative"
        entry = self._cache.put(tool_name, args_key)

        async def _run():
            try:
                result = await tool_fn()
                entry.future.set_result(result)
            except Exception as exc:
                entry.future.set_exception(exc)

        asyncio.ensure_future(_run())
```

## Solution 4: Prefetch Effectiveness Tracker

```python
import time
from typing import List


class PrefetchEffectivenessTracker:
    """
    Measures the hit rate and latency savings from speculative prefetching.
    """

    def __init__(self):
        self._records: List[dict] = []

    def record(
        self,
        tool_name: str,
        was_prefetch_hit: bool,
        actual_latency_ms: float,
        prefetch_latency_ms: Optional[float] = None,
    ) -> None:
        self._records.append({
            "ts": time.time(),
            "tool_name": tool_name,
            "prefetch_hit": was_prefetch_hit,
            "actual_latency_ms": actual_latency_ms,
            "prefetch_latency_ms": prefetch_latency_ms,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "calls": 0}

        hits = [r for r in recent if r["prefetch_hit"]]
        total_saved_ms = sum(
            r["actual_latency_ms"]
            for r in hits
        )
        return {
            "window_seconds": window_seconds,
            "calls": len(recent),
            "prefetch_hits": len(hits),
            "hit_rate": round(len(hits) / len(recent), 4),
            "total_latency_saved_ms": round(total_saved_ms, 1),
            "avg_latency_saved_ms": round(total_saved_ms / max(len(hits), 1), 1),
        }
```

## Solution 5: Prefetch Decision Policy

```python
from dataclasses import dataclass
from typing import Optional, Set


@dataclass
class PrefetchPolicy:
    enabled: bool = True
    min_confidence: float = 0.70
    max_concurrent_prefetches: int = 3
    excluded_tools: Set[str] = None  # tools that must never be prefetched (e.g., write ops)
    prefetch_only_tools: Set[str] = None  # whitelist; if set, only these are prefetched

    def __post_init__(self):
        if self.excluded_tools is None:
            self.excluded_tools = set()
        if self.prefetch_only_tools is None:
            self.prefetch_only_tools = set()

    def should_prefetch(self, tool_name: str, confidence: float) -> bool:
        if not self.enabled:
            return False
        if confidence < self.min_confidence:
            return False
        if tool_name in self.excluded_tools:
            return False
        if self.prefetch_only_tools and tool_name not in self.prefetch_only_tools:
            return False
        return True
```

## Solution 6: Speculative Prefetch Dashboard

```python
import time


class SpeculativePrefetchDashboard:
    """
    Combines prediction accuracy, prefetch hit rate, and cache stats
    into a single operational view.
    """

    def __init__(
        self,
        predictor: ToolSequencePredictor,
        cache: PrefetchCache,
        tracker: PrefetchEffectivenessTracker,
    ):
        self._predictor = predictor
        self._cache = cache
        self._tracker = tracker

    def render(self, window_seconds: float = 3600.0) -> dict:
        return {
            "generated_at": time.time(),
            "prefetch_effectiveness": self._tracker.summary(window_seconds),
            "active_prefetches": self._cache.stats(),
            "top_sequences": self._predictor.top_sequences(top_n=5),
        }
```

## Comparison

| Approach | Sequence Learning | Prefetch Cache | Speculative Launch | Hit Rate Tracking | Policy Control |
|---|---|---|---|---|---|
| ToolSequencePredictor | Yes (Markov) | No | No | No | No |
| PrefetchCache | No | Yes (TTL) | No | No | No |
| SpeculativePrefetchExecutor | Via predictor | Via cache | Yes | No | No |
| PrefetchEffectivenessTracker | No | No | No | Yes | No |
| PrefetchDecisionPolicy | No | No | No | No | Yes |
| SpeculativePrefetchDashboard | No | No | No | No | No |

**Best for production**: Start with the `ToolSequencePredictor` in observation-only mode for the first week to build a transition table from real traffic before enabling prefetching. Set `excluded_tools` to include any tool with write side effects (database writes, email sends, file deletes) — a wrong prefetch prediction that executes a write is a correctness bug, not just wasted compute. Monitor `hit_rate` via `PrefetchEffectivenessTracker`: below 0.40 means predictions are wrong more than they're right and prefetching is net-negative (wasted compute and potential side effects). Disable prefetching for tools with sub-50ms latency — the overhead of managing the cache exceeds the savings.
