---
title: "Agent Doesn't Implement Bulkhead Isolation for Concurrent Session Types"
description: "Agents that share a single thread pool or connection pool across all session types allow one session type to starve others: a burst of expensive batch-processing jobs consumes all workers, leaving interactive user sessions unable to get a slot. Implement bulkhead isolation that partitions resources into named pools per session type, prevents inter-pool resource bleeding, and enforces separate concurrency limits for interactive, batch, and background workloads."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-bulkhead-isolation-for-concurrent-session-types
tags: [bulkhead, resource-isolation, session-types, concurrency-limits, starvation-prevention, pool-partitioning]
symptoms:
  - "Interactive user sessions queue behind batch jobs that monopolize the worker pool"
  - "A single runaway session type saturates resources and degrades all other sessions"
  - "No way to reserve capacity for high-priority sessions during load spikes"
  - "Batch workloads and real-time workloads share the same connection pool and compete"
  - "SLA violation for interactive sessions during batch processing windows"
---

## Why This Happens

A single asyncio semaphore with `max=20` is shared across all session types. When 20 batch jobs acquire all 20 slots, interactive sessions queue behind them. Bulkhead isolation divides the total capacity into named partitions: `interactive=8`, `batch=10`, `background=2`. Each partition has its own semaphore. Interactive sessions can never be blocked by batch jobs because they draw from separate resource pools, even if batch jobs fill their partition completely.

## Solution 1: Bulkhead Configuration

```python
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class BulkheadConfig:
    name: str
    max_concurrent: int
    queue_timeout_seconds: float = 10.0
    priority: int = 0   # higher = more important (for future preemption)


@dataclass
class BulkheadSystemConfig:
    partitions: Dict[str, BulkheadConfig] = field(default_factory=dict)

    def add(self, config: BulkheadConfig) -> "BulkheadSystemConfig":
        self.partitions[config.name] = config
        return self

    @classmethod
    def standard(cls) -> "BulkheadSystemConfig":
        return (
            cls()
            .add(BulkheadConfig("interactive", max_concurrent=8, queue_timeout_seconds=5.0, priority=10))
            .add(BulkheadConfig("batch", max_concurrent=10, queue_timeout_seconds=60.0, priority=1))
            .add(BulkheadConfig("background", max_concurrent=2, queue_timeout_seconds=120.0, priority=0))
        )
```

## Solution 2: Bulkhead Partition

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BulkheadStats:
    partition_name: str
    max_concurrent: int
    current_active: int = 0
    total_acquired: int = 0
    total_rejected: int = 0
    total_timeouts: int = 0
    current_queue_depth: int = 0


class BulkheadPartition:
    """
    A named resource pool with a concurrency limit.
    Callers acquire a slot via the context manager.
    Raises TimeoutError if no slot is available within queue_timeout_seconds.
    """

    def __init__(self, config: BulkheadConfig):
        self._config = config
        self._semaphore = asyncio.Semaphore(config.max_concurrent)
        self._active = 0
        self._queued = 0
        self._total_acquired = 0
        self._total_rejected = 0
        self._total_timeouts = 0

    async def acquire(self):
        self._queued += 1
        try:
            acquired = await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=self._config.queue_timeout_seconds,
            )
            self._queued -= 1
            self._active += 1
            self._total_acquired += 1
            return self
        except asyncio.TimeoutError:
            self._queued -= 1
            self._total_timeouts += 1
            raise TimeoutError(
                f"Bulkhead '{self._config.name}' queue timeout after "
                f"{self._config.queue_timeout_seconds}s — partition at capacity ({self._config.max_concurrent})"
            )

    def release(self) -> None:
        self._active -= 1
        self._semaphore.release()

    def stats(self) -> BulkheadStats:
        return BulkheadStats(
            partition_name=self._config.name,
            max_concurrent=self._config.max_concurrent,
            current_active=self._active,
            total_acquired=self._total_acquired,
            total_rejected=self._total_rejected,
            total_timeouts=self._total_timeouts,
            current_queue_depth=self._queued,
        )

    def utilization(self) -> float:
        return self._active / max(self._config.max_concurrent, 1)
```

## Solution 3: Bulkhead Manager

```python
import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Dict, Optional


class BulkheadManager:
    """
    Manages all bulkhead partitions.
    Callers acquire a slot from a named partition using the context manager.
    """

    def __init__(self, system_config: BulkheadSystemConfig):
        self._partitions: Dict[str, BulkheadPartition] = {
            name: BulkheadPartition(cfg)
            for name, cfg in system_config.partitions.items()
        }

    @asynccontextmanager
    async def acquire(self, partition_name: str) -> AsyncGenerator[BulkheadPartition, None]:
        partition = self._partitions.get(partition_name)
        if partition is None:
            raise KeyError(
                f"No bulkhead partition '{partition_name}'. "
                f"Available: {list(self._partitions.keys())}"
            )
        slot = await partition.acquire()
        try:
            yield partition
        finally:
            partition.release()

    def get_partition(self, name: str) -> Optional[BulkheadPartition]:
        return self._partitions.get(name)

    def all_stats(self) -> Dict[str, BulkheadStats]:
        return {name: p.stats() for name, p in self._partitions.items()}

    def total_active(self) -> int:
        return sum(p._active for p in self._partitions.values())

    def most_loaded_partition(self) -> Optional[str]:
        if not self._partitions:
            return None
        return max(self._partitions, key=lambda k: self._partitions[k].utilization())
```

## Solution 4: Session Type Classifier

```python
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class SessionClassification:
    partition_name: str
    priority: int
    reason: str


class SessionTypeClassifier:
    """
    Classifies incoming sessions into bulkhead partitions based on
    session metadata: source, user tier, estimated complexity, etc.
    """

    def classify(self, session_metadata: Dict[str, Any]) -> SessionClassification:
        source = session_metadata.get("source", "")
        user_tier = session_metadata.get("user_tier", "standard")
        is_batch = session_metadata.get("is_batch", False)
        is_background = session_metadata.get("is_background", False)

        if is_background:
            return SessionClassification("background", 0, "background job")

        if is_batch:
            return SessionClassification("batch", 1, "batch processing request")

        if user_tier in ("premium", "enterprise"):
            return SessionClassification("interactive", 10, f"premium user tier: {user_tier}")

        if source in ("api", "webhook"):
            return SessionClassification("batch", 2, f"api source: {source}")

        return SessionClassification("interactive", 5, "default interactive session")
```

## Solution 5: Bulkhead-Protected Session Runner

```python
from typing import Any, Callable, Dict


class BulkheadProtectedSessionRunner:
    """
    Wraps session execution with bulkhead acquisition.
    Classifies the session type and acquires the appropriate partition slot.
    Provides a clean interface: callers just pass session metadata and a factory.
    """

    def __init__(
        self,
        manager: BulkheadManager,
        classifier: SessionTypeClassifier,
    ):
        self._manager = manager
        self._classifier = classifier

    async def run(
        self,
        session_metadata: Dict[str, Any],
        session_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        classification = self._classifier.classify(session_metadata)
        async with self._manager.acquire(classification.partition_name):
            return await session_fn(*args, **kwargs)
```

## Solution 6: Bulkhead Health Dashboard

```python
import time


class BulkheadHealthDashboard:
    """
    Reports utilization, queue depth, and saturation alerts per partition.
    """

    def __init__(
        self,
        manager: BulkheadManager,
        saturation_threshold: float = 0.85,
    ):
        self._manager = manager
        self._saturation = saturation_threshold

    def render(self) -> dict:
        all_stats = self._manager.all_stats()
        alerts = []

        for name, stats in all_stats.items():
            utilization = stats.current_active / max(stats.max_concurrent, 1)
            if utilization >= self._saturation:
                alerts.append({
                    "partition": name,
                    "utilization": round(utilization, 3),
                    "recommendation": f"increase max_concurrent for '{name}' or reduce load",
                })
            if stats.current_queue_depth > 5:
                alerts.append({
                    "partition": name,
                    "queue_depth": stats.current_queue_depth,
                    "recommendation": f"'{name}' has deep queue — sessions may experience timeout",
                })

        return {
            "generated_at": time.time(),
            "total_active": self._manager.total_active(),
            "most_loaded": self._manager.most_loaded_partition(),
            "partitions": [
                {
                    "name": s.partition_name,
                    "active": s.current_active,
                    "max": s.max_concurrent,
                    "utilization": round(s.current_active / max(s.max_concurrent, 1), 3),
                    "queue_depth": s.current_queue_depth,
                    "total_timeouts": s.total_timeouts,
                }
                for s in all_stats.values()
            ],
            "alerts": alerts,
        }
```

## Comparison

| Approach | Partition Isolation | Timeout Enforcement | Session Classification | Utilization Monitoring | Dashboard |
|---|---|---|---|---|---|
| BulkheadPartition | Yes (per-partition semaphore) | Yes | No | Via stats() | No |
| BulkheadManager | Via partitions | Via partitions | No | Yes | No |
| SessionTypeClassifier | No | No | Yes | No | No |
| BulkheadProtectedSessionRunner | Via manager | Via manager | Via classifier | No | No |
| BulkheadHealthDashboard | No | No | No | Yes | Yes |

**Best for production**: Start with three partitions — `interactive` (tight timeout, 5s), `batch` (generous timeout, 60s), `background` (no rush, 120s). Size each partition based on the SLO: interactive sessions need low latency so the pool must rarely queue; batch can tolerate queueing. Monitor `total_timeouts` per partition — timeouts in the `interactive` partition mean users are waiting too long and the pool needs more slots. The bulkhead prevents the common failure mode where a runaway cron job or batch import starves the interactive user pool, which is otherwise invisible until users start complaining.
