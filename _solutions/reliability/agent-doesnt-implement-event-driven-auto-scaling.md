---
title: "Agent Doesn't Implement Event-Driven Auto-Scaling"
description: "Static worker pools either over-provision (wasting cost during low traffic) or under-provision (dropping requests during spikes). Event-driven auto-scaling adjusts capacity continuously based on queue depth, latency, and error signals."
difficulty: advanced
category: reliability
tags: [autoscaling, workers, queue, scaling, reliability, kubernetes, load]
---

## Problem

An agent fleet runs a fixed number of worker processes. During a traffic spike, the queue fills, latency spikes, and requests time out. During off-peak hours, idle workers consume resources needlessly. Manual scaling requires human intervention and lags the actual load curve by minutes.

```python
# Broken: fixed worker count, no scaling signals
workers = [asyncio.create_task(worker_loop()) for _ in range(4)]
# 4 workers = always: too few at peak, too many at 3am
```

---

## Solution 1: Queue-Depth-Based Scaling Controller

```python
import asyncio
import time
from dataclasses import dataclass, field

@dataclass
class ScalingConfig:
    min_workers: int = 2
    max_workers: int = 20
    scale_up_threshold: int = 10    # queue depth per worker
    scale_down_threshold: int = 2   # queue depth per worker
    scale_up_cooldown: float = 30.0
    scale_down_cooldown: float = 120.0
    step_up: int = 2    # workers to add per scale-up event
    step_down: int = 1  # workers to remove per scale-down event

class WorkerPool:
    def __init__(self, worker_fn, config: ScalingConfig = ScalingConfig()):
        self._worker_fn = worker_fn
        self._config = config
        self._workers: dict[int, asyncio.Task] = {}
        self._worker_id_counter = 0
        self._last_scale_up = 0.0
        self._last_scale_down = 0.0
        self._lock = asyncio.Lock()

    async def initialize(self):
        async with self._lock:
            for _ in range(self._config.min_workers):
                await self._spawn_worker()

    async def _spawn_worker(self) -> int:
        wid = self._worker_id_counter
        self._worker_id_counter += 1
        task = asyncio.create_task(self._worker_fn(wid))
        task.add_done_callback(lambda t: self._workers.pop(wid, None))
        self._workers[wid] = task
        print(f"[Scale] Worker {wid} spawned. Total: {len(self._workers)}")
        return wid

    async def _stop_worker(self) -> bool:
        if len(self._workers) <= self._config.min_workers:
            return False
        wid = next(iter(self._workers))
        task = self._workers.pop(wid)
        task.cancel()
        print(f"[Scale] Worker {wid} stopped. Total: {len(self._workers)}")
        return True

    async def scale_up(self, n: int = 1) -> int:
        now = time.monotonic()
        if now - self._last_scale_up < self._config.scale_up_cooldown:
            return 0
        async with self._lock:
            spawned = 0
            for _ in range(n):
                if len(self._workers) >= self._config.max_workers:
                    break
                await self._spawn_worker()
                spawned += 1
            if spawned:
                self._last_scale_up = now
        return spawned

    async def scale_down(self, n: int = 1) -> int:
        now = time.monotonic()
        if now - self._last_scale_down < self._config.scale_down_cooldown:
            return 0
        async with self._lock:
            stopped = 0
            for _ in range(n):
                if not await self._stop_worker():
                    break
                stopped += 1
            if stopped:
                self._last_scale_down = now
        return stopped

    @property
    def worker_count(self) -> int:
        return len(self._workers)

async def queue_depth_autoscaler(
    pool: WorkerPool,
    queue: asyncio.Queue,
    config: ScalingConfig,
    check_interval: float = 10.0
):
    """
    Scales workers based on queue depth per worker.
    High queue depth → scale up; low queue depth → scale down.
    """
    while True:
        await asyncio.sleep(check_interval)
        depth = queue.qsize()
        workers = pool.worker_count
        depth_per_worker = depth / max(workers, 1)

        if depth_per_worker > config.scale_up_threshold:
            added = await pool.scale_up(config.step_up)
            if added:
                print(f"[Autoscaler] Scale UP: depth={depth}, "
                      f"workers={workers}→{pool.worker_count}")
        elif depth_per_worker < config.scale_down_threshold and workers > config.min_workers:
            removed = await pool.scale_down(config.step_down)
            if removed:
                print(f"[Autoscaler] Scale DOWN: depth={depth}, "
                      f"workers={workers}→{pool.worker_count}")
```

---

## Solution 2: Multi-Signal Scaling (Queue + Latency + Error Rate)

```python
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

@dataclass
class ScalingSignal:
    queue_depth: int
    p95_latency_ms: float
    error_rate_1m: float
    worker_count: int

class MultiSignalScaler:
    """
    Combines queue depth, tail latency, and error rate into a single
    scaling decision. Any signal being critical triggers scale-up.
    All signals must be healthy for scale-down.
    """

    def __init__(self,
                 latency_warn_ms: float = 3000.0,
                 latency_critical_ms: float = 8000.0,
                 error_warn: float = 0.05,
                 error_critical: float = 0.15,
                 queue_per_worker_warn: int = 5,
                 queue_per_worker_critical: int = 20):
        self._lat_warn = latency_warn_ms
        self._lat_crit = latency_critical_ms
        self._err_warn = error_warn
        self._err_crit = error_critical
        self._q_warn = queue_per_worker_warn
        self._q_crit = queue_per_worker_critical

    def evaluate(self, signal: ScalingSignal) -> tuple[str, str]:
        """Returns (action, reason). action: 'up', 'down', or 'hold'."""
        q_per_worker = signal.queue_depth / max(signal.worker_count, 1)
        reasons_up: list[str] = []
        reasons_down_blocked: list[str] = []

        # Critical conditions → always scale up
        if q_per_worker > self._q_crit:
            reasons_up.append(f"queue critical ({q_per_worker:.1f}/worker)")
        if signal.p95_latency_ms > self._lat_crit:
            reasons_up.append(f"latency critical ({signal.p95_latency_ms:.0f}ms)")
        if signal.error_rate_1m > self._err_crit:
            reasons_up.append(f"error rate critical ({signal.error_rate_1m:.1%})")

        if reasons_up:
            return "up", "; ".join(reasons_up)

        # Warn conditions → scale up if multiple warn
        warn_count = sum([
            q_per_worker > self._q_warn,
            signal.p95_latency_ms > self._lat_warn,
            signal.error_rate_1m > self._err_warn,
        ])
        if warn_count >= 2:
            return "up", f"{warn_count} warn signals active"

        # Check if it's safe to scale down
        if q_per_worker > self._q_warn:
            reasons_down_blocked.append(f"queue elevated ({q_per_worker:.1f}/worker)")
        if signal.p95_latency_ms > self._lat_warn:
            reasons_down_blocked.append(f"latency elevated ({signal.p95_latency_ms:.0f}ms)")
        if signal.error_rate_1m > self._err_warn:
            reasons_down_blocked.append(f"error rate elevated ({signal.error_rate_1m:.1%})")

        if reasons_down_blocked:
            return "hold", "; ".join(reasons_down_blocked)

        return "down", "all signals healthy"

class LatencyTracker:
    """Rolling p95 latency tracker."""

    def __init__(self, window_size: int = 100):
        self._samples: deque[float] = deque(maxlen=window_size)

    def record(self, latency_ms: float):
        self._samples.append(latency_ms)

    def p95(self) -> float:
        if not self._samples:
            return 0.0
        sorted_samples = sorted(self._samples)
        idx = int(len(sorted_samples) * 0.95)
        return sorted_samples[min(idx, len(sorted_samples) - 1)]

async def multi_signal_autoscaler(
    pool: "WorkerPool",
    queue: asyncio.Queue,
    latency_tracker: LatencyTracker,
    error_tracker: "ErrorRateTracker",
    scaler: MultiSignalScaler,
    check_interval: float = 15.0
):
    while True:
        await asyncio.sleep(check_interval)
        signal = ScalingSignal(
            queue_depth=queue.qsize(),
            p95_latency_ms=latency_tracker.p95(),
            error_rate_1m=error_tracker.rate_1m(),
            worker_count=pool.worker_count,
        )
        action, reason = scaler.evaluate(signal)
        print(f"[MultiScaler] action={action} reason={reason} "
              f"workers={pool.worker_count}")
        if action == "up":
            await pool.scale_up(2)
        elif action == "down":
            await pool.scale_down(1)

class ErrorRateTracker:
    def __init__(self):
        self._events: deque[tuple[float, bool]] = deque()

    def record(self, is_error: bool):
        self._events.append((time.monotonic(), is_error))

    def rate_1m(self) -> float:
        cutoff = time.monotonic() - 60.0
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()
        if not self._events:
            return 0.0
        errors = sum(1 for _, e in self._events if e)
        return errors / len(self._events)
```

---

## Solution 3: Predictive Scaling with Time-of-Day Patterns

```python
import asyncio
import time
import math
from dataclasses import dataclass, field

@dataclass
class TrafficPattern:
    """Describes expected traffic by hour of day (0-23)."""
    # Relative load factor per hour (1.0 = baseline)
    hourly_factors: list[float] = field(default_factory=lambda: [1.0] * 24)

    def expected_factor(self, hour: int | None = None) -> float:
        if hour is None:
            import datetime
            hour = datetime.datetime.now().hour
        return self.hourly_factors[hour % 24]

    @classmethod
    def business_hours(cls) -> "TrafficPattern":
        """Typical 9-5 business hours pattern."""
        factors = [0.2] * 24
        for h in range(9, 18):   # 9am-6pm: high load
            factors[h] = 1.0
        for h in range(7, 9):    # 7-9am ramp up
            factors[h] = 0.5
        for h in range(18, 20):  # 6-8pm ramp down
            factors[h] = 0.5
        return cls(hourly_factors=factors)

class PredictiveScaler:
    """
    Pre-scales workers based on predicted load before traffic arrives.
    Combines pattern-based prediction with reactive signal adjustments.
    """

    def __init__(self,
                 pattern: TrafficPattern,
                 base_workers: int = 4,
                 min_workers: int = 2,
                 max_workers: int = 20,
                 scale_ahead_minutes: int = 5):
        self._pattern = pattern
        self._base = base_workers
        self._min = min_workers
        self._max = max_workers
        self._ahead_minutes = scale_ahead_minutes

    def predicted_workers(self) -> int:
        import datetime
        future_time = datetime.datetime.now() + datetime.timedelta(
            minutes=self._ahead_minutes
        )
        factor = self._pattern.expected_factor(future_time.hour)
        predicted = int(self._base * factor)
        return max(self._min, min(self._max, predicted))

    def reactive_adjustment(self, predicted: int,
                             queue_depth: int,
                             p95_latency_ms: float) -> int:
        """Adjust predicted count based on real-time signals."""
        adjusted = predicted
        if queue_depth > predicted * 5:  # severe queue buildup
            adjusted = min(self._max, predicted + 4)
        elif p95_latency_ms > 5000:
            adjusted = min(self._max, predicted + 2)
        return adjusted

    async def scaling_loop(self, pool: "WorkerPool",
                           queue: asyncio.Queue,
                           latency_tracker: "LatencyTracker",
                           interval: float = 60.0):
        while True:
            await asyncio.sleep(interval)
            predicted = self.predicted_workers()
            adjusted = self.reactive_adjustment(
                predicted, queue.qsize(), latency_tracker.p95()
            )
            current = pool.worker_count
            if adjusted > current:
                await pool.scale_up(adjusted - current)
            elif adjusted < current:
                await pool.scale_down(current - adjusted)
            print(f"[PredictiveScaler] predicted={predicted} "
                  f"adjusted={adjusted} current={pool.worker_count}")
```

---

## Solution 4: Token-Bucket Rate Limiter with Dynamic Capacity

```python
import asyncio
import time
from dataclasses import dataclass

@dataclass
class DynamicCapacityConfig:
    initial_rate: float = 100.0    # requests/second
    min_rate: float = 10.0
    max_rate: float = 1000.0
    increase_factor: float = 1.1   # grow by 10% on success
    decrease_factor: float = 0.7   # shrink by 30% on overload

class AdaptiveRateLimiter:
    """
    Rate limiter that adjusts its capacity based on observed success/failure.
    Successful processing → gradually increase rate.
    Errors or timeouts → rapidly decrease rate.
    Drives auto-scaling by signaling when to add/remove workers.
    """

    def __init__(self, config: DynamicCapacityConfig = DynamicCapacityConfig()):
        self._cfg = config
        self._rate = config.initial_rate
        self._tokens = config.initial_rate
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()
        self._total_allowed = 0
        self._total_rejected = 0

    async def acquire(self) -> bool:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
            self._last_refill = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                self._total_allowed += 1
                return True
            self._total_rejected += 1
            return False

    async def record_success(self):
        async with self._lock:
            self._rate = min(
                self._cfg.max_rate,
                self._rate * self._cfg.increase_factor
            )

    async def record_overload(self):
        async with self._lock:
            self._rate = max(
                self._cfg.min_rate,
                self._rate * self._cfg.decrease_factor
            )
        print(f"[RateLimiter] Overload detected. Rate reduced to {self._rate:.1f}/s")

    def recommended_workers(self, avg_task_duration_s: float = 1.0) -> int:
        """Estimate worker count needed to sustain current rate."""
        return max(1, int(self._rate * avg_task_duration_s) + 1)

    def stats(self) -> dict:
        return {
            "current_rate": round(self._rate, 1),
            "allowed": self._total_allowed,
            "rejected": self._total_rejected,
            "rejection_rate": round(
                self._total_rejected / max(1, self._total_allowed + self._total_rejected), 3
            )
        }
```

---

## Solution 5: Kubernetes HPA-Compatible Custom Metric Exporter

```python
import asyncio
import json
import time
from aiohttp import web

class CustomMetricServer:
    """
    Exposes custom scaling metrics in a format compatible with
    Kubernetes Custom Metrics API (used by HPA for pod autoscaling).
    Endpoints:
      GET /metrics/queue_depth → {"value": 42}
      GET /metrics/latency_p95 → {"value": 1250.0}
      GET /metrics/requests_per_second → {"value": 87.3}
    """

    def __init__(self,
                 queue: asyncio.Queue,
                 latency_tracker: "LatencyTracker",
                 error_tracker: "ErrorRateTracker",
                 port: int = 8081):
        self._queue = queue
        self._latency = latency_tracker
        self._errors = error_tracker
        self._port = port
        self._rps_tracker = RPSTracker()

    def record_request(self, latency_ms: float, is_error: bool = False):
        self._latency.record(latency_ms)
        self._errors.record(is_error)
        self._rps_tracker.record()

    async def handle_queue_depth(self, request: web.Request) -> web.Response:
        return web.json_response({"value": self._queue.qsize()})

    async def handle_latency_p95(self, request: web.Request) -> web.Response:
        return web.json_response({"value": self._latency.p95()})

    async def handle_rps(self, request: web.Request) -> web.Response:
        return web.json_response({"value": self._rps_tracker.rate()})

    async def handle_error_rate(self, request: web.Request) -> web.Response:
        return web.json_response({"value": round(self._errors.rate_1m(), 4)})

    async def handle_combined(self, request: web.Request) -> web.Response:
        """Combined health/scaling metric for HPA external metrics."""
        # Composite scaling score: higher = more workers needed
        queue_pressure = min(10.0, self._queue.qsize() / 10.0)
        latency_pressure = min(10.0, self._latency.p95() / 1000.0)
        error_pressure = self._errors.rate_1m() * 10.0
        scaling_score = queue_pressure + latency_pressure + error_pressure
        return web.json_response({"value": round(scaling_score, 2)})

    async def start(self):
        app = web.Application()
        app.router.add_get("/metrics/queue_depth", self.handle_queue_depth)
        app.router.add_get("/metrics/latency_p95", self.handle_latency_p95)
        app.router.add_get("/metrics/requests_per_second", self.handle_rps)
        app.router.add_get("/metrics/error_rate", self.handle_error_rate)
        app.router.add_get("/metrics/scaling_score", self.handle_combined)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self._port)
        await site.start()
        print(f"[MetricServer] Listening on :{self._port}")

class RPSTracker:
    def __init__(self, window: float = 60.0):
        from collections import deque
        self._timestamps: "deque[float]" = __import__("collections").deque()
        self._window = window

    def record(self):
        now = time.monotonic()
        self._timestamps.append(now)
        while self._timestamps and self._timestamps[0] < now - self._window:
            self._timestamps.popleft()

    def rate(self) -> float:
        return len(self._timestamps) / self._window

# Kubernetes HPA YAML (for reference — placed in k8s manifests)
K8S_HPA_YAML = """
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: agent-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: agent
  minReplicas: 2
  maxReplicas: 20
  metrics:
  - type: External
    external:
      metric:
        name: agent_scaling_score
      target:
        type: AverageValue
        averageValue: "5"  # scale out when score > 5
  behavior:
    scaleUp:
      stabilizationWindowSeconds: 30
      policies:
      - type: Pods
        value: 4
        periodSeconds: 60
    scaleDown:
      stabilizationWindowSeconds: 120
      policies:
      - type: Pods
        value: 1
        periodSeconds: 60
"""
```

---

## Solution 6: Graceful Scale-Down with In-Flight Draining

```python
import asyncio
import time
from dataclasses import dataclass, field

@dataclass
class DrainableWorker:
    worker_id: int
    task: asyncio.Task
    in_flight: int = 0
    draining: bool = False

class GracefulWorkerPool:
    """
    Worker pool that drains in-flight requests before stopping a worker.
    Scale-down only removes workers with zero active requests.
    """

    def __init__(self, worker_factory,
                 drain_timeout: float = 30.0):
        self._factory = worker_factory
        self._drain_timeout = drain_timeout
        self._workers: dict[int, DrainableWorker] = {}
        self._id_counter = 0
        self._lock = asyncio.Lock()

    async def spawn(self) -> int:
        async with self._lock:
            wid = self._id_counter
            self._id_counter += 1
            task = asyncio.create_task(self._factory(wid, self))
            dw = DrainableWorker(worker_id=wid, task=task)
            self._workers[wid] = dw
            print(f"[Pool] Worker {wid} spawned. Count: {len(self._workers)}")
            return wid

    def mark_busy(self, worker_id: int):
        if worker_id in self._workers:
            self._workers[worker_id].in_flight += 1

    def mark_idle(self, worker_id: int):
        if worker_id in self._workers:
            self._workers[worker_id].in_flight = max(0,
                self._workers[worker_id].in_flight - 1)
            # If draining and now idle, cancel immediately
            dw = self._workers[worker_id]
            if dw.draining and dw.in_flight == 0:
                dw.task.cancel()

    async def scale_down_gracefully(self, n: int = 1) -> int:
        removed = 0
        async with self._lock:
            # Pick workers that are currently idle (no in-flight requests)
            idle_workers = [
                dw for dw in self._workers.values()
                if not dw.draining and dw.in_flight == 0
            ]
            for dw in idle_workers[:n]:
                dw.draining = True
                dw.task.cancel()
                del self._workers[dw.worker_id]
                removed += 1
                print(f"[Pool] Worker {dw.worker_id} removed (was idle). "
                      f"Count: {len(self._workers)}")

        # For workers that still have in-flight requests, wait for drain
        async with self._lock:
            busy_candidates = [
                dw for dw in list(self._workers.values())
                if not dw.draining
            ][:max(0, n - removed)]
            for dw in busy_candidates:
                dw.draining = True

        return removed

    @property
    def worker_count(self) -> int:
        return len(self._workers)

    def busy_count(self) -> int:
        return sum(1 for dw in self._workers.values() if dw.in_flight > 0)
```

---

## Comparison

| Solution | Scaling Signal | Prediction | Graceful Drain | K8s Compatible | Complexity | Best For |
|---|---|---|---|---|---|---|
| 1. Queue depth | Queue only | No | No | No | Low | Simple queue-based agents |
| 2. Multi-signal | Queue + latency + error | No | No | No | Med | Production agents |
| 3. Predictive | Time-of-day + reactive | Yes | No | No | Med | Known traffic patterns |
| 4. Adaptive rate limiter | Internal request rate | No | No | No | Med | Self-regulating capacity |
| 5. K8s custom metrics | All signals (HTTP API) | No | No | Yes (HPA) | Med | Kubernetes deployments |
| 6. Graceful drain | Any (external) | No | Yes | No | Med | Long-running request agents |

**Key principle**: scale-up should be fast (aggressive cooldown, large step size) because the cost of under-provisioning (dropped requests, user impact) is higher than over-provisioning (wasted compute). Scale-down should be slow (conservative cooldown, small step size) to avoid oscillation. Always drain before removing a worker to avoid cutting off in-flight LLM API calls.
