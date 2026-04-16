---
title: "Agent Doesn't Implement Agent Resource Usage Sampling"
description: "Agents that emit no CPU, memory, or file descriptor metrics cannot detect resource leaks, correlate latency spikes with memory pressure, or set meaningful resource limits. Without periodic resource sampling, a gradual memory leak is invisible until the process OOM-kills, and high CPU usage from a runaway tool call is only noticed when response times collapse. Implement periodic resource usage sampling that tracks CPU, memory, and open file descriptors and surfaces anomalies."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-agent-resource-usage-sampling
tags: [resource-sampling, memory-leak, cpu-monitoring, file-descriptors, process-metrics, oom-prevention]
symptoms:
  - "Process OOM-killed with no warning — memory growth was never tracked"
  - "CPU spikes from runaway tool calls are invisible until response times collapse"
  - "File descriptor leak causes 'too many open files' errors after days of uptime"
  - "No correlation between memory usage and latency — cannot diagnose GC-pressure-driven slowdowns"
  - "Resource utilization is only visible at the host level — no per-process agent metrics"
---

## Why This Happens

Agent processes typically emit application-level metrics (request count, latency, error rate) but not process-level resource metrics. The process-level view — how much RSS memory is in use, what fraction of CPU is consumed, how many file descriptors are open — is available from the OS but requires explicit sampling code. Without sampling, resource leaks grow silently: a cache that never evicts, a connection that never closes, a background task that accumulates results. Periodic sampling at a 30–60 second interval produces a time series that enables trending, anomaly detection, and correlation with application events.

## Solution 1: Resource Sample

```python
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ResourceSample:
    sampled_at: float = field(default_factory=time.time)
    rss_bytes: int = 0               # resident set size
    vms_bytes: int = 0               # virtual memory size
    cpu_percent: float = 0.0         # CPU usage percent (0–100 per core)
    open_fds: int = 0                # open file descriptors
    threads: int = 0
    gc_collections: Optional[int] = None   # GC cycle count since last sample
    gc_objects: Optional[int] = None       # objects tracked by GC

    @property
    def rss_mb(self) -> float:
        return round(self.rss_bytes / 1024 / 1024, 2)
```

## Solution 2: Resource Sampler

```python
import gc
import os
import time
from typing import Optional


class AgentResourceSampler:
    """
    Collects process-level resource metrics using psutil if available,
    falling back to /proc/self/status on Linux.
    """

    def __init__(self):
        self._prev_cpu_times = None
        self._prev_sample_time = None
        self._prev_gc_count = sum(gc.get_count())

        try:
            import psutil
            self._process = psutil.Process(os.getpid())
            self._use_psutil = True
        except ImportError:
            self._process = None
            self._use_psutil = False

    def sample(self) -> ResourceSample:
        now = time.time()
        sample = ResourceSample(sampled_at=now)

        if self._use_psutil:
            try:
                import psutil
                mem = self._process.memory_info()
                sample.rss_bytes = mem.rss
                sample.vms_bytes = mem.vms

                cpu_times = self._process.cpu_times()
                if self._prev_cpu_times and self._prev_sample_time:
                    elapsed = now - self._prev_sample_time
                    user_delta = cpu_times.user - self._prev_cpu_times.user
                    sys_delta = cpu_times.system - self._prev_cpu_times.system
                    sample.cpu_percent = round(
                        (user_delta + sys_delta) / max(elapsed, 0.001) * 100, 2
                    )
                self._prev_cpu_times = cpu_times
                self._prev_sample_time = now

                sample.open_fds = self._process.num_fds()
                sample.threads = self._process.num_threads()
            except Exception:
                pass
        else:
            self._sample_proc_status(sample)

        # GC stats
        current_gc = sum(gc.get_count())
        sample.gc_collections = current_gc - self._prev_gc_count
        sample.gc_objects = len(gc.get_objects())
        self._prev_gc_count = current_gc

        return sample

    def _sample_proc_status(self, sample: ResourceSample) -> None:
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        sample.rss_bytes = int(line.split()[1]) * 1024
                    elif line.startswith("VmSize:"):
                        sample.vms_bytes = int(line.split()[1]) * 1024
                    elif line.startswith("Threads:"):
                        sample.threads = int(line.split()[1])
        except (OSError, ValueError):
            pass
```

## Solution 3: Resource Sample Store

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, List, Optional


class ResourceSampleStore:
    """
    Accumulates resource samples in a bounded ring buffer.
    Supports window-based trend queries for anomaly detection.
    """

    def __init__(self, max_samples: int = 2880):  # 24h at 30s intervals
        self._max = max_samples
        self._samples: Deque[ResourceSample] = deque()
        self._lock = Lock()

    def record(self, sample: ResourceSample) -> None:
        with self._lock:
            self._samples.append(sample)
            if len(self._samples) > self._max:
                self._samples.popleft()

    def recent(self, window_seconds: float = 3600.0) -> List[ResourceSample]:
        cutoff = time.time() - window_seconds
        with self._lock:
            return [s for s in self._samples if s.sampled_at >= cutoff]

    def latest(self) -> Optional[ResourceSample]:
        with self._lock:
            return self._samples[-1] if self._samples else None

    def summary(self, window_seconds: float = 3600.0) -> dict:
        samples = self.recent(window_seconds)
        if not samples:
            return {"window_seconds": window_seconds, "samples": 0}
        rss = [s.rss_mb for s in samples]
        cpu = [s.cpu_percent for s in samples]
        fds = [s.open_fds for s in samples]
        return {
            "window_seconds": window_seconds,
            "samples": len(samples),
            "rss_mb": {"min": min(rss), "max": max(rss), "latest": rss[-1]},
            "cpu_pct": {"min": min(cpu), "max": max(cpu), "avg": round(sum(cpu) / len(cpu), 2)},
            "open_fds": {"min": min(fds), "max": max(fds), "latest": fds[-1]},
        }
```

## Solution 4: Resource Leak Detector

```python
from typing import List, Optional


class ResourceLeakDetector:
    """
    Detects memory and file descriptor leaks by measuring the
    growth rate over a sliding window. Flags linear growth patterns
    that suggest a leak rather than normal variance.
    """

    def __init__(
        self,
        rss_growth_threshold_mb_per_hour: float = 50.0,
        fd_growth_threshold_per_hour: float = 100.0,
    ):
        self._rss_threshold = rss_growth_threshold_mb_per_hour
        self._fd_threshold = fd_growth_threshold_per_hour

    def analyze(self, store: ResourceSampleStore, window_seconds: float = 3600.0) -> dict:
        samples = store.recent(window_seconds)
        if len(samples) < 3:
            return {"status": "insufficient_data", "samples": len(samples)}

        first = samples[0]
        last = samples[-1]
        elapsed_hours = (last.sampled_at - first.sampled_at) / 3600.0
        if elapsed_hours < 0.01:
            return {"status": "insufficient_time", "elapsed_hours": elapsed_hours}

        rss_growth = (last.rss_mb - first.rss_mb) / max(elapsed_hours, 0.01)
        fd_growth = (last.open_fds - first.open_fds) / max(elapsed_hours, 0.01)

        alerts = []
        if rss_growth > self._rss_threshold:
            alerts.append(f"memory_leak: RSS growing at {rss_growth:.1f} MB/hour (threshold {self._rss_threshold})")
        if fd_growth > self._fd_threshold:
            alerts.append(f"fd_leak: open FDs growing at {fd_growth:.1f}/hour (threshold {self._fd_threshold})")

        return {
            "status": "leak_detected" if alerts else "ok",
            "alerts": alerts,
            "rss_growth_mb_per_hour": round(rss_growth, 2),
            "fd_growth_per_hour": round(fd_growth, 2),
            "elapsed_hours": round(elapsed_hours, 2),
            "rss_start_mb": first.rss_mb,
            "rss_end_mb": last.rss_mb,
        }
```

## Solution 5: Periodic Resource Sampler

```python
import asyncio
import time
from typing import Callable, Optional


class PeriodicResourceSamplingTask:
    """
    Background asyncio task that samples resource metrics on a fixed interval
    and records them to the store. Calls an alert callback when thresholds are breached.
    """

    def __init__(
        self,
        sampler: AgentResourceSampler,
        store: ResourceSampleStore,
        detector: ResourceLeakDetector,
        interval_seconds: float = 30.0,
        alert_fn: Optional[Callable[[dict], None]] = None,
    ):
        self._sampler = sampler
        self._store = store
        self._detector = detector
        self._interval = interval_seconds
        self._alert_fn = alert_fn
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            try:
                sample = self._sampler.sample()
                self._store.record(sample)
                analysis = self._detector.analyze(self._store)
                if analysis.get("status") == "leak_detected" and self._alert_fn:
                    self._alert_fn(analysis)
            except Exception:
                pass
            await asyncio.sleep(self._interval)
```

## Solution 6: Resource Usage Dashboard

```python
import time


class AgentResourceUsageDashboard:
    """
    Combines latest sample, windowed summary, and leak detection
    into a single operational resource health report.
    """

    def __init__(
        self,
        store: ResourceSampleStore,
        detector: ResourceLeakDetector,
    ):
        self._store = store
        self._detector = detector

    def render(self) -> dict:
        latest = self._store.latest()
        summary = self._store.summary(window_seconds=3600.0)
        leak_analysis = self._detector.analyze(self._store, window_seconds=3600.0)
        return {
            "generated_at": time.time(),
            "latest_sample": {
                "rss_mb": latest.rss_mb if latest else None,
                "cpu_pct": latest.cpu_percent if latest else None,
                "open_fds": latest.open_fds if latest else None,
                "threads": latest.threads if latest else None,
            },
            "last_hour_summary": summary,
            "leak_detection": leak_analysis,
        }
```

## Comparison

| Approach | CPU Sampling | Memory Sampling | FD Sampling | Leak Detection | Periodic Background Task |
|---|---|---|---|---|---|
| AgentResourceSampler | Yes (delta) | Yes (RSS+VMS) | Yes | No | No |
| ResourceSampleStore | No | No | No | No | No |
| ResourceLeakDetector | No | Yes (growth rate) | Yes (growth rate) | Yes | No |
| PeriodicResourceSamplingTask | Via sampler | Via sampler | Via sampler | Via detector | Yes |
| AgentResourceUsageDashboard | No | No | No | No | No |

**Best for production**: Sample at `interval_seconds=30` — this provides 2880 samples per day with a 24-hour retention window, sufficient for trend detection without storage overhead. Set `rss_growth_threshold_mb_per_hour=50` conservatively — a healthy long-running agent may grow 10–20 MB/hour due to Python interpreter overhead; growth beyond 50 MB/hour consistently indicates a leak. Alert on `fd_growth_per_hour > 100` — file descriptor leaks from unclosed HTTP connections or database cursors tend to grow linearly and are exploitable by attackers who can exhaust the FD limit. Use `psutil` when available; it provides more accurate CPU measurement than the /proc fallback by using cumulative CPU time deltas.
