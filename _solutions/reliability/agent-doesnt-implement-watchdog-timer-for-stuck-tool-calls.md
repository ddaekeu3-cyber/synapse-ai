---
title: "Agent Doesn't Implement Watchdog Timer for Stuck Tool Calls"
description: "AI agents that await tool calls without a watchdog timer hang indefinitely when a tool deadlocks, network I/O blocks forever, or an external process stalls. A watchdog timer runs alongside every tool call, cancels it after a configurable deadline, and either returns a timeout error or triggers a fallback — preventing one stuck tool from freezing the entire agent."
date: 2025-02-14
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-watchdog-timer-for-stuck-tool-calls
tags:
  - watchdog
  - timeout
  - deadlock
  - stuck
  - tool-call
  - asyncio
  - reliability
symptoms:
  - "Agent hangs indefinitely after calling a database tool that holds a lock"
  - "No response from agent after 5 minutes — tool call is blocking on a TCP read"
  - "Agent process must be killed and restarted to recover from a stuck external API call"
  - "Tool call has no timeout; asyncio.wait_for is only applied inconsistently"
  - "One slow tool call blocks all subsequent tool calls in the same agent step"
---

## Problem

`asyncio.wait_for` and `httpx` request timeouts only protect against slow I/O; they do not protect against code that acquires a lock and never releases it, a subprocess that hangs without producing output, or an async function that enters an infinite retry loop internally. A watchdog timer runs as a concurrent task that forcibly cancels the tool coroutine after a wall-clock deadline, regardless of what the coroutine is doing internally. Combined with structured logging of which tool timed out, watchdogs make stuck-tool failures visible and recoverable.

---

## Solution 1: WatchdogTimer — Coroutine-Level Deadline Enforcement

```python
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class WatchdogResult:
    value: Optional[Any]
    timed_out: bool
    elapsed_ms: float
    tool_name: str


class WatchdogTimer:
    """
    Runs a coroutine under a strict wall-clock deadline.
    If the coroutine does not complete within `timeout_s`, the watchdog
    cancels it and returns a WatchdogResult with timed_out=True.

    Usage:
        wd = WatchdogTimer(default_timeout_s=10.0)

        result = await wd.run("db_query", db.fetch_user, user_id=42)
        if result.timed_out:
            return {"error": "db_query timed out after 10s"}
        return result.value
    """

    def __init__(self, default_timeout_s: float = 10.0):
        self._default = default_timeout_s

    async def run(self, tool_name: str, fn: Callable,
                  *args,
                  timeout_s: Optional[float] = None,
                  **kwargs) -> WatchdogResult:
        deadline = timeout_s if timeout_s is not None else self._default
        t0 = time.monotonic()
        try:
            value = await asyncio.wait_for(fn(*args, **kwargs), timeout=deadline)
            elapsed = (time.monotonic() - t0) * 1000
            return WatchdogResult(value=value, timed_out=False,
                                   elapsed_ms=elapsed, tool_name=tool_name)
        except asyncio.TimeoutError:
            elapsed = (time.monotonic() - t0) * 1000
            logger.error(
                "watchdog_timeout tool=%s deadline_s=%.1f elapsed_ms=%.0f",
                tool_name, deadline, elapsed,
            )
            return WatchdogResult(value=None, timed_out=True,
                                   elapsed_ms=elapsed, tool_name=tool_name)
        except Exception as exc:
            elapsed = (time.monotonic() - t0) * 1000
            logger.error(
                "watchdog_error tool=%s error=%s elapsed_ms=%.0f",
                tool_name, exc, elapsed,
            )
            raise
```

---

## Solution 2: PerToolTimeoutRegistry — Per-Tool Deadline Configuration

```python
import asyncio
import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class PerToolTimeoutRegistry:
    """
    Registry that maps tool names to individual timeouts.
    Fast tools (key-value lookup) get short deadlines; slow tools
    (LLM re-ranking, PDF parsing) get longer ones.
    Falls back to a global default for unregistered tools.

    Usage:
        reg = PerToolTimeoutRegistry(default_timeout_s=10.0)
        reg.register("db_query",       timeout_s=3.0)
        reg.register("web_fetch",      timeout_s=15.0)
        reg.register("pdf_parse",      timeout_s=60.0)
        reg.register("cache_lookup",   timeout_s=1.0)

        result = await reg.call("db_query", db.fetch, user_id=u)
    """

    def __init__(self, default_timeout_s: float = 10.0):
        self._default = default_timeout_s
        self._timeouts: Dict[str, float] = {}
        self._fns: Dict[str, Callable] = {}
        self._watchdog = WatchdogTimer(default_timeout_s)

    def register(self, name: str, fn: Optional[Callable] = None,
                 timeout_s: Optional[float] = None):
        if fn:
            self._fns[name] = fn
        self._timeouts[name] = timeout_s or self._default

    async def call(self, tool_name: str, fn: Optional[Callable] = None,
                    *args, **kwargs) -> Any:
        resolved_fn = fn or self._fns.get(tool_name)
        if resolved_fn is None:
            raise KeyError(f"Tool '{tool_name}' not registered and no fn provided")
        timeout_s = self._timeouts.get(tool_name, self._default)
        result = await self._watchdog.run(
            tool_name, resolved_fn, *args,
            timeout_s=timeout_s, **kwargs
        )
        if result.timed_out:
            raise asyncio.TimeoutError(
                f"Tool '{tool_name}' timed out after {timeout_s}s"
            )
        return result.value

    def timeout_for(self, tool_name: str) -> float:
        return self._timeouts.get(tool_name, self._default)
```

---

## Solution 3: WatchdogWithFallback — Automatic Fallback on Timeout

```python
import asyncio
import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class WatchdogWithFallback:
    """
    Extends WatchdogTimer with a per-tool fallback callable.
    If the primary tool times out, the fallback is invoked with the same
    arguments — returning a degraded but non-empty result rather than an error.

    Usage:
        wd = WatchdogWithFallback(default_timeout_s=5.0)
        wd.set_fallback("search",
                        fallback_fn=cached_search,
                        fallback_timeout_s=2.0)

        result = await wd.run("search", live_search, query=q)
        # On timeout: returns cached_search(query=q) result
    """

    def __init__(self, default_timeout_s: float = 5.0):
        self._default = default_timeout_s
        self._fallbacks: Dict[str, tuple] = {}  # name -> (fn, timeout_s)

    def set_fallback(self, tool_name: str, fallback_fn: Callable,
                     fallback_timeout_s: float = 2.0):
        self._fallbacks[tool_name] = (fallback_fn, fallback_timeout_s)

    async def run(self, tool_name: str, primary_fn: Callable,
                  *args, timeout_s: Optional[float] = None, **kwargs) -> Any:
        deadline = timeout_s or self._default
        try:
            return await asyncio.wait_for(
                primary_fn(*args, **kwargs), timeout=deadline
            )
        except asyncio.TimeoutError:
            logger.warning("watchdog_timeout tool=%s — trying fallback", tool_name)
            fallback = self._fallbacks.get(tool_name)
            if fallback:
                fb_fn, fb_timeout = fallback
                try:
                    return await asyncio.wait_for(
                        fb_fn(*args, **kwargs), timeout=fb_timeout
                    )
                except asyncio.TimeoutError:
                    logger.error("watchdog_fallback_also_timed_out tool=%s", tool_name)
            raise asyncio.TimeoutError(
                f"Tool '{tool_name}' and its fallback both timed out"
            )
```

---

## Solution 4: WatchdogHealthTracker — Timeout Rate Monitoring

```python
import logging
import time
from collections import defaultdict, deque
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class WatchdogHealthTracker:
    """
    Wraps WatchdogTimer and tracks timeout rates per tool over a rolling window.
    Fires an alert when a tool's timeout rate exceeds a threshold — indicating
    a degraded upstream dependency rather than transient jitter.

    Usage:
        tracker = WatchdogHealthTracker(window_s=300, alert_rate=0.2)
        asyncio.create_task(tracker.monitor(interval_s=30))

        result = await tracker.run("db_query", db.fetch, user_id=u)
    """

    def __init__(self, default_timeout_s: float = 10.0,
                 window_s: float = 300.0,
                 alert_rate: float = 0.2):
        self._wd = WatchdogTimer(default_timeout_s)
        self._window = window_s
        self._alert_rate = alert_rate
        # tool -> deque of (timestamp, timed_out)
        self._history: Dict[str, deque] = defaultdict(lambda: deque())
        self._alerted: Dict[str, float] = {}

    async def run(self, tool_name: str, fn: Callable,
                  *args, timeout_s: Optional[float] = None, **kwargs) -> Any:
        result = await self._wd.run(tool_name, fn, *args,
                                     timeout_s=timeout_s, **kwargs)
        now = time.monotonic()
        self._history[tool_name].append((now, result.timed_out))
        # Evict old entries
        cutoff = now - self._window
        dq = self._history[tool_name]
        while dq and dq[0][0] < cutoff:
            dq.popleft()
        self._check_alert(tool_name, now)
        if result.timed_out:
            raise TimeoutError(f"Tool '{tool_name}' watchdog timed out")
        return result.value

    def _check_alert(self, tool_name: str, now: float):
        dq = self._history[tool_name]
        if len(dq) < 5:
            return
        rate = sum(1 for _, to in dq if to) / len(dq)
        if rate >= self._alert_rate:
            last = self._alerted.get(tool_name, 0)
            if now - last > self._window:
                self._alerted[tool_name] = now
                logger.error(
                    "watchdog_alert tool=%s timeout_rate=%.0f%% samples=%d",
                    tool_name, rate * 100, len(dq),
                )

    def health_report(self) -> Dict[str, Dict]:
        report = {}
        now = time.monotonic()
        for tool, dq in self._history.items():
            recent = [(ts, to) for ts, to in dq if now - ts <= self._window]
            if not recent:
                continue
            timeouts = sum(1 for _, to in recent if to)
            report[tool] = {
                "calls": len(recent),
                "timeouts": timeouts,
                "timeout_rate": round(timeouts / len(recent), 3),
            }
        return report
```

---

## Solution 5: AdaptiveWatchdog — Dynamic Timeout from Latency History

```python
import asyncio
import logging
import statistics
import time
from collections import defaultdict, deque
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class AdaptiveWatchdog:
    """
    Sets timeouts dynamically based on observed p99 latency for each tool.
    Timeout = max(min_s, p99 * multiplier). New tools use a default until
    enough samples are collected.

    Usage:
        wd = AdaptiveWatchdog(
            warmup_default_s=10.0,
            p99_multiplier=2.0,
            min_timeout_s=1.0,
        )
        result = await wd.run("api_call", fetch_fn, url=url)
    """

    def __init__(self, warmup_default_s: float = 10.0,
                 p99_multiplier: float = 2.0,
                 min_timeout_s: float = 1.0,
                 warmup_samples: int = 20,
                 window: int = 200):
        self._default = warmup_default_s
        self._mult = p99_multiplier
        self._min = min_timeout_s
        self._warmup = warmup_samples
        self._latencies: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=window)
        )

    def _timeout_for(self, tool_name: str) -> float:
        samples = list(self._latencies[tool_name])
        if len(samples) < self._warmup:
            return self._default
        p99 = sorted(samples)[int(len(samples) * 0.99)]
        return max(self._min, p99 * self._mult)

    async def run(self, tool_name: str, fn: Callable,
                  *args, **kwargs) -> Any:
        timeout_s = self._timeout_for(tool_name)
        t0 = time.monotonic()
        try:
            value = await asyncio.wait_for(fn(*args, **kwargs), timeout=timeout_s)
            elapsed = time.monotonic() - t0
            self._latencies[tool_name].append(elapsed)
            return value
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - t0
            self._latencies[tool_name].append(elapsed)
            logger.error(
                "adaptive_watchdog_timeout tool=%s computed_timeout_s=%.2f",
                tool_name, timeout_s,
            )
            raise

    def timeout_table(self) -> Dict[str, float]:
        return {name: self._timeout_for(name) for name in self._latencies}
```

---

## Solution 6: WatchdogMiddleware — Transparent Wrap for All Agent Tools

```python
import asyncio
import functools
import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class WatchdogMiddleware:
    """
    Decorates all registered agent tool functions with a watchdog timer.
    Tool authors do not need to add timeout logic themselves.

    Usage:
        wm = WatchdogMiddleware(default_timeout_s=10.0)

        @wm.protect(timeout_s=3.0)
        async def db_query(user_id: str) -> dict:
            return await db.fetchrow("SELECT * FROM users WHERE id=$1", user_id)

        @wm.protect(timeout_s=20.0, fallback=lambda **kw: [])
        async def web_search(query: str) -> list:
            return await live_search(query)
    """

    def __init__(self, default_timeout_s: float = 10.0):
        self._default = default_timeout_s
        self._registry: Dict[str, float] = {}

    def protect(self, timeout_s: Optional[float] = None,
                fallback: Optional[Callable] = None):
        def decorator(fn: Callable) -> Callable:
            deadline = timeout_s or self._default
            self._registry[fn.__name__] = deadline

            @functools.wraps(fn)
            async def wrapper(*args, **kwargs) -> Any:
                try:
                    return await asyncio.wait_for(
                        fn(*args, **kwargs), timeout=deadline
                    )
                except asyncio.TimeoutError:
                    logger.error(
                        "watchdog_middleware_timeout fn=%s timeout_s=%.1f",
                        fn.__name__, deadline,
                    )
                    if fallback:
                        return await asyncio.wait_for(
                            fallback(*args, **kwargs)
                            if asyncio.iscoroutinefunction(fallback)
                            else asyncio.coroutine(lambda: fallback(*args, **kwargs))(),
                            timeout=deadline / 2,
                        )
                    raise
            return wrapper
        return decorator

    def registered_timeouts(self) -> Dict[str, float]:
        return dict(self._registry)
```

---

## Comparison

| Approach | Timeout Source | Fallback | Health Tracking | Adaptive | Transparent |
|---|---|---|---|---|---|
| **WatchdogTimer** | Static | No | No | No | No |
| **PerToolTimeoutRegistry** | Per-tool config | No | No | No | No |
| **WatchdogWithFallback** | Static | Yes | No | No | No |
| **WatchdogHealthTracker** | Static | No | Yes | No | No |
| **AdaptiveWatchdog** | p99 × multiplier | No | Implicit | Yes | No |
| **WatchdogMiddleware** | Per-decorator | Optional | No | No | Yes |

**Key insight**: wrap every tool call in `asyncio.wait_for` with a per-tool deadline — not a global one. A `db_query` deserves a 3-second limit; a `pdf_parse` might need 60 seconds. Use `AdaptiveWatchdog` in production to automatically tighten deadlines as you observe real latency distributions, and pair with `WatchdogHealthTracker` to detect when a tool's timeout rate crosses 20% — which signals a dependency outage rather than random jitter.
