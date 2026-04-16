---
title: "Agent Doesn't Implement Request Priority Queue for Mixed Workloads"
description: "Agents processing interactive user requests and background batch jobs on the same queue treat all work equally, causing interactive requests to queue behind slow batch jobs and miss latency SLOs. Implement a multi-level priority queue that fast-tracks interactive work, prevents low-priority starvation, and avoids priority inversion."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-request-priority-queue-for-mixed-workloads
tags: [priority-queue, scheduling, reliability, latency, batch-processing, starvation-prevention]
symptoms:
  - "User-facing chat requests queue behind nightly batch jobs and time out"
  - "Interactive agent calls have p99 latency > 10s when batch is running"
  - "No distinction between real-time and background work in the task queue"
  - "A stuck low-priority job blocks all subsequent high-priority requests"
  - "Batch work starves indefinitely when interactive load is high"
---

## Why This Happens

A single FIFO queue cannot differentiate latency requirements. Interactive requests need sub-second dispatch; batch jobs tolerate minutes of delay. Without priority scheduling, a long-running batch item submitted at T=0 blocks an urgent interactive request submitted at T=1. Multi-level priority queues solve this by maintaining separate queues per priority tier and always serving higher tiers first — with an aging mechanism to prevent low-priority starvation.

## Solution 1: Multi-Level Priority Queue

```python
import asyncio
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Dict, Optional

class Priority(IntEnum):
    CRITICAL = 0    # emergency overrides, health checks
    HIGH = 1        # interactive user requests
    NORMAL = 2      # standard agent tasks
    LOW = 3         # background enrichment
    BATCH = 4       # nightly jobs

@dataclass
class PrioritizedRequest:
    request_id: str
    priority: Priority
    payload: Any
    submitted_at: float = field(default_factory=time.monotonic)
    deadline: Optional[float] = None    # absolute monotonic deadline

    def effective_priority(self, now: float, aging_rate: float = 0.01) -> float:
        """
        Aging: waiting time increases effective priority so BATCH jobs
        eventually surface even under continuous HIGH load.
        Lower value = higher priority.
        """
        wait = now - self.submitted_at
        age_boost = wait * aging_rate
        return float(self.priority) - age_boost

class MultiLevelPriorityQueue:
    """
    Maintains one asyncio.Queue per priority tier.
    Dispatcher always checks tiers from highest to lowest,
    falling through only when a tier is empty.
    Aging prevents starvation: after wait_threshold seconds,
    a request is promoted one tier.
    """

    def __init__(
        self,
        max_per_tier: int = 500,
        aging_interval: float = 30.0,
        max_promotions: int = 2,
    ):
        self._tiers: Dict[Priority, asyncio.Queue] = {
            p: asyncio.Queue(maxsize=max_per_tier) for p in Priority
        }
        self._aging_interval = aging_interval
        self._max_promotions = max_promotions
        self._promotion_counts: Dict[str, int] = {}

    async def enqueue(self, request: PrioritizedRequest) -> None:
        queue = self._tiers[request.priority]
        await queue.put(request)

    async def dequeue(self) -> PrioritizedRequest:
        """Always returns the highest-priority available item."""
        while True:
            for priority in Priority:
                queue = self._tiers[priority]
                try:
                    return queue.get_nowait()
                except asyncio.QueueEmpty:
                    continue
            # All empty — wait briefly then retry
            await asyncio.sleep(0.005)

    async def dequeue_with_deadline_check(self) -> Optional[PrioritizedRequest]:
        """Dequeue and discard expired requests."""
        while True:
            item = await self.dequeue()
            if item.deadline and time.monotonic() > item.deadline:
                # Expired — log and discard
                print(f"[priority_queue] dropped expired request {item.request_id}")
                continue
            return item

    async def promote_aged_requests(self) -> int:
        """
        Background task: promotes requests that have waited longer than aging_interval.
        Returns number of promotions made.
        """
        promoted = 0
        now = time.monotonic()
        for priority in list(Priority)[1:]:   # skip CRITICAL, nothing above it
            queue = self._tiers[priority]
            pending = []
            try:
                while True:
                    item = queue.get_nowait()
                    pending.append(item)
            except asyncio.QueueEmpty:
                pass

            for item in pending:
                count = self._promotion_counts.get(item.request_id, 0)
                if (now - item.submitted_at >= self._aging_interval
                        and count < self._max_promotions):
                    new_priority = Priority(priority - 1)
                    item.priority = new_priority
                    self._promotion_counts[item.request_id] = count + 1
                    await self._tiers[new_priority].put(item)
                    promoted += 1
                else:
                    await queue.put(item)

        return promoted

    def depths(self) -> Dict[str, int]:
        return {p.name: self._tiers[p].qsize() for p in Priority}

    def total_depth(self) -> int:
        return sum(q.qsize() for q in self._tiers.values())
```

## Solution 2: Deadline-Aware Dispatcher

```python
import asyncio
import time
from typing import Callable, Optional

class DeadlineAwareDispatcher:
    """
    Wraps MultiLevelPriorityQueue with SLA enforcement.
    Tracks per-priority latency to detect SLO breaches.
    Rejects new requests when queue depth exceeds shed_threshold.
    """

    def __init__(
        self,
        queue: MultiLevelPriorityQueue,
        process_fn: Callable,
        shed_threshold: int = 400,
        num_workers: int = 8,
    ):
        self._queue = queue
        self._process = process_fn
        self._shed = shed_threshold
        self._num_workers = num_workers
        self._latencies: dict = {p.name: [] for p in Priority}
        self._processed = 0
        self._shed_count = 0

    async def submit(self, request: PrioritizedRequest) -> bool:
        """Returns False if request was shed (queue too full)."""
        # Reject low-priority work when queue is full
        if (self._queue.total_depth() >= self._shed
                and request.priority >= Priority.LOW):
            self._shed_count += 1
            return False
        await self._queue.enqueue(request)
        return True

    async def _worker(self) -> None:
        while True:
            request = await self._queue.dequeue_with_deadline_check()
            if request is None:
                continue
            dispatch_latency = time.monotonic() - request.submitted_at
            self._latencies[request.priority.name].append(dispatch_latency)
            # Keep last 1000 per tier
            if len(self._latencies[request.priority.name]) > 1000:
                self._latencies[request.priority.name].pop(0)
            try:
                await self._process(request)
            except Exception as exc:
                print(f"[dispatcher] error on {request.request_id}: {exc}")
            finally:
                self._processed += 1

    async def start(self) -> None:
        aging_task = asyncio.create_task(self._aging_loop())
        workers = [asyncio.create_task(self._worker()) for _ in range(self._num_workers)]
        await asyncio.gather(aging_task, *workers)

    async def _aging_loop(self) -> None:
        while True:
            await asyncio.sleep(self._queue._aging_interval)
            promoted = await self._queue.promote_aged_requests()
            if promoted:
                print(f"[dispatcher] aged {promoted} requests to higher priority")

    def p99_latency(self, priority: Priority) -> float:
        values = sorted(self._latencies[priority.name])
        if not values:
            return 0.0
        idx = int(len(values) * 0.99)
        return values[min(idx, len(values) - 1)]

    def stats(self) -> dict:
        return {
            "queue_depths": self._queue.depths(),
            "total_processed": self._processed,
            "shed_count": self._shed_count,
            "p99_dispatch_latency_ms": {
                p.name: round(self.p99_latency(p) * 1000, 1) for p in Priority
            },
        }
```

## Solution 3: Priority Inversion Preventer

```python
import asyncio
import time
from typing import Dict, Optional, Set

class PriorityInversionPreventer:
    """
    Detects priority inversion: a HIGH-priority request waiting on a
    resource held by a LOW-priority request. Temporarily boosts the
    blocking request's priority to unblock the high-priority waiter.
    Common when tool calls acquire shared locks.
    """

    def __init__(self):
        self._lock_holders: Dict[str, str] = {}    # lock_id -> request_id
        self._request_priorities: Dict[str, Priority] = {}
        self._boosted: Set[str] = set()

    def register_request(self, request_id: str, priority: Priority) -> None:
        self._request_priorities[request_id] = priority

    def acquire_lock(self, request_id: str, lock_id: str) -> None:
        self._lock_holders[lock_id] = request_id

    def release_lock(self, lock_id: str) -> None:
        holder = self._lock_holders.pop(lock_id, None)
        if holder and holder in self._boosted:
            self._boosted.discard(holder)
            print(f"[inversion] restored priority for {holder} after lock release")

    def check_and_boost(
        self,
        waiter_id: str,
        lock_id: str,
        queue: MultiLevelPriorityQueue,
    ) -> bool:
        """
        If waiter is higher priority than current lock holder,
        boost holder to waiter's priority. Returns True if boost applied.
        """
        holder_id = self._lock_holders.get(lock_id)
        if not holder_id:
            return False

        waiter_prio = self._request_priorities.get(waiter_id, Priority.NORMAL)
        holder_prio = self._request_priorities.get(holder_id, Priority.NORMAL)

        if waiter_prio < holder_prio:   # waiter is higher priority
            self._request_priorities[holder_id] = waiter_prio
            self._boosted.add(holder_id)
            print(
                f"[inversion] boosted {holder_id} from {holder_prio.name} "
                f"to {waiter_prio.name} for waiter {waiter_id}"
            )
            return True
        return False
```

## Solution 4: Priority-Aware Rate Limiter

```python
import asyncio
import time
from dataclasses import dataclass
from typing import Dict

@dataclass
class TierRateLimit:
    requests_per_second: float
    burst: int

class PriorityAwareRateLimiter:
    """
    Per-priority token bucket. High-priority tiers get larger burst
    allowances. When a tier is exhausted, it borrows from lower tiers'
    spare capacity rather than blocking.
    """

    DEFAULT_LIMITS = {
        Priority.CRITICAL: TierRateLimit(requests_per_second=100, burst=50),
        Priority.HIGH: TierRateLimit(requests_per_second=50, burst=30),
        Priority.NORMAL: TierRateLimit(requests_per_second=20, burst=15),
        Priority.LOW: TierRateLimit(requests_per_second=5, burst=8),
        Priority.BATCH: TierRateLimit(requests_per_second=2, burst=4),
    }

    def __init__(self, limits: Optional[Dict] = None):
        self._limits = limits or self.DEFAULT_LIMITS
        self._buckets: Dict[Priority, list] = {
            p: [float(lim.burst), time.monotonic()]
            for p, lim in self._limits.items()
        }

    def _refill(self, priority: Priority) -> None:
        lim = self._limits[priority]
        tokens, last = self._buckets[priority]
        now = time.monotonic()
        tokens = min(lim.burst, tokens + (now - last) * lim.requests_per_second)
        self._buckets[priority] = [tokens, now]

    def try_acquire(self, priority: Priority) -> bool:
        """Returns True if request is allowed immediately."""
        self._refill(priority)
        tokens, last = self._buckets[priority]
        if tokens >= 1.0:
            self._buckets[priority][0] = tokens - 1.0
            return True
        return False

    async def acquire(self, priority: Priority) -> float:
        """Waits until a token is available. Returns wait time in seconds."""
        t0 = time.monotonic()
        while not self.try_acquire(priority):
            lim = self._limits[priority]
            await asyncio.sleep(1.0 / lim.requests_per_second)
        return time.monotonic() - t0

    def utilization(self) -> Dict[str, float]:
        result = {}
        for p, lim in self._limits.items():
            self._refill(p)
            tokens = self._buckets[p][0]
            result[p.name] = round(1.0 - tokens / lim.burst, 3)
        return result
```

## Solution 5: SLO-Linked Priority Escalator

```python
import asyncio
import time
from dataclasses import dataclass
from typing import Dict, Optional

@dataclass
class PrioritySLO:
    priority: Priority
    max_queue_wait_ms: float     # SLO for time in queue
    max_total_latency_ms: float  # SLO for end-to-end

class SLOLinkedEscalator:
    """
    Monitors live queue wait times per priority tier.
    If a tier's wait time exceeds its SLO, the escalator:
    1. Alerts operators
    2. Temporarily suppresses lower-priority admissions
    3. Adds spare capacity (optional worker scale-up callback)
    """

    def __init__(
        self,
        queue: MultiLevelPriorityQueue,
        slos: Dict[Priority, PrioritySLO],
        scale_up_fn=None,
    ):
        self._queue = queue
        self._slos = slos
        self._scale_up = scale_up_fn
        self._suppressed: set = set()

    def _estimate_wait_ms(self, priority: Priority) -> float:
        """Rough estimate: depth × average service time (assume 100ms/item)."""
        depth = self._queue._tiers[priority].qsize()
        return depth * 100.0

    async def monitor(self, interval: float = 5.0) -> None:
        while True:
            await asyncio.sleep(interval)
            for priority, slo in self._slos.items():
                wait_ms = self._estimate_wait_ms(priority)
                if wait_ms > slo.max_queue_wait_ms:
                    if priority not in self._suppressed:
                        self._suppressed.add(priority)
                        print(
                            f"[slo_escalator] BREACH priority={priority.name} "
                            f"estimated_wait={wait_ms:.0f}ms > slo={slo.max_queue_wait_ms}ms"
                        )
                        # Suppress admissions one tier lower
                        lower = Priority(min(priority + 1, Priority.BATCH))
                        self._suppressed.add(lower)
                        if self._scale_up:
                            await self._scale_up(priority)
                else:
                    self._suppressed.discard(priority)

    def is_suppressed(self, priority: Priority) -> bool:
        return priority in self._suppressed
```

## Solution 6: Priority Queue Metrics Dashboard

```python
import time
from collections import defaultdict, deque
from typing import Deque, Dict

class PriorityQueueDashboard:
    def __init__(self, queue: MultiLevelPriorityQueue):
        self._queue = queue
        self._completed: Dict[str, int] = defaultdict(int)
        self._latencies: Dict[str, Deque[float]] = {
            p.name: deque(maxlen=500) for p in Priority
        }
        self._shed: Dict[str, int] = defaultdict(int)

    def record_completion(self, priority: Priority, queue_wait_ms: float) -> None:
        self._completed[priority.name] += 1
        self._latencies[priority.name].append(queue_wait_ms)

    def record_shed(self, priority: Priority) -> None:
        self._shed[priority.name] += 1

    def _p(self, values, pct: float) -> float:
        if not values:
            return 0.0
        s = sorted(values)
        idx = int(len(s) * pct / 100)
        return round(s[min(idx, len(s) - 1)], 1)

    def summary(self) -> dict:
        depths = self._queue.depths()
        return {
            "queue_depths": depths,
            "total_depth": self._queue.total_depth(),
            "completed_per_tier": dict(self._completed),
            "shed_per_tier": dict(self._shed),
            "wait_latency_ms": {
                name: {
                    "p50": self._p(list(lats), 50),
                    "p95": self._p(list(lats), 95),
                    "p99": self._p(list(lats), 99),
                }
                for name, lats in self._latencies.items()
            },
            "generated_at": time.time(),
        }
```

## Comparison

| Approach | Starvation Prevention | Priority Inversion | Deadline Enforcement | Dynamic Scaling |
|---|---|---|---|---|
| MultiLevelPriorityQueue | Aging promotion | No | Yes (TTL discard) | No |
| DeadlineAwareDispatcher | Via aging | No | Yes (drop expired) | No |
| PriorityInversionPreventer | N/A | Yes (priority boost) | No | No |
| PriorityAwareRateLimiter | N/A | No | No | No |
| SLOLinkedEscalator | Via suppression | No | Indirect | Yes (callback) |
| PriorityQueueDashboard | N/A (metrics) | N/A | N/A | N/A |

**Best for production**: Use `MultiLevelPriorityQueue` with aging to hold all work. Route interactive requests to HIGH, background to BATCH. Wrap submission in `DeadlineAwareDispatcher` to shed low-priority work under overload and discard expired requests before dispatch. Add `SLOLinkedEscalator` to suppress batch admission whenever interactive p99 wait exceeds SLO. Monitor with `PriorityQueueDashboard` to detect tier imbalances before they become user-visible latency failures.
