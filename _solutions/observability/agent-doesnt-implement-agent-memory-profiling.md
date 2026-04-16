---
title: "Agent Doesn't Implement Agent Memory Profiling"
description: "Long-running AI agents accumulate memory over time — from growing conversation histories, unreleased tool result buffers, and leaking asyncio tasks — but no profiling exists to detect or attribute the growth."
category: observability
difficulty: advanced
tags: [memory, profiling, leak-detection, tracemalloc, gc, asyncio, observability, heap]
---

# Agent Doesn't Implement Agent Memory Profiling

## Problem

AI agents that run for hours or days frequently exhibit memory growth: conversation histories are never pruned, tool result buffers accumulate, asyncio tasks leak references, or LRU caches grow without bounds. Without memory profiling, the growth is invisible until the process OOMs and dies. Even moderate leaks (~10 MB/hour) compound to gigabytes over a week. Memory profiling gives you the allocation site, the object type, and the growth rate — turning a production incident into a routine fix.

## Solution 1: tracemalloc Snapshot Diff — Find the Top Allocation Sites

Use Python's built-in `tracemalloc` to take snapshots at intervals and diff them to find which code paths are allocating the most memory.

```python
import asyncio
import gc
import tracemalloc
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

class TraceMallocProfiler:
    """
    Periodic memory snapshot profiler using tracemalloc.
    Reports top allocation sites by growth between snapshots.
    """

    def __init__(self, top_n: int = 20, interval_seconds: float = 60.0):
        self.top_n = top_n
        self.interval = interval_seconds
        self._baseline: tracemalloc.Snapshot | None = None
        self._task: asyncio.Task | None = None

    def start(self, nframe: int = 5) -> None:
        tracemalloc.start(nframe)
        self._baseline = tracemalloc.take_snapshot()
        self._task = asyncio.create_task(self._periodic_report())

    async def stop(self) -> None:
        tracemalloc.stop()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _periodic_report(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            self.report()

    def report(self) -> list[dict]:
        gc.collect()
        current = tracemalloc.take_snapshot()
        stats = current.compare_to(self._baseline, "lineno")

        results = []
        for stat in stats[:self.top_n]:
            if stat.size_diff <= 0:
                continue
            frame = stat.traceback[0] if stat.traceback else None
            results.append({
                "file": frame.filename if frame else "unknown",
                "line": frame.lineno if frame else 0,
                "size_kb": round(stat.size / 1024, 1),
                "size_diff_kb": round(stat.size_diff / 1024, 1),
                "count": stat.count,
                "count_diff": stat.count_diff,
            })
            if results:
                print(
                    f"  {results[-1]['file']}:{results[-1]['line']} "
                    f"+{results[-1]['size_diff_kb']} KB "
                    f"({results[-1]['count_diff']} objects)"
                )
        return results

    def snapshot_top_by_type(self) -> list[dict]:
        """Group current allocations by object type."""
        gc.collect()
        snap = tracemalloc.take_snapshot()
        stats = snap.statistics("traceback")
        type_totals: dict[str, int] = {}
        for stat in stats:
            key = str(stat.traceback[0]) if stat.traceback else "unknown"
            type_totals[key] = type_totals.get(key, 0) + stat.size

        return sorted(
            [{"location": k, "size_kb": round(v / 1024, 1)} for k, v in type_totals.items()],
            key=lambda x: -x["size_kb"],
        )[:20]

# Usage in agent startup
profiler = TraceMallocProfiler(top_n=20, interval_seconds=60.0)

async def agent_with_profiling():
    profiler.start(nframe=5)

    history = []
    for i in range(100):
        history.append({"role": "user", "content": f"Message {i}"})
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=history[-20:],  # keep last 20
        )
        history.append({"role": "assistant", "content": resp.content[0].text})

        if i % 20 == 0:
            print(f"\n--- Memory snapshot at turn {i} ---")
            profiler.report()

    await profiler.stop()
```

**When to use**: Initial diagnosis of memory growth. Run tracemalloc in staging with realistic workloads to identify the top allocating code paths before they become production incidents.

---

## Solution 2: RSS Tracking — Measure Real Process Memory Over Time

Track RSS (Resident Set Size) using `psutil` to detect growth trends and alert when memory exceeds thresholds.

```python
import asyncio
import os
import time
from collections import deque
from dataclasses import dataclass, field
import psutil
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass
class MemorySample:
    timestamp: float
    rss_mb: float
    vms_mb: float
    gc_objects: int

class RSSTracker:
    """
    Tracks RSS memory over time. Detects growth trends and fires alerts.
    """

    def __init__(
        self,
        interval_seconds: float = 30.0,
        window_samples: int = 60,      # last 30 min at 30s interval
        alert_rss_mb: float = 512.0,
        growth_alert_mb_per_min: float = 5.0,
    ):
        self.interval = interval_seconds
        self._samples: deque[MemorySample] = deque(maxlen=window_samples)
        self.alert_rss_mb = alert_rss_mb
        self.growth_alert_mb_per_min = growth_alert_mb_per_min
        self._process = psutil.Process(os.getpid())
        self._task: asyncio.Task | None = None
        self._alert_callbacks: list = []

    def on_alert(self, callback) -> None:
        self._alert_callbacks.append(callback)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        import gc
        while True:
            mem = self._process.memory_info()
            sample = MemorySample(
                timestamp=time.time(),
                rss_mb=mem.rss / 1_048_576,
                vms_mb=mem.vms / 1_048_576,
                gc_objects=len(gc.get_objects()),
            )
            self._samples.append(sample)
            await self._check_alerts(sample)
            await asyncio.sleep(self.interval)

    async def _check_alerts(self, latest: MemorySample) -> None:
        if latest.rss_mb > self.alert_rss_mb:
            await self._fire_alert("rss_threshold", latest)

        # Check growth rate over last 5 minutes
        if len(self._samples) >= 10:
            oldest = list(self._samples)[-10]
            elapsed_min = (latest.timestamp - oldest.timestamp) / 60
            if elapsed_min > 0:
                growth_rate = (latest.rss_mb - oldest.rss_mb) / elapsed_min
                if growth_rate > self.growth_alert_mb_per_min:
                    await self._fire_alert("growth_rate", latest, extra={"rate_mb_per_min": round(growth_rate, 2)})

    async def _fire_alert(self, kind: str, sample: MemorySample, extra: dict | None = None):
        alert = {
            "kind": kind,
            "rss_mb": round(sample.rss_mb, 1),
            "gc_objects": sample.gc_objects,
            **(extra or {}),
        }
        print(f"[memory_alert] {alert}")
        for cb in self._alert_callbacks:
            await cb(alert)

    def current_stats(self) -> dict:
        if not self._samples:
            return {}
        latest = self._samples[-1]
        oldest = self._samples[0]
        elapsed_min = (latest.timestamp - oldest.timestamp) / 60
        growth = (latest.rss_mb - oldest.rss_mb) / elapsed_min if elapsed_min > 0 else 0.0
        return {
            "rss_mb": round(latest.rss_mb, 1),
            "vms_mb": round(latest.vms_mb, 1),
            "gc_objects": latest.gc_objects,
            "growth_mb_per_min": round(growth, 3),
            "samples": len(self._samples),
        }

tracker = RSSTracker(interval_seconds=30, alert_rss_mb=256, growth_alert_mb_per_min=2.0)

async def main():
    tracker.start()
    # ... run agent ...
    await asyncio.sleep(300)
    print(tracker.current_stats())
    await tracker.stop()
```

**When to use**: Production agents. RSS tracking is low-overhead (~0% CPU) and runs continuously. It's the first signal that something is leaking before you need detailed tracemalloc profiling.

---

## Solution 3: GC Object Census — Find Leak by Object Type

Count live Python objects by type at regular intervals. A type whose count grows monotonically is likely leaking.

```python
import asyncio
import gc
import time
from collections import Counter, defaultdict
from typing import Any
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

class GCObjectCensus:
    """
    Takes periodic GC object counts by type.
    Reports types whose counts are growing.
    """

    def __init__(self, interval_seconds: float = 60.0, top_n: int = 30):
        self.interval = interval_seconds
        self.top_n = top_n
        self._history: list[tuple[float, Counter]] = []
        self._task: asyncio.Task | None = None

    def start(self) -> None:
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
            await asyncio.sleep(self.interval)
            self._take_snapshot()

    def _take_snapshot(self) -> Counter:
        gc.collect()
        counts: Counter = Counter()
        for obj in gc.get_objects():
            counts[type(obj).__name__] += 1
        self._history.append((time.time(), counts))
        # Keep last 30 snapshots
        if len(self._history) > 30:
            self._history.pop(0)
        return counts

    def growing_types(self, min_growth: int = 100) -> list[dict]:
        """Return types whose count has grown by at least min_growth over all snapshots."""
        if len(self._history) < 2:
            return []

        first_ts, first_counts = self._history[0]
        last_ts, last_counts = self._history[-1]
        elapsed_min = (last_ts - first_ts) / 60

        growing = []
        for type_name in set(list(first_counts.keys()) + list(last_counts.keys())):
            diff = last_counts.get(type_name, 0) - first_counts.get(type_name, 0)
            if diff >= min_growth:
                growing.append({
                    "type": type_name,
                    "count_start": first_counts.get(type_name, 0),
                    "count_end": last_counts.get(type_name, 0),
                    "growth": diff,
                    "growth_per_min": round(diff / elapsed_min, 1) if elapsed_min > 0 else 0,
                })

        return sorted(growing, key=lambda x: -x["growth"])[:self.top_n]

    def snapshot_now(self) -> list[dict]:
        """Take an immediate snapshot and return top types by count."""
        counts = self._take_snapshot()
        return [
            {"type": k, "count": v}
            for k, v in counts.most_common(self.top_n)
        ]

census = GCObjectCensus(interval_seconds=60)

async def agent_loop():
    census.start()

    # Run agent for a while
    history = []
    for i in range(200):
        history.append({"role": "user", "content": f"Task {i}"})
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=32,
            messages=history[-10:],
        )
        history.append({"role": "assistant", "content": resp.content[0].text})

    growing = census.growing_types(min_growth=50)
    if growing:
        print("Growing object types (potential leaks):")
        for item in growing:
            print(f"  {item['type']}: +{item['growth']} ({item['growth_per_min']}/min)")

    await census.stop()
```

**When to use**: When RSS is growing but tracemalloc shows nothing obvious. GC census identifies which Python type is accumulating — dict, list, Task, Message, etc.

---

## Solution 4: Asyncio Task Leak Detector — Find Unreferenced Running Tasks

Asyncio tasks that are created but never awaited accumulate as "fire-and-forget leaks". Detect them by periodically checking `asyncio.all_tasks()`.

```python
import asyncio
import time
import weakref
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass
class TaskSnapshot:
    timestamp: float
    count: int
    names: list[str]

class AsyncioTaskLeakDetector:
    """
    Monitors asyncio task count over time.
    A monotonically growing task count indicates unawaited task leaks.
    """

    def __init__(self, interval_seconds: float = 30.0, alert_threshold: int = 100):
        self.interval = interval_seconds
        self.alert_threshold = alert_threshold
        self._snapshots: list[TaskSnapshot] = []
        self._task: asyncio.Task | None = None
        self._baseline_count: int = 0

    def start(self) -> None:
        self._baseline_count = len(asyncio.all_tasks())
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
            await asyncio.sleep(self.interval)
            self._check()

    def _check(self) -> TaskSnapshot:
        all_tasks = asyncio.all_tasks()
        # Exclude the monitoring task itself
        agent_tasks = [t for t in all_tasks if t is not self._task]
        names = [t.get_name() for t in agent_tasks if not t.done()]

        snap = TaskSnapshot(
            timestamp=time.time(),
            count=len(names),
            names=names[:20],  # sample first 20
        )
        self._snapshots.append(snap)
        if len(self._snapshots) > 60:
            self._snapshots.pop(0)

        if snap.count > self.alert_threshold:
            print(f"[task_leak] WARNING: {snap.count} live tasks (threshold={self.alert_threshold})")
            print(f"  Sample task names: {snap.names[:5]}")

        return snap

    def growth_rate(self) -> float:
        """Tasks/minute growth rate over observed window."""
        if len(self._snapshots) < 2:
            return 0.0
        first, last = self._snapshots[0], self._snapshots[-1]
        elapsed_min = (last.timestamp - first.timestamp) / 60
        if elapsed_min <= 0:
            return 0.0
        return (last.count - first.count) / elapsed_min

    def report(self) -> dict:
        if not self._snapshots:
            return {}
        latest = self._snapshots[-1]
        return {
            "live_tasks": latest.count,
            "growth_per_min": round(self.growth_rate(), 2),
            "sample_names": latest.names[:5],
        }

detector = AsyncioTaskLeakDetector(interval_seconds=30, alert_threshold=50)

async def agent_with_task_monitoring():
    detector.start()

    # Simulate potential task leak: fire-and-forget without keeping reference
    async def background_work():
        await asyncio.sleep(100)  # never completes in this demo

    for i in range(10):
        asyncio.create_task(background_work(), name=f"bg_work_{i}")  # leaking!

    await asyncio.sleep(60)
    print(f"Task leak report: {detector.report()}")
    await detector.stop()
```

**When to use**: Agents that use `asyncio.create_task()` for background work. Task leaks are invisible without this monitor and cause gradual memory growth that looks like object leaks.

---

## Solution 5: Conversation History Memory Guard — Bound the Largest Allocation

Conversation histories are often the single largest memory consumer. Enforce a byte budget and prune automatically.

```python
import asyncio
import sys
from dataclasses import dataclass, field
from typing import Any
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass
class MemoryBoundedHistory:
    """
    Conversation history that enforces a memory budget.
    Oldest messages are pruned first when budget is exceeded.
    System message is always preserved.
    """

    budget_bytes: int = 1 * 1024 * 1024  # 1 MB default
    system: str = ""
    _messages: list[dict] = field(default_factory=list)
    _bytes_used: int = 0

    def _message_size(self, msg: dict) -> int:
        content = msg.get("content", "")
        if isinstance(content, str):
            return sys.getsizeof(content)
        if isinstance(content, list):
            return sum(sys.getsizeof(str(block)) for block in content)
        return sys.getsizeof(str(content))

    def add(self, role: str, content: str) -> None:
        msg = {"role": role, "content": content}
        size = self._message_size(msg)

        self._messages.append(msg)
        self._bytes_used += size

        # Prune oldest non-system messages until under budget
        while self._bytes_used > self.budget_bytes and len(self._messages) > 1:
            removed = self._messages.pop(0)
            self._bytes_used -= self._message_size(removed)

    def get_messages(self) -> list[dict]:
        return list(self._messages)

    @property
    def stats(self) -> dict:
        return {
            "message_count": len(self._messages),
            "bytes_used": self._bytes_used,
            "budget_bytes": self.budget_bytes,
            "utilization_pct": round(100 * self._bytes_used / self.budget_bytes, 1),
        }

async def agent_with_bounded_history():
    history = MemoryBoundedHistory(
        budget_bytes=512 * 1024,  # 512 KB per conversation
        system="You are a helpful assistant.",
    )

    prompts = [f"Tell me about topic number {i}" for i in range(200)]

    for prompt in prompts:
        history.add("user", prompt)
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            system=history.system,
            messages=history.get_messages(),
        )
        reply = resp.content[0].text
        history.add("assistant", reply)

    print(f"Final history stats: {history.stats}")
    # Bytes used stays bounded regardless of conversation length
```

**When to use**: Every agent with multi-turn conversations. Unbounded conversation histories are the #1 cause of agent memory growth in production.

---

## Solution 6: Memory Profiling Endpoint — On-Demand Heap Dump for Production

Expose an HTTP endpoint that triggers a memory snapshot on demand, without restarting the process.

```python
import asyncio
import gc
import io
import json
import os
import tracemalloc
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

class MemoryProfilingServer:
    """
    Lightweight HTTP server for on-demand memory profiling in production.
    Runs in a background thread so it doesn't interfere with the event loop.
    Endpoints:
      GET /memory/stats     — RSS, GC counts, tracemalloc summary
      GET /memory/top       — Top allocation sites (requires tracemalloc)
      GET /memory/gc-census — Top object types by count
      POST /memory/gc       — Force GC collection
    """

    def __init__(self, port: int = 9090):
        self.port = port
        self._server: HTTPServer | None = None
        self._thread: Thread | None = None
        self._profiler_active = False

    def start(self, enable_tracemalloc: bool = True) -> None:
        if enable_tracemalloc and not tracemalloc.is_tracing():
            tracemalloc.start(5)
            self._profiler_active = True

        handler = self._make_handler()
        self._server = HTTPServer(("127.0.0.1", self.port), handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        print(f"[memory_profiler] listening on http://127.0.0.1:{self.port}/memory/")

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
        if self._profiler_active:
            tracemalloc.stop()

    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass  # silence access logs

            def do_GET(self):
                if self.path == "/memory/stats":
                    self._respond(server._stats())
                elif self.path == "/memory/top":
                    self._respond(server._top_allocations())
                elif self.path == "/memory/gc-census":
                    self._respond(server._gc_census())
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):
                if self.path == "/memory/gc":
                    collected = gc.collect()
                    self._respond({"collected": collected})
                else:
                    self.send_response(404)
                    self.end_headers()

            def _respond(self, data: dict):
                body = json.dumps(data, default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    def _stats(self) -> dict:
        import psutil
        proc = psutil.Process(os.getpid())
        mem = proc.memory_info()
        return {
            "rss_mb": round(mem.rss / 1_048_576, 1),
            "vms_mb": round(mem.vms / 1_048_576, 1),
            "gc_counts": gc.get_count(),
            "gc_objects": len(gc.get_objects()),
            "tracemalloc_active": tracemalloc.is_tracing(),
        }

    def _top_allocations(self, top_n: int = 20) -> list[dict]:
        if not tracemalloc.is_tracing():
            return [{"error": "tracemalloc not active"}]
        gc.collect()
        snap = tracemalloc.take_snapshot()
        return [
            {
                "file": str(stat.traceback[0].filename) if stat.traceback else "?",
                "line": stat.traceback[0].lineno if stat.traceback else 0,
                "size_kb": round(stat.size / 1024, 1),
                "count": stat.count,
            }
            for stat in snap.statistics("lineno")[:top_n]
        ]

    def _gc_census(self, top_n: int = 30) -> list[dict]:
        gc.collect()
        counts: dict[str, int] = {}
        for obj in gc.get_objects():
            name = type(obj).__name__
            counts[name] = counts.get(name, 0) + 1
        sorted_counts = sorted(counts.items(), key=lambda x: -x[1])
        return [{"type": k, "count": v} for k, v in sorted_counts[:top_n]]

profiler_server = MemoryProfilingServer(port=9090)

async def run_agent():
    profiler_server.start(enable_tracemalloc=True)
    # curl http://127.0.0.1:9090/memory/stats
    # curl http://127.0.0.1:9090/memory/top
    # curl http://127.0.0.1:9090/memory/gc-census

    for i in range(50):
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": f"Message {i}"}],
        )

    await asyncio.sleep(300)  # keep running for profiling
    profiler_server.stop()
```

**When to use**: Production agents where you cannot attach a profiler interactively. The endpoint lets on-call engineers inspect heap state without a restart or deployment.

---

## Comparison

| Solution | Overhead | Granularity | Online | Root Cause | Best For |
|---|---|---|---|---|---|
| tracemalloc snapshot diff | Medium (2–5%) | Line-level | No | Allocation site | Pre-production leak diagnosis |
| RSS tracking | ~0% | Process-level | Yes | Growth rate | Continuous production monitoring |
| GC object census | Low | Type-level | Yes | Object type | Identifying leaking type |
| Asyncio task leak detector | ~0% | Task-level | Yes | Unawaited tasks | Task reference leaks |
| Bounded history | None | App-level | Yes | Prevention | Largest single allocator |
| Profiling endpoint | Medium (when queried) | Line + type | Yes | On-demand | Production heap inspection |

**Rule of thumb**: Always run RSS tracking (Solution 2) in production — zero overhead, immediate growth signal. Add the profiling endpoint (Solution 6) for on-demand diagnosis. Use tracemalloc (Solution 1) in staging to identify allocation sites before deploying.
