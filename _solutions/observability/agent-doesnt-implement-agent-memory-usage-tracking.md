---
title: "Agent Doesn't Implement Agent Memory Usage Tracking"
description: "Agents that never measure their own memory consumption silently grow until OOM kills take them down: conversation history accumulates without eviction, tool result buffers expand unboundedly, and embedding caches fill available RAM. Implement memory usage tracking that measures per-component allocation, detects growth trends, and alerts before resident set size reaches dangerous thresholds."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-agent-memory-usage-tracking
tags: [memory-tracking, oom-prevention, rss-monitoring, memory-growth, resource-observability, heap-profiling]
symptoms:
  - "Agent process is OOM-killed with no prior warning in logs"
  - "Memory grows monotonically over long sessions with no plateau"
  - "No visibility into which component (history, cache, embeddings) is consuming memory"
  - "On-call engineers restart the agent to recover memory without knowing the root cause"
  - "Memory usage at peak load is unknown — capacity planning is guesswork"
---

## Why This Happens

Python objects — conversation turns, cached embeddings, tool result buffers — are allocated on the heap and freed only when all references are dropped. In long-running agent processes, these structures accumulate: history lists grow with each turn, LRU caches fill to their configured maximum, and temporary buffers are sometimes held by closures or module-level variables. Without explicit measurement, memory consumption is invisible until the OOM killer or a container memory limit terminates the process. Tracking requires periodic sampling of `resource.getrusage`, `tracemalloc` snapshots, or `psutil.Process().memory_info()`, combined with per-component size estimates derived from object introspection.

## Solution 1: Memory Snapshot

```python
import os
import time
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class MemorySnapshot:
    snapshot_id: str
    rss_bytes: int              # resident set size
    vms_bytes: int              # virtual memory size
    component_sizes: Dict[str, int] = field(default_factory=dict)  # component -> bytes
    taken_at: float = field(default_factory=time.time)
    process_uptime_seconds: float = 0.0

    def rss_mb(self) -> float:
        return round(self.rss_bytes / 1024 / 1024, 2)

    def vms_mb(self) -> float:
        return round(self.vms_bytes / 1024 / 1024, 2)
```

## Solution 2: Process Memory Sampler

```python
import os
import time
import uuid
from typing import Optional

try:
    import psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False

try:
    import resource
    _RESOURCE_AVAILABLE = True
except ImportError:
    _RESOURCE_AVAILABLE = False


class ProcessMemorySampler:
    """
    Samples the current process's memory usage using psutil if available,
    falling back to resource.getrusage for environments without psutil.
    """

    def __init__(self):
        self._process = psutil.Process(os.getpid()) if _PSUTIL_AVAILABLE else None
        self._start_time = time.time()

    def sample(self) -> MemorySnapshot:
        rss = 0
        vms = 0

        if self._process:
            info = self._process.memory_info()
            rss = info.rss
            vms = info.vms
        elif _RESOURCE_AVAILABLE:
            usage = resource.getrusage(resource.RUSAGE_SELF)
            # ru_maxrss is in kilobytes on Linux, bytes on macOS
            rss = usage.ru_maxrss * 1024 if os.uname().sysname == "Linux" else usage.ru_maxrss
            vms = rss  # not separately available without psutil

        return MemorySnapshot(
            snapshot_id=uuid.uuid4().hex[:8],
            rss_bytes=rss,
            vms_bytes=vms,
            process_uptime_seconds=round(time.time() - self._start_time, 1),
        )
```

## Solution 3: Component Memory Estimator

```python
import sys
from typing import Any, Dict, List


class ComponentMemoryEstimator:
    """
    Estimates the memory footprint of named agent components
    using sys.getsizeof for shallow sizes and recursive traversal
    for containers. Returns per-component byte estimates.
    """

    def estimate(self, components: Dict[str, Any]) -> Dict[str, int]:
        return {name: self._deep_sizeof(obj) for name, obj in components.items()}

    def _deep_sizeof(self, obj: Any, _seen: set = None) -> int:
        if _seen is None:
            _seen = set()
        obj_id = id(obj)
        if obj_id in _seen:
            return 0
        _seen.add(obj_id)
        size = sys.getsizeof(obj)
        if isinstance(obj, dict):
            size += sum(
                self._deep_sizeof(k, _seen) + self._deep_sizeof(v, _seen)
                for k, v in obj.items()
            )
        elif isinstance(obj, (list, tuple, set, frozenset)):
            size += sum(self._deep_sizeof(item, _seen) for item in obj)
        return size
```

## Solution 4: Memory Growth Detector

```python
import time
from collections import deque
from typing import Deque, Optional, Tuple


class MemoryGrowthDetector:
    """
    Detects monotonic RSS growth trends by comparing recent snapshots
    against a baseline. Raises an alert when growth rate exceeds a
    configured threshold.
    """

    def __init__(
        self,
        window_snapshots: int = 20,
        growth_rate_alert_mb_per_minute: float = 10.0,
    ):
        self._window = window_snapshots
        self._alert_rate = growth_rate_alert_mb_per_minute
        self._history: Deque[Tuple[float, int]] = deque(maxlen=window_snapshots)
        # (timestamp, rss_bytes)

    def record(self, snapshot: MemorySnapshot) -> None:
        self._history.append((snapshot.taken_at, snapshot.rss_bytes))

    def growth_rate_mb_per_minute(self) -> Optional[float]:
        if len(self._history) < 2:
            return None
        oldest_ts, oldest_rss = self._history[0]
        newest_ts, newest_rss = self._history[-1]
        elapsed_minutes = (newest_ts - oldest_ts) / 60.0
        if elapsed_minutes < 0.01:
            return None
        delta_mb = (newest_rss - oldest_rss) / 1024 / 1024
        return round(delta_mb / elapsed_minutes, 3)

    def check(self) -> dict:
        rate = self.growth_rate_mb_per_minute()
        alerting = rate is not None and rate > self._alert_rate
        return {
            "growth_rate_mb_per_minute": rate,
            "alert_threshold_mb_per_minute": self._alert_rate,
            "alerting": alerting,
            "snapshots_in_window": len(self._history),
        }
```

## Solution 5: Memory Usage Tracker

```python
import asyncio
import time
from typing import Any, Dict, List, Optional


class AgentMemoryUsageTracker:
    """
    Periodically samples process memory, estimates per-component sizes,
    detects growth trends, and maintains a rolling history of snapshots.
    """

    def __init__(
        self,
        sampler: ProcessMemorySampler,
        estimator: ComponentMemoryEstimator,
        growth_detector: MemoryGrowthDetector,
        sample_interval_seconds: float = 30.0,
        max_history: int = 200,
        rss_alert_mb: float = 1024.0,
    ):
        self._sampler = sampler
        self._estimator = estimator
        self._growth = growth_detector
        self._interval = sample_interval_seconds
        self._max_history = max_history
        self._alert_mb = rss_alert_mb
        self._history: List[MemorySnapshot] = []
        self._components: Dict[str, Any] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None

    def register_component(self, name: str, obj: Any) -> None:
        self._components[name] = obj

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        while self._running:
            self._take_snapshot()
            await asyncio.sleep(self._interval)

    def _take_snapshot(self) -> MemorySnapshot:
        snapshot = self._sampler.sample()
        snapshot.component_sizes = self._estimator.estimate(self._components)
        self._growth.record(snapshot)
        self._history.append(snapshot)
        if len(self._history) > self._max_history:
            self._history.pop(0)
        return snapshot

    def current(self) -> Optional[MemorySnapshot]:
        return self._history[-1] if self._history else None

    def summary(self) -> dict:
        current = self.current()
        growth = self._growth.check()
        over_limit = current is not None and current.rss_mb() >= self._alert_mb
        return {
            "current_rss_mb": current.rss_mb() if current else None,
            "alert_threshold_mb": self._alert_mb,
            "over_limit": over_limit,
            "growth": growth,
            "component_sizes_bytes": current.component_sizes if current else {},
            "snapshots_recorded": len(self._history),
        }
```

## Solution 6: Memory Usage Dashboard

```python
import time


class AgentMemoryDashboard:
    """
    Combines process memory, component-level breakdown, and
    growth trend analysis into a single operational view.
    """

    def __init__(self, tracker: AgentMemoryUsageTracker):
        self._tracker = tracker

    def render(self) -> dict:
        summary = self._tracker.summary()
        history = self._tracker._history
        rss_trend = [
            {"ts": s.taken_at, "rss_mb": s.rss_mb()}
            for s in history[-10:]
        ]
        component_mb = {
            k: round(v / 1024 / 1024, 3)
            for k, v in summary.get("component_sizes_bytes", {}).items()
        }
        return {
            "generated_at": time.time(),
            "current_rss_mb": summary["current_rss_mb"],
            "alert_threshold_mb": summary["alert_threshold_mb"],
            "over_limit": summary["over_limit"],
            "growth_rate_mb_per_minute": summary["growth"]["growth_rate_mb_per_minute"],
            "growth_alerting": summary["growth"]["alerting"],
            "component_sizes_mb": component_mb,
            "rss_trend": rss_trend,
        }
```

## Comparison

| Approach | RSS Sampling | Component Sizing | Growth Detection | Continuous Monitoring | Dashboard |
|---|---|---|---|---|---|
| ProcessMemorySampler | Yes (psutil/resource) | No | No | No | No |
| ComponentMemoryEstimator | No | Yes (deep sizeof) | No | No | No |
| MemoryGrowthDetector | No | No | Yes (rate/window) | No | No |
| AgentMemoryUsageTracker | Via sampler | Via estimator | Via detector | Yes (async loop) | No |
| AgentMemoryDashboard | No | No | No | No | Yes |

**Best for production**: Set `rss_alert_mb` to 80% of the container memory limit — this gives enough headroom for a burst before the OOM killer fires. Register the conversation history list, the tool result buffer, and the embedding cache as named components so `ComponentMemoryEstimator` can show which one is growing. Set `sample_interval_seconds=30` for steady-state monitoring and `growth_rate_alert_mb_per_minute=10` to catch runaway accumulation within a few minutes of it starting. A growth rate consistently above threshold with no corresponding increase in active sessions indicates a reference leak — use `tracemalloc` snapshots at that point to identify the specific allocation site.
