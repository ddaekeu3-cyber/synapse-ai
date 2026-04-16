---
title: "Agent Doesn't Implement Weighted Fair Queuing for Multi-Tenant Agents"
description: "AI agents serving multiple tenants with a shared worker pool can let high-volume tenants starve low-volume ones. Weighted fair queuing (WFQ) allocates processing capacity proportionally to each tenant's weight, guaranteeing minimum throughput for all while allowing burst usage when capacity is available."
date: 2025-02-04
difficulty: advanced
category: performance
slug: agent-doesnt-implement-weighted-fair-queuing-for-multi-tenant-agents
tags:
  - weighted-fair-queuing
  - multi-tenant
  - fairness
  - scheduling
  - queue
  - performance
  - resource-allocation
symptoms:
  - "One high-traffic tenant monopolizes the agent worker pool for seconds at a time"
  - "Low-tier tenants see 10× higher latency than high-tier tenants under shared load"
  - "Tenant SLA guarantees are violated because there is no per-tenant queue isolation"
  - "Adding more workers helps average latency but not tail latency for small tenants"
  - "No way to give premium tenants priority without starving free-tier tenants completely"
---

## Problem

Shared agent infrastructure without per-tenant scheduling allows high-volume tenants to monopolise the worker pool. Under load, one tenant submitting 1000 requests/second will starve a tenant submitting 10 requests/second — even though both pay for service.

Weighted Fair Queuing guarantees each tenant a share of capacity proportional to their weight. A tenant with weight 2 gets twice the throughput of a tenant with weight 1, but a tenant with no pending work yields its share to active tenants automatically.

---

## Solution 1: Deficit Round-Robin Scheduler

DRR is a practical WFQ approximation. Each tenant has a deficit counter that accumulates a quantum each round; requests are served until the deficit is exhausted, then the scheduler moves to the next tenant.

```python
import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TenantQueue:
    tenant_id: str
    weight: float              # relative throughput share
    quantum: float             # bytes or cost units per DRR round
    deficit: float = 0.0
    queue: deque = field(default_factory=deque)
    total_served: int = 0
    total_dropped: int = 0


@dataclass
class QueuedTask:
    task_id: str
    tenant_id: str
    payload: Any
    cost: float = 1.0          # task cost in DRR units
    future: Optional[asyncio.Future] = None


class DeficitRoundRobinScheduler:
    """
    DRR weighted fair queue for multi-tenant agent work.

    Usage:
        scheduler = DeficitRoundRobinScheduler()
        scheduler.add_tenant("premium",  weight=4, capacity=100)
        scheduler.add_tenant("standard", weight=2, capacity=100)
        scheduler.add_tenant("free",     weight=1, capacity=50)

        # Producer:
        fut = await scheduler.enqueue("premium", task_payload, cost=1.0)
        result = await fut   # resolves when task is processed

        # Consumer (worker):
        asyncio.create_task(scheduler.run(worker_fn))
    """

    def __init__(self, base_quantum: float = 10.0):
        self._base_quantum = base_quantum
        self._tenants: Dict[str, TenantQueue] = {}
        self._order: List[str] = []
        self._task_ready = asyncio.Event()

    def add_tenant(self, tenant_id: str, weight: float = 1.0,
                   capacity: int = 1000):
        self._tenants[tenant_id] = TenantQueue(
            tenant_id=tenant_id,
            weight=weight,
            quantum=weight * self._base_quantum,
        )
        self._order.append(tenant_id)

    async def enqueue(self, tenant_id: str, payload: Any,
                      cost: float = 1.0) -> asyncio.Future:
        tenant = self._tenants.get(tenant_id)
        if tenant is None:
            raise KeyError(f"Unknown tenant: {tenant_id}")
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        task = QueuedTask(
            task_id=f"{tenant_id}-{len(tenant.queue)}",
            tenant_id=tenant_id,
            payload=payload,
            cost=cost,
            future=fut,
        )
        tenant.queue.append(task)
        self._task_ready.set()
        return fut

    async def run(self, worker_fn):
        """Drive the DRR scheduler and execute tasks via worker_fn."""
        while True:
            await self._task_ready.wait()
            self._task_ready.clear()
            served_any = True
            while served_any:
                served_any = False
                for tid in self._order:
                    tenant = self._tenants[tid]
                    if not tenant.queue:
                        continue
                    tenant.deficit += tenant.quantum
                    while tenant.queue and tenant.deficit > 0:
                        task = tenant.queue.popleft()
                        tenant.deficit -= task.cost
                        tenant.total_served += 1
                        served_any = True
                        try:
                            result = await worker_fn(task.payload)
                            if task.future and not task.future.done():
                                task.future.set_result(result)
                        except Exception as exc:
                            if task.future and not task.future.done():
                                task.future.set_exception(exc)
                    if tenant.deficit < 0:
                        tenant.deficit = 0

    def stats(self) -> Dict[str, dict]:
        return {
            tid: {
                "weight": t.weight,
                "queue_depth": len(t.queue),
                "total_served": t.total_served,
                "deficit": round(t.deficit, 2),
            }
            for tid, t in self._tenants.items()
        }
```

---

## Solution 2: Token Bucket WFQ (Per-Tenant Rate + Burst)

Each tenant has a token bucket: a steady fill rate proportional to weight and a burst capacity. Tokens are consumed per request; a tenant with no tokens waits while others run.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class TenantBucket:
    tenant_id: str
    rate: float          # tokens per second (proportional to weight)
    capacity: float      # burst capacity in tokens
    tokens: float = 0.0
    last_refill: float = field(default_factory=time.monotonic)
    waiters: asyncio.Queue = field(default_factory=asyncio.Queue)

    def refill(self):
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last_refill = now

    def consume(self, cost: float = 1.0) -> bool:
        self.refill()
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False


class TokenBucketWFQ:
    """
    Per-tenant token-bucket weighted fair queue.
    Tenants with higher weight fill faster and burst more.

    Usage:
        wfq = TokenBucketWFQ(total_capacity=100.0)
        wfq.add_tenant("premium",  weight=4)   # 40 req/s, burst 40
        wfq.add_tenant("standard", weight=2)   # 20 req/s, burst 20
        wfq.add_tenant("free",     weight=1)   # 10 req/s, burst 10

        async with wfq.acquire("premium"):     # blocks until token available
            result = await process_request()
    """

    def __init__(self, total_capacity: float = 100.0):
        self._total = total_capacity
        self._tenants: Dict[str, TenantBucket] = {}
        self._total_weight: float = 0.0

    def add_tenant(self, tenant_id: str, weight: float = 1.0):
        self._total_weight += weight
        # Recompute all rates
        for bucket in self._tenants.values():
            bucket.rate = (bucket.rate / (self._total_weight - weight)) * self._total_weight
        rate = (weight / self._total_weight) * self._total
        self._tenants[tenant_id] = TenantBucket(
            tenant_id=tenant_id,
            rate=rate,
            capacity=rate * 2,   # 2-second burst
            tokens=rate,
        )

    def acquire(self, tenant_id: str, cost: float = 1.0) -> "_BucketContext":
        return _BucketContext(self._tenants[tenant_id], cost)

    def current_rates(self) -> Dict[str, float]:
        return {tid: round(b.rate, 2) for tid, b in self._tenants.items()}


class _BucketContext:
    def __init__(self, bucket: TenantBucket, cost: float):
        self._bucket = bucket
        self._cost = cost

    async def __aenter__(self):
        while True:
            if self._bucket.consume(self._cost):
                return self
            # Wait until tokens refill
            deficit = self._cost - self._bucket.tokens
            wait = deficit / max(0.001, self._bucket.rate)
            await asyncio.sleep(wait)

    async def __aexit__(self, *_):
        pass
```

---

## Solution 3: Weighted Min-Heap Priority Queue

Uses a virtual-time-based scheduler (similar to Stochastic Fair Queuing). Each task is tagged with a virtual finish time; tasks are dequeued in virtual finish time order, which produces fair service.

```python
import asyncio
import heapq
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass(order=True)
class VirtualTimedTask:
    virtual_finish: float
    arrival: float = field(compare=False)
    tenant_id: str = field(compare=False)
    payload: Any = field(compare=False)
    future: Optional[asyncio.Future] = field(default=None, compare=False)


class VirtualTimeScheduler:
    """
    Fair scheduling using virtual finish times.
    tenant with weight W processes at rate proportional to W.
    Virtual time advances based on the slowest active tenant.

    Usage:
        sched = VirtualTimeScheduler()
        sched.add_tenant("premium",  weight=3.0)
        sched.add_tenant("standard", weight=1.0)

        fut = sched.enqueue("premium",  payload)
        fut2 = sched.enqueue("standard", payload)
        asyncio.create_task(sched.drain(worker_fn))
    """

    def __init__(self):
        self._weights: Dict[str, float] = {}
        self._vtime: Dict[str, float] = {}   # per-tenant virtual time
        self._heap: List[VirtualTimedTask] = []
        self._event = asyncio.Event()

    def add_tenant(self, tenant_id: str, weight: float = 1.0):
        self._weights[tenant_id] = weight
        self._vtime[tenant_id] = 0.0

    def enqueue(self, tenant_id: str, payload: Any,
                cost: float = 1.0) -> asyncio.Future:
        if tenant_id not in self._weights:
            raise KeyError(f"Unknown tenant: {tenant_id}")
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        w = self._weights[tenant_id]
        v_start = max(self._vtime[tenant_id], self._global_vtime())
        v_finish = v_start + cost / w
        self._vtime[tenant_id] = v_finish
        task = VirtualTimedTask(
            virtual_finish=v_finish,
            arrival=time.monotonic(),
            tenant_id=tenant_id,
            payload=payload,
            future=fut,
        )
        heapq.heappush(self._heap, task)
        self._event.set()
        return fut

    def _global_vtime(self) -> float:
        return min(self._vtime.values()) if self._vtime else 0.0

    async def drain(self, worker_fn):
        while True:
            await self._event.wait()
            self._event.clear()
            while self._heap:
                task = heapq.heappop(self._heap)
                try:
                    result = await worker_fn(task.payload)
                    if task.future and not task.future.done():
                        task.future.set_result(result)
                except Exception as exc:
                    if task.future and not task.future.done():
                        task.future.set_exception(exc)
```

---

## Solution 4: Per-Tenant Concurrency Limiter

Each tenant gets a semaphore with a capacity proportional to its weight. Ensures no single tenant can consume more than its fair share of concurrent worker slots.

```python
import asyncio
from dataclasses import dataclass
from typing import Dict


@dataclass
class TenantConcurrencyLimit:
    tenant_id: str
    weight: float
    max_concurrent: int
    semaphore: asyncio.Semaphore


class PerTenantConcurrencyLimiter:
    """
    Allocates concurrent capacity per tenant proportional to weight.

    Usage:
        limiter = PerTenantConcurrencyLimiter(total_concurrency=20)
        limiter.add_tenant("premium",  weight=4)  # 10 slots (4/8 * 20)
        limiter.add_tenant("standard", weight=2)  # 5 slots
        limiter.add_tenant("free",     weight=2)  # 5 slots

        async with limiter.acquire("premium"):
            result = await process()
    """

    def __init__(self, total_concurrency: int = 20):
        self._total = total_concurrency
        self._tenants: Dict[str, TenantConcurrencyLimit] = {}
        self._total_weight = 0.0

    def add_tenant(self, tenant_id: str, weight: float = 1.0):
        self._total_weight += weight
        # Recompute all slot allocations
        for limit in self._tenants.values():
            new_max = max(1, int(
                (limit.weight / self._total_weight) * self._total
            ))
            if new_max != limit.max_concurrent:
                limit.max_concurrent = new_max
                limit.semaphore = asyncio.Semaphore(new_max)
        my_max = max(1, int((weight / self._total_weight) * self._total))
        self._tenants[tenant_id] = TenantConcurrencyLimit(
            tenant_id=tenant_id,
            weight=weight,
            max_concurrent=my_max,
            semaphore=asyncio.Semaphore(my_max),
        )

    def acquire(self, tenant_id: str) -> "_TenantSemaphoreContext":
        limit = self._tenants.get(tenant_id)
        if limit is None:
            raise KeyError(f"Unknown tenant: {tenant_id}")
        return _TenantSemaphoreContext(limit)

    def stats(self) -> Dict[str, dict]:
        return {
            tid: {
                "weight": t.weight,
                "max_concurrent": t.max_concurrent,
                "available_slots": t.semaphore._value,
            }
            for tid, t in self._tenants.items()
        }


class _TenantSemaphoreContext:
    def __init__(self, limit: TenantConcurrencyLimit):
        self._limit = limit

    async def __aenter__(self):
        await self._limit.semaphore.acquire()
        return self

    async def __aexit__(self, *_):
        self._limit.semaphore.release()
```

---

## Solution 5: WFQ Metrics Collector

Tracks per-tenant throughput, latency, and fairness metrics. Detects when a tenant's actual throughput deviates significantly from its weighted allocation.

```python
import statistics
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List


@dataclass
class TenantMetricsSample:
    timestamp: float
    tenant_id: str
    latency_ms: float
    success: bool


class WFQMetricsCollector:
    """
    Tracks per-tenant queue metrics and detects fairness violations.

    Usage:
        metrics = WFQMetricsCollector(window=60.0)
        metrics.record("premium",  latency_ms=45.0, success=True)
        metrics.record("free",     latency_ms=400.0, success=True)

        report = metrics.fairness_report(weights={"premium": 4, "free": 1})
    """

    def __init__(self, window: float = 60.0):
        self._window = window
        self._samples: Deque[TenantMetricsSample] = deque()
        self._counts: Dict[str, int] = defaultdict(int)

    def record(self, tenant_id: str, latency_ms: float, success: bool = True):
        self._samples.append(TenantMetricsSample(
            timestamp=time.time(), tenant_id=tenant_id,
            latency_ms=latency_ms, success=success,
        ))
        self._counts[tenant_id] += 1
        self._evict()

    def _evict(self):
        cutoff = time.time() - self._window
        while self._samples and self._samples[0].timestamp < cutoff:
            self._samples.popleft()

    def per_tenant_stats(self) -> Dict[str, dict]:
        self._evict()
        by_tenant: Dict[str, List[float]] = defaultdict(list)
        errors: Dict[str, int] = defaultdict(int)
        for s in self._samples:
            by_tenant[s.tenant_id].append(s.latency_ms)
            if not s.success:
                errors[s.tenant_id] += 1
        result = {}
        for tid, latencies in by_tenant.items():
            result[tid] = {
                "requests": len(latencies),
                "rps": round(len(latencies) / self._window, 2),
                "p50_ms": round(statistics.median(latencies), 1),
                "p99_ms": round(sorted(latencies)[int(len(latencies) * 0.99)], 1),
                "error_rate": round(errors[tid] / max(1, len(latencies)), 4),
            }
        return result

    def fairness_report(self, weights: Dict[str, float]) -> dict:
        stats = self.per_tenant_stats()
        total_weight = sum(weights.values())
        total_rps = sum(s["rps"] for s in stats.values())
        report = {}
        for tid, w in weights.items():
            expected_fraction = w / total_weight
            actual_rps = stats.get(tid, {}).get("rps", 0.0)
            actual_fraction = actual_rps / max(0.001, total_rps)
            report[tid] = {
                "expected_share_pct": round(expected_fraction * 100, 1),
                "actual_share_pct": round(actual_fraction * 100, 1),
                "fairness_ratio": round(actual_fraction / max(0.001, expected_fraction), 3),
                "starved": actual_fraction < expected_fraction * 0.5,
            }
        return report
```

---

## Solution 6: Unified Multi-Tenant Agent Request Manager

Combines DRR scheduling, per-tenant concurrency limits, and fairness metrics into a single facade.

```python
import asyncio
import time
from typing import Any, Callable, Dict, Optional


class MultiTenantAgentRequestManager:
    """
    Unified WFQ request manager for multi-tenant AI agents.

    Usage:
        mgr = MultiTenantAgentRequestManager(worker_concurrency=20)
        mgr.add_tenant("enterprise", weight=5, monthly_quota=100_000)
        mgr.add_tenant("pro",        weight=2, monthly_quota=20_000)
        mgr.add_tenant("free",       weight=1, monthly_quota=1_000)

        await mgr.start(worker_fn)

        result = await mgr.submit("enterprise", payload)
        report = mgr.fairness_report()
    """

    def __init__(self, worker_concurrency: int = 20):
        self._scheduler = DeficitRoundRobinScheduler()
        self._limiter = PerTenantConcurrencyLimiter(worker_concurrency)
        self._metrics = WFQMetricsCollector()
        self._weights: Dict[str, float] = {}

    def add_tenant(self, tenant_id: str, weight: float = 1.0,
                   monthly_quota: Optional[int] = None):
        self._scheduler.add_tenant(tenant_id, weight=weight)
        self._limiter.add_tenant(tenant_id, weight=weight)
        self._weights[tenant_id] = weight

    async def start(self, worker_fn: Callable):
        async def rate_limited_worker(payload):
            # Extract tenant from payload wrapper
            tenant_id = payload.get("tenant_id", "default")
            async with self._limiter.acquire(tenant_id):
                t0 = time.monotonic()
                try:
                    result = await worker_fn(payload["data"])
                    ms = (time.monotonic() - t0) * 1000
                    self._metrics.record(tenant_id, ms, success=True)
                    return result
                except Exception as exc:
                    ms = (time.monotonic() - t0) * 1000
                    self._metrics.record(tenant_id, ms, success=False)
                    raise

        asyncio.create_task(self._scheduler.run(rate_limited_worker))

    async def submit(self, tenant_id: str, payload: Any) -> Any:
        wrapped = {"tenant_id": tenant_id, "data": payload}
        return await await self._scheduler.enqueue(tenant_id, wrapped)

    def fairness_report(self) -> dict:
        return {
            "fairness": self._metrics.fairness_report(self._weights),
            "queue_stats": self._scheduler.stats(),
            "concurrency": self._limiter.stats(),
        }
```

---

## Comparison

| Approach | Scheduling Algorithm | Burst Support | Implementation Complexity |
|---|---|---|---|
| **Deficit Round-Robin** | DRR (O(n) per round) | Via deficit accumulation | Low |
| **Token Bucket WFQ** | Token bucket per tenant | Yes (bucket capacity) | Low |
| **Virtual Time Scheduler** | SFQ / WFQ (heap-based) | No | Medium |
| **Per-Tenant Concurrency** | Semaphore-based | No | Low |
| **WFQ Metrics Collector** | N/A (observability) | N/A | Low |
| **Unified Request Manager** | DRR + concurrency | Via DRR | Medium |

**Recommendation**: start with Per-Tenant Concurrency Limiting as the simplest solution; add Deficit Round-Robin if queue depths vary significantly between tenants; add the Metrics Collector to detect starvation before it affects SLAs.
