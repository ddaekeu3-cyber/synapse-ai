---
title: "Agent Doesn't Implement Connection Pool Health Monitoring"
description: "Agents that maintain HTTP or database connection pools without monitoring pool health accumulate stale, leaked, or exhausted connections silently. When the pool is exhausted, new tool calls block indefinitely waiting for a connection, appearing as hangs rather than errors. Implement connection pool health monitoring that tracks pool utilization, detects exhaustion, reports leaked connections, and alerts before the pool becomes a bottleneck."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-connection-pool-health-monitoring
tags: [connection-pool, pool-exhaustion, leaked-connections, pool-health, http-client, database-connections]
symptoms:
  - "Tool calls hang indefinitely with no timeout when the connection pool is exhausted"
  - "Pool utilization climbs to 100% and never recovers between bursts"
  - "Leaked connections accumulate after errors, reducing effective pool capacity"
  - "No visibility into pool utilization — only hung requests reveal the problem"
  - "Pool exhaustion during load tests looks identical to a downstream service outage"
---

## Why This Happens

Connection pools are finite resources. When every connection is in use and a new request arrives, it waits. If connections are not returned promptly after errors — because exception handlers skip the finally block, or because a coroutine is cancelled mid-execution — the pool shrinks permanently until it is recycled. Without health monitoring, the agent has no signal that utilization is trending toward exhaustion. By the time requests start hanging, the pool is already at capacity. Monitoring requires tracking acquire and release events, detecting connections that were acquired but not released within a deadline, and alerting when utilization crosses a threshold.

## Solution 1: Pool Utilization Snapshot

```python
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PoolUtilizationSnapshot:
    pool_name: str
    total_connections: int
    active_connections: int
    idle_connections: int
    waiting_requests: int
    timestamp: float = field(default_factory=time.time)

    @property
    def utilization_pct(self) -> float:
        if self.total_connections == 0:
            return 0.0
        return round(self.active_connections / self.total_connections * 100, 1)

    @property
    def is_exhausted(self) -> bool:
        return self.active_connections >= self.total_connections and self.waiting_requests > 0
```

## Solution 2: Connection Lease Tracker

```python
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, List, Optional
import uuid


@dataclass
class ConnectionLease:
    lease_id: str
    pool_name: str
    acquired_at: float = field(default_factory=time.time)
    released: bool = False
    released_at: Optional[float] = None

    def age_seconds(self) -> float:
        return time.time() - self.acquired_at

    def is_leaked(self, max_age_seconds: float) -> bool:
        return not self.released and self.age_seconds() > max_age_seconds


class ConnectionLeaseTracker:
    """
    Tracks outstanding connection leases. Detects leases that exceed
    a maximum hold time and classifies them as potential leaks.
    """

    def __init__(self, max_lease_seconds: float = 30.0):
        self._max_lease = max_lease_seconds
        self._leases: Dict[str, ConnectionLease] = {}
        self._lock = Lock()

    def acquire(self, pool_name: str) -> str:
        lease_id = str(uuid.uuid4())[:8]
        with self._lock:
            self._leases[lease_id] = ConnectionLease(
                lease_id=lease_id,
                pool_name=pool_name,
            )
        return lease_id

    def release(self, lease_id: str) -> None:
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease:
                lease.released = True
                lease.released_at = time.time()

    def leaked_leases(self, pool_name: Optional[str] = None) -> List[ConnectionLease]:
        with self._lock:
            return [
                lease for lease in self._leases.values()
                if lease.is_leaked(self._max_lease)
                and (pool_name is None or lease.pool_name == pool_name)
            ]

    def active_count(self, pool_name: str) -> int:
        with self._lock:
            return sum(
                1 for l in self._leases.values()
                if l.pool_name == pool_name and not l.released
            )

    def purge_released(self) -> int:
        with self._lock:
            before = len(self._leases)
            self._leases = {
                k: v for k, v in self._leases.items()
                if not v.released
            }
            return before - len(self._leases)
```

## Solution 3: Pool Health Evaluator

```python
from dataclasses import dataclass
from enum import Enum
from typing import List


class PoolHealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CRITICAL = "critical"
    EXHAUSTED = "exhausted"


@dataclass
class PoolHealthReport:
    pool_name: str
    status: PoolHealthStatus
    utilization_pct: float
    leaked_connection_count: int
    waiting_requests: int
    recommendations: List[str]


class PoolHealthEvaluator:
    """
    Combines utilization snapshot and leak data into a health report
    with a severity classification and actionable recommendations.
    """

    def __init__(
        self,
        warn_utilization_pct: float = 70.0,
        critical_utilization_pct: float = 90.0,
    ):
        self._warn = warn_utilization_pct
        self._critical = critical_utilization_pct

    def evaluate(
        self,
        snapshot: PoolUtilizationSnapshot,
        leaked_count: int,
    ) -> PoolHealthReport:
        recommendations = []
        util = snapshot.utilization_pct

        if snapshot.is_exhausted:
            status = PoolHealthStatus.EXHAUSTED
            recommendations.append("increase pool size or reduce concurrent tool calls")
        elif util >= self._critical or leaked_count >= 3:
            status = PoolHealthStatus.CRITICAL
        elif util >= self._warn or leaked_count >= 1:
            status = PoolHealthStatus.DEGRADED
        else:
            status = PoolHealthStatus.HEALTHY

        if leaked_count > 0:
            recommendations.append(
                f"{leaked_count} connection(s) appear leaked — audit error-path release logic"
            )
        if snapshot.waiting_requests > 5:
            recommendations.append("requests queuing for connections — consider pool resize")

        return PoolHealthReport(
            pool_name=snapshot.pool_name,
            status=status,
            utilization_pct=util,
            leaked_connection_count=leaked_count,
            waiting_requests=snapshot.waiting_requests,
            recommendations=recommendations,
        )
```

## Solution 4: Pool Health Monitor

```python
import time
from collections import deque
from threading import Lock
from typing import Callable, Deque, Dict, Optional, Tuple


class ConnectionPoolHealthMonitor:
    """
    Periodically evaluates pool health and maintains a history of
    utilization snapshots for trend analysis.
    """

    def __init__(
        self,
        evaluator: PoolHealthEvaluator,
        lease_tracker: ConnectionLeaseTracker,
        history_size: int = 100,
    ):
        self._evaluator = evaluator
        self._tracker = lease_tracker
        self._history: Dict[str, Deque[Tuple[float, PoolUtilizationSnapshot]]] = {}
        self._lock = Lock()
        self._history_size = history_size

    def record_snapshot(self, snapshot: PoolUtilizationSnapshot) -> PoolHealthReport:
        with self._lock:
            if snapshot.pool_name not in self._history:
                self._history[snapshot.pool_name] = deque(maxlen=self._history_size)
            self._history[snapshot.pool_name].append((time.time(), snapshot))

        leaked = self._tracker.leaked_leases(snapshot.pool_name)
        return self._evaluator.evaluate(snapshot, len(leaked))

    def utilization_trend(self, pool_name: str, last_n: int = 10) -> list:
        with self._lock:
            history = list(self._history.get(pool_name, []))
        return [
            {"ts": ts, "utilization_pct": snap.utilization_pct}
            for ts, snap in history[-last_n:]
        ]
```

## Solution 5: Pool Exhaustion Alert

```python
import time
from typing import List, Optional


class PoolExhaustionAlerter:
    """
    Fires an alert when pool utilization has been above the critical
    threshold for a sustained window. Prevents alert storms by enforcing
    a minimum interval between repeated alerts.
    """

    def __init__(
        self,
        critical_threshold_pct: float = 90.0,
        sustained_seconds: float = 30.0,
        alert_cooldown_seconds: float = 300.0,
    ):
        self._threshold = critical_threshold_pct
        self._sustained = sustained_seconds
        self._cooldown = alert_cooldown_seconds
        self._above_threshold_since: Optional[float] = None
        self._last_alert_at: Optional[float] = None

    def evaluate(self, report: PoolHealthReport) -> Optional[dict]:
        now = time.time()

        if report.utilization_pct >= self._threshold:
            if self._above_threshold_since is None:
                self._above_threshold_since = now
            sustained_duration = now - self._above_threshold_since

            if sustained_duration >= self._sustained:
                if self._last_alert_at is None or now - self._last_alert_at >= self._cooldown:
                    self._last_alert_at = now
                    return {
                        "alert": "pool_exhaustion_sustained",
                        "pool_name": report.pool_name,
                        "utilization_pct": report.utilization_pct,
                        "sustained_seconds": round(sustained_duration, 1),
                        "leaked_connections": report.leaked_connection_count,
                    }
        else:
            self._above_threshold_since = None

        return None
```

## Solution 6: Connection Pool Health Dashboard

```python
import time
from typing import Dict, List, Optional


class ConnectionPoolHealthDashboard:
    """
    Renders health reports, utilization trends, and leak summaries
    for all monitored connection pools.
    """

    def __init__(
        self,
        monitor: ConnectionPoolHealthMonitor,
        alerter: PoolExhaustionAlerter,
        latest_reports: Optional[Dict[str, PoolHealthReport]] = None,
    ):
        self._monitor = monitor
        self._alerter = alerter
        self._latest: Dict[str, PoolHealthReport] = latest_reports or {}

    def update(self, report: PoolHealthReport) -> None:
        self._latest[report.pool_name] = report

    def render(self) -> dict:
        pools = {}
        active_alerts = []
        for pool_name, report in self._latest.items():
            alert = self._alerter.evaluate(report)
            if alert:
                active_alerts.append(alert)
            pools[pool_name] = {
                "status": report.status.value,
                "utilization_pct": report.utilization_pct,
                "leaked_connections": report.leaked_connection_count,
                "waiting_requests": report.waiting_requests,
                "trend": self._monitor.utilization_trend(pool_name, last_n=5),
                "recommendations": report.recommendations,
            }
        return {
            "generated_at": time.time(),
            "pools": pools,
            "active_alerts": active_alerts,
        }
```

## Comparison

| Approach | Utilization Tracking | Leak Detection | Health Scoring | Trend History | Alerting |
|---|---|---|---|---|---|
| ConnectionLeaseTracker | Via active count | Yes (age-based) | No | No | No |
| PoolHealthEvaluator | Via snapshot | Via leak count | Yes | No | No |
| ConnectionPoolHealthMonitor | Via snapshots | Via tracker | Via evaluator | Yes | No |
| PoolExhaustionAlerter | No | No | No | No | Yes (sustained) |
| ConnectionPoolHealthDashboard | No | No | No | Via monitor | Via alerter |

**Best for production**: Instrument connection acquire/release at the pool wrapper layer — never rely on pool implementations to surface health externally. Set `max_lease_seconds` to 2× the expected P99 tool call latency; leases exceeding this are almost certainly leaked. Alert when utilization stays above 90% for 30 seconds — at this point new requests are either queuing or failing, and the on-call team should either increase pool size or shed load. Run `purge_released()` on the lease tracker every 5 minutes to prevent unbounded memory growth.
