---
title: "Agent Doesn't Implement Speculative Prefetching of Tool Results"
description: "Agents that fetch tool results sequentially on demand add the full latency of each tool call to the critical path. When the next tool call is predictable from context — a follow-up lookup after an initial search, a schema fetch after a table list — prefetching it speculatively while the LLM processes the current result hides the round-trip behind computation. Implement speculative prefetching that predicts likely next tool calls and starts them before the LLM requests them."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-speculative-prefetching-of-tool-results
tags: [prefetching, speculative-execution, latency-hiding, tool-pipelining, async-prefetch, performance-optimization]
symptoms:
  - "Sequential tool calls each add full round-trip latency to the critical path"
  - "LLM sits idle waiting for tool results that were predictable one step earlier"
  - "Common tool call sequences (search → fetch → summarize) always run serially"
  - "No mechanism to start a predictable follow-up call before the LLM requests it"
  - "P95 latency is dominated by tool call latency even when calls are independent"
---

## Why This Happens

Agents operate in a request-response loop: call LLM, receive tool call request, execute tool, return result, repeat. Each iteration adds a full network round-trip to the critical path. When tool call sequences are predictable — a web search almost always precedes a page fetch; a database table list almost always precedes a column schema lookup — the follow-up call could start during the LLM's processing of the previous result. Speculative prefetching breaks the serial dependency by starting likely next calls immediately after a tool returns, caching results in a short-lived prefetch store, and serving them instantly when the LLM eventually requests them.

## Solution 1: Tool Call Predictor

```python
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class PrefetchPrediction:
    tool_name: str
    predicted_args: Dict[str, Any]
    confidence: float          # 0.0 – 1.0
    based_on_tool: str         # the tool call that triggered this prediction
    based_on_args: Dict[str, Any]


class ToolCallPredictor:
    """
    Predicts likely follow-up tool calls based on the current tool name
    and result content. Uses a rule table of (tool, result_pattern) → next_tool.
    """

    def __init__(self, min_confidence: float = 0.60):
        self._min_confidence = min_confidence
        # List of (trigger_tool, arg_extractor_fn, target_tool, confidence)
        self._rules: List[Tuple[str, Any, str, float]] = []

    def add_rule(
        self,
        trigger_tool: str,
        target_tool: str,
        arg_extractor,   # Callable[[dict result, dict args] -> dict | None]
        confidence: float = 0.80,
    ) -> None:
        self._rules.append((trigger_tool, arg_extractor, target_tool, confidence))

    def predict(
        self,
        completed_tool: str,
        completed_args: Dict[str, Any],
        tool_result: Any,
    ) -> List[PrefetchPrediction]:
        predictions = []
        for trigger, extractor, target_tool, confidence in self._rules:
            if trigger != completed_tool:
                continue
            if confidence < self._min_confidence:
                continue
            try:
                predicted_args = extractor(tool_result, completed_args)
            except Exception:
                continue
            if predicted_args is None:
                continue
            predictions.append(PrefetchPrediction(
                tool_name=target_tool,
                predicted_args=predicted_args,
                confidence=confidence,
                based_on_tool=completed_tool,
                based_on_args=completed_args,
            ))
        return predictions
```

## Solution 2: Prefetch Result Cache

```python
import asyncio
import hashlib
import json
import time
from typing import Any, Dict, Optional


@dataclass
class PrefetchEntry:
    tool_name: str
    args: Dict[str, Any]
    result: Any
    fetched_at: float
    hit: bool = False   # True once the agent actually requested this result


class PrefetchResultCache:
    """
    Short-lived cache for speculatively fetched tool results.
    Entries expire quickly — prefetch results are only useful for seconds.
    """

    def __init__(self, ttl_seconds: float = 30.0, max_entries: int = 50):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._store: Dict[str, PrefetchEntry] = {}
        self._lock = asyncio.Lock()

    def _key(self, tool_name: str, args: Dict[str, Any]) -> str:
        canonical = json.dumps({"tool": tool_name, "args": args}, sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    async def put(self, entry: PrefetchEntry) -> None:
        async with self._lock:
            self._evict()
            key = self._key(entry.tool_name, entry.args)
            self._store[key] = entry

    async def get(
        self, tool_name: str, args: Dict[str, Any]
    ) -> Optional[PrefetchEntry]:
        async with self._lock:
            key = self._key(tool_name, args)
            entry = self._store.get(key)
            if entry is None:
                return None
            if time.time() - entry.fetched_at > self._ttl:
                del self._store[key]
                return None
            entry.hit = True
            return entry

    def _evict(self) -> None:
        now = time.time()
        expired = [k for k, e in self._store.items() if now - e.fetched_at > self._ttl]
        for k in expired:
            del self._store[k]
        while len(self._store) >= self._max:
            oldest_key = min(self._store, key=lambda k: self._store[k].fetched_at)
            del self._store[oldest_key]

    def stats(self) -> dict:
        return {
            "entries": len(self._store),
            "hits": sum(1 for e in self._store.values() if e.hit),
        }
```

## Solution 3: Speculative Prefetcher

```python
import asyncio
import time
from typing import Any, Callable, Dict, List


class SpeculativePrefetcher:
    """
    After a tool call completes, predicts likely follow-up calls and starts
    them in the background. Results are stored in the prefetch cache.
    """

    def __init__(
        self,
        predictor: ToolCallPredictor,
        cache: PrefetchResultCache,
        max_concurrent_prefetches: int = 3,
    ):
        self._predictor = predictor
        self._cache = cache
        self._semaphore = asyncio.Semaphore(max_concurrent_prefetches)
        self._prefetch_count = 0
        self._hit_count = 0

    async def on_tool_completed(
        self,
        completed_tool: str,
        completed_args: Dict[str, Any],
        tool_result: Any,
        tool_registry: Dict[str, Callable],
    ) -> None:
        predictions = self._predictor.predict(
            completed_tool, completed_args, tool_result
        )
        for pred in predictions:
            fn = tool_registry.get(pred.tool_name)
            if fn is None:
                continue
            asyncio.create_task(
                self._prefetch(pred, fn)
            )

    async def _prefetch(
        self, pred: PrefetchPrediction, fn: Callable
    ) -> None:
        async with self._semaphore:
            # Skip if already cached
            existing = await self._cache.get(pred.tool_name, pred.predicted_args)
            if existing is not None:
                return
            try:
                start = time.time()
                result = await fn(**pred.predicted_args)
                latency_ms = round((time.time() - start) * 1000, 2)
                await self._cache.put(PrefetchEntry(
                    tool_name=pred.tool_name,
                    args=pred.predicted_args,
                    result=result,
                    fetched_at=time.time(),
                ))
                self._prefetch_count += 1
            except Exception:
                pass  # prefetch failures are silent — the real call will retry

    def stats(self) -> dict:
        return {
            "prefetches_started": self._prefetch_count,
        }
```

## Solution 4: Prefetch-Aware Tool Executor

```python
import time
from typing import Any, Callable, Dict, Optional


@dataclass
class ToolExecutionRecord:
    tool_name: str
    args: Dict[str, Any]
    result: Any
    latency_ms: float
    served_from_prefetch: bool


class PrefetchAwareToolExecutor:
    """
    Checks the prefetch cache before executing a tool call.
    On a cache hit, returns the prefetched result immediately (zero latency).
    On a miss, executes normally and triggers prefetch for follow-ups.
    """

    def __init__(
        self,
        cache: PrefetchResultCache,
        prefetcher: SpeculativePrefetcher,
        tool_registry: Dict[str, Callable],
    ):
        self._cache = cache
        self._prefetcher = prefetcher
        self._registry = tool_registry
        self._records: list = []

    async def execute(
        self, tool_name: str, args: Dict[str, Any]
    ) -> ToolExecutionRecord:
        start = time.time()

        # Check prefetch cache first
        cached = await self._cache.get(tool_name, args)
        if cached is not None:
            latency_ms = round((time.time() - start) * 1000, 2)
            record = ToolExecutionRecord(
                tool_name=tool_name,
                args=args,
                result=cached.result,
                latency_ms=latency_ms,
                served_from_prefetch=True,
            )
            self._records.append(record)
            asyncio.create_task(
                self._prefetcher.on_tool_completed(
                    tool_name, args, cached.result, self._registry
                )
            )
            return record

        # Cache miss — execute normally
        fn = self._registry[tool_name]
        result = await fn(**args)
        latency_ms = round((time.time() - start) * 1000, 2)

        record = ToolExecutionRecord(
            tool_name=tool_name,
            args=args,
            result=result,
            latency_ms=latency_ms,
            served_from_prefetch=False,
        )
        self._records.append(record)

        asyncio.create_task(
            self._prefetcher.on_tool_completed(
                tool_name, args, result, self._registry
            )
        )
        return record

    def hit_rate(self) -> float:
        if not self._records:
            return 0.0
        hits = sum(1 for r in self._records if r.served_from_prefetch)
        return round(hits / len(self._records), 4)
```

## Solution 5: Prefetch Latency Savings Recorder

```python
import time
from threading import Lock
from typing import List


class PrefetchLatencySavingsRecorder:
    """
    Tracks how much latency was saved by prefetch hits versus misses.
    """

    def __init__(self):
        self._lock = Lock()
        self._records: List[dict] = []

    def record(self, record: ToolExecutionRecord, baseline_latency_ms: float) -> None:
        saved = max(baseline_latency_ms - record.latency_ms, 0.0) if record.served_from_prefetch else 0.0
        with self._lock:
            self._records.append({
                "ts": time.time(),
                "tool_name": record.tool_name,
                "latency_ms": record.latency_ms,
                "served_from_prefetch": record.served_from_prefetch,
                "saved_ms": round(saved, 2),
            })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "calls": 0}
        hits = [r for r in recent if r["served_from_prefetch"]]
        total_saved = sum(r["saved_ms"] for r in hits)
        return {
            "window_seconds": window_seconds,
            "calls": len(recent),
            "prefetch_hits": len(hits),
            "hit_rate": round(len(hits) / len(recent), 4),
            "total_latency_saved_ms": round(total_saved, 2),
            "avg_saved_per_hit_ms": round(total_saved / max(len(hits), 1), 2),
        }
```

## Solution 6: Prefetch Dashboard

```python
import time


class PrefetchDashboard:
    """
    Combines prefetch hit rate, latency savings, and cache state
    into a single operational snapshot.
    """

    def __init__(
        self,
        executor: PrefetchAwareToolExecutor,
        cache: PrefetchResultCache,
        prefetcher: SpeculativePrefetcher,
        savings_recorder: PrefetchLatencySavingsRecorder,
    ):
        self._executor = executor
        self._cache = cache
        self._prefetcher = prefetcher
        self._savings = savings_recorder

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "executor_hit_rate": self._executor.hit_rate(),
            "cache": self._cache.stats(),
            "prefetcher": self._prefetcher.stats(),
            "savings": self._savings.summary(window_seconds=3600.0),
        }
```

## Comparison

| Approach | Prediction Rules | Result Cache | Background Prefetch | Hit-Rate Tracking | Latency Savings |
|---|---|---|---|---|---|
| ToolCallPredictor | Yes (rule table) | No | No | No | No |
| PrefetchResultCache | No | Yes (TTL+LRU) | No | No | No |
| SpeculativePrefetcher | Via predictor | Via cache | Yes (asyncio tasks) | No | No |
| PrefetchAwareToolExecutor | No | Via cache | Via prefetcher | Yes | No |
| PrefetchLatencySavingsRecorder | No | No | No | No | Yes |

**Best for production**: Start with two or three high-confidence rules covering your most common tool sequences (e.g., `web_search` → `fetch_page` for the top URL in results; `list_tables` → `get_schema` for the first table returned). Set `min_confidence=0.70` to avoid wasting quota on low-probability predictions. Keep `PrefetchResultCache` TTL at 30 seconds — prefetched results older than that are stale for a conversational agent. Monitor `hit_rate` in `PrefetchLatencySavingsRecorder`: a hit rate below 0.20 means the prediction rules need refinement; above 0.50 means significant P50 latency improvement is being captured.
