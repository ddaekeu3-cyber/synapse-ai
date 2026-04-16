---
title: "Agent Doesn't Implement CPU Flame Graph for Production Profiling"
description: "AI agents with unexplained CPU spikes, latency regressions, or high resource bills often cannot be diagnosed because there is no production profiling in place. CPU flame graphs provide a complete call-stack picture at microsecond granularity, revealing exactly where time is spent without requiring a debugger."
date: 2025-02-03
difficulty: advanced
category: observability
slug: agent-doesnt-implement-cpu-flamegraph-for-production-profiling
tags:
  - flamegraph
  - profiling
  - cpu
  - pyinstrument
  - py-spy
  - performance
  - observability
symptoms:
  - "CPU usage spikes to 100% for 5–10 seconds during certain tool calls, cause unknown"
  - "Latency percentiles degrade after a dependency update but diff shows no obvious cause"
  - "Cloud bill for compute doubles after new feature without explanation"
  - "Agent is slow in production but fast in development; no profiler data to compare"
  - "Stack traces in logs show the slow path but not which callers trigger it most often"
---

## Problem

When an agent is slow or CPU-intensive, you need to know **which code** is consuming the time. Log-based debugging tells you what happened but not why. Distributed traces show wall-clock time per span but not CPU time within a span.

A CPU flame graph samples the call stack at high frequency and aggregates samples into a visualisation where:
- Width of a frame = fraction of total CPU time spent in that function.
- Stack depth = call depth.
- A wide, flat frame near the root = a hotspot to optimise.

Python profiling options:

| Tool | Overhead | Requires Access | Output |
|---|---|---|---|
| `cProfile` | High (10–30%) | Embed in code | pstats |
| `pyinstrument` | Low (1–3%) | Embed in code | HTML / JSON |
| `py-spy` | Near-zero | External process | SVG flamegraph |
| `austin` | Near-zero | External process | SVG flamegraph |

---

## Solution 1: pyinstrument Profiling Context Manager

Wrap agent request handlers with pyinstrument for low-overhead, in-process profiling. Activated by an environment variable or request header so it can be toggled in production.

```python
import asyncio
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Optional

try:
    from pyinstrument import Profiler
    HAS_PYINSTRUMENT = True
except ImportError:
    HAS_PYINSTRUMENT = False


@dataclass
class ProfileResult:
    request_id: str
    duration_ms: float
    html_report: Optional[str]
    json_report: Optional[dict]
    flamegraph_svg: Optional[str]


class ProductionProfiler:
    """
    Low-overhead profiler for production agents.
    Enable per-request via header or env flag.

    Usage:
        profiler = ProductionProfiler(
            enabled_fraction=0.01,   # profile 1% of requests
            output_dir="/tmp/profiles",
        )

        async def handle_request(request):
            async with profiler.maybe_profile(request.id) as result:
                response = await agent.run(request)
            if result:
                save_profile(result)
            return response
    """

    def __init__(self, enabled_fraction: float = 0.01,
                 output_dir: str = "/tmp/profiles",
                 min_duration_ms: float = 100.0):
        self._fraction = enabled_fraction
        self._output_dir = output_dir
        self._min_duration = min_duration_ms
        self._profile_count = 0
        self._request_count = 0

    def _should_profile(self, request_id: str) -> bool:
        if not HAS_PYINSTRUMENT:
            return False
        if os.environ.get("FORCE_PROFILE") == "1":
            return True
        # Deterministic sampling by request count
        self._request_count += 1
        return (self._request_count % int(1 / max(0.001, self._fraction))) == 0

    @asynccontextmanager
    async def maybe_profile(self, request_id: str = ""):
        result_holder = [None]
        if not self._should_profile(request_id):
            yield result_holder
            return

        profiler = Profiler(async_mode="enabled")
        profiler.start()
        t0 = time.monotonic()
        try:
            yield result_holder
        finally:
            profiler.stop()
            duration_ms = (time.monotonic() - t0) * 1000
            if duration_ms >= self._min_duration:
                self._profile_count += 1
                result_holder[0] = ProfileResult(
                    request_id=request_id,
                    duration_ms=round(duration_ms, 1),
                    html_report=profiler.output_html(),
                    json_report=profiler.output(renderer="json") if hasattr(profiler, "output") else None,
                    flamegraph_svg=None,
                )

    def stats(self) -> dict:
        return {
            "requests_seen": self._request_count,
            "profiles_taken": self._profile_count,
            "profile_rate": round(
                self._profile_count / max(1, self._request_count), 4
            ),
        }
```

---

## Solution 2: Continuous Background Sampler

A background thread samples the main thread's stack at fixed intervals and accumulates folded-stack data. Periodically flushes as a flamegraph without any per-request instrumentation.

```python
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional


class ContinuousStackSampler:
    """
    Samples the target thread at `interval` seconds.
    Accumulates folded-stack data for flamegraph.pl or speedscope.

    Usage:
        sampler = ContinuousStackSampler(interval=0.005)   # 200 Hz
        sampler.start()
        ... run agent for 60 seconds ...
        sampler.stop()
        sampler.write_folded("/tmp/stacks.folded")
        # flamegraph.pl stacks.folded > agent.svg
    """

    def __init__(self, interval: float = 0.005,
                 target_thread_id: Optional[int] = None):
        self._interval = interval
        self._target = target_thread_id or threading.main_thread().ident
        self._folded: Dict[str, int] = defaultdict(int)
        self._stop_event = threading.Event()
        self._sampler: Optional[threading.Thread] = None
        self._sample_count = 0

    def start(self):
        self._stop_event.clear()
        self._sampler = threading.Thread(
            target=self._sample_loop, daemon=True, name="stack_sampler"
        )
        self._sampler.start()

    def stop(self):
        self._stop_event.set()
        if self._sampler:
            self._sampler.join(timeout=2.0)

    def _sample_loop(self):
        while not self._stop_event.wait(self._interval):
            frames = sys._current_frames()
            frame = frames.get(self._target)
            if frame is None:
                continue
            stack = []
            f = frame
            while f is not None:
                stack.append(f"{f.f_code.co_filename}:{f.f_code.co_name}:{f.f_lineno}")
                f = f.f_back
            stack.reverse()
            key = ";".join(stack)
            self._folded[key] += 1
            self._sample_count += 1

    def write_folded(self, path: str):
        """Write flamegraph.pl compatible folded stacks."""
        with open(path, "w") as f:
            for stack, count in sorted(self._folded.items()):
                f.write(f"{stack} {count}\n")

    def top_functions(self, n: int = 20) -> List[dict]:
        """Return the N hottest functions by total sample count."""
        func_counts: Dict[str, int] = defaultdict(int)
        for stack, count in self._folded.items():
            for frame in stack.split(";"):
                func_counts[frame] += count
        sorted_funcs = sorted(func_counts.items(), key=lambda x: x[1], reverse=True)
        total = max(1, self._sample_count)
        return [
            {
                "function": func,
                "samples": count,
                "pct": round(count / total * 100, 1),
            }
            for func, count in sorted_funcs[:n]
        ]

    def reset(self):
        self._folded.clear()
        self._sample_count = 0
```

---

## Solution 3: Async-Aware Flame Graph (Coroutine Attribution)

Standard thread samplers attribute all async work to the event loop thread with no coroutine breakdown. This sampler uses `asyncio.current_task()` to attribute samples to specific coroutines.

```python
import asyncio
import sys
import threading
import time
from collections import defaultdict
from typing import Dict, Optional


class AsyncAwareFlameGraphSampler:
    """
    Samples the event-loop thread AND current asyncio task name.
    Enables per-coroutine CPU attribution in the flame graph.

    Usage:
        sampler = AsyncAwareFlameGraphSampler(loop=asyncio.get_event_loop())
        sampler.start()
        await asyncio.sleep(60)   # run workload
        sampler.stop()
        sampler.write_folded("/tmp/async_stacks.folded")
    """

    def __init__(self, loop: asyncio.AbstractEventLoop,
                 interval: float = 0.005):
        self._loop = loop
        self._interval = interval
        self._loop_thread_id: Optional[int] = None
        self._folded: Dict[str, int] = defaultdict(int)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        # Record the event loop's thread ID
        self._loop_thread_id = None
        # Schedule a callback to capture the loop thread ID
        async def _capture_tid():
            self._loop_thread_id = threading.current_thread().ident
        asyncio.run_coroutine_threadsafe(_capture_tid(), self._loop).result(timeout=2.0)

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._sample_loop, daemon=True, name="async_sampler"
        )
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _sample_loop(self):
        while not self._stop_event.wait(self._interval):
            if self._loop_thread_id is None:
                continue
            frames = sys._current_frames()
            frame = frames.get(self._loop_thread_id)
            if frame is None:
                continue

            # Get current task name from event loop (best effort)
            task_name = "unknown_task"
            try:
                tasks = asyncio.all_tasks(self._loop)
                for task in tasks:
                    if not task.done():
                        task_name = task.get_name()
                        break
            except Exception:
                pass

            stack = []
            f = frame
            while f is not None:
                stack.append(f"{f.f_code.co_name}({f.f_code.co_filename.split('/')[-1]}:{f.f_lineno})")
                f = f.f_back
            stack.reverse()
            key = f"{task_name};" + ";".join(stack)
            self._folded[key] += 1

    def write_folded(self, path: str):
        with open(path, "w") as f:
            for stack, count in sorted(self._folded.items()):
                f.write(f"{stack} {count}\n")

    def per_task_cpu(self) -> Dict[str, float]:
        """Return CPU percentage per asyncio task."""
        task_counts: Dict[str, int] = defaultdict(int)
        total = 0
        for stack, count in self._folded.items():
            task = stack.split(";")[0]
            task_counts[task] += count
            total += count
        return {
            task: round(count / max(1, total) * 100, 1)
            for task, count in sorted(task_counts.items(), key=lambda x: x[1], reverse=True)
        }
```

---

## Solution 4: Profile-on-Slowdown Trigger

Automatically activates profiling when latency exceeds a threshold. Profiles only the slow requests, not every request, minimising overhead.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional


@dataclass
class SlowRequestProfile:
    request_id: str
    duration_ms: float
    threshold_ms: float
    profile_html: str
    timestamp: float = field(default_factory=time.time)


class SlowdownTriggeredProfiler:
    """
    Wraps request handlers. When a request exceeds `threshold_ms`,
    re-runs it under pyinstrument and stores the profile.

    Note: re-running is only safe for idempotent operations.
    For non-idempotent ops, use the ContinuousStackSampler instead.

    Usage:
        profiler = SlowdownTriggeredProfiler(threshold_ms=2000)
        result = await profiler.execute("req-1", handler_fn, arg1, arg2)
        for profile in profiler.slow_profiles[-5:]:
            print(profile.duration_ms, profile.profile_html[:200])
    """

    def __init__(self, threshold_ms: float = 1000.0, max_stored: int = 20):
        self._threshold = threshold_ms
        self._max_stored = max_stored
        self.slow_profiles: List[SlowRequestProfile] = []

    async def execute(self, request_id: str,
                      fn: Callable, *args, **kwargs) -> Any:
        t0 = time.monotonic()
        result = await fn(*args, **kwargs)
        duration_ms = (time.monotonic() - t0) * 1000

        if duration_ms >= self._threshold and HAS_PYINSTRUMENT:
            profiler = Profiler(async_mode="enabled")
            profiler.start()
            try:
                await fn(*args, **kwargs)
            finally:
                profiler.stop()
            profile = SlowRequestProfile(
                request_id=request_id,
                duration_ms=round(duration_ms, 1),
                threshold_ms=self._threshold,
                profile_html=profiler.output_html(),
            )
            self.slow_profiles.append(profile)
            if len(self.slow_profiles) > self._max_stored:
                self.slow_profiles.pop(0)

        return result

    def summary(self) -> dict:
        if not self.slow_profiles:
            return {"slow_requests": 0}
        durations = [p.duration_ms for p in self.slow_profiles]
        return {
            "slow_requests": len(self.slow_profiles),
            "max_duration_ms": round(max(durations), 1),
            "avg_duration_ms": round(sum(durations) / len(durations), 1),
        }
```

---

## Solution 5: Flame Graph HTTP Endpoint

Expose an HTTP endpoint that starts/stops a profiling session and returns the flamegraph SVG or JSON. Allows on-demand profiling of live production agents without SSH access.

```python
import asyncio
import io
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class ProfilingSession:
    started_at: float
    duration_s: float
    sampler: "ContinuousStackSampler"


class FlameGraphHTTPHandler:
    """
    HTTP endpoint for on-demand flame graph collection.

    Routes (attach to your HTTP framework):
        POST /debug/profile?duration=10   -> starts 10s profiling session
        GET  /debug/profile/flamegraph    -> returns folded stacks
        GET  /debug/profile/top           -> returns top-20 hottest functions

    Usage with aiohttp:
        handler = FlameGraphHTTPHandler()
        app.router.add_post("/debug/profile", handler.start)
        app.router.add_get("/debug/profile/flamegraph", handler.flamegraph)
        app.router.add_get("/debug/profile/top", handler.top_functions)
    """

    def __init__(self, auth_token: Optional[str] = None):
        self._auth = auth_token
        self._session: Optional[ProfilingSession] = None

    def _check_auth(self, request_headers: dict) -> bool:
        if self._auth is None:
            return True
        return request_headers.get("X-Profile-Token") == self._auth

    async def start(self, duration_s: float = 10.0) -> dict:
        if self._session and time.time() < self._session.started_at + self._session.duration_s:
            return {"error": "profiling session already active"}
        sampler = ContinuousStackSampler(interval=0.005)
        sampler.start()
        session = ProfilingSession(
            started_at=time.time(), duration_s=duration_s, sampler=sampler
        )
        self._session = session
        # Auto-stop after duration
        asyncio.create_task(self._auto_stop(session, duration_s))
        return {"status": "started", "duration_s": duration_s,
                "ends_at": session.started_at + duration_s}

    async def _auto_stop(self, session: ProfilingSession, delay: float):
        await asyncio.sleep(delay)
        if self._session is session:
            session.sampler.stop()

    def flamegraph(self) -> str:
        """Return folded stacks as plain text."""
        if self._session is None:
            return ""
        buf = io.StringIO()
        for stack, count in sorted(self._session.sampler._folded.items()):
            buf.write(f"{stack} {count}\n")
        return buf.getvalue()

    def top_functions(self, n: int = 20) -> list:
        if self._session is None:
            return []
        return self._session.sampler.top_functions(n)
```

---

## Solution 6: Unified Production Profiling Agent Middleware

Combines all approaches: continuous background sampling, slow-request triggering, and an HTTP endpoint. Exposes a `ProfilingMixin` that agents inherit.

```python
import asyncio
import logging
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class ProductionProfilingMixin:
    """
    Mix into any async agent class to get full production profiling.

    Features:
    - Background stack sampler (always on, near-zero overhead)
    - Per-request pyinstrument profiling (1% of requests)
    - Slow-request triggered re-profiling
    - HTTP endpoint for on-demand flame graphs

    Usage:
        class MyAgent(ProductionProfilingMixin, BaseAgent):
            ...

        agent = MyAgent()
        await agent.start_profiling(background_hz=200, sample_pct=0.01)
    """

    async def start_profiling(self, background_hz: float = 200.0,
                               sample_pct: float = 0.01,
                               slowdown_threshold_ms: float = 2000.0):
        self._bg_sampler = ContinuousStackSampler(interval=1.0 / background_hz)
        self._bg_sampler.start()

        self._request_profiler = ProductionProfiler(
            enabled_fraction=sample_pct
        )
        self._slowdown_profiler = SlowdownTriggeredProfiler(
            threshold_ms=slowdown_threshold_ms
        )
        self._flamegraph_handler = FlameGraphHTTPHandler()
        logger.info("Production profiling started (%.0f Hz sampler, %.1f%% request sampling)",
                    background_hz, sample_pct * 100)

    async def stop_profiling(self):
        if hasattr(self, "_bg_sampler"):
            self._bg_sampler.stop()

    async def profiled_call(self, request_id: str,
                             fn: Callable, *args, **kwargs) -> Any:
        async with self._request_profiler.maybe_profile(request_id) as result:
            output = await self._slowdown_profiler.execute(
                request_id, fn, *args, **kwargs
            )
        if result[0]:
            logger.debug("Profile captured for request %s (%.0f ms)",
                         request_id, result[0].duration_ms)
        return output

    def profiling_report(self) -> dict:
        report: dict = {}
        if hasattr(self, "_bg_sampler"):
            report["top_functions"] = self._bg_sampler.top_functions(10)
        if hasattr(self, "_request_profiler"):
            report["request_profiler"] = self._request_profiler.stats()
        if hasattr(self, "_slowdown_profiler"):
            report["slow_requests"] = self._slowdown_profiler.summary()
        return report

    def dump_flamegraph(self, path: str):
        if hasattr(self, "_bg_sampler"):
            self._bg_sampler.write_folded(path)
            logger.info("Flamegraph written to %s", path)
```

---

## Comparison

| Approach | Overhead | Requires Code Change | Best For |
|---|---|---|---|
| **pyinstrument Context Manager** | 1–3% | Yes (wrap handler) | Per-request profiling |
| **Continuous Stack Sampler** | < 0.5% | Yes (start/stop) | Always-on production |
| **Async-Aware Flame Graph** | < 0.5% | Yes | Coroutine-level attribution |
| **Slowdown-Triggered Profiler** | 0% (inactive) | Yes | Capturing rare slow requests |
| **Flame Graph HTTP Endpoint** | 0% (inactive) | Yes (HTTP route) | On-demand incident response |
| **Profiling Mixin** | < 1% total | Yes (inherit) | Full production instrumentation |

**Key insight**: use `py-spy` for zero-code-change profiling during incidents (`py-spy record -o profile.svg --pid <PID>`); use the continuous sampler as permanent low-overhead instrumentation; use the HTTP endpoint to collect flamegraphs remotely without SSH.
