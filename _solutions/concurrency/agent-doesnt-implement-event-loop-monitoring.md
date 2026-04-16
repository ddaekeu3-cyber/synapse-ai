---
layout: solution
title: "Agent Doesn't Implement Event Loop Monitoring"
category: concurrency
description: "Async agents with a blocked event loop fail silently — requests queue up, latency spikes, and nothing in the logs explains why. Event loop monitoring detects stalls, measures lag, and alerts before users notice the degradation."
tags: [concurrency, event-loop, monitoring, asyncio, latency, blocking, sqlite]
---

# Agent Doesn't Implement Event Loop Monitoring

## Problem

A single blocking call in an async agent stalls the entire event loop. While `time.sleep(5)` or a synchronous database query runs, every other coroutine waits. Latency climbs, requests time out, and the error logs show nothing because the agent is technically "running."

Event loop monitoring measures the gap between when a callback was scheduled and when it actually ran. A large gap means something is blocking the loop.

---

## Option 1: Simple Event Loop Lag Detector

```python
import asyncio
import time
import anthropic

STALL_THRESHOLD_MS = 50  # Flag if loop is blocked for >50ms

async def measure_loop_lag() -> float:
    """
    Schedule a callback and measure how long it takes to actually run.
    Under no load, this should be <1ms. A blocked loop shows 50-500ms+.
    """
    scheduled_at = time.monotonic()
    await asyncio.sleep(0)  # Yield control once
    ran_at = time.monotonic()
    return (ran_at - scheduled_at) * 1000  # ms


async def monitor_loop(interval_sec: float = 1.0, stop_event: asyncio.Event | None = None):
    """Background task that samples loop lag at regular intervals."""
    samples = []
    iterations = 0
    while stop_event is None or not stop_event.is_set():
        lag_ms = await measure_loop_lag()
        samples.append(lag_ms)
        iterations += 1

        if lag_ms > STALL_THRESHOLD_MS:
            print(f"[LOOP MONITOR] ⚠️  Loop stall detected: {lag_ms:.1f}ms lag")
        elif iterations % 5 == 0:
            avg = sum(samples[-10:]) / min(len(samples), 10)
            print(f"[LOOP MONITOR] avg_lag={avg:.1f}ms (last 10 samples)")

        await asyncio.sleep(interval_sec)


async def run_agent_with_monitoring(prompts: list[str]):
    client = anthropic.AsyncAnthropic()
    stop = asyncio.Event()

    # Start monitor in background
    monitor_task = asyncio.create_task(monitor_loop(interval_sec=0.5, stop_event=stop))

    for prompt in prompts:
        t0 = time.time()
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": prompt}],
        )
        elapsed_ms = (time.time() - t0) * 1000
        print(f"[Agent] {prompt[:35]} → {elapsed_ms:.0f}ms")

    stop.set()
    await monitor_task
    print("[Agent] Done")


if __name__ == "__main__":
    asyncio.run(run_agent_with_monitoring([
        "What is Python?",
        "Name a sorting algorithm.",
        "What is REST?",
    ]))
# Expected Token Savings: 0% direct — monitoring overhead is <0.1ms per sample; detects blocking that causes retries
# Environment: pip install anthropic; asyncio, time are stdlib
```

---

## Option 2: Loop Lag Histogram with Percentiles

```python
import asyncio
import time
import statistics
import anthropic
from collections import deque
from dataclasses import dataclass

@dataclass
class LagStats:
    count: int
    mean_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    stalls: int  # samples exceeding threshold


class LoopLagMonitor:
    STALL_MS = 100
    WINDOW = 100  # rolling window size

    def __init__(self):
        self._samples: deque[float] = deque(maxlen=self.WINDOW)
        self._all_samples: list[float] = []
        self._stalls = 0
        self._running = False
        self._task: asyncio.Task | None = None

    async def _sample_loop(self, interval: float):
        while self._running:
            t0 = time.monotonic()
            await asyncio.sleep(0)
            lag = (time.monotonic() - t0) * 1000
            self._samples.append(lag)
            self._all_samples.append(lag)
            if lag > self.STALL_MS:
                self._stalls += 1
                print(f"[LoopMonitor] STALL {lag:.1f}ms")
            await asyncio.sleep(interval)

    def start(self, interval: float = 0.2):
        self._running = True
        self._task = asyncio.create_task(self._sample_loop(interval))

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def stats(self) -> LagStats:
        s = sorted(self._all_samples) if self._all_samples else [0.0]
        n = len(s)
        return LagStats(
            count=n,
            mean_ms=round(statistics.mean(s), 2),
            p50_ms=round(s[int(n * 0.50)], 2),
            p95_ms=round(s[min(int(n * 0.95), n - 1)], 2),
            p99_ms=round(s[min(int(n * 0.99), n - 1)], 2),
            max_ms=round(max(s), 2),
            stalls=self._stalls,
        )

    def current_window_avg(self) -> float:
        return statistics.mean(self._samples) if self._samples else 0.0


async def simulate_blocking_work():
    """Simulates a blocking call that stalls the event loop."""
    print("[Simulation] Introducing blocking sleep...")
    time.sleep(0.15)  # Blocks the event loop for 150ms


async def run_monitored_agent_with_histogram():
    client = anthropic.AsyncAnthropic()
    monitor = LoopLagMonitor()
    monitor.start(interval=0.1)

    prompts = [
        "What is Python?",
        "Explain async/await.",
        "What is a REST API?",
    ]

    print("Phase 1: Normal operation")
    for p in prompts:
        r = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": p}],
        )
        print(f"  → {r.content[0].text[:50]}")

    print("\nPhase 2: With simulated blocking")
    await simulate_blocking_work()

    for p in prompts[:2]:
        r = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": p}],
        )
        print(f"  → {r.content[0].text[:50]}")

    await monitor.stop()
    stats = monitor.stats()
    print(f"\nLoop Lag Histogram:")
    print(f"  Samples:  {stats.count}")
    print(f"  Mean:     {stats.mean_ms}ms")
    print(f"  p50:      {stats.p50_ms}ms")
    print(f"  p95:      {stats.p95_ms}ms")
    print(f"  p99:      {stats.p99_ms}ms")
    print(f"  Max:      {stats.max_ms}ms")
    print(f"  Stalls:   {stats.stalls}")


if __name__ == "__main__":
    asyncio.run(run_monitored_agent_with_histogram())
# Expected Token Savings: 0% — histogram identifies blocking hotspots that inflate latency
# Environment: pip install anthropic; asyncio, time, statistics are stdlib
```

---

## Option 3: Per-Task Execution Time Tracker

```python
import asyncio
import time
import anthropic
from dataclasses import dataclass, field
from typing import Any

@dataclass
class TaskTrace:
    name: str
    created_at: float
    started_at: float | None = None
    completed_at: float | None = None
    result: Any = None
    error: str | None = None

    @property
    def queue_wait_ms(self) -> float | None:
        if self.started_at:
            return (self.started_at - self.created_at) * 1000
        return None

    @property
    def execution_ms(self) -> float | None:
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at) * 1000
        return None


class TaskTracer:
    """Wraps coroutines to measure queue wait time and execution time."""

    def __init__(self, stall_threshold_ms: float = 50):
        self.stall_threshold = stall_threshold_ms
        self.traces: list[TaskTrace] = []

    async def trace(self, name: str, coro) -> Any:
        trace = TaskTrace(name=name, created_at=time.monotonic())
        self.traces.append(trace)

        # Measure queue wait (time from task creation to first execution)
        await asyncio.sleep(0)
        trace.started_at = time.monotonic()

        queue_wait = trace.queue_wait_ms or 0
        if queue_wait > self.stall_threshold:
            print(f"[Tracer] ⚠️  '{name}' waited {queue_wait:.1f}ms in queue (stall?)")

        try:
            result = await coro
            trace.completed_at = time.monotonic()
            trace.result = result
            return result
        except Exception as e:
            trace.completed_at = time.monotonic()
            trace.error = str(e)
            raise

    def report(self):
        if not self.traces:
            print("No traces recorded.")
            return

        completed = [t for t in self.traces if t.execution_ms is not None]
        if not completed:
            return

        print(f"\nTask Execution Report ({len(completed)} tasks):")
        print(f"{'Task':<40} {'Queue':<12} {'Exec':<12} {'Status'}")
        print("-" * 75)
        for t in sorted(completed, key=lambda x: x.created_at):
            q = f"{t.queue_wait_ms:.1f}ms" if t.queue_wait_ms else "?"
            e = f"{t.execution_ms:.1f}ms" if t.execution_ms else "?"
            status = "ERROR" if t.error else "ok"
            print(f"{t.name:<40} {q:<12} {e:<12} {status}")

        avg_queue = sum(t.queue_wait_ms or 0 for t in completed) / len(completed)
        avg_exec = sum(t.execution_ms or 0 for t in completed) / len(completed)
        print(f"\nAverages: queue_wait={avg_queue:.1f}ms, execution={avg_exec:.1f}ms")


async def run_traced_agent():
    tracer = TaskTracer(stall_threshold_ms=30)
    client = anthropic.AsyncAnthropic()

    async def llm_call(prompt: str) -> str:
        r = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": prompt}],
        )
        return r.content[0].text

    async def slow_tool(name: str) -> str:
        await asyncio.sleep(0.05)  # Simulates a slow tool
        return f"Result from {name}"

    # Mix of LLM calls and tool calls
    tasks = [
        asyncio.create_task(tracer.trace(f"llm_call_{i}", llm_call(p)))
        for i, p in enumerate(["What is Python?", "What is REST?", "Name a fruit."])
    ] + [
        asyncio.create_task(tracer.trace(f"tool_{name}", slow_tool(name)))
        for name in ["search_db", "read_file", "fetch_url"]
    ]

    await asyncio.gather(*tasks)
    tracer.report()


if __name__ == "__main__":
    asyncio.run(run_traced_agent())
# Expected Token Savings: 0% — queue wait tracking reveals scheduling starvation patterns
# Environment: pip install anthropic; asyncio, time are stdlib
```

---

## Option 4: SQLite-Backed Loop Health Dashboard

```python
import asyncio
import sqlite3
import time
import json
import anthropic
from datetime import datetime

SAMPLE_INTERVAL = 0.25   # seconds between lag samples
STALL_THRESHOLD = 80     # ms — log as stall
CRITICAL_THRESHOLD = 300 # ms — log as critical

class LoopHealthDB:
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS loop_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lag_ms REAL,
                severity TEXT,
                sampled_at TEXT DEFAULT (datetime('now'))
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prompt TEXT,
                latency_ms REAL,
                loop_lag_at_call REAL,
                called_at TEXT DEFAULT (datetime('now'))
            )
        """)
        self.conn.commit()

    def record_sample(self, lag_ms: float):
        if lag_ms >= CRITICAL_THRESHOLD:
            severity = "critical"
        elif lag_ms >= STALL_THRESHOLD:
            severity = "stall"
        else:
            severity = "normal"
        self.conn.execute(
            "INSERT INTO loop_samples (lag_ms, severity) VALUES (?,?)",
            (round(lag_ms, 2), severity),
        )
        self.conn.commit()

    def record_call(self, prompt: str, latency_ms: float, current_lag: float):
        self.conn.execute(
            "INSERT INTO agent_calls (prompt, latency_ms, loop_lag_at_call) VALUES (?,?,?)",
            (prompt[:80], round(latency_ms, 1), round(current_lag, 2)),
        )
        self.conn.commit()

    def dashboard(self) -> dict:
        samples = self.conn.execute(
            "SELECT COUNT(*), AVG(lag_ms), MAX(lag_ms), SUM(CASE WHEN severity='stall' THEN 1 ELSE 0 END), SUM(CASE WHEN severity='critical' THEN 1 ELSE 0 END) FROM loop_samples"
        ).fetchone()
        calls = self.conn.execute(
            "SELECT COUNT(*), AVG(latency_ms), AVG(loop_lag_at_call) FROM agent_calls"
        ).fetchone()
        return {
            "loop_samples": {
                "total": samples[0],
                "avg_lag_ms": round(samples[1] or 0, 2),
                "max_lag_ms": round(samples[2] or 0, 2),
                "stalls": samples[3],
                "criticals": samples[4],
            },
            "agent_calls": {
                "total": calls[0],
                "avg_latency_ms": round(calls[1] or 0, 1),
                "avg_loop_lag_at_call": round(calls[2] or 0, 2),
            },
        }


_current_lag = 0.0

async def loop_sampler(db: LoopHealthDB, stop: asyncio.Event):
    global _current_lag
    while not stop.is_set():
        t0 = time.monotonic()
        await asyncio.sleep(0)
        lag = (time.monotonic() - t0) * 1000
        _current_lag = lag
        db.record_sample(lag)
        if lag >= CRITICAL_THRESHOLD:
            print(f"[LoopHealth] CRITICAL LAG: {lag:.0f}ms")
        elif lag >= STALL_THRESHOLD:
            print(f"[LoopHealth] Stall: {lag:.0f}ms")
        await asyncio.sleep(SAMPLE_INTERVAL)


async def run_monitored_agent_with_db():
    global _current_lag
    db = LoopHealthDB()
    stop = asyncio.Event()

    sampler_task = asyncio.create_task(loop_sampler(db, stop))
    client = anthropic.AsyncAnthropic()

    prompts = [
        "What is Python?",
        "Name a data structure.",
        "What is async/await?",
        "Explain REST briefly.",
    ]

    for prompt in prompts:
        t0 = time.time()
        r = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": prompt}],
        )
        latency_ms = (time.time() - t0) * 1000
        db.record_call(prompt, latency_ms, _current_lag)
        print(f"[Agent] {prompt[:35]} → {latency_ms:.0f}ms (loop_lag={_current_lag:.1f}ms)")

    stop.set()
    await sampler_task

    dashboard = db.dashboard()
    print(f"\nLoop Health Dashboard:")
    print(json.dumps(dashboard, indent=2))


if __name__ == "__main__":
    asyncio.run(run_monitored_agent_with_db())
# Expected Token Savings: 0% — dashboard correlates loop lag with call latency to diagnose blocking
# Environment: pip install anthropic; asyncio, sqlite3, time, json are stdlib
```

---

## Option 5: Blocking Call Detector via Thread Monitoring

```python
import asyncio
import threading
import time
import traceback
import anthropic
from dataclasses import dataclass

BLOCK_DETECTION_INTERVAL = 0.05   # Check every 50ms
BLOCK_ALARM_THRESHOLD = 0.15       # Alarm if loop hasn't ticked in 150ms

@dataclass
class BlockEvent:
    duration_ms: float
    stack_trace: str
    detected_at: float


class BlockingCallDetector:
    """
    Runs a watchdog thread that alarms if the async event loop
    fails to execute a scheduled callback within the threshold.
    """

    def __init__(self, threshold_ms: float = BLOCK_ALARM_THRESHOLD * 1000):
        self.threshold_ms = threshold_ms
        self._last_tick = time.monotonic()
        self._events: list[BlockEvent] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._thread: threading.Thread | None = None

    async def _tick(self):
        """Coroutine that updates last_tick; if it stops running, the loop is blocked."""
        while self._running:
            self._last_tick = time.monotonic()
            await asyncio.sleep(BLOCK_DETECTION_INTERVAL)

    def _watchdog(self):
        """Thread that checks if ticks are happening regularly."""
        while self._running:
            time.sleep(BLOCK_DETECTION_INTERVAL)
            since_tick = (time.monotonic() - self._last_tick) * 1000
            if since_tick > self.threshold_ms:
                # Capture stack trace of main thread
                frames = sys_traceback()
                event = BlockEvent(
                    duration_ms=round(since_tick, 1),
                    stack_trace=frames,
                    detected_at=time.monotonic(),
                )
                self._events.append(event)
                print(f"[BlockDetector] ⚠️  Event loop blocked for {since_tick:.0f}ms!")

    def start(self):
        self._running = True
        self._loop = asyncio.get_event_loop()
        asyncio.create_task(self._tick())
        self._thread = threading.Thread(target=self._watchdog, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    @property
    def events(self) -> list[BlockEvent]:
        return list(self._events)


def sys_traceback() -> str:
    """Capture stack trace of all Python threads."""
    import sys
    frames = []
    for thread_id, frame in sys._current_frames().items():
        frames.append(f"Thread {thread_id}:")
        for line in traceback.format_stack(frame):
            frames.append(line.strip())
    return "\n".join(frames[:20])  # Limit output


async def run_with_block_detection():
    detector = BlockingCallDetector(threshold_ms=100)
    detector.start()
    client = anthropic.AsyncAnthropic()

    print("Phase 1: Normal async calls")
    for prompt in ["What is Python?", "Name a fruit."]:
        r = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=32,
            messages=[{"role": "user", "content": prompt}],
        )
        print(f"  → {r.content[0].text[:50]}")

    print("\nPhase 2: Blocking call (simulated sync sleep)")
    time.sleep(0.2)  # This blocks the loop — detector should fire

    print("\nPhase 3: Back to normal")
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=32,
        messages=[{"role": "user", "content": "Say hello."}],
    )
    print(f"  → {r.content[0].text[:50]}")

    detector.stop()

    events = detector.events
    if events:
        print(f"\n[BlockDetector] Detected {len(events)} blocking event(s):")
        for e in events:
            print(f"  Duration: {e.duration_ms:.0f}ms")
    else:
        print("\n[BlockDetector] No blocking events detected")


if __name__ == "__main__":
    asyncio.run(run_with_block_detection())
# Expected Token Savings: 0% — watchdog thread adds zero async overhead; catches sync blocking calls
# Environment: pip install anthropic; asyncio, threading, time, traceback are stdlib
```

---

## Option 6: Event Loop Health Exporter for Prometheus/Metrics

```python
import asyncio
import time
import json
import sqlite3
import anthropic
from dataclasses import dataclass, field
from collections import deque

@dataclass
class LoopMetrics:
    """Prometheus-compatible metric snapshot."""
    loop_lag_p50_ms: float
    loop_lag_p95_ms: float
    loop_lag_p99_ms: float
    loop_lag_max_ms: float
    stall_count_total: int
    tasks_pending: int
    samples_collected: int

    def to_prometheus(self) -> str:
        lines = [
            f"# HELP event_loop_lag_ms Event loop scheduling lag in milliseconds",
            f"event_loop_lag_p50_ms {self.loop_lag_p50_ms}",
            f"event_loop_lag_p95_ms {self.loop_lag_p95_ms}",
            f"event_loop_lag_p99_ms {self.loop_lag_p99_ms}",
            f"event_loop_lag_max_ms {self.loop_lag_max_ms}",
            f"event_loop_stalls_total {self.stall_count_total}",
            f"event_loop_tasks_pending {self.tasks_pending}",
        ]
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps({
            "loop_lag": {
                "p50": self.loop_lag_p50_ms,
                "p95": self.loop_lag_p95_ms,
                "p99": self.loop_lag_p99_ms,
                "max": self.loop_lag_max_ms,
            },
            "stalls": self.stall_count_total,
            "pending_tasks": self.tasks_pending,
            "samples": self.samples_collected,
        })


class LoopMetricsCollector:
    STALL_MS = 50
    WINDOW = 200

    def __init__(self):
        self._window: deque[float] = deque(maxlen=self.WINDOW)
        self._all: list[float] = []
        self._stalls = 0
        self._running = False

    def _percentile(self, data: list[float], pct: float) -> float:
        if not data:
            return 0.0
        s = sorted(data)
        idx = min(int(len(s) * pct / 100), len(s) - 1)
        return round(s[idx], 2)

    async def _collect(self, interval: float):
        while self._running:
            t0 = time.monotonic()
            await asyncio.sleep(0)
            lag = (time.monotonic() - t0) * 1000
            self._window.append(lag)
            self._all.append(lag)
            if lag > self.STALL_MS:
                self._stalls += 1
            await asyncio.sleep(interval)

    def start(self, interval: float = 0.1):
        self._running = True
        asyncio.create_task(self._collect(interval))

    def stop(self):
        self._running = False

    def snapshot(self) -> LoopMetrics:
        data = list(self._all)
        loop = asyncio.get_event_loop()
        pending = len(asyncio.all_tasks(loop))
        return LoopMetrics(
            loop_lag_p50_ms=self._percentile(data, 50),
            loop_lag_p95_ms=self._percentile(data, 95),
            loop_lag_p99_ms=self._percentile(data, 99),
            loop_lag_max_ms=round(max(data, default=0), 2),
            stall_count_total=self._stalls,
            tasks_pending=pending,
            samples_collected=len(data),
        )


async def run_agent_with_metrics_export():
    collector = LoopMetricsCollector()
    collector.start(interval=0.1)
    client = anthropic.AsyncAnthropic()

    prompts = [
        "What is Python?",
        "Explain recursion.",
        "What is a hash table?",
        "What is machine learning?",
    ]

    print("Running agent with metrics export...")
    tasks = [
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": p}],
        )
        for p in prompts
    ]
    responses = await asyncio.gather(*tasks)
    for p, r in zip(prompts, responses):
        print(f"  {p[:35]} → {r.content[0].text[:50]}")

    await asyncio.sleep(0.3)  # Let monitor collect more samples
    collector.stop()

    metrics = collector.snapshot()
    print(f"\nPrometheus-format metrics:")
    print(metrics.to_prometheus())
    print(f"\nJSON metrics:")
    print(metrics.to_json())


if __name__ == "__main__":
    asyncio.run(run_agent_with_metrics_export())
# Expected Token Savings: 0% — metrics enable SLO tracking and PagerDuty integration
# Environment: pip install anthropic; asyncio, time, json, collections are stdlib
```

---

## Comparison

| Option | Detection Method | Overhead | SQLite | Alert | Prometheus | Best For |
|--------|-----------------|----------|--------|-------|------------|----------|
| 1 | `asyncio.sleep(0)` roundtrip | Minimal | No | Console | No | Quick stall detection |
| 2 | Rolling lag histogram | Minimal | No | Console | No | p50/p95/p99 profiling |
| 3 | Per-task queue wait | Per-task | No | Console | No | Identifying slow/starved tasks |
| 4 | Sampler + SQLite | Minimal | Yes | Console | No | Dashboard + call correlation |
| 5 | Watchdog thread | Thread | No | Console | No | Detecting sync blocking calls |
| 6 | Percentile exporter | Minimal | No | External | Yes | Production observability stack |
