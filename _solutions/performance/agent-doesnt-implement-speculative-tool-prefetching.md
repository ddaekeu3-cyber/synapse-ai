---
title: "Agent Doesn't Implement Speculative Tool Prefetching"
description: "Agents that wait for the LLM to explicitly request each tool call before starting the next one serialize what could be parallelized: while the LLM is processing the result of tool A, tool B — which is almost certainly needed next — sits idle. Implement speculative tool prefetching that predicts likely next tool calls based on current context and begins executing them before the LLM explicitly asks, cancelling unused prefetches and returning prefetched results instantly when confirmed."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-speculative-tool-prefetching
tags: [speculative-execution, prefetching, latency-hiding, tool-parallelism, prediction, pipeline-optimization]
symptoms:
  - "Tool call latency adds up serially even though most calls are predictable from context"
  - "LLM always requests the same sequence of tools for a given intent — but waits between each"
  - "No measurement of how often the next tool call is predictable from the current state"
  - "Prefetch opportunity rate unknown — cannot quantify the latency hiding potential"
  - "P99 response latency dominated by sequential tool round-trips"
---

## Why This Happens

Sequential tool dispatch is the default because the LLM decides what to call next after seeing each result. But many agent workflows are highly predictable: a weather query almost always follows a location lookup; a product detail fetch almost always follows a search result; a user profile load almost always precedes a permission check. Speculative prefetching starts these predicted calls in the background while the LLM processes the current result. If the prediction is correct, the result is ready instantly. If wrong, the prefetch is cancelled (for cancellable operations) or the result is discarded.

## Solution 1: Prefetch Prediction Rule

```python
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class PrefetchPredictionRule:
    """
    Declares that after tool `trigger_tool` completes,
    `predicted_tool` is likely to be called next.
    """
    trigger_tool: str
    predicted_tool: str
    confidence: float = 0.8          # 0.0–1.0; only prefetch if above threshold
    arg_extractor: Optional[Callable[[Any], Dict[str, Any]]] = None
    # Extracts args for predicted_tool from trigger_tool's result.
    # If None, prefetch is started with no args (must be provided on confirm).
    description: str = ""
```

## Solution 2: Prefetch Request

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class PrefetchRequest:
    tool_name: str
    args: Dict[str, Any]
    triggered_by: str              # which tool result triggered this prefetch
    started_at: float = field(default_factory=time.time)
    task: Optional[asyncio.Task] = field(default=None, repr=False)
    result: Any = None
    error: Optional[str] = None
    completed: bool = False
    cancelled: bool = False
    used: bool = False

    @property
    def latency_ms(self) -> float:
        return round((time.time() - self.started_at) * 1000, 2)
```

## Solution 3: Speculative Prefetch Engine

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional


class SpeculativePrefetchEngine:
    """
    Maintains a set of in-flight speculative tool calls.
    On trigger_tool completion, starts predicted tool calls.
    On confirm, returns the already-completed result or awaits it.
    On mismatch, cancels in-flight prefetches.
    """

    def __init__(
        self,
        rules: List[PrefetchPredictionRule],
        min_confidence: float = 0.7,
    ):
        self._rules: Dict[str, List[PrefetchPredictionRule]] = {}
        for rule in rules:
            self._rules.setdefault(rule.trigger_tool, []).append(rule)
        self._min_confidence = min_confidence
        self._prefetches: Dict[str, PrefetchRequest] = {}
        self._hits = 0
        self._misses = 0
        self._total_prefetches = 0

    def on_tool_complete(
        self,
        tool_name: str,
        result: Any,
        tool_registry: Dict[str, Callable],
    ) -> List[str]:
        """
        Called after a tool completes. Starts eligible prefetches.
        Returns list of tool names prefetched.
        """
        started = []
        for rule in self._rules.get(tool_name, []):
            if rule.confidence < self._min_confidence:
                continue
            args = {}
            if rule.arg_extractor:
                try:
                    args = rule.arg_extractor(result) or {}
                except Exception:
                    continue
            tool_fn = tool_registry.get(rule.predicted_tool)
            if not tool_fn:
                continue

            req = PrefetchRequest(
                tool_name=rule.predicted_tool,
                args=args,
                triggered_by=tool_name,
            )
            req.task = asyncio.create_task(self._run(req, tool_fn))
            self._prefetches[rule.predicted_tool] = req
            self._total_prefetches += 1
            started.append(rule.predicted_tool)

        return started

    async def _run(self, req: PrefetchRequest, tool_fn: Callable) -> None:
        try:
            req.result = await tool_fn(**req.args)
            req.completed = True
        except Exception as exc:
            req.error = str(exc)
            req.completed = True

    async def confirm(
        self,
        tool_name: str,
        args: Dict[str, Any],
    ) -> Optional[Any]:
        """
        Called when the LLM confirms it wants tool_name with args.
        Returns prefetched result if available, else None (caller dispatches normally).
        """
        req = self._prefetches.pop(tool_name, None)
        if req is None:
            self._misses += 1
            return None

        if not req.completed and req.task:
            try:
                await asyncio.wait_for(req.task, timeout=0.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        if req.completed and req.error is None:
            req.used = True
            self._hits += 1
            return req.result

        self._misses += 1
        return None

    def cancel_all(self) -> None:
        for req in self._prefetches.values():
            if req.task and not req.task.done():
                req.task.cancel()
                req.cancelled = True
        self._prefetches.clear()

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "total_prefetches_started": self._total_prefetches,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 4) if total else 0.0,
            "pending": len(self._prefetches),
        }
```

## Solution 4: Prefetch-Aware Tool Dispatcher

```python
from typing import Any, Callable, Dict, Optional


class PrefetchAwareToolDispatcher:
    """
    Integrates the prefetch engine into the tool dispatch path.
    On each dispatch, checks for a waiting prefetch result before
    executing the tool normally.
    """

    def __init__(
        self,
        engine: SpeculativePrefetchEngine,
        tool_registry: Dict[str, Callable],
    ):
        self._engine = engine
        self._registry = tool_registry
        self._prefetch_saves_ms: float = 0.0

    async def dispatch(
        self,
        tool_name: str,
        args: Dict[str, Any],
    ) -> Any:
        import time
        start = time.time()

        prefetched = await self._engine.confirm(tool_name, args)
        if prefetched is not None:
            elapsed = round((time.time() - start) * 1000, 2)
            # Result was ready — near-zero latency
            return prefetched

        tool_fn = self._registry.get(tool_name)
        if not tool_fn:
            raise KeyError(f"Tool '{tool_name}' not in registry")

        result = await tool_fn(**args)

        # Trigger prefetches for next likely calls
        self._engine.on_tool_complete(tool_name, result, self._registry)

        return result
```

## Solution 5: Prefetch Hit Rate Tracker

```python
import time
from threading import Lock
from typing import List


class PrefetchHitRateTracker:
    """
    Accumulates prefetch engine stats snapshots over time.
    Surfaces hit rate trends to validate that prediction rules are useful.
    """

    def __init__(self):
        self._snapshots: List[dict] = []
        self._lock = Lock()

    def record(self, stats: dict) -> None:
        with self._lock:
            self._snapshots.append({"ts": time.time(), **stats})
            if len(self._snapshots) > 5000:
                self._snapshots.pop(0)

    def trend(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [s for s in self._snapshots if s["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "snapshots": 0}
        avg_hit_rate = sum(s.get("hit_rate", 0) for s in recent) / len(recent)
        total_hits = sum(s.get("hits", 0) for s in recent)
        total_misses = sum(s.get("misses", 0) for s in recent)
        return {
            "window_seconds": window_seconds,
            "snapshots": len(recent),
            "avg_hit_rate": round(avg_hit_rate, 4),
            "total_hits": total_hits,
            "total_misses": total_misses,
        }
```

## Solution 6: Speculative Prefetch Dashboard

```python
import time


class SpeculativePrefetchDashboard:
    """
    Combines engine stats, hit rate trends, and active prefetch state
    into a single operational snapshot.
    """

    def __init__(
        self,
        engine: SpeculativePrefetchEngine,
        tracker: PrefetchHitRateTracker,
    ):
        self._engine = engine
        self._tracker = tracker

    def render(self) -> dict:
        stats = self._engine.stats()
        self._tracker.record(stats)
        return {
            "generated_at": time.time(),
            "engine_stats": stats,
            "hit_rate_trend_1h": self._tracker.trend(3600.0),
            "active_prefetches": stats.get("pending", 0),
            "recommendation": (
                "hit rate healthy"
                if stats.get("hit_rate", 0) >= 0.6
                else "consider removing low-confidence rules"
            ),
        }
```

## Comparison

| Approach | Rule-Based Prediction | Background Execution | Result Reuse | Cancel on Miss | Hit Rate Tracking |
|---|---|---|---|---|---|
| SpeculativePrefetchEngine | Yes | Yes (asyncio.Task) | Yes | Yes | Yes (counters) |
| PrefetchAwareToolDispatcher | Via engine | Via engine | Via engine | Via engine | No |
| PrefetchHitRateTracker | No | No | No | No | Yes (over time) |
| SpeculativePrefetchDashboard | No | No | No | No | Via tracker |

**Best for production**: Start with `min_confidence=0.75` and only add rules for tool sequences that appear in more than 30% of sessions — speculative calls that hit less than 60% of the time waste compute and add noise to downstream service logs. Use `arg_extractor` only for idempotent read operations; never speculatively execute write or mutation tools. Call `engine.cancel_all()` at conversation end to avoid dangling tasks. Monitor `hit_rate` via `PrefetchHitRateTracker`: if it drops below 50% after a prompt change, the new prompt is producing different tool-call sequences and the rules need updating.
