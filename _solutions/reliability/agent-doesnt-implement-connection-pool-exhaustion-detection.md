---
title: "Agent Doesn't Implement Connection Pool Exhaustion Detection"
description: "Agents that acquire database or HTTP connections without monitoring pool utilization exhaust the pool under load, causing all requests to block waiting for a connection — with no diagnostic signal indicating the pool is the bottleneck. Implement connection pool exhaustion detection that tracks pool utilization, detects saturation before full exhaustion, alerts on pool pressure, and identifies which operations hold connections longest."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-connection-pool-exhaustion-detection
tags: [connection-pool, pool-exhaustion, saturation-detection, database-connections, http-connections, resource-monitoring]
symptoms:
  - "All agent requests stall simultaneously under moderate load"
  - "Timeout errors reference connection acquisition, not query execution"
  - "P99 latency spikes correlate with traffic spikes but root cause is unclear"
  - "No metric showing how many connections are currently in use vs. available"
  - "Pool exhaustion is discovered only after users report timeouts"
---

## Why This Happens

Connection pools have a fixed maximum size. When all connections are in use, new acquisition requests wait in a queue. If the queue fills or the wait exceeds a timeout, requests fail. Without monitoring pool utilization, engineers see symptoms (slow requests, timeouts) but not the cause (pool saturation). Detection requires tracking active connections, wait queue depth, acquisition latency, and the operations holding connections longest — so pool exhaustion is detected at 80% utilization rather than at 100% when requests are already failing.

## Solution 1: Pool Utilization Snapshot

```python
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class PoolUtilizationSnapshot:
    pool_name: str
    timestamp: float = field(default_factory=time.time)
    pool_size: int = 0              # maximum pool size
    active_connections: int = 0     # connections currently checked out
    idle_connections: int = 0       # connections available in pool
    waiting_requests: int = 0       # requests waiting for a connection
    acquisition_timeout_ms: float = 0.0

    @property
    def utilization(self) -> float:
        if self.pool_size == 0:
            return 0.0
        return self.active_connections / self.pool_size

    @property
    def is_saturated(self) -> bool:
        return self.utilization >= 0.90

    @property
    def is_exhausted(self) -> bool:
        return self.active_connections >= self.pool_size and self.waiting_requests > 0


@dataclass
class ConnectionHoldRecord:
    operation_name: str
    acquired_at: float
    connection_id: str
    released_at: Optional[float] = None

    @property
    def hold_duration_ms(self) -> float:
        end = self.released_at or time.time()
        return round((end - self.acquired_at) * 1000, 2)
```

## Solution 2: Pool Utilization Monitor

```python
import time
from collections import deque
from threading import Lock
from typing import Callable, Deque, Dict, List, Optional


class PoolUtilizationMonitor:
    """
    Tracks connection pool utilization over time using snapshots.
    Detects saturation trends and identifies peak utilization windows.
    """

    def __init__(
        self,
        pool_name: str,
        pool_size: int,
        saturation_threshold: float = 0.80,
        max_snapshots: int = 3600,
    ):
        self._pool_name = pool_name
        self._pool_size = pool_size
        self._saturation_threshold = saturation_threshold
        self._snapshots: Deque[PoolUtilizationSnapshot] = deque(maxlen=max_snapshots)
        self._active: Dict[str, ConnectionHoldRecord] = {}
        self._lock = Lock()
        self._saturation_events = 0
        self._exhaustion_events = 0

    def record_snapshot(
        self,
        active: int,
        idle: int,
        waiting: int = 0,
    ) -> PoolUtilizationSnapshot:
        snap = PoolUtilizationSnapshot(
            pool_name=self._pool_name,
            pool_size=self._pool_size,
            active_connections=active,
            idle_connections=idle,
            waiting_requests=waiting,
        )
        with self._lock:
            self._snapshots.append(snap)
            if snap.utilization >= self._saturation_threshold:
                self._saturation_events += 1
            if snap.is_exhausted:
                self._exhaustion_events += 1
        return snap

    def record_acquisition(self, connection_id: str, operation_name: str) -> None:
        with self._lock:
            self._active[connection_id] = ConnectionHoldRecord(
                operation_name=operation_name,
                acquired_at=time.time(),
                connection_id=connection_id,
            )

    def record_release(self, connection_id: str) -> Optional[ConnectionHoldRecord]:
        with self._lock:
            record = self._active.pop(connection_id, None)
            if record:
                record.released_at = time.time()
            return record

    def long_held_connections(self, threshold_ms: float = 5000.0) -> List[ConnectionHoldRecord]:
        with self._lock:
            return [
                r for r in self._active.values()
                if r.hold_duration_ms >= threshold_ms
            ]

    def peak_utilization(self, window_seconds: float = 300.0) -> float:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [s for s in self._snapshots if s.timestamp >= cutoff]
        if not recent:
            return 0.0
        return max(s.utilization for s in recent)

    def summary(self, window_seconds: float = 300.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [s for s in self._snapshots if s.timestamp >= cutoff]
            active_count = len(self._active)

        if not recent:
            return {"pool_name": self._pool_name, "snapshots": 0}

        avg_util = sum(s.utilization for s in recent) / len(recent)
        peak = max(s.utilization for s in recent)
        return {
            "pool_name": self._pool_name,
            "pool_size": self._pool_size,
            "current_active": active_count,
            "avg_utilization": round(avg_util, 3),
            "peak_utilization": round(peak, 3),
            "saturation_events": self._saturation_events,
            "exhaustion_events": self._exhaustion_events,
            "window_seconds": window_seconds,
        }
```

## Solution 3: Acquisition Latency Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, List, Optional, Tuple


class AcquisitionLatencyTracker:
    """
    Measures how long each connection acquisition wait takes.
    High acquisition latency is a leading indicator of pool exhaustion.
    """

    def __init__(self, max_samples: int = 10000):
        self._samples: Deque[Tuple[float, float]] = deque(maxlen=max_samples)
        # (timestamp, latency_ms)
        self._lock = Lock()

    def record(self, latency_ms: float) -> None:
        with self._lock:
            self._samples.append((time.time(), latency_ms))

    def percentile(self, pct: float, window_seconds: float = 300.0) -> Optional[float]:
        cutoff = time.time() - window_seconds
        with self._lock:
            values = sorted(ms for ts, ms in self._samples if ts >= cutoff)
        if not values:
            return None
        idx = min(int(len(values) * pct / 100.0), len(values) - 1)
        return round(values[idx], 2)

    def summary(self, window_seconds: float = 300.0) -> dict:
        return {
            "p50_ms": self.percentile(50, window_seconds),
            "p95_ms": self.percentile(95, window_seconds),
            "p99_ms": self.percentile(99, window_seconds),
            "window_seconds": window_seconds,
        }
```

## Solution 4: Pool Pressure Alert Manager

```python
import time
from typing import Callable, List, Optional


class PoolPressureAlert:
    def __init__(self, pool_name: str, level: str, utilization: float, message: str):
        self.pool_name = pool_name
        self.level = level          # "warning" | "critical"
        self.utilization = utilization
        self.message = message
        self.timestamp = time.time()


class PoolPressureAlertManager:
    """
    Fires alerts when pool utilization crosses warning or critical thresholds.
    Implements alert suppression to prevent alert storms.
    """

    def __init__(
        self,
        warning_threshold: float = 0.75,
        critical_threshold: float = 0.90,
        alert_fn: Optional[Callable[[PoolPressureAlert], None]] = None,
        cooldown_seconds: float = 60.0,
    ):
        self._warning = warning_threshold
        self._critical = critical_threshold
        self._alert_fn = alert_fn or (lambda a: None)
        self._cooldown = cooldown_seconds
        self._last_alert: dict = {}
        self._alerts: List[PoolPressureAlert] = []

    def evaluate(self, snapshot: PoolUtilizationSnapshot) -> Optional[PoolPressureAlert]:
        util = snapshot.utilization
        if util < self._warning:
            return None

        level = "critical" if util >= self._critical else "warning"
        key = f"{snapshot.pool_name}:{level}"
        now = time.time()

        if now - self._last_alert.get(key, 0) < self._cooldown:
            return None  # suppressed

        alert = PoolPressureAlert(
            pool_name=snapshot.pool_name,
            level=level,
            utilization=round(util, 3),
            message=(
                f"Pool '{snapshot.pool_name}' at {util:.0%} utilization "
                f"({snapshot.active_connections}/{snapshot.pool_size} connections). "
                f"Waiting requests: {snapshot.waiting_requests}"
            ),
        )
        self._last_alert[key] = now
        self._alerts.append(alert)
        self._alert_fn(alert)
        return alert

    def recent_alerts(self, window_seconds: float = 3600.0) -> List[PoolPressureAlert]:
        cutoff = time.time() - window_seconds
        return [a for a in self._alerts if a.timestamp >= cutoff]
```

## Solution 5: Instrumented Connection Context Manager

```python
import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Callable


class InstrumentedConnectionPool:
    """
    Wraps an existing connection pool with monitoring instrumentation.
    Records acquisition latency, hold duration, and operation attribution.
    """

    def __init__(
        self,
        pool: Any,
        monitor: PoolUtilizationMonitor,
        latency_tracker: AcquisitionLatencyTracker,
        alert_manager: PoolPressureAlertManager,
        acquire_fn: Callable = None,
        release_fn: Callable = None,
    ):
        self._pool = pool
        self._monitor = monitor
        self._latency_tracker = latency_tracker
        self._alerts = alert_manager
        self._acquire = acquire_fn or (lambda p: p.acquire())
        self._release = release_fn or (lambda p, c: p.release(c))

    @asynccontextmanager
    async def acquire(self, operation_name: str = "") -> AsyncGenerator[Any, None]:
        conn_id = str(uuid.uuid4())[:8]
        acq_start = time.time()

        conn = await self._acquire(self._pool)
        acq_latency_ms = (time.time() - acq_start) * 1000
        self._latency_tracker.record(acq_latency_ms)
        self._monitor.record_acquisition(conn_id, operation_name)

        try:
            yield conn
        finally:
            self._monitor.record_release(conn_id)
            await self._release(self._pool, conn)
```

## Solution 6: Connection Pool Exhaustion Dashboard

```python
import time


class ConnectionPoolExhaustionDashboard:
    """
    Combines utilization summary, acquisition latency, long-held connections,
    and recent alerts into a single operational view.
    """

    def __init__(
        self,
        monitor: PoolUtilizationMonitor,
        latency_tracker: AcquisitionLatencyTracker,
        alert_manager: PoolPressureAlertManager,
    ):
        self._monitor = monitor
        self._latency = latency_tracker
        self._alerts = alert_manager

    def render(self, window_seconds: float = 300.0) -> dict:
        long_held = self._monitor.long_held_connections(threshold_ms=3000.0)
        return {
            "generated_at": time.time(),
            "utilization_summary": self._monitor.summary(window_seconds),
            "acquisition_latency": self._latency.summary(window_seconds),
            "long_held_connections": [
                {
                    "operation": r.operation_name,
                    "hold_ms": r.hold_duration_ms,
                    "connection_id": r.connection_id,
                }
                for r in sorted(long_held, key=lambda r: -r.hold_duration_ms)[:5]
            ],
            "recent_alerts": [
                {"level": a.level, "utilization": a.utilization, "msg": a.message}
                for a in self._alerts.recent_alerts(window_seconds)
            ],
        }
```

## Comparison

| Approach | Utilization Tracking | Hold Duration | Acquisition Latency | Pressure Alerts | Dashboard |
|---|---|---|---|---|---|
| PoolUtilizationMonitor | Yes (snapshots) | Yes (per-conn) | No | No | No |
| AcquisitionLatencyTracker | No | No | Yes (P95/P99) | No | No |
| PoolPressureAlertManager | Via snapshots | No | No | Yes (cooldown) | No |
| InstrumentedConnectionPool | Via monitor | Via monitor | Via tracker | Via alert mgr | No |
| ConnectionPoolExhaustionDashboard | No | No | No | No | Yes |

**Best for production**: Alert at 75% utilization — not 90%. By the time you hit 90%, acquisition wait times are already spiking and some requests are timing out. Set `pool_size` to match the database server's `max_connections` divided by the number of agent instances, leaving 20% headroom for admin connections. Use `long_held_connections(threshold_ms=3000)` as a leading indicator: connections held for more than 3 seconds are usually stuck waiting on a slow query or a deadlock, not normal operation. Poll `record_snapshot()` every 10 seconds from a background task — more frequent polling adds overhead, less frequent misses transient saturation spikes.
