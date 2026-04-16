---
title: "Agent Doesn't Implement Queue Depth Monitoring for Async Task Processing"
description: "AI agents that process tasks from an async queue without monitoring queue depth have no early warning before consumer lag turns into user-visible latency. Queue depth monitoring tracks pending task count, consumer throughput, and lag growth rate — alerting before the queue grows large enough to cause timeouts, and autoscaling when sustained depth exceeds a threshold."
date: 2025-02-15
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-queue-depth-monitoring-for-async-task-processing
tags:
  - queue-depth
  - consumer-lag
  - async-processing
  - monitoring
  - autoscaling
  - reliability
  - backpressure
symptoms:
  - "Users experience 10-minute delays while the queue grows to 50,000 tasks"
  - "No alert fires when consumer falls behind producer — lag is discovered by users"
  - "Queue depth is unknown; capacity planning is done by guessing"
  - "Consumer crashes silently; queue grows indefinitely with no detection"
  - "Spike in task submission causes queue to grow 10× with no autoscaling trigger"
---

## Problem

An async task queue without depth monitoring is a blindspot: consumer crashes, producer spikes, and slow external dependencies all manifest as queue growth that is invisible until users complain. Queue depth monitoring measures three things continuously: absolute depth (how many tasks are waiting), throughput delta (is depth growing or shrinking), and estimated drain time (at current throughput, how long until the queue is empty). Alerts fire on absolute thresholds and on growth rate; autoscaling fires when drain time exceeds a deadline.

---

## Solution 1: QueueDepthMonitor — Real-Time Depth and Lag Tracking

```python
import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class QueueSnapshot:
    depth: int
    throughput_per_s: float    # tasks completed per second (rolling)
    drain_time_s: Optional[float]  # None if throughput == 0
    growth_rate: float         # tasks/s added to queue (positive = growing)
    timestamp: float = field(default_factory=time.time)


class QueueDepthMonitor:
    """
    Monitors an asyncio.Queue (or any object with .qsize()) and tracks
    depth, throughput, drain time, and growth rate over a rolling window.

    Usage:
        queue = asyncio.Queue()
        monitor = QueueDepthMonitor(queue, window_s=60.0)
        asyncio.create_task(monitor.run(interval_s=5.0))

        # In your consumer:
        monitor.record_completion()

        # Alerts:
        monitor.on_depth_alert = lambda snap: send_pagerduty(snap)
    """

    def __init__(self, queue,
                 window_s: float = 60.0,
                 depth_alert_threshold: int = 1000,
                 drain_time_alert_s: float = 300.0):
        self._queue = queue
        self._window = window_s
        self._depth_alert = depth_alert_threshold
        self._drain_alert = drain_time_alert_s
        self._completions: Deque[float] = deque()
        self._depth_history: Deque[tuple] = deque()  # (ts, depth)
        self.on_depth_alert: Optional[Callable] = None
        self._alerted_at: Dict[str, float] = {}

    def record_completion(self, n: int = 1):
        now = time.monotonic()
        for _ in range(n):
            self._completions.append(now)

    def snapshot(self) -> QueueSnapshot:
        now = time.monotonic()
        cutoff = now - self._window

        # Evict old completions
        while self._completions and self._completions[0] < cutoff:
            self._completions.popleft()

        depth = self._queue.qsize() if hasattr(self._queue, "qsize") else 0
        throughput = len(self._completions) / self._window

        drain = depth / throughput if throughput > 0 else None

        # Growth rate: compare depth now vs depth `window_s` ago
        self._depth_history.append((now, depth))
        while self._depth_history and self._depth_history[0][0] < cutoff:
            self._depth_history.popleft()

        if len(self._depth_history) >= 2:
            oldest_ts, oldest_depth = self._depth_history[0]
            elapsed = now - oldest_ts
            growth_rate = (depth - oldest_depth) / elapsed if elapsed > 0 else 0.0
        else:
            growth_rate = 0.0

        return QueueSnapshot(
            depth=depth,
            throughput_per_s=round(throughput, 2),
            drain_time_s=round(drain, 1) if drain is not None else None,
            growth_rate=round(growth_rate, 2),
        )

    async def run(self, interval_s: float = 5.0):
        while True:
            await asyncio.sleep(interval_s)
            snap = self.snapshot()
            self._evaluate(snap)
            logger.debug(
                "queue_depth=%d throughput=%.1f/s drain=%s growth=%.1f/s",
                snap.depth, snap.throughput_per_s,
                f"{snap.drain_time_s:.0f}s" if snap.drain_time_s else "∞",
                snap.growth_rate,
            )

    def _evaluate(self, snap: QueueSnapshot):
        now = time.monotonic()
        cb = self.on_depth_alert or (lambda s: logger.warning("QUEUE ALERT: %s", s))

        if snap.depth > self._depth_alert:
            self._fire("depth", snap, cb, now)
        if snap.drain_time_s and snap.drain_time_s > self._drain_alert:
            self._fire("drain_time", snap, cb, now)
        if snap.growth_rate > 10:
            self._fire("growth_rate", snap, cb, now)

    def _fire(self, key: str, snap: QueueSnapshot, cb: Callable, now: float):
        if now - self._alerted_at.get(key, 0) > 300:
            self._alerted_at[key] = now
            cb(snap)
```

---

## Solution 2: MultiQueueDepthDashboard — Monitor Multiple Queues

```python
import asyncio
import time
from typing import Any, Dict, List


class MultiQueueDepthDashboard:
    """
    Aggregates depth monitoring across multiple named queues.
    Surfaces per-queue and aggregate statistics for dashboards.

    Usage:
        dashboard = MultiQueueDepthDashboard()
        dashboard.register("high_priority", hp_queue, alert_threshold=100)
        dashboard.register("default", default_queue, alert_threshold=1000)
        dashboard.register("batch", batch_queue, alert_threshold=10000)

        asyncio.create_task(dashboard.run(interval_s=10.0))
        report = dashboard.report()
    """

    def __init__(self):
        self._monitors: Dict[str, QueueDepthMonitor] = {}

    def register(self, name: str, queue,
                  alert_threshold: int = 1000,
                  drain_time_alert_s: float = 300.0):
        self._monitors[name] = QueueDepthMonitor(
            queue,
            depth_alert_threshold=alert_threshold,
            drain_time_alert_s=drain_time_alert_s,
        )

    def record_completion(self, queue_name: str, n: int = 1):
        if queue_name in self._monitors:
            self._monitors[queue_name].record_completion(n)

    async def run(self, interval_s: float = 10.0):
        while True:
            await asyncio.sleep(interval_s)
            for name, monitor in self._monitors.items():
                snap = monitor.snapshot()
                monitor._evaluate(snap)

    def report(self) -> Dict[str, Any]:
        return {
            name: {
                "depth": snap.depth,
                "throughput_per_s": snap.throughput_per_s,
                "drain_time_s": snap.drain_time_s,
                "growth_rate": snap.growth_rate,
            }
            for name, monitor in self._monitors.items()
            for snap in [monitor.snapshot()]
        }

    def total_depth(self) -> int:
        return sum(
            m.snapshot().depth for m in self._monitors.values()
        )
```

---

## Solution 3: AdaptiveConsumerScaler — Scale Workers Based on Queue Depth

```python
import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class AdaptiveConsumerScaler:
    """
    Spawns and terminates consumer tasks based on queue depth.
    Scales up when depth exceeds scale_up_threshold;
    scales down when depth drops below scale_down_threshold.

    Usage:
        scaler = AdaptiveConsumerScaler(
            queue=task_queue,
            consumer_fn=process_task,
            min_workers=2,
            max_workers=20,
            scale_up_threshold=50,
            scale_down_threshold=5,
        )
        await scaler.run()
    """

    def __init__(self, queue: asyncio.Queue,
                 consumer_fn: Callable,
                 min_workers: int = 2,
                 max_workers: int = 20,
                 scale_up_threshold: int = 50,
                 scale_down_threshold: int = 5,
                 check_interval_s: float = 10.0):
        self._queue = queue
        self._consumer_fn = consumer_fn
        self._min = min_workers
        self._max = max_workers
        self._up_thresh = scale_up_threshold
        self._down_thresh = scale_down_threshold
        self._interval = check_interval_s
        self._workers: Set[asyncio.Task] = set()
        self._monitor = QueueDepthMonitor(queue)

    async def run(self):
        # Start minimum workers
        for _ in range(self._min):
            self._spawn_worker()
        asyncio.create_task(self._monitor.run(self._interval))

        while True:
            await asyncio.sleep(self._interval)
            snap = self._monitor.snapshot()
            current = len(self._workers)

            if snap.depth > self._up_thresh and current < self._max:
                needed = min(
                    self._max - current,
                    max(1, snap.depth // self._up_thresh),
                )
                for _ in range(needed):
                    self._spawn_worker()
                logger.info(
                    "scale_up workers=%d->%d depth=%d",
                    current, current + needed, snap.depth,
                )
            elif snap.depth < self._down_thresh and current > self._min:
                # Workers will exit when queue is drained — just stop spawning

            # Clean up finished workers
            self._workers = {w for w in self._workers if not w.done()}

    def _spawn_worker(self):
        task = asyncio.create_task(self._worker_loop())
        self._workers.add(task)

    async def _worker_loop(self):
        while True:
            try:
                item = await asyncio.wait_for(
                    self._queue.get(), timeout=30.0
                )
            except asyncio.TimeoutError:
                break  # Idle timeout — worker exits
            try:
                await self._consumer_fn(item)
                self._monitor.record_completion()
            except Exception as exc:
                logger.error("consumer_error item=%s error=%s", item, exc)
            finally:
                self._queue.task_done()

    def worker_count(self) -> int:
        return len(self._workers)
```

---

## Solution 4: QueueDepthPrometheusExporter — Metrics Integration

```python
import time
from typing import Any, Dict


class QueueDepthPrometheusExporter:
    """
    Exports queue depth metrics in Prometheus text format.
    Mount as a /metrics endpoint or scrape from a push gateway.

    Usage:
        exporter = QueueDepthPrometheusExporter(dashboard)
        metrics_text = exporter.render()

        @app.get("/metrics")
        def metrics():
            return Response(exporter.render(), media_type="text/plain")
    """

    def __init__(self, dashboard: MultiQueueDepthDashboard):
        self._dashboard = dashboard

    def render(self) -> str:
        report = self._dashboard.report()
        lines = [
            "# HELP agent_queue_depth Number of pending tasks in queue",
            "# TYPE agent_queue_depth gauge",
            "# HELP agent_queue_throughput Tasks completed per second",
            "# TYPE agent_queue_throughput gauge",
            "# HELP agent_queue_drain_time_seconds Estimated drain time",
            "# TYPE agent_queue_drain_time_seconds gauge",
            "# HELP agent_queue_growth_rate Tasks added per second",
            "# TYPE agent_queue_growth_rate gauge",
        ]
        for name, stats in report.items():
            label = f'queue="{name}"'
            lines.append(f'agent_queue_depth{{{label}}} {stats["depth"]}')
            lines.append(f'agent_queue_throughput{{{label}}} {stats["throughput_per_s"]}')
            drain = stats["drain_time_s"]
            lines.append(
                f'agent_queue_drain_time_seconds{{{label}}} '
                f'{drain if drain is not None else "Inf"}'
            )
            lines.append(f'agent_queue_growth_rate{{{label}}} {stats["growth_rate"]}')
        return "\n".join(lines) + "\n"
```

---

## Solution 5: QueueHealthGate — Block Submissions When Queue Is Unhealthy

```python
import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class QueueHealthGate:
    """
    Rejects new task submissions when the queue is at capacity or
    drain time exceeds a threshold. Prevents unbounded queue growth
    by applying back-pressure to producers.

    Usage:
        gate = QueueHealthGate(
            queue=task_queue,
            monitor=monitor,
            max_depth=5000,
            max_drain_time_s=120,
        )

        ok = await gate.submit(task)
        if not ok:
            return {"error": "service_overloaded", "retry_after": 30}
    """

    def __init__(self, queue: asyncio.Queue,
                 monitor: QueueDepthMonitor,
                 max_depth: int = 5000,
                 max_drain_time_s: float = 120.0):
        self._queue = queue
        self._monitor = monitor
        self._max_depth = max_depth
        self._max_drain = max_drain_time_s
        self._rejected = 0

    def is_healthy(self) -> bool:
        snap = self._monitor.snapshot()
        if snap.depth >= self._max_depth:
            return False
        if snap.drain_time_s and snap.drain_time_s > self._max_drain:
            return False
        return True

    async def submit(self, task) -> bool:
        if not self.is_healthy():
            self._rejected += 1
            logger.warning(
                "queue_gate_reject task=%s rejected_total=%d",
                task, self._rejected,
            )
            return False
        await self._queue.put(task)
        return True

    def rejected_count(self) -> int:
        return self._rejected
```

---

## Solution 6: QueueDepthAlerter — Runbook-Linked Alerting

```python
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class QueueDepthAlerter:
    """
    Fires structured alerts with runbook links when queue depth thresholds
    are crossed. Supports escalating severity: WARNING -> CRITICAL.

    Usage:
        alerter = QueueDepthAlerter(
            on_warning=send_slack,
            on_critical=send_pagerduty,
            warning_depth=500,
            critical_depth=2000,
        )
        monitor.on_depth_alert = alerter.handle
    """

    def __init__(self, on_warning=None, on_critical=None,
                 warning_depth: int = 500,
                 critical_depth: int = 2000,
                 cooldown_s: float = 300.0):
        self._warn = on_warning or (lambda m: logger.warning(m))
        self._crit = on_critical or (lambda m: logger.critical(m))
        self._warn_thresh = warning_depth
        self._crit_thresh = critical_depth
        self._cooldown = cooldown_s
        self._last_warn = 0.0
        self._last_crit = 0.0

    def handle(self, snap: QueueSnapshot):
        now = time.time()
        if snap.depth >= self._crit_thresh:
            if now - self._last_crit > self._cooldown:
                self._last_crit = now
                msg = self._format(snap, "CRITICAL")
                self._crit(msg)
        elif snap.depth >= self._warn_thresh:
            if now - self._last_warn > self._cooldown:
                self._last_warn = now
                msg = self._format(snap, "WARNING")
                self._warn(msg)

    def _format(self, snap: QueueSnapshot, severity: str) -> str:
        return (
            f"[{severity}] Queue depth={snap.depth} "
            f"throughput={snap.throughput_per_s:.1f}/s "
            f"drain_time={snap.drain_time_s or 'never'}s "
            f"growth_rate={snap.growth_rate:+.1f}/s"
        )
```

---

## Comparison

| Approach | Depth | Throughput | Drain Time | Alerting | Autoscaling | Backpressure |
|---|---|---|---|---|---|---|
| **QueueDepthMonitor** | Yes | Yes | Yes | Yes | No | No |
| **MultiQueueDepthDashboard** | Yes | Yes | Yes | Partial | No | No |
| **AdaptiveConsumerScaler** | Via monitor | Yes | No | No | Yes | No |
| **QueueDepthPrometheusExporter** | Yes | Yes | Yes | No | No | No |
| **QueueHealthGate** | Yes | Via monitor | Yes | No | No | Yes |
| **QueueDepthAlerter** | Yes | No | Partial | Yes | No | No |

**Key insight**: instrument three metrics — depth, throughput (tasks/s), and estimated drain time. Depth alone is misleading: a queue with depth 5,000 is healthy if throughput is 500/s (10-second drain) but critical if throughput is 5/s (1,000-second drain). Alert on drain time > 5 minutes as the primary signal, and use depth only as a secondary cap. Add `QueueHealthGate` to prevent unbounded queue growth under producer spikes — a bounded queue that rejects submissions is always preferable to an unbounded queue that causes OOM.
