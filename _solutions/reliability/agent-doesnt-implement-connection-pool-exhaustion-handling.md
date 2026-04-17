---
title: "Agent Doesn't Implement Connection Pool Exhaustion Handling"
description: "Agents that acquire database or HTTP connections without bounds or backoff will exhaust the connection pool under load: each concurrent request blocks waiting for a connection, wait queues grow unbounded, and eventually all agent tasks stall or fail with timeout errors. Implement connection pool exhaustion handling with configurable wait limits, pool utilization monitoring, request shedding under saturation, and automatic pool size adaptation."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-connection-pool-exhaustion-handling
tags: [connection-pool, exhaustion-handling, backpressure, pool-monitoring, request-shedding, resource-limits]
symptoms:
  - "Agent tasks stall with 'connection pool exhausted' or 'timeout acquiring connection' errors under load"
  - "Pool utilization hits 100% and stays there — connections are never released cleanly"
  - "No visibility into pool wait times or queue depth before exhaustion occurs"
  - "All concurrent agent requests compete for the same fixed pool with no fairness or shedding"
  - "Connection leaks accumulate over time until the pool is permanently exhausted"
---

## Why This Happens

Connection pools have a fixed maximum size set at initialization. When all connections are in use and a new request arrives, the pool either blocks the caller until a connection is freed, returns an error immediately, or queues the request with a timeout. Agents that run many concurrent tool calls — each of which acquires a connection — will exhaust the pool faster than connections can be returned. The problem compounds when connections are leaked (not returned on error paths), when pool size is set too small relative to expected concurrency, and when there is no mechanism to shed low-priority requests when the pool is saturated.

## Solution 1: Pool Utilization Snapshot

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class PoolSaturationLevel(str, Enum):
    HEALTHY = "healthy"         # < 70% utilized
    ELEVATED = "elevated"       # 70-90% utilized
    SATURATED = "saturated"     # 90-100% utilized
    EXHAUSTED = "exhausted"     # 100% + waiters queued


@dataclass
class PoolUtilizationSnapshot:
    pool_name: str
    total_connections: int
    active_connections: int
    idle_connections: int
    queued_waiters: int
    avg_wait_ms: float
    sampled_at: float = field(default_factory=time.time)

    @property
    def utilization_pct(self) -> float:
        if self.total_connections == 0:
            return 0.0
        return round(self.active_connections / self.total_connections * 100, 1)

    @property
    def saturation_level(self) -> PoolSaturationLevel:
        pct = self.utilization_pct
        if self.queued_waiters > 0:
            return PoolSaturationLevel.EXHAUSTED
        if pct >= 90:
            return PoolSaturationLevel.SATURATED
        if pct >= 70:
            return PoolSaturationLevel.ELEVATED
        return PoolSaturationLevel.HEALTHY
```

## Solution 2: Bounded Connection Pool

```python
import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any, Callable, List, Optional


class BoundedConnectionPool:
    """
    Async connection pool with bounded size, acquire timeout, and
    utilization tracking. Connections are created lazily up to max_size.
    """

    def __init__(
        self,
        pool_name: str,
        create_fn: Callable,          # async fn() -> connection
        close_fn: Callable,           # async fn(conn) -> None
        max_size: int = 10,
        min_size: int = 2,
        acquire_timeout_seconds: float = 5.0,
        max_idle_seconds: float = 300.0,
    ):
        self._name = pool_name
        self._create = create_fn
        self._close = close_fn
        self._max = max_size
        self._min = min_size
        self._acquire_timeout = acquire_timeout_seconds
        self._max_idle = max_idle_seconds
        self._semaphore = asyncio.Semaphore(max_size)
        self._idle: List[tuple] = []       # (conn, last_used_at)
        self._active_count = 0
        self._queued_waiters = 0
        self._total_wait_ms: float = 0.0
        self._acquire_count = 0
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self):
        self._queued_waiters += 1
        wait_start = time.monotonic()
        try:
            acquired = await asyncio.wait_for(
                self._semaphore.acquire(), timeout=self._acquire_timeout
            )
        except asyncio.TimeoutError:
            self._queued_waiters -= 1
            raise ConnectionPoolExhaustedError(
                self._name, self._max, self._acquire_timeout
            )
        finally:
            pass

        self._queued_waiters -= 1
        wait_ms = (time.monotonic() - wait_start) * 1000
        self._total_wait_ms += wait_ms
        self._acquire_count += 1

        conn = await self._get_or_create()
        self._active_count += 1
        try:
            yield conn
        finally:
            self._active_count -= 1
            await self._return(conn)
            self._semaphore.release()

    async def _get_or_create(self) -> Any:
        async with self._lock:
            now = time.monotonic()
            while self._idle:
                conn, last_used = self._idle.pop()
                if now - last_used < self._max_idle:
                    return conn
                await self._close(conn)
        return await self._create()

    async def _return(self, conn: Any) -> None:
        async with self._lock:
            self._idle.append((conn, time.monotonic()))

    def snapshot(self) -> PoolUtilizationSnapshot:
        idle = len(self._idle)
        avg_wait = self._total_wait_ms / max(self._acquire_count, 1)
        return PoolUtilizationSnapshot(
            pool_name=self._name,
            total_connections=self._max,
            active_connections=self._active_count,
            idle_connections=idle,
            queued_waiters=self._queued_waiters,
            avg_wait_ms=round(avg_wait, 2),
        )


class ConnectionPoolExhaustedError(Exception):
    def __init__(self, pool_name: str, max_size: int, timeout: float):
        super().__init__(
            f"pool '{pool_name}' exhausted (max={max_size}, timeout={timeout}s)"
        )
        self.pool_name = pool_name
```

## Solution 3: Pool Saturation Shedder

```python
import asyncio
from typing import Any, Callable, Optional


class PoolSaturationShedder:
    """
    Rejects low-priority requests when pool saturation exceeds a threshold,
    preventing queue pile-up that delays high-priority requests.
    """

    def __init__(
        self,
        pool: BoundedConnectionPool,
        shed_at_saturation: PoolSaturationLevel = PoolSaturationLevel.SATURATED,
        high_priority_label: str = "high",
    ):
        self._pool = pool
        self._shed_at = shed_at_saturation
        self._high_priority = high_priority_label
        self._shed_count = 0

    async def acquire(self, priority: str = "normal"):
        snapshot = self._pool.snapshot()
        saturation = snapshot.saturation_level

        # Shed non-high-priority requests when saturated
        saturation_levels = list(PoolSaturationLevel)
        current_idx = saturation_levels.index(saturation)
        shed_idx = saturation_levels.index(self._shed_at)

        if current_idx >= shed_idx and priority != self._high_priority:
            self._shed_count += 1
            raise RequestSheddingError(
                pool_name=self._pool._name,
                saturation=saturation.value,
                priority=priority,
            )

        return self._pool.acquire()

    def shed_count(self) -> int:
        return self._shed_count


class RequestSheddingError(Exception):
    def __init__(self, pool_name: str, saturation: str, priority: str):
        super().__init__(
            f"request shed: pool '{pool_name}' at {saturation} saturation, priority={priority}"
        )
        self.pool_name = pool_name
        self.saturation = saturation
```

## Solution 4: Connection Leak Detector

```python
import asyncio
import time
from typing import Dict, Optional


class ConnectionLeakDetector:
    """
    Tracks active connection acquisitions by request ID with timestamps.
    Reports connections held longer than the leak threshold as suspected leaks.
    """

    def __init__(self, leak_threshold_seconds: float = 30.0):
        self._threshold = leak_threshold_seconds
        self._active: Dict[str, float] = {}   # request_id -> acquired_at
        self._lock = asyncio.Lock()

    async def on_acquire(self, request_id: str) -> None:
        async with self._lock:
            self._active[request_id] = time.monotonic()

    async def on_release(self, request_id: str) -> Optional[float]:
        async with self._lock:
            acquired_at = self._active.pop(request_id, None)
        if acquired_at is None:
            return None
        return round((time.monotonic() - acquired_at) * 1000, 2)

    async def suspected_leaks(self) -> list:
        now = time.monotonic()
        async with self._lock:
            return [
                {
                    "request_id": rid,
                    "held_seconds": round(now - acquired_at, 1),
                }
                for rid, acquired_at in self._active.items()
                if now - acquired_at > self._threshold
            ]
```

## Solution 5: Pool Size Advisor

```python
from typing import List


class PoolSizeAdvisor:
    """
    Analyzes pool utilization snapshots and recommends a pool size adjustment.
    Uses the 95th-percentile active connection count as the recommended minimum.
    """

    def __init__(self, target_headroom_pct: float = 20.0):
        self._headroom = target_headroom_pct / 100.0

    def advise(self, snapshots: List[PoolUtilizationSnapshot]) -> dict:
        if not snapshots:
            return {"recommendation": "insufficient_data"}

        active_counts = sorted(s.active_connections for s in snapshots)
        p95_idx = min(int(len(active_counts) * 0.95), len(active_counts) - 1)
        p95_active = active_counts[p95_idx]
        current_max = snapshots[-1].total_connections
        recommended = int(p95_active * (1.0 + self._headroom))

        if recommended > current_max * 1.5:
            action = "increase_significantly"
        elif recommended > current_max:
            action = "increase"
        elif recommended < current_max * 0.5:
            action = "decrease"
        else:
            action = "maintain"

        return {
            "current_max": current_max,
            "p95_active": p95_active,
            "recommended_size": recommended,
            "action": action,
            "headroom_pct": self._headroom * 100,
        }
```

## Solution 6: Connection Pool Dashboard

```python
import time
from typing import List


class ConnectionPoolDashboard:
    """
    Aggregates pool utilization snapshots and advisor recommendations
    into a single operational view.
    """

    def __init__(
        self,
        pool: BoundedConnectionPool,
        shedder: PoolSaturationShedder,
        leak_detector: ConnectionLeakDetector,
        advisor: PoolSizeAdvisor,
        history_limit: int = 1000,
    ):
        self._pool = pool
        self._shedder = shedder
        self._leak_detector = leak_detector
        self._advisor = advisor
        self._history: List[PoolUtilizationSnapshot] = []
        self._history_limit = history_limit

    def sample(self) -> PoolUtilizationSnapshot:
        snapshot = self._pool.snapshot()
        if len(self._history) >= self._history_limit:
            self._history.pop(0)
        self._history.append(snapshot)
        return snapshot

    async def render(self) -> dict:
        current = self.sample()
        leaks = await self._leak_detector.suspected_leaks()
        advice = self._advisor.advise(self._history[-100:])

        return {
            "generated_at": time.time(),
            "pool": current.__dict__,
            "saturation_level": current.saturation_level.value,
            "shed_count": self._shedder.shed_count(),
            "suspected_leaks": leaks,
            "size_advice": advice,
        }
```

## Comparison

| Approach | Acquire Timeout | Utilization Tracking | Request Shedding | Leak Detection | Size Recommendation |
|---|---|---|---|---|---|
| BoundedConnectionPool | Yes (asyncio timeout) | Yes (snapshot) | No | No | No |
| PoolSaturationShedder | No | Via pool | Yes (priority-based) | No | No |
| ConnectionLeakDetector | No | No | No | Yes (threshold) | No |
| PoolSizeAdvisor | No | Via snapshots | No | No | Yes (P95-based) |
| ConnectionPoolDashboard | No | Yes (history) | No | Via detector | Via advisor |

**Best for production**: Set `acquire_timeout_seconds` equal to your agent request timeout divided by the maximum expected number of sequential database calls — if the request times out in 10s and makes at most 3 DB calls, set the pool acquire timeout to 3s. Use `PoolSaturationShedder` to protect high-priority agent tasks (user-facing) from being starved by background batch tasks: label batch tasks `priority="low"` and configure shedding at `SATURATED`. Run `ConnectionLeakDetector` with `leak_threshold_seconds=30` — any connection held longer than 30 seconds in a web-serving context is almost certainly a bug. Check `PoolSizeAdvisor.advise()` weekly: pools set at initialization are rarely revisited as traffic grows, leading to chronic saturation that masquerades as application slowness.
