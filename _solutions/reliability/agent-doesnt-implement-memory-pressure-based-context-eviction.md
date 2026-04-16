---
title: "Agent Doesn't Implement Memory-Pressure-Based Context Eviction"
description: "Agents that accumulate conversation history and tool results in memory without eviction eventually exhaust available RAM — especially in long-running sessions with large tool outputs. When the process is OOM-killed, all session state is lost. Implement memory-pressure-based context eviction that monitors process RSS, evicts low-priority context segments when memory approaches a threshold, and prevents unbounded growth without disrupting active tasks."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-memory-pressure-based-context-eviction
tags: [memory-pressure, context-eviction, oom-prevention, session-memory, rss-monitoring, memory-management]
symptoms:
  - "Agent process OOM-killed after multi-hour sessions with large tool outputs"
  - "Memory grows monotonically across sessions with no eviction mechanism"
  - "No visibility into how much memory conversation history and tool results consume"
  - "Long-running sessions degrade system stability for all concurrent sessions"
  - "No distinction between high-priority and evictable context when memory is tight"
---

## Why This Happens

Python objects representing conversation history, tool results, and retrieved documents accumulate in heap memory for the duration of a session. In multi-session agents, each session adds to the process RSS without releasing memory until the session ends. If sessions are long, tool results are large, or many sessions are concurrent, RSS grows until the OOM killer terminates the process. Memory-pressure eviction monitors RSS proactively and, when pressure is detected, evicts lower-priority context — old tool results, compressed history summaries, cached embeddings — to free memory before the OS intervenes.

## Solution 1: Memory Pressure Monitor

```python
import os
import time
from dataclasses import dataclass


@dataclass
class MemorySnapshot:
    rss_bytes: int
    rss_mb: float
    timestamp: float
    pressure_level: str    # "normal" | "elevated" | "critical"


class MemoryPressureMonitor:
    """
    Reads process RSS from /proc/self/status (Linux) or psutil (cross-platform).
    Classifies memory pressure into levels based on configurable thresholds.
    """

    def __init__(
        self,
        elevated_threshold_mb: float = 512.0,
        critical_threshold_mb: float = 900.0,
    ):
        self._elevated = elevated_threshold_mb * 1024 * 1024
        self._critical = critical_threshold_mb * 1024 * 1024

    def snapshot(self) -> MemorySnapshot:
        rss = self._read_rss()
        rss_mb = round(rss / 1024 / 1024, 2)
        if rss >= self._critical:
            level = "critical"
        elif rss >= self._elevated:
            level = "elevated"
        else:
            level = "normal"
        return MemorySnapshot(rss_bytes=rss, rss_mb=rss_mb,
                              timestamp=time.time(), pressure_level=level)

    @staticmethod
    def _read_rss() -> int:
        try:
            import psutil
            return psutil.Process(os.getpid()).memory_info().rss
        except ImportError:
            pass
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) * 1024
        except OSError:
            pass
        return 0
```

## Solution 2: Context Segment Registry

```python
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class EvictionPriority(int, Enum):
    NEVER = 0          # system prompt, active task state
    LOW = 1            # recent tool results
    MEDIUM = 2         # older tool results, intermediate summaries
    HIGH = 3           # old conversation turns, cached embeddings
    IMMEDIATE = 4      # explicitly marked for eviction


@dataclass
class ContextSegment:
    segment_id: str
    session_id: str
    content: Any
    priority: EvictionPriority
    size_bytes: int = 0
    created_at: float = field(default_factory=time.time)
    last_accessed_at: float = field(default_factory=time.time)
    access_count: int = 0
    description: str = ""

    def __post_init__(self) -> None:
        if self.size_bytes == 0:
            self.size_bytes = sys.getsizeof(self.content)

    def touch(self) -> None:
        self.last_accessed_at = time.time()
        self.access_count += 1


class ContextSegmentRegistry:
    """
    Tracks all context segments across sessions with their eviction priorities.
    Supports listing segments by evictability for pressure-triggered eviction.
    """

    def __init__(self):
        self._segments: Dict[str, ContextSegment] = {}

    def register(self, segment: ContextSegment) -> None:
        self._segments[segment.segment_id] = segment

    def access(self, segment_id: str) -> Optional[ContextSegment]:
        seg = self._segments.get(segment_id)
        if seg:
            seg.touch()
        return seg

    def evict(self, segment_id: str) -> Optional[ContextSegment]:
        return self._segments.pop(segment_id, None)

    def candidates(
        self,
        min_priority: EvictionPriority = EvictionPriority.HIGH,
        session_id: Optional[str] = None,
    ) -> List[ContextSegment]:
        """Returns segments eligible for eviction, sorted by priority then age."""
        result = [
            s for s in self._segments.values()
            if s.priority >= min_priority
            and (session_id is None or s.session_id == session_id)
        ]
        return sorted(result, key=lambda s: (-s.priority.value, s.last_accessed_at))

    def total_tracked_bytes(self) -> int:
        return sum(s.size_bytes for s in self._segments.values())

    def stats(self) -> dict:
        return {
            "segment_count": len(self._segments),
            "total_tracked_bytes": self.total_tracked_bytes(),
            "by_priority": {
                p.name: sum(1 for s in self._segments.values() if s.priority == p)
                for p in EvictionPriority
            },
        }
```

## Solution 3: Pressure-Triggered Eviction Engine

```python
import time
from typing import Callable, List, Optional


class PressureTriggeredEvictionEngine:
    """
    Monitors memory pressure and evicts context segments when
    elevated or critical pressure is detected.
    Calls an optional eviction callback so callers can persist
    evicted content before it is dropped.
    """

    def __init__(
        self,
        monitor: MemoryPressureMonitor,
        registry: ContextSegmentRegistry,
        eviction_callback: Optional[Callable[[ContextSegment], None]] = None,
        target_eviction_bytes_elevated: int = 50 * 1024 * 1024,   # 50 MB
        target_eviction_bytes_critical: int = 200 * 1024 * 1024,  # 200 MB
    ):
        self._monitor = monitor
        self._registry = registry
        self._callback = eviction_callback
        self._target_elevated = target_eviction_bytes_elevated
        self._target_critical = target_eviction_bytes_critical
        self._eviction_events: List[dict] = []

    def check_and_evict(self) -> dict:
        snap = self._monitor.snapshot()
        if snap.pressure_level == "normal":
            return {"pressure": "normal", "evicted": 0}

        target_bytes = (
            self._target_critical if snap.pressure_level == "critical"
            else self._target_elevated
        )
        min_priority = (
            EvictionPriority.MEDIUM if snap.pressure_level == "critical"
            else EvictionPriority.HIGH
        )

        candidates = self._registry.candidates(min_priority=min_priority)
        evicted_count = 0
        evicted_bytes = 0

        for segment in candidates:
            if evicted_bytes >= target_bytes:
                break
            if self._callback:
                self._callback(segment)
            self._registry.evict(segment.segment_id)
            evicted_bytes += segment.size_bytes
            evicted_count += 1

        event = {
            "ts": time.time(),
            "pressure_level": snap.pressure_level,
            "rss_mb": snap.rss_mb,
            "evicted_count": evicted_count,
            "evicted_bytes": evicted_bytes,
        }
        self._eviction_events.append(event)

        return event

    def eviction_history(self, limit: int = 50) -> List[dict]:
        return self._eviction_events[-limit:]
```

## Solution 4: Session Memory Budget Enforcer

```python
import sys
from typing import Any, Dict


class SessionMemoryBudgetEnforcer:
    """
    Enforces a per-session memory budget by tracking segment sizes
    and rejecting new segments that would exceed the limit.
    Triggers eviction of low-priority segments from the same session
    before accepting new content when near the limit.
    """

    def __init__(
        self,
        registry: ContextSegmentRegistry,
        per_session_budget_bytes: int = 100 * 1024 * 1024,  # 100 MB per session
    ):
        self._registry = registry
        self._budget = per_session_budget_bytes

    def session_usage(self, session_id: str) -> int:
        return sum(
            s.size_bytes for s in self._registry._segments.values()
            if s.session_id == session_id
        )

    def accept(self, segment: ContextSegment) -> bool:
        """
        Attempts to register a segment within budget.
        Evicts lower-priority segments from the same session if needed.
        Returns True if accepted, False if budget cannot be freed.
        """
        current = self.session_usage(segment.session_id)
        needed = segment.size_bytes

        if current + needed <= self._budget:
            self._registry.register(segment)
            return True

        # Try to free space by evicting lower-priority segments
        deficit = (current + needed) - self._budget
        candidates = self._registry.candidates(
            min_priority=segment.priority,
            session_id=segment.session_id,
        )
        freed = 0
        for cand in candidates:
            self._registry.evict(cand.segment_id)
            freed += cand.size_bytes
            if freed >= deficit:
                break

        if freed >= deficit:
            self._registry.register(segment)
            return True

        return False   # Cannot free enough space
```

## Solution 5: Periodic Eviction Scheduler

```python
import asyncio
import time
from typing import Optional


class PeriodicEvictionScheduler:
    """
    Runs the eviction engine on a configurable interval in the background.
    Ensures memory pressure is checked continuously rather than only
    when new context is added.
    """

    def __init__(
        self,
        engine: PressureTriggeredEvictionEngine,
        check_interval_seconds: float = 30.0,
    ):
        self._engine = engine
        self._interval = check_interval_seconds
        self._task: Optional[asyncio.Task] = None
        self._check_count = 0

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            self._engine.check_and_evict()
            self._check_count += 1
            await asyncio.sleep(self._interval)

    def stats(self) -> dict:
        return {
            "check_count": self._check_count,
            "check_interval_seconds": self._interval,
        }
```

## Solution 6: Memory Pressure Dashboard

```python
import time


class MemoryPressureDashboard:
    """
    Combines real-time memory snapshot, segment registry stats,
    and eviction history into a single operational view.
    """

    def __init__(
        self,
        monitor: MemoryPressureMonitor,
        registry: ContextSegmentRegistry,
        engine: PressureTriggeredEvictionEngine,
    ):
        self._monitor = monitor
        self._registry = registry
        self._engine = engine

    def render(self) -> dict:
        snap = self._monitor.snapshot()
        history = self._engine.eviction_history(limit=10)
        total_evicted = sum(e["evicted_count"] for e in history)

        return {
            "generated_at": time.time(),
            "memory": {
                "rss_mb": snap.rss_mb,
                "pressure_level": snap.pressure_level,
            },
            "registry": self._registry.stats(),
            "recent_evictions": history,
            "total_evicted_segments_recent": total_evicted,
        }
```

## Comparison

| Approach | RSS Monitoring | Segment Tracking | Pressure Eviction | Per-Session Budget | Scheduled Checks |
|---|---|---|---|---|---|
| MemoryPressureMonitor | Yes | No | No | No | No |
| ContextSegmentRegistry | No | Yes (priority + LRU) | No | No | No |
| PressureTriggeredEvictionEngine | Via monitor | Via registry | Yes | No | No |
| SessionMemoryBudgetEnforcer | No | Via registry | Yes (preemptive) | Yes | No |
| PeriodicEvictionScheduler | No | No | Via engine | No | Yes |
| MemoryPressureDashboard | No | No | No | No | No |

**Best for production**: Install `psutil` for accurate cross-platform RSS readings — the `/proc/self/status` fallback is Linux-only and may be unavailable in some container environments. Set `elevated_threshold_mb` to 70% of the container memory limit and `critical_threshold_mb` to 85%: this gives the eviction engine two opportunities to free memory before the OOM killer triggers at 100%. Mark tool results older than 10 minutes as `EvictionPriority.HIGH` and active conversation turns as `EvictionPriority.LOW` — the LLM can reconstruct context from summaries but cannot recover evicted intermediate tool outputs that were never summarized. Run `PeriodicEvictionScheduler` with `check_interval_seconds=30`: checking more frequently wastes CPU on RSS reads, less frequently risks missing pressure spikes from sudden large tool outputs.
