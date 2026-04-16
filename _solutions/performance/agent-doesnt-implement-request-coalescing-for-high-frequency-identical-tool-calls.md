---
title: "Agent Doesn't Implement Request Coalescing for High-Frequency Identical Tool Calls"
description: "Agents that dispatch the same tool call many times within a short window — polling a status endpoint, checking a price feed, querying the same record — issue redundant requests where a single request would serve all callers. Implement request coalescing that collapses high-frequency identical calls within a time window into one execution and broadcasts the result."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-request-coalescing-for-high-frequency-identical-tool-calls
tags: [request-coalescing, high-frequency-dedup, time-window-batching, broadcast-result, rate-reduction, tool-efficiency]
symptoms:
  - "Status polling tool called 50 times per second by multiple concurrent sessions"
  - "Price feed API receives 200 identical requests per minute from the same agent pool"
  - "No coalescing — each logical caller independently dispatches the same tool call"
  - "Downstream rate limits triggered by duplicate requests from different sessions"
  - "Tool response is the same for all callers within a 100ms window but executed N times"
---

## Why This Happens

When multiple sessions or components need the same external data within a short time window — a stock price, an API status, a shared configuration — each independently calls the tool. Without coalescing, N callers produce N requests even if a single request would satisfy all of them. Request coalescing differs from in-flight deduplication (which collapses concurrent identical calls) by extending the deduplication window: even if caller A finishes before caller B submits, B can still receive A's cached result if B arrives within the coalescing window. This trades a small amount of data freshness for a large reduction in request rate.

## Solution 1: Coalescing Window Entry

```python
import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class CoalescingWindowEntry:
    key: str
    tool_name: str
    args_hash: str
    result: Optional[Any] = None
    error: Optional[Exception] = None
    executing: bool = False
    completed: bool = False
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    waiter_count: int = 0
    future: Optional[asyncio.Future] = field(default=None, repr=False)
    window_seconds: float = 1.0     # how long to reuse this result

    def is_reusable(self) -> bool:
        if not self.completed or self.completed_at is None:
            return False
        return time.time() - self.completed_at <= self.window_seconds

    def age_ms(self) -> float:
        return round((time.time() - self.created_at) * 1000, 2)
```

## Solution 2: Request Coalescing Registry

```python
import asyncio
import hashlib
import json
import time
from typing import Any, Dict, Optional, Tuple


class RequestCoalescingRegistry:
    """
    Manages coalescing windows per (tool_name, args) key.
    Within the window, all callers share the single result.
    After the window expires, the next call re-executes.
    """

    def __init__(self, default_window_seconds: float = 1.0):
        self._entries: Dict[str, CoalescingWindowEntry] = {}
        self._lock = asyncio.Lock()
        self._default_window = default_window_seconds
        self._coalesced_count = 0
        self._executed_count = 0

    @staticmethod
    def _make_key(tool_name: str, args: Dict[str, Any]) -> str:
        payload = json.dumps({"tool": tool_name, "args": args}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    async def get_or_register(
        self,
        tool_name: str,
        args: Dict[str, Any],
        window_seconds: Optional[float] = None,
    ) -> Tuple[str, bool, CoalescingWindowEntry]:
        """
        Returns (key, should_execute, entry).
        should_execute=True means caller must run the tool and call resolve().
        should_execute=False means caller should await entry.future.
        """
        key = self._make_key(tool_name, args)
        ws = window_seconds or self._default_window

        async with self._lock:
            existing = self._entries.get(key)

            # Reuse a recently-completed result within the window
            if existing and existing.is_reusable():
                existing.waiter_count += 1
                self._coalesced_count += 1
                return key, False, existing

            # Join an in-flight execution
            if existing and existing.executing and existing.future:
                existing.waiter_count += 1
                self._coalesced_count += 1
                return key, False, existing

            # Start a new execution
            loop = asyncio.get_event_loop()
            future: asyncio.Future = loop.create_future()
            entry = CoalescingWindowEntry(
                key=key,
                tool_name=tool_name,
                args_hash=key,
                executing=True,
                future=future,
                window_seconds=ws,
            )
            self._entries[key] = entry
            self._executed_count += 1
            return key, True, entry

    async def resolve(self, key: str, result: Any) -> None:
        async with self._lock:
            entry = self._entries.get(key)
        if entry:
            entry.result = result
            entry.completed = True
            entry.executing = False
            entry.completed_at = time.time()
            if entry.future and not entry.future.done():
                entry.future.set_result(result)

    async def reject(self, key: str, error: Exception) -> None:
        async with self._lock:
            entry = self._entries.pop(key, None)
        if entry and entry.future and not entry.future.done():
            entry.future.set_exception(error)

    def coalescing_ratio(self) -> float:
        total = self._coalesced_count + self._executed_count
        return round(self._coalesced_count / max(total, 1), 4)

    def stats(self) -> dict:
        return {
            "executed_calls": self._executed_count,
            "coalesced_calls": self._coalesced_count,
            "coalescing_ratio": self.coalescing_ratio(),
            "active_windows": len(self._entries),
        }
```

## Solution 3: Coalescing Tool Executor

```python
import asyncio
from typing import Any, Callable, Dict, Optional


class CoalescingToolExecutor:
    """
    Executes tool calls through the coalescing registry.
    Actual tool execution occurs at most once per coalescing window per key.
    """

    def __init__(
        self,
        registry: RequestCoalescingRegistry,
        window_seconds: float = 1.0,
    ):
        self._registry = registry
        self._window = window_seconds

    async def execute(
        self,
        tool_name: str,
        args: Dict[str, Any],
        tool_fn: Callable,
    ) -> Any:
        key, should_execute, entry = await self._registry.get_or_register(
            tool_name, args, self._window
        )

        if not should_execute:
            # Wait for the executing caller's result
            return await asyncio.shield(entry.future)

        try:
            result = await tool_fn(tool_name, args)
            await self._registry.resolve(key, result)
            return result
        except Exception as exc:
            await self._registry.reject(key, exc)
            raise
```

## Solution 4: Per-Tool Window Configuration

```python
from typing import Dict, Optional


class PerToolWindowConfig:
    """
    Maps tool names to appropriate coalescing windows.
    High-frequency polling tools use longer windows (acceptable staleness);
    mutation tools bypass coalescing entirely.
    """

    def __init__(
        self,
        tool_windows: Optional[Dict[str, Optional[float]]] = None,
        default_window_seconds: float = 1.0,
    ):
        # None value = bypass coalescing for this tool (mutations)
        self._windows: Dict[str, Optional[float]] = tool_windows or {}
        self._default = default_window_seconds

    def get_window(self, tool_name: str) -> Optional[float]:
        """Returns window in seconds, or None to bypass coalescing."""
        if tool_name in self._windows:
            return self._windows[tool_name]
        return self._default

    def register(self, tool_name: str, window_seconds: Optional[float]) -> None:
        self._windows[tool_name] = window_seconds

    def bypass_tools(self) -> list:
        return [name for name, w in self._windows.items() if w is None]
```

## Solution 5: Coalescing Savings Monitor

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Tuple


class CoalescingSavingsMonitor:
    """
    Tracks coalescing efficiency over time.
    Surfaces how many downstream API calls were avoided per minute.
    """

    def __init__(self, window_seconds: float = 300.0):
        self._window = window_seconds
        self._log: Deque[Tuple[float, int, int]] = deque()
        # (ts, executed, coalesced) per snapshot
        self._lock = Lock()

    def snapshot(self, registry: RequestCoalescingRegistry) -> None:
        stats = registry.stats()
        with self._lock:
            self._log.append((
                time.time(),
                stats["executed_calls"],
                stats["coalesced_calls"],
            ))
            cutoff = time.time() - self._window
            while self._log and self._log[0][0] < cutoff:
                self._log.popleft()

    def summary(self) -> dict:
        with self._lock:
            if len(self._log) < 2:
                return {"window_seconds": self._window, "snapshots": len(self._log)}
            first = self._log[0]
            last = self._log[-1]
        elapsed = last[0] - first[0]
        exec_delta = last[1] - first[1]
        coal_delta = last[2] - first[2]
        total = exec_delta + coal_delta
        return {
            "window_seconds": elapsed,
            "executed_calls": exec_delta,
            "coalesced_calls": coal_delta,
            "calls_avoided": coal_delta,
            "coalescing_ratio": round(coal_delta / max(total, 1), 4),
            "calls_per_second_saved": round(coal_delta / max(elapsed, 1), 2),
        }
```

## Solution 6: Coalescing Dashboard

```python
import time


class RequestCoalescingDashboard:
    """
    Combines registry stats, window config, and savings monitor
    into an operational coalescing health report.
    """

    def __init__(
        self,
        registry: RequestCoalescingRegistry,
        window_config: PerToolWindowConfig,
        monitor: CoalescingSavingsMonitor,
    ):
        self._registry = registry
        self._window_config = window_config
        self._monitor = monitor

    def render(self) -> dict:
        self._monitor.snapshot(self._registry)
        return {
            "generated_at": time.time(),
            "registry_stats": self._registry.stats(),
            "savings_summary": self._monitor.summary(),
            "bypass_tools": self._window_config.bypass_tools(),
        }
```

## Comparison

| Approach | Window-Based Reuse | In-Flight Join | Per-Tool Config | Savings Tracking | Dashboard |
|---|---|---|---|---|---|
| RequestCoalescingRegistry | Yes (TTL window) | Yes (future join) | No | No | No |
| CoalescingToolExecutor | Via registry | Via registry | No | No | No |
| PerToolWindowConfig | No | No | Yes | No | No |
| CoalescingSavingsMonitor | No | No | No | Yes | No |
| RequestCoalescingDashboard | No | No | No | No | Yes |

**Best for production**: Set `window_seconds=1.0` for status and feed endpoints that update at most once per second; use `window_seconds=0.1` (100ms) for higher-resolution data where staleness matters. Register mutation tools (POST, DELETE, write operations) with `window_seconds=None` to bypass coalescing entirely — never reuse results from write operations. Monitor `coalescing_ratio` via the dashboard: for polling-heavy workloads this should exceed 0.80 (80% of calls served from coalesced results); lower ratios indicate callers are using different argument shapes for logically identical requests and should be normalized.
