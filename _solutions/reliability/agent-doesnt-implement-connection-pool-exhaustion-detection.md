---
title: "Agent Doesn't Implement Connection Pool Exhaustion Detection"
description: "Agents that acquire database or HTTP connection pool slots without monitoring pool depth silently stall when the pool is exhausted — requests queue indefinitely, latency spikes, and the agent appears hung with no diagnostic signal. Implement connection pool exhaustion detection that tracks pool utilization, fires alerts before saturation, and sheds load gracefully when the pool is full."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-connection-pool-exhaustion-detection
tags: [connection-pool, exhaustion-detection, pool-saturation, load-shedding, database-reliability, pool-monitoring]
symptoms:
  - "Requests hang for 30+ seconds with no error — pool queue is backed up silently"
  - "P99 latency spikes to pool timeout value exactly, indicating queue saturation"
  - "No metric showing current pool utilization or wait queue depth"
  - "All pool slots acquired at once during traffic bursts, starving other request types"
  - "Pool exhaustion during one tool type blocks unrelated tool calls sharing the same pool"
---

## Why This Happens

Connection pools have a fixed maximum size. When all slots are checked out, the next acquisition blocks until a slot is returned. Without monitoring, this blocking is invisible: the agent appears to be working but is actually waiting in the pool queue. At saturation, every new request adds to the wait queue, and latency grows proportionally to queue depth. Detection requires tracking two numbers — current active connections and maximum pool size — and alerting when the ratio approaches 1.0. Load shedding requires refusing new acquisitions beyond a configurable high-water mark rather than letting the queue grow unbounded.

## Solution 1: Pool Utilization Tracker

```python
import threading
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PoolSnapshot:
    pool_name: str
    active: int
    idle: int
    max_size: int
    waiting: int
    utilization: float          # active / max_size
    recorded_at: float = field(default_factory=time.time)

    @property
    def is_saturated(self) -> bool:
        return self.utilization >= 1.0

    @property
    def is_near_saturated(self) -> bool:
        return self.utilization >= 0.85


class ConnectionPoolUtilizationTracker:
    """
    Wraps a connection pool object and tracks acquisition/release counts
    to maintain a live utilization snapshot.
    """

    def __init__(self, pool_name: str, max_size: int):
        self._name = pool_name
        self._max = max_size
        self._active = 0
        self._waiting = 0
        self._lock = threading.Lock()
        self._high_watermark = 0

    def on_acquire_start(self) -> None:
        with self._lock:
            self._waiting += 1

    def on_acquire_end(self) -> None:
        with self._lock:
            self._waiting = max(0, self._waiting - 1)
            self._active += 1
            if self._active > self._high_watermark:
                self._high_watermark = self._active

    def on_release(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)

    def snapshot(self) -> PoolSnapshot:
        with self._lock:
            return PoolSnapshot(
                pool_name=self._name,
                active=self._active,
                idle=max(0, self._max - self._active),
                max_size=self._max,
                waiting=self._waiting,
                utilization=round(self._active / max(self._max, 1), 4),
            )

    def high_watermark(self) -> int:
        with self._lock:
            return self._high_watermark
```

## Solution 2: Pool Exhaustion Alerter

```python
import time
from typing import List


class PoolExhaustionAlerter:
    """
    Fires structured alerts when pool utilization crosses warning and
    critical thresholds. Debounces repeated alerts within a cooldown window.
    """

    def __init__(
        self,
        tracker: ConnectionPoolUtilizationTracker,
        warn_threshold: float = 0.80,
        critical_threshold: float = 0.95,
        alert_cooldown_seconds: float = 30.0,
    ):
        self._tracker = tracker
        self._warn = warn_threshold
        self._critical = critical_threshold
        self._cooldown = alert_cooldown_seconds
        self._last_alert_at: float = 0.0
        self._alerts: List[dict] = []

    def check(self) -> List[dict]:
        snap = self._tracker.snapshot()
        now = time.time()
        new_alerts = []

        if now - self._last_alert_at < self._cooldown:
            return []

        if snap.utilization >= self._critical:
            alert = {
                "level": "critical",
                "pool_name": snap.pool_name,
                "utilization": snap.utilization,
                "active": snap.active,
                "max_size": snap.max_size,
                "waiting": snap.waiting,
                "ts": now,
            }
            new_alerts.append(alert)
            self._last_alert_at = now
        elif snap.utilization >= self._warn:
            alert = {
                "level": "warn",
                "pool_name": snap.pool_name,
                "utilization": snap.utilization,
                "active": snap.active,
                "max_size": snap.max_size,
                "waiting": snap.waiting,
                "ts": now,
            }
            new_alerts.append(alert)
            self._last_alert_at = now

        self._alerts.extend(new_alerts)
        return new_alerts
```

## Solution 3: Load-Shedding Pool Gate

```python
import asyncio
import time
from typing import Any, AsyncIterator
from contextlib import asynccontextmanager


class LoadSheddingPoolGate:
    """
    Wraps pool acquisition with a high-water-mark check.
    Rejects new acquisitions when the pool is at or above the shedding
    threshold, returning a PoolExhaustedError immediately rather than
    queuing indefinitely.
    """

    def __init__(
        self,
        tracker: ConnectionPoolUtilizationTracker,
        shed_threshold: float = 0.90,
        acquire_timeout_seconds: float = 5.0,
    ):
        self._tracker = tracker
        self._shed = shed_threshold
        self._timeout = acquire_timeout_seconds
        self._shed_count = 0

    @asynccontextmanager
    async def acquire(self, pool_acquire_fn, pool_release_fn):
        snap = self._tracker.snapshot()
        if snap.utilization >= self._shed:
            self._shed_count += 1
            raise PoolExhaustedError(
                snap.pool_name,
                snap.utilization,
                snap.waiting,
            )

        self._tracker.on_acquire_start()
        conn = None
        try:
            conn = await asyncio.wait_for(pool_acquire_fn(), timeout=self._timeout)
            self._tracker.on_acquire_end()
            yield conn
        except asyncio.TimeoutError:
            self._tracker.on_acquire_start.__doc__  # noop
            raise PoolAcquireTimeoutError(self._tracker._name, self._timeout)
        finally:
            if conn is not None:
                self._tracker.on_release()
                await pool_release_fn(conn)

    def shed_count(self) -> int:
        return self._shed_count


class PoolExhaustedError(Exception):
    def __init__(self, pool_name: str, utilization: float, waiting: int):
        super().__init__(
            f"Pool '{pool_name}' exhausted (utilization={utilization:.0%}, waiting={waiting})"
        )
        self.pool_name = pool_name
        self.utilization = utilization
        self.waiting = waiting


class PoolAcquireTimeoutError(Exception):
    def __init__(self, pool_name: str, timeout: float):
        super().__init__(f"Pool '{pool_name}' acquire timed out after {timeout}s")
        self.pool_name = pool_name
        self.timeout = timeout
```

## Solution 4: Pool Utilization History Recorder

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, List, Optional, Tuple


class PoolUtilizationHistoryRecorder:
    """
    Records periodic pool snapshots for trend analysis and
    saturation event counting.
    """

    def __init__(self, max_records: int = 10000):
        self._max = max_records
        self._records: Deque[Tuple[float, float, int]] = deque()
        # (ts, utilization, waiting)
        self._lock = Lock()

    def record(self, snapshot: PoolSnapshot) -> None:
        with self._lock:
            self._records.append((
                snapshot.recorded_at,
                snapshot.utilization,
                snapshot.waiting,
            ))
            if len(self._records) > self._max:
                self._records.popleft()

    def saturation_events(
        self,
        window_seconds: float = 3600.0,
        threshold: float = 0.95,
    ) -> int:
        cutoff = time.time() - window_seconds
        with self._lock:
            return sum(
                1 for ts, util, _ in self._records
                if ts >= cutoff and util >= threshold
            )

    def percentile(self, pct: float, window_seconds: float = 3600.0) -> Optional[float]:
        cutoff = time.time() - window_seconds
        with self._lock:
            vals = sorted(u for ts, u, _ in self._records if ts >= cutoff)
        if not vals:
            return None
        idx = min(int(len(vals) * pct / 100.0), len(vals) - 1)
        return round(vals[idx], 4)
```

## Solution 5: Multi-Pool Registry Monitor

```python
from typing import Dict, List


class MultiPoolRegistryMonitor:
    """
    Monitors multiple named connection pools and returns a
    cross-pool health summary, identifying which pool is closest to saturation.
    """

    def __init__(self):
        self._trackers: Dict[str, ConnectionPoolUtilizationTracker] = {}
        self._alerters: Dict[str, PoolExhaustionAlerter] = {}

    def register(
        self,
        tracker: ConnectionPoolUtilizationTracker,
        alerter: PoolExhaustionAlerter,
    ) -> None:
        self._trackers[tracker._name] = tracker
        self._alerters[tracker._name] = alerter

    def health_summary(self) -> dict:
        snapshots = {
            name: tracker.snapshot()
            for name, tracker in self._trackers.items()
        }
        alerts = []
        for alerter in self._alerters.values():
            alerts.extend(alerter.check())

        most_loaded = max(snapshots.values(), key=lambda s: s.utilization, default=None)

        return {
            "pool_count": len(snapshots),
            "pools": {
                name: {
                    "utilization": s.utilization,
                    "active": s.active,
                    "max_size": s.max_size,
                    "waiting": s.waiting,
                }
                for name, s in snapshots.items()
            },
            "most_loaded_pool": most_loaded.pool_name if most_loaded else None,
            "active_alerts": alerts,
        }
```

## Solution 6: Connection Pool Dashboard

```python
import time


class ConnectionPoolExhaustionDashboard:
    """
    Combines live utilization, history percentiles, saturation event counts,
    and load-shedding stats into a single operational report.
    """

    def __init__(
        self,
        monitor: MultiPoolRegistryMonitor,
        history: PoolUtilizationHistoryRecorder,
        gate: LoadSheddingPoolGate,
    ):
        self._monitor = monitor
        self._history = history
        self._gate = gate

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "live_health": self._monitor.health_summary(),
            "history_1h": {
                "p50_utilization": self._history.percentile(50, 3600.0),
                "p95_utilization": self._history.percentile(95, 3600.0),
                "p99_utilization": self._history.percentile(99, 3600.0),
                "saturation_events": self._history.saturation_events(3600.0),
            },
            "load_shedding": {
                "shed_requests_total": self._gate.shed_count(),
            },
        }
```

## Comparison

| Approach | Live Utilization | Threshold Alerts | Load Shedding | History Tracking | Multi-Pool |
|---|---|---|---|---|---|
| ConnectionPoolUtilizationTracker | Yes | No | No | No | No |
| PoolExhaustionAlerter | Via tracker | Yes (warn/critical) | No | No | No |
| LoadSheddingPoolGate | Via tracker | No | Yes (reject early) | No | No |
| PoolUtilizationHistoryRecorder | No | No | No | Yes (percentiles) | No |
| MultiPoolRegistryMonitor | Via trackers | Via alerters | No | No | Yes |
| ConnectionPoolExhaustionDashboard | No | No | No | No | Yes |

**Best for production**: Set `shed_threshold=0.90` so that requests are rejected with a fast `PoolExhaustedError` (HTTP 503) rather than queuing behind a 30-second pool timeout — a fast failure lets the caller retry or return an error immediately instead of tying up a thread. Set `warn_threshold=0.80` to get early warning before shedding begins. Emit `pool_utilization` as a gauge metric with `pool_name` as a tag: sustained P95 utilization above 0.70 indicates the pool is undersized and should be enlarged before the next traffic peak.
