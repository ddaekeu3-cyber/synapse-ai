---
title: "Agent Doesn't Implement Speculative Tool Pre-Fetching"
description: "Agents that wait for the LLM to request a tool before initiating the fetch introduce unnecessary serial latency: the LLM call completes, the tool request is parsed, the tool executes, and only then does the LLM receive the result. Implement speculative tool pre-fetching that predicts which tools will be needed based on the user intent and starts fetching in parallel with the LLM call, so results are ready when the LLM asks for them."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-speculative-tool-pre-fetching
tags: [speculative-execution, pre-fetching, parallel-execution, latency-reduction, intent-prediction, tool-prefetch]
symptoms:
  - "Tool execution is entirely serial — each tool waits for the LLM to explicitly request it"
  - "High latency on turns that always use the same two tools regardless of query content"
  - "No prediction of likely tool calls based on user intent patterns"
  - "Pre-fetching results that are already available are fetched again on LLM request"
  - "Tool call latency adds to LLM call latency sequentially rather than overlapping"
---

## Why This Happens

Standard agentic loops are request-response: LLM decides → tool called → result returned → LLM decides again. Every tool call adds its latency sequentially. For tools that are predictably needed (a knowledge base lookup is needed for almost every question; a user profile fetch is needed for every personalized response), the decision to call them is not uncertain — they will always be called. Speculative pre-fetching starts these predictable calls in parallel with the LLM call so their results arrive before or shortly after the LLM's tool request.

## Solution 1: Tool Prediction Rule

```python
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolPredictionRule:
    """
    Maps intent signals to a predicted tool call.
    Confidence is used to decide whether to pre-fetch speculatively.
    """
    tool_name: str
    intent_keywords: List[str]          # keywords that suggest this tool
    intent_patterns: List[str] = field(default_factory=list)   # regex patterns
    default_args: Dict[str, Any] = field(default_factory=dict)
    confidence_threshold: float = 0.70  # only prefetch above this confidence
    arg_extractor: Optional[object] = None  # callable(query) -> dict

    def matches(self, query: str) -> float:
        """Returns a confidence score 0.0–1.0."""
        query_lower = query.lower()
        keyword_hits = sum(1 for kw in self.intent_keywords if kw in query_lower)
        pattern_hits = sum(
            1 for p in self.intent_patterns
            if re.search(p, query_lower)
        )
        total_signals = len(self.intent_keywords) + len(self.intent_patterns)
        if total_signals == 0:
            return 0.0
        return min(1.0, (keyword_hits + pattern_hits * 1.5) / total_signals)
```

## Solution 2: Speculative Prefetch Scheduler

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional


class SpeculativePrefetchScheduler:
    """
    Evaluates prediction rules against the incoming query and
    starts pre-fetch tasks for tools exceeding the confidence threshold.
    Pre-fetch results are stored and returned immediately when requested.
    """

    def __init__(self, rules: List[ToolPredictionRule]):
        self._rules = rules
        self._inflight: Dict[str, asyncio.Task] = {}
        self._results: Dict[str, Any] = {}
        self._prefetch_times: Dict[str, float] = {}
        self._hits = 0
        self._misses = 0

    def schedule(
        self,
        query: str,
        tool_fns: Dict[str, Callable],
    ) -> List[str]:
        """
        Evaluates rules and schedules prefetches.
        Returns list of tool names scheduled.
        """
        scheduled = []
        for rule in self._rules:
            confidence = rule.matches(query)
            if confidence < rule.confidence_threshold:
                continue
            if rule.tool_name in self._inflight:
                continue

            fn = tool_fns.get(rule.tool_name)
            if fn is None:
                continue

            args = dict(rule.default_args)
            if rule.arg_extractor:
                try:
                    extracted = rule.arg_extractor(query)
                    args.update(extracted)
                except Exception:
                    pass

            task = asyncio.create_task(self._run_prefetch(rule.tool_name, fn, args))
            self._inflight[rule.tool_name] = task
            self._prefetch_times[rule.tool_name] = time.time()
            scheduled.append(rule.tool_name)

        return scheduled

    async def _run_prefetch(self, tool_name: str, fn: Callable, args: dict) -> None:
        try:
            result = await fn(**args)
            self._results[tool_name] = {"success": True, "result": result}
        except Exception as exc:
            self._results[tool_name] = {"success": False, "error": str(exc)}
        finally:
            self._inflight.pop(tool_name, None)

    async def get_result(
        self,
        tool_name: str,
        timeout_seconds: float = 5.0,
    ) -> Optional[dict]:
        if tool_name in self._results:
            self._hits += 1
            return self._results.pop(tool_name)

        task = self._inflight.get(tool_name)
        if task:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
                if tool_name in self._results:
                    self._hits += 1
                    return self._results.pop(tool_name)
            except asyncio.TimeoutError:
                pass

        self._misses += 1
        return None

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "prefetch_hits": self._hits,
            "prefetch_misses": self._misses,
            "hit_rate": round(self._hits / total, 4) if total else 0.0,
            "inflight": list(self._inflight.keys()),
        }
```

## Solution 3: Prefetch-Backed Tool Dispatcher

```python
import time
from typing import Any, Callable, Optional


class PrefetchBackedToolDispatcher:
    """
    Wraps tool calls: checks the prefetch scheduler first,
    falls back to a live call on cache miss.
    """

    def __init__(self, scheduler: SpeculativePrefetchScheduler):
        self._scheduler = scheduler
        self._latency_saved_ms: float = 0.0

    async def dispatch(
        self,
        tool_name: str,
        live_fn: Callable,
        prefetch_timeout_seconds: float = 2.0,
        **kwargs: Any,
    ) -> dict:
        start = time.time()

        prefetched = await self._scheduler.get_result(tool_name, prefetch_timeout_seconds)
        if prefetched is not None and prefetched["success"]:
            elapsed = round((time.time() - start) * 1000, 2)
            prefetch_start = self._scheduler._prefetch_times.get(tool_name, start)
            saved = max(0.0, time.time() - prefetch_start) * 1000
            self._latency_saved_ms += saved
            return {
                "result": prefetched["result"],
                "source": "prefetch",
                "wait_ms": elapsed,
            }

        # Live fallback
        result = await live_fn(**kwargs)
        return {
            "result": result,
            "source": "live",
            "latency_ms": round((time.time() - start) * 1000, 2),
        }

    def total_latency_saved_ms(self) -> float:
        return round(self._latency_saved_ms, 2)
```

## Solution 4: Prefetch Prediction Accuracy Tracker

```python
import time
from typing import List


class PrefetchPredictionAccuracyTracker:
    """
    Records whether prefetched tools were actually requested,
    used to refine confidence thresholds and retire stale rules.
    """

    def __init__(self):
        self._events: List[dict] = []

    def record(self, tool_name: str, was_requested: bool, was_prefetched: bool) -> None:
        self._events.append({
            "ts": time.time(),
            "tool": tool_name,
            "was_requested": was_requested,
            "was_prefetched": was_prefetched,
            "useful": was_requested and was_prefetched,
            "wasted": was_prefetched and not was_requested,
        })

    def per_tool_accuracy(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [e for e in self._events if e["ts"] >= cutoff]
        by_tool: dict = {}
        for e in recent:
            t = e["tool"]
            if t not in by_tool:
                by_tool[t] = {"prefetched": 0, "requested": 0, "useful": 0, "wasted": 0}
            by_tool[t]["prefetched"] += int(e["was_prefetched"])
            by_tool[t]["requested"] += int(e["was_requested"])
            by_tool[t]["useful"] += int(e["useful"])
            by_tool[t]["wasted"] += int(e["wasted"])
        return by_tool
```

## Solution 5: Adaptive Confidence Tuner

```python
from typing import List


class AdaptiveConfidenceTuner:
    """
    Adjusts rule confidence thresholds based on observed
    prediction accuracy — raises threshold for wasteful rules,
    lowers it for under-predicted rules.
    """

    def __init__(
        self,
        rules: List[ToolPredictionRule],
        accuracy_tracker: PrefetchPredictionAccuracyTracker,
        target_precision: float = 0.80,
        adjustment_step: float = 0.05,
    ):
        self._rules = {r.tool_name: r for r in rules}
        self._tracker = accuracy_tracker
        self._target = target_precision
        self._step = adjustment_step

    def tune(self, window_seconds: float = 3600.0) -> dict:
        accuracy = self._tracker.per_tool_accuracy(window_seconds)
        adjustments = {}
        for tool_name, stats in accuracy.items():
            rule = self._rules.get(tool_name)
            if not rule or stats["prefetched"] < 10:
                continue
            precision = stats["useful"] / max(stats["prefetched"], 1)
            if precision < self._target:
                old = rule.confidence_threshold
                rule.confidence_threshold = min(0.99, old + self._step)
                adjustments[tool_name] = {"old": old, "new": rule.confidence_threshold, "precision": precision}
            elif precision > self._target + 0.10:
                old = rule.confidence_threshold
                rule.confidence_threshold = max(0.10, old - self._step)
                adjustments[tool_name] = {"old": old, "new": rule.confidence_threshold, "precision": precision}
        return adjustments
```

## Solution 6: Speculative Prefetch Dashboard

```python
import time


class SpeculativePrefetchDashboard:
    """Combines scheduler stats, accuracy, and latency savings."""

    def __init__(
        self,
        scheduler: SpeculativePrefetchScheduler,
        dispatcher: PrefetchBackedToolDispatcher,
        accuracy_tracker: PrefetchPredictionAccuracyTracker,
    ):
        self._scheduler = scheduler
        self._dispatcher = dispatcher
        self._accuracy = accuracy_tracker

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "prefetch_stats": self._scheduler.stats(),
            "total_latency_saved_ms": self._dispatcher.total_latency_saved_ms(),
            "per_tool_accuracy": self._accuracy.per_tool_accuracy(window_seconds=3600.0),
        }
```

## Comparison

| Approach | Intent Prediction | Async Prefetch | Live Fallback | Accuracy Tracking | Threshold Tuning |
|---|---|---|---|---|---|
| ToolPredictionRule | Yes (keywords+regex) | No | No | No | No |
| SpeculativePrefetchScheduler | Via rules | Yes | No | No | No |
| PrefetchBackedToolDispatcher | No | Via scheduler | Yes | No | No |
| PrefetchPredictionAccuracyTracker | No | No | No | Yes | No |
| AdaptiveConfidenceTuner | No | No | No | Via tracker | Yes |

**Best for production**: Start with `confidence_threshold=0.85` for new rules — prefetch false positives burn API quota with no benefit. Run `AdaptiveConfidenceTuner.tune()` weekly on accuracy data to converge thresholds toward the target precision. Always implement a live fallback in `PrefetchBackedToolDispatcher` — speculative pre-fetching is a latency optimization, not a correctness guarantee; the LLM may decide not to call a prefetched tool at all. Monitor `wasted` prefetches per tool: a wasted rate above 20% means the prediction rule is too aggressive and the threshold should be raised.
