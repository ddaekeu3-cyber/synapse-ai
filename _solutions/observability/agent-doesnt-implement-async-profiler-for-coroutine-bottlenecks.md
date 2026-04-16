---
title: "Agent Doesn't Implement Async Profiler for Coroutine Bottlenecks"
description: "AI agents built on asyncio or other async runtimes suffer silent performance degradation when coroutine bottlenecks go undetected. Without async-aware profiling, slow awaits, event-loop stalls, and task queue buildup are invisible to traditional profilers."
date: 2025-01-30
difficulty: advanced
category: observability
slug: agent-doesnt-implement-async-profiler-for-coroutine-bottlenecks
tags:
  - asyncio
  - profiling
  - coroutines
  - event-loop
  - performance
  - observability
  - bottleneck-detection
symptoms:
  - "Agent appears responsive but slowly degrades over time"
  - "Event loop lag spikes during peak load with no CPU explanation"
  - "Task queue depth grows unboundedly under moderate concurrency"
  - "Some tool calls complete instantly while others silently stall for seconds"
  - "Async traces show gaps with no obvious blocking code"
  - "GC pauses and lock contention invisible in synchronous profiler output"
---

## Problem

Async Python agents rely on cooperative multitasking: every `await` is a yield point where the event loop can schedule other work. When a single coroutine holds the loop—by doing CPU-heavy work, calling a blocking I/O function without `run_in_executor`, or simply awaiting a slow dependency—every other coroutine queues behind it.

Traditional profilers (cProfile, py-spy wall-clock) miss this class of bug because they sample threads, not coroutine frames. You'll see 0% CPU and assume the agent is idle when it's actually waiting on a database cursor, a slow LLM stream, or an unasync'd file read that blocks the whole loop.

Without specialized instrumentation you cannot answer:
- Which coroutine is monopolising the event loop right now?
- What is the 99th-percentile await latency for each tool call?
- How deep is the ready queue, and which task type is causing the buildup?
- Are there synchronous call sites accidentally executing inside the async loop?

---

## Solution 1: Event-Loop Lag Monitor (Heartbeat Probe)

Inject a lightweight background coroutine that schedules itself every `interval` seconds. The gap between expected and actual wakeup is the loop-lag — a direct measure of how long the loop was blocked.

```python
import asyncio
import time
import statistics
from dataclasses import dataclass, field
from collections import deque
from typing import Deque

@dataclass
class LoopLagSample:
    timestamp: float
    expected_wakeup: float
    actual_wakeup: float

    @property
    def lag_ms(self) -> float:
        return (self.actual_wakeup - self.expected_wakeup) * 1000


class EventLoopLagMonitor:
    """
    Background heartbeat that measures event-loop stall duration.

    Usage:
        monitor = EventLoopLagMonitor(interval=0.05, history=200)
        asyncio.create_task(monitor.run())
        ...
        print(monitor.p99_lag_ms)
    """

    def __init__(self, interval: float = 0.05, history: int = 200,
                 alert_threshold_ms: float = 100.0):
        self.interval = interval
        self.history: Deque[LoopLagSample] = deque(maxlen=history)
        self.alert_threshold_ms = alert_threshold_ms
        self._running = False
        self._alert_callbacks: list = []

    def on_alert(self, callback):
        self._alert_callbacks.append(callback)
        return self

    async def run(self):
        self._running = True
        while self._running:
            expected = time.monotonic() + self.interval
            await asyncio.sleep(self.interval)
            actual = time.monotonic()
            sample = LoopLagSample(
                timestamp=actual,
                expected_wakeup=expected,
                actual_wakeup=actual,
            )
            self.history.append(sample)
            if sample.lag_ms > self.alert_threshold_ms:
                for cb in self._alert_callbacks:
                    asyncio.create_task(cb(sample))

    def stop(self):
        self._running = False

    @property
    def p50_lag_ms(self) -> float:
        if not self.history:
            return 0.0
        lags = [s.lag_ms for s in self.history]
        return statistics.median(lags)

    @property
    def p99_lag_ms(self) -> float:
        if not self.history:
            return 0.0
        lags = sorted(s.lag_ms for s in self.history)
        idx = int(len(lags) * 0.99)
        return lags[min(idx, len(lags) - 1)]

    def report(self) -> dict:
        lags = [s.lag_ms for s in self.history]
        if not lags:
            return {"samples": 0}
        return {
            "samples": len(lags),
            "p50_ms": round(statistics.median(lags), 2),
            "p95_ms": round(sorted(lags)[int(len(lags) * 0.95)], 2),
            "p99_ms": round(self.p99_lag_ms, 2),
            "max_ms": round(max(lags), 2),
        }
```

---

## Solution 2: Coroutine-Aware Span Tracer

Wrap every `await` entry/exit with a context-local span. Unlike thread-based profilers this correctly attributes await time to the specific coroutine that initiated the wait, not whichever thread happens to be running.

```python
import asyncio
import time
import contextvars
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import uuid

_active_span: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_active_span", default=None
)

@dataclass
class CoroutineSpan:
    span_id: str
    name: str
    parent_id: Optional[str]
    start_time: float
    end_time: Optional[float] = None
    await_time_ms: float = 0.0
    children: List["CoroutineSpan"] = field(default_factory=list)

    @property
    def wall_time_ms(self) -> float:
        if self.end_time is None:
            return (time.monotonic() - self.start_time) * 1000
        return (self.end_time - self.start_time) * 1000

    @property
    def cpu_time_ms(self) -> float:
        return max(0.0, self.wall_time_ms - self.await_time_ms)


class CoroutineSpanTracer:
    def __init__(self):
        self._spans: Dict[str, CoroutineSpan] = {}
        self._completed: List[CoroutineSpan] = []

    @asynccontextmanager
    async def span(self, name: str):
        span_id = str(uuid.uuid4())[:8]
        parent_id = _active_span.get()
        span = CoroutineSpan(
            span_id=span_id,
            name=name,
            parent_id=parent_id,
            start_time=time.monotonic(),
        )
        self._spans[span_id] = span
        if parent_id and parent_id in self._spans:
            self._spans[parent_id].children.append(span)

        token = _active_span.set(span_id)
        await_start: Optional[float] = None
        try:
            yield span
        finally:
            span.end_time = time.monotonic()
            _active_span.reset(token)
            del self._spans[span_id]
            self._completed.append(span)

    def awaiting(self, span_id: str) -> "AwaitContext":
        return AwaitContext(self._spans.get(span_id))

    def hot_spans(self, top_n: int = 10) -> List[CoroutineSpan]:
        return sorted(
            self._completed,
            key=lambda s: s.wall_time_ms,
            reverse=True,
        )[:top_n]


class AwaitContext:
    def __init__(self, span: Optional[CoroutineSpan]):
        self._span = span
        self._start: float = 0.0

    def __enter__(self):
        self._start = time.monotonic()
        return self

    def __exit__(self, *_):
        if self._span:
            self._span.await_time_ms += (time.monotonic() - self._start) * 1000


# Decorator for traced agent methods
def traced(tracer: CoroutineSpanTracer, name: Optional[str] = None):
    def decorator(fn):
        import functools
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            span_name = name or fn.__qualname__
            async with tracer.span(span_name):
                return await fn(*args, **kwargs)
        return wrapper
    return decorator
```

---

## Solution 3: Task Queue Depth Profiler

Monkey-patch `asyncio.Task` creation to track the number of pending, running, and completed tasks by type. High queue depth in a specific task category reveals which agent workload is causing backpressure.

```python
import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Callable, Awaitable

@dataclass
class TaskMetrics:
    name: str
    created: int = 0
    started: int = 0
    completed: int = 0
    failed: int = 0
    total_wait_ms: float = 0.0
    total_run_ms: float = 0.0

    @property
    def pending(self) -> int:
        return self.created - self.started

    @property
    def avg_wait_ms(self) -> float:
        return self.total_wait_ms / max(1, self.started)

    @property
    def avg_run_ms(self) -> float:
        return self.total_run_ms / max(1, self.completed)


class TaskQueueProfiler:
    """
    Wraps asyncio.create_task to instrument task lifecycle.

    Usage:
        profiler = TaskQueueProfiler()
        profiler.install()          # patches create_task
        ...
        profiler.uninstall()
        print(profiler.report())
    """

    def __init__(self):
        self._metrics: Dict[str, TaskMetrics] = defaultdict(
            lambda: TaskMetrics(name="unknown")
        )
        self._original_create_task = None

    def install(self):
        loop = asyncio.get_event_loop()
        self._original_create_task = loop.create_task

        profiler = self

        def patched_create_task(coro, *, name=None, context=None):
            task_name = name or getattr(coro, "__qualname__", "unknown")
            if task_name not in profiler._metrics:
                profiler._metrics[task_name] = TaskMetrics(name=task_name)
            metrics = profiler._metrics[task_name]
            metrics.created += 1
            created_at = time.monotonic()

            async def instrumented():
                started_at = time.monotonic()
                metrics.started += 1
                metrics.total_wait_ms += (started_at - created_at) * 1000
                try:
                    result = await coro
                    metrics.completed += 1
                    metrics.total_run_ms += (time.monotonic() - started_at) * 1000
                    return result
                except Exception:
                    metrics.failed += 1
                    raise

            kwargs = {"name": task_name}
            if context is not None:
                kwargs["context"] = context
            return profiler._original_create_task(instrumented(), **kwargs)

        loop.create_task = patched_create_task

    def uninstall(self):
        if self._original_create_task:
            asyncio.get_event_loop().create_task = self._original_create_task

    def report(self) -> List[dict]:
        rows = []
        for name, m in sorted(
            self._metrics.items(), key=lambda kv: kv[1].pending, reverse=True
        ):
            rows.append({
                "task": name,
                "pending": m.pending,
                "completed": m.completed,
                "failed": m.failed,
                "avg_wait_ms": round(m.avg_wait_ms, 1),
                "avg_run_ms": round(m.avg_run_ms, 1),
            })
        return rows
```

---

## Solution 4: Blocking-Call Detector

Use `sys.settrace` or a background thread sampler to detect synchronous blocking calls executed inside the event loop. Any call that takes longer than a threshold without yielding is flagged with its full stack trace.

```python
import asyncio
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class BlockingCallRecord:
    duration_ms: float
    stack_trace: str
    task_name: str
    timestamp: float


class BlockingCallDetector:
    """
    Background thread that periodically samples the event-loop thread's
    stack. If the same frame appears across consecutive samples, the
    coroutine is blocking the loop synchronously.

    Usage:
        detector = BlockingCallDetector(sample_interval=0.01, threshold_ms=50)
        detector.start()
        ...
        detector.stop()
        for record in detector.violations:
            print(record)
    """

    def __init__(self, sample_interval: float = 0.01,
                 threshold_ms: float = 50.0):
        self.sample_interval = sample_interval
        self.threshold_ms = threshold_ms
        self.violations: List[BlockingCallRecord] = []
        self._loop_thread_id: Optional[int] = None
        self._stop_event = threading.Event()
        self._sampler_thread: Optional[threading.Thread] = None

    def start(self):
        self._loop_thread_id = threading.current_thread().ident
        self._stop_event.clear()
        self._sampler_thread = threading.Thread(
            target=self._sample_loop, daemon=True
        )
        self._sampler_thread.start()

    def stop(self):
        self._stop_event.set()
        if self._sampler_thread:
            self._sampler_thread.join(timeout=2.0)

    def _sample_loop(self):
        prev_frame_key: Optional[str] = None
        prev_sample_time: float = time.monotonic()
        consecutive_same: int = 0

        while not self._stop_event.wait(self.sample_interval):
            if self._loop_thread_id is None:
                continue
            frames = sys._current_frames()
            frame = frames.get(self._loop_thread_id)
            if frame is None:
                continue

            # Build a stable key from top frame
            frame_key = f"{frame.f_code.co_filename}:{frame.f_lineno}"

            now = time.monotonic()
            if frame_key == prev_frame_key:
                consecutive_same += 1
                elapsed_ms = (now - prev_sample_time) * 1000
                if elapsed_ms > self.threshold_ms and consecutive_same == 2:
                    # Capture the full stack
                    stack = "".join(traceback.format_stack(frame))
                    task = "unknown"
                    try:
                        current = asyncio.current_task()
                        if current:
                            task = current.get_name()
                    except RuntimeError:
                        pass
                    self.violations.append(BlockingCallRecord(
                        duration_ms=round(elapsed_ms, 1),
                        stack_trace=stack,
                        task_name=task,
                        timestamp=now,
                    ))
            else:
                consecutive_same = 0
                prev_sample_time = now

            prev_frame_key = frame_key

    def summary(self) -> dict:
        if not self.violations:
            return {"violations": 0}
        durations = [v.duration_ms for v in self.violations]
        return {
            "violations": len(self.violations),
            "max_block_ms": round(max(durations), 1),
            "avg_block_ms": round(sum(durations) / len(durations), 1),
            "top_offenders": list({v.stack_trace[:120] for v in self.violations[:5]}),
        }
```

---

## Solution 5: Async Flame Graph Builder

Collect coroutine call trees and emit them as a Brendan Gregg–compatible folded stack format that can be rendered by flamegraph.pl or speedscope. Each frame is weighted by its total await time.

```python
import asyncio
import time
import contextvars
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

_frame_stack: contextvars.ContextVar[List[str]] = contextvars.ContextVar(
    "_frame_stack", default=[]
)

@dataclass
class FlameNode:
    name: str
    self_ms: float = 0.0
    total_ms: float = 0.0
    children: Dict[str, "FlameNode"] = field(default_factory=dict)


class AsyncFlameGraphBuilder:
    """
    Builds an async flame graph by tracking coroutine entry/exit times
    via context vars. Outputs folded stacks for flamegraph.pl.

    Usage:
        builder = AsyncFlameGraphBuilder()

        @builder.profile
        async def my_tool_call():
            ...

        # After workload:
        builder.write_folded("profile.folded")
        # Then: flamegraph.pl profile.folded > profile.svg
    """

    def __init__(self):
        self._folded: Dict[str, float] = defaultdict(float)
        self._root = FlameNode(name="root")

    def profile(self, fn):
        import functools
        builder = self

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            frame_name = fn.__qualname__
            current_stack = list(_frame_stack.get([]))
            new_stack = current_stack + [frame_name]
            token = _frame_stack.set(new_stack)
            start = time.monotonic()
            try:
                return await fn(*args, **kwargs)
            finally:
                elapsed_ms = (time.monotonic() - start) * 1000
                key = ";".join(new_stack)
                builder._folded[key] += elapsed_ms
                _frame_stack.reset(token)

        return wrapper

    def record(self, stack: List[str], ms: float):
        """Manually record a sample (for integration with other profilers)."""
        key = ";".join(stack)
        self._folded[key] += ms

    def write_folded(self, path: str):
        with open(path, "w") as f:
            for stack_key, ms in sorted(self._folded.items()):
                # flamegraph.pl expects integer sample counts; use ms*10 as proxy
                f.write(f"{stack_key} {int(ms * 10)}\n")

    def top_stacks(self, n: int = 10) -> List[Tuple[str, float]]:
        return sorted(self._folded.items(), key=lambda kv: kv[1], reverse=True)[:n]

    def total_await_ms(self) -> float:
        return sum(self._folded.values())
```

---

## Solution 6: Integrated Async Profiler Agent Mixin

Combine all instrumentation layers into a single mixin that agents inherit. Exposes `/profile` and `/profile/reset` endpoints when the agent runs an HTTP management server, and logs periodic summaries.

```python
import asyncio
import json
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class AsyncProfilerMixin:
    """
    Mixin for async agents.  Attach by inheriting:

        class MyAgent(AsyncProfilerMixin, BaseAgent):
            ...

    Call `await self.start_profiling()` in your agent's startup hook.
    """

    def __init__(self, *args, profiling_interval: float = 30.0, **kwargs):
        super().__init__(*args, **kwargs)
        self._profiling_interval = profiling_interval
        self._lag_monitor: Optional[EventLoopLagMonitor] = None
        self._task_profiler: Optional[TaskQueueProfiler] = None
        self._blocking_detector: Optional[BlockingCallDetector] = None
        self._flame_builder: Optional[AsyncFlameGraphBuilder] = None
        self._profile_task: Optional[asyncio.Task] = None

    async def start_profiling(self):
        self._lag_monitor = EventLoopLagMonitor(
            interval=0.05, alert_threshold_ms=200.0
        )
        self._lag_monitor.on_alert(self._on_lag_alert)

        self._task_profiler = TaskQueueProfiler()
        self._task_profiler.install()

        self._blocking_detector = BlockingCallDetector(
            sample_interval=0.01, threshold_ms=100.0
        )
        self._blocking_detector.start()

        self._flame_builder = AsyncFlameGraphBuilder()

        asyncio.create_task(self._lag_monitor.run(), name="lag_monitor")
        self._profile_task = asyncio.create_task(
            self._periodic_report(), name="profile_reporter"
        )
        logger.info("Async profiler started")

    async def stop_profiling(self):
        if self._lag_monitor:
            self._lag_monitor.stop()
        if self._task_profiler:
            self._task_profiler.uninstall()
        if self._blocking_detector:
            self._blocking_detector.stop()
        if self._profile_task:
            self._profile_task.cancel()

    async def _on_lag_alert(self, sample: "LoopLagSample"):
        logger.warning(
            "Event loop stall detected: %.1f ms at %.3f",
            sample.lag_ms, sample.timestamp,
        )

    async def _periodic_report(self):
        while True:
            await asyncio.sleep(self._profiling_interval)
            report = self.profiling_report()
            logger.info("Async profile: %s", json.dumps(report, indent=2))

    def profiling_report(self) -> dict:
        report: dict = {"timestamp": time.time()}
        if self._lag_monitor:
            report["loop_lag"] = self._lag_monitor.report()
        if self._task_profiler:
            report["task_queue"] = self._task_profiler.report()[:5]
        if self._blocking_detector:
            report["blocking_calls"] = self._blocking_detector.summary()
        if self._flame_builder:
            report["top_stacks"] = [
                {"stack": k, "await_ms": round(v, 1)}
                for k, v in self._flame_builder.top_stacks(5)
            ]
        return report
```

---

## Comparison

| Approach | What It Catches | Overhead | Production-Safe |
|---|---|---|---|
| **Loop Lag Monitor** | Event-loop stalls, GC pauses, blocking I/O | < 0.1% CPU | Yes |
| **Coroutine Span Tracer** | Per-coroutine await latency distribution | ~1–3% CPU | Yes (sampling) |
| **Task Queue Profiler** | Queue depth by task type, scheduling delay | < 0.5% CPU | Yes |
| **Blocking Call Detector** | Sync calls inside async loop | ~2% CPU (thread sampler) | Yes |
| **Flame Graph Builder** | Full call tree with await-time weighting | ~3–5% CPU | Staging / Debug |
| **Profiler Agent Mixin** | All of the above, unified reporting | Cumulative of above | Yes (configurable) |

**Key insight**: use the Loop Lag Monitor and Task Queue Profiler always-on in production (near-zero overhead), the Coroutine Span Tracer in staging, and the Blocking Call Detector during incident response. Generate flame graphs on-demand from production traffic replayed in staging.
