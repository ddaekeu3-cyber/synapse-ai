---
title: "Agent Doesn't Implement Tool Error Rate per Category Tracking"
description: "Agents that aggregate all tool errors into a single counter cannot distinguish whether failures are concentrated in network-bound tools, database tools, or file system tools — masking the root cause of reliability incidents. Implement per-category tool error rate tracking that bins tools by type, computes error rates per category over sliding windows, and surfaces which categories are degrading."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-tool-error-rate-per-category-tracking
tags: [tool-error-rate, error-categorization, reliability-metrics, sliding-window, anomaly-detection, per-category-metrics]
symptoms:
  - "Overall tool error rate spikes but it's unclear which tool category is responsible"
  - "HTTP tool failures are averaged with file I/O failures masking network-specific outages"
  - "No breakdown of error rates by tool type — only a single global error counter"
  - "On-call engineers cannot tell from dashboards whether failures are transient or systemic"
  - "Error rate trends per category are unavailable for capacity planning"
---

## Why This Happens

A single `tool_error_count` metric is easy to instrument but provides no diagnostic signal: a spike could be caused by one flaky HTTP endpoint, a saturated database pool, or a misconfigured file path. Per-category tracking requires classifying each tool into a category at registration time, maintaining separate sliding-window counters per category, and computing error rates independently. When a category's error rate crosses a threshold, the signal is actionable — it points directly to the class of dependency that is failing.

## Solution 1: Tool Category Registry

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class ToolCategory(str, Enum):
    HTTP = "http"
    DATABASE = "database"
    FILE_SYSTEM = "file_system"
    CACHE = "cache"
    LLM = "llm"
    SEARCH = "search"
    MESSAGING = "messaging"
    COMPUTE = "compute"
    CUSTOM = "custom"


@dataclass
class ToolCategorySpec:
    tool_name: str
    category: ToolCategory
    subcategory: str = ""     # e.g. "postgres", "redis", "openai"
    tags: List[str] = field(default_factory=list)


class ToolCategoryRegistry:
    """Maps tool names to their category specifications."""

    def __init__(self):
        self._specs: Dict[str, ToolCategorySpec] = {}

    def register(self, spec: ToolCategorySpec) -> None:
        self._specs[spec.tool_name] = spec

    def category_of(self, tool_name: str) -> ToolCategory:
        spec = self._specs.get(tool_name)
        if spec:
            return spec.category
        # Heuristic fallback based on name prefix
        name_lower = tool_name.lower()
        if any(k in name_lower for k in ("http", "request", "fetch", "api")):
            return ToolCategory.HTTP
        if any(k in name_lower for k in ("db", "sql", "query", "database")):
            return ToolCategory.DATABASE
        if any(k in name_lower for k in ("file", "read", "write", "fs")):
            return ToolCategory.FILE_SYSTEM
        if any(k in name_lower for k in ("cache", "redis", "memcache")):
            return ToolCategory.CACHE
        if any(k in name_lower for k in ("embed", "complete", "llm", "gpt")):
            return ToolCategory.LLM
        return ToolCategory.CUSTOM

    def all_categories(self) -> List[ToolCategory]:
        return list({s.category for s in self._specs.values()})
```

## Solution 2: Sliding Window Error Rate Counter

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Tuple


class SlidingWindowErrorRateCounter:
    """
    Tracks call outcomes in a sliding time window and computes
    error rate on demand. Thread-safe.
    """

    def __init__(self, window_seconds: float = 300.0):
        self._window = window_seconds
        # Each entry: (timestamp, is_error)
        self._events: Deque[Tuple[float, bool]] = deque()
        self._lock = Lock()

    def record(self, is_error: bool) -> None:
        now = time.time()
        with self._lock:
            self._events.append((now, is_error))
            self._evict(now)

    def _evict(self, now: float) -> None:
        cutoff = now - self._window
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def error_rate(self) -> float:
        now = time.time()
        with self._lock:
            self._evict(now)
            if not self._events:
                return 0.0
            errors = sum(1 for _, e in self._events if e)
            return round(errors / len(self._events), 4)

    def call_count(self) -> int:
        now = time.time()
        with self._lock:
            self._evict(now)
            return len(self._events)

    def error_count(self) -> int:
        now = time.time()
        with self._lock:
            self._evict(now)
            return sum(1 for _, e in self._events if e)
```

## Solution 3: Per-Category Error Rate Tracker

```python
import time
from typing import Dict, List, Optional


class PerCategoryErrorRateTracker:
    """
    Maintains one SlidingWindowErrorRateCounter per tool category
    and one per individual tool name for granular breakdown.
    """

    def __init__(
        self,
        registry: ToolCategoryRegistry,
        window_seconds: float = 300.0,
    ):
        self._registry = registry
        self._window = window_seconds
        self._by_category: Dict[str, SlidingWindowErrorRateCounter] = {}
        self._by_tool: Dict[str, SlidingWindowErrorRateCounter] = {}

    def _get_or_create(self, store: dict, key: str) -> SlidingWindowErrorRateCounter:
        if key not in store:
            store[key] = SlidingWindowErrorRateCounter(self._window)
        return store[key]

    def record(self, tool_name: str, is_error: bool) -> None:
        category = self._registry.category_of(tool_name).value
        self._get_or_create(self._by_category, category).record(is_error)
        self._get_or_create(self._by_tool, tool_name).record(is_error)

    def category_error_rate(self, category: ToolCategory) -> float:
        counter = self._by_category.get(category.value)
        return counter.error_rate() if counter else 0.0

    def tool_error_rate(self, tool_name: str) -> float:
        counter = self._by_tool.get(tool_name)
        return counter.error_rate() if counter else 0.0

    def all_category_rates(self) -> Dict[str, dict]:
        return {
            cat: {
                "error_rate": counter.error_rate(),
                "calls": counter.call_count(),
                "errors": counter.error_count(),
            }
            for cat, counter in self._by_category.items()
        }

    def all_tool_rates(self) -> Dict[str, dict]:
        return {
            tool: {
                "error_rate": counter.error_rate(),
                "calls": counter.call_count(),
                "errors": counter.error_count(),
            }
            for tool, counter in self._by_tool.items()
        }
```

## Solution 4: Error Rate Threshold Alerter

```python
from typing import Dict, List, Optional


class ErrorRateThresholdAlerter:
    """
    Evaluates category error rates against configured thresholds
    and returns a list of active alerts with severity levels.
    """

    def __init__(
        self,
        tracker: PerCategoryErrorRateTracker,
        thresholds: Optional[Dict[str, float]] = None,
        min_calls_to_alert: int = 10,
    ):
        self._tracker = tracker
        self._thresholds = thresholds or {
            "http": 0.10,
            "database": 0.05,
            "llm": 0.08,
            "cache": 0.05,
            "file_system": 0.15,
            "search": 0.10,
            "messaging": 0.10,
            "compute": 0.10,
            "custom": 0.15,
        }
        self._min_calls = min_calls_to_alert

    def evaluate(self) -> List[dict]:
        alerts = []
        for category, data in self._tracker.all_category_rates().items():
            if data["calls"] < self._min_calls:
                continue
            threshold = self._thresholds.get(category, 0.10)
            rate = data["error_rate"]
            if rate >= threshold:
                severity = "critical" if rate >= threshold * 2 else "warning"
                alerts.append({
                    "category": category,
                    "error_rate": rate,
                    "threshold": threshold,
                    "calls": data["calls"],
                    "errors": data["errors"],
                    "severity": severity,
                    "excess_pct": round((rate - threshold) / threshold * 100, 1),
                })
        return sorted(alerts, key=lambda a: a["error_rate"], reverse=True)
```

## Solution 5: Instrumented Tool Call Dispatcher

```python
import time
from typing import Any, Callable


class CategoryInstrumentedToolDispatcher:
    """
    Wraps every tool call with error rate recording.
    Records both success and failure outcomes per tool and category.
    """

    def __init__(self, tracker: PerCategoryErrorRateTracker):
        self._tracker = tracker

    async def dispatch(
        self,
        tool_name: str,
        fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> dict:
        start = time.time()
        try:
            result = await fn(*args, **kwargs)
            self._tracker.record(tool_name, is_error=False)
            return {
                "success": True,
                "result": result,
                "tool_name": tool_name,
                "latency_ms": round((time.time() - start) * 1000, 2),
            }
        except Exception as exc:
            self._tracker.record(tool_name, is_error=True)
            return {
                "success": False,
                "error": str(exc),
                "tool_name": tool_name,
                "latency_ms": round((time.time() - start) * 1000, 2),
            }
```

## Solution 6: Tool Error Rate Dashboard

```python
import time


class ToolErrorRateDashboard:
    """
    Combines per-category rates, per-tool rates, and active alerts
    into a single operational snapshot.
    """

    def __init__(
        self,
        tracker: PerCategoryErrorRateTracker,
        alerter: ErrorRateThresholdAlerter,
    ):
        self._tracker = tracker
        self._alerter = alerter

    def render(self) -> dict:
        category_rates = self._tracker.all_category_rates()
        tool_rates = self._tracker.all_tool_rates()
        alerts = self._alerter.evaluate()

        worst_tools = sorted(
            [(t, d) for t, d in tool_rates.items() if d["calls"] >= 5],
            key=lambda x: x[1]["error_rate"],
            reverse=True,
        )[:5]

        return {
            "generated_at": time.time(),
            "by_category": category_rates,
            "worst_tools": [
                {"tool": t, **d} for t, d in worst_tools
            ],
            "active_alerts": alerts,
            "alert_count": len(alerts),
        }
```

## Comparison

| Approach | Category Binning | Per-Tool Rate | Sliding Window | Threshold Alerts | Instrumentation |
|---|---|---|---|---|---|
| ToolCategoryRegistry | Yes | No | No | No | No |
| SlidingWindowErrorRateCounter | No | Per instance | Yes | No | No |
| PerCategoryErrorRateTracker | Yes | Yes | Via counters | No | No |
| ErrorRateThresholdAlerter | Via tracker | No | Via tracker | Yes | No |
| CategoryInstrumentedToolDispatcher | Via tracker | Via tracker | Via tracker | No | Yes |
| ToolErrorRateDashboard | No | No | No | Via alerter | No |

**Best for production**: Use a 5-minute sliding window (`window_seconds=300`) for real-time alerting and a separate 1-hour window for trend dashboards — the 5-minute window catches sudden outages while the 1-hour window shows gradual degradation. Set `min_calls_to_alert=10` to avoid false alarms from tools that are called rarely and have 1 error out of 2 calls. Register all tools explicitly in `ToolCategoryRegistry` rather than relying on name heuristics — misclassified tools produce misleading category rates. Alert on `database` error rate above 5% before HTTP: database failures cascade wider and recovery is slower.
