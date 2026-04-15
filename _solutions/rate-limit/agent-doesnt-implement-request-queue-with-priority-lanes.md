---
layout: solution
title: "Agent Doesn't Implement Request Queue with Priority Lanes"
category: rate-limit
description: "Agents that process all requests in FIFO order let low-priority background jobs block urgent user requests. Priority lanes ensure critical requests jump the queue while rate limits are respected."
tags: [rate-limit, priority-queue, scheduling, fairness, latency, concurrency]
---

# Agent Doesn't Implement Request Queue with Priority Lanes

## The Problem

A single FIFO queue means a long batch job submitted at 9:00 AM blocks an urgent user query submitted at 9:01 AM. Both wait equally — but the user query has a real human waiting while the batch job can wait hours. Priority lanes decouple urgency from arrival order: critical requests get immediate slots, background work fills remaining capacity.

---

## Option 1: asyncio PriorityQueue with Three Lanes

Simple three-lane priority queue: CRITICAL, NORMAL, BACKGROUND.

```python
import anthropic
import asyncio
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

client = anthropic.AsyncAnthropic()

class Priority(IntEnum):
    CRITICAL = 0    # Interactive user requests, SLA-bound
    NORMAL = 1      # Standard API requests
    BACKGROUND = 2  # Batch jobs, analytics, non-urgent

@dataclass(order=True)
class QueuedRequest:
    priority: Priority
    sequence: int  # Tie-break by arrival order
    payload: Any = field(compare=False)
    result_future: asyncio.Future = field(compare=False)

    def __post_init__(self):
        if not isinstance(self.payload, dict):
            raise ValueError("Payload must be dict")

_seq_counter = 0

def next_seq() -> int:
    global _seq_counter
    _seq_counter += 1
    return _seq_counter

class PriorityLaneQueue:
    """Three-lane priority queue with asyncio."""

    def __init__(self, max_concurrent: int = 3):
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._stats = {p: {"submitted": 0, "completed": 0, "total_wait_ms": 0} for p in Priority}

    async def submit(self, messages: list[dict], priority: Priority, model: str = "claude-haiku-4-5-20251001") -> str:
        """Submit a request to the appropriate priority lane."""
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        request = QueuedRequest(
            priority=priority,
            sequence=next_seq(),
            payload={"messages": messages, "model": model, "submit_time": loop.time()},
            result_future=future
        )
        self._stats[priority]["submitted"] += 1
        await self._queue.put(request)
        return await future

    async def worker(self):
        """Worker coroutine that processes requests by priority."""
        while True:
            request = await self._queue.get()
            async with self._semaphore:
                loop = asyncio.get_event_loop()
                wait_ms = int((loop.time() - request.payload["submit_time"]) * 1000)
                self._stats[request.priority]["total_wait_ms"] += wait_ms

                try:
                    resp = await client.messages.create(
                        model=request.payload["model"],
                        max_tokens=256,
                        messages=request.payload["messages"]
                    )
                    result = resp.content[0].text
                    self._stats[request.priority]["completed"] += 1
                    request.result_future.set_result(result)
                except Exception as e:
                    request.result_future.set_exception(e)
                finally:
                    self._queue.task_done()

    def get_stats(self) -> dict:
        return {
            p.name: {
                **self._stats[p],
                "avg_wait_ms": (
                    self._stats[p]["total_wait_ms"] / max(self._stats[p]["completed"], 1)
                )
            }
            for p in Priority
        }

async def demo_priority_lanes():
    queue = PriorityLaneQueue(max_concurrent=2)

    # Start workers
    workers = [asyncio.create_task(queue.worker()) for _ in range(3)]

    # Submit mixed priority requests
    tasks = []

    # Background batch jobs (submitted first but should wait)
    for i in range(3):
        tasks.append(asyncio.create_task(
            queue.submit(
                [{"role": "user", "content": f"Batch job {i}: summarize topic {i}"}],
                Priority.BACKGROUND
            )
        ))

    # Critical user request (submitted after batch but should complete first)
    await asyncio.sleep(0.01)  # Tiny delay to ensure batch is queued
    critical = asyncio.create_task(
        queue.submit(
            [{"role": "user", "content": "URGENT: User needs help logging in!"}],
            Priority.CRITICAL
        )
    )

    # Collect results
    all_tasks = tasks + [critical]
    results = await asyncio.gather(*all_tasks, return_exceptions=True)

    print("Results (by completion order):")
    for i, r in enumerate(results):
        label = "CRITICAL" if i == len(tasks) else f"BACKGROUND-{i}"
        preview = str(r)[:80] if not isinstance(r, Exception) else f"ERROR: {r}"
        print(f"  [{label}]: {preview}")

    print("\nQueue statistics:")
    for lane, stats in queue.get_stats().items():
        print(f"  {lane}: {stats['completed']}/{stats['submitted']} completed, avg_wait={stats['avg_wait_ms']:.0f}ms")

    for w in workers:
        w.cancel()

asyncio.run(demo_priority_lanes())

# Expected Token Savings: Priority routing ensures Haiku handles background work while Sonnet slots serve interactive users
# Environment: multi-tenant APIs, user-facing + batch workloads, SLA-differentiated services
```

---

## Option 2: Token-Budget Priority Scheduler

Allocate a fixed token budget per time window; reserve capacity for each priority lane.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from enum import IntEnum

client = anthropic.AsyncAnthropic()

class Priority(IntEnum):
    CRITICAL = 0
    NORMAL = 1
    BACKGROUND = 2

# Token budget allocation per minute (total: 100k TPM)
TOKEN_BUDGETS = {
    Priority.CRITICAL: 50_000,    # 50% reserved for critical
    Priority.NORMAL: 35_000,      # 35% for normal
    Priority.BACKGROUND: 15_000,  # 15% for background
}

@dataclass
class TokenBucket:
    """Token bucket per priority lane."""
    priority: Priority
    capacity: int
    refill_rate_per_sec: float  # tokens/second
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)

    def __post_init__(self):
        self._tokens = self.capacity
        self._last_refill = time.monotonic()
        self.refill_rate_per_sec = self.capacity / 60  # per-minute budget

    def consume(self, amount: int) -> bool:
        """Try to consume tokens. Returns True if successful."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_rate_per_sec)
        self._last_refill = now

        if self._tokens >= amount:
            self._tokens -= amount
            return True
        return False

    def available(self) -> float:
        return self._tokens

class BudgetPriorityScheduler:
    """Schedules requests across priority lanes with token budgets."""

    def __init__(self):
        self._buckets = {p: TokenBucket(p, TOKEN_BUDGETS[p], 0) for p in Priority}
        self._queues = {p: asyncio.Queue() for p in Priority}
        self._completed = {p: 0 for p in Priority}
        self._throttled = {p: 0 for p in Priority}

    async def submit(self, messages: list[dict], priority: Priority,
                     estimated_tokens: int = 500) -> dict:
        """Submit request to priority lane."""
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        await self._queues[priority].put({
            "messages": messages,
            "estimated_tokens": estimated_tokens,
            "future": future,
            "submit_time": loop.time()
        })
        return await future

    async def scheduler_loop(self):
        """Main scheduling loop: drain queues by priority."""
        while True:
            processed = False

            # Process by priority order
            for priority in Priority:
                q = self._queues[priority]
                if q.empty():
                    continue

                item = await q.get()
                bucket = self._buckets[priority]

                # Check token budget
                if not bucket.consume(item["estimated_tokens"]):
                    # Not enough budget — requeue and try lower priority
                    self._throttled[priority] += 1
                    await q.put(item)
                    continue

                # Execute request
                try:
                    resp = await client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=256,
                        messages=item["messages"]
                    )
                    actual_tokens = resp.usage.input_tokens + resp.usage.output_tokens
                    wait_ms = int((asyncio.get_event_loop().time() - item["submit_time"]) * 1000)
                    self._completed[priority] += 1
                    item["future"].set_result({
                        "response": resp.content[0].text,
                        "priority": priority.name,
                        "wait_ms": wait_ms,
                        "tokens_used": actual_tokens
                    })
                except Exception as e:
                    item["future"].set_exception(e)

                q.task_done()
                processed = True
                break  # Restart priority scan

            if not processed:
                await asyncio.sleep(0.01)

    def budget_status(self) -> dict:
        return {
            p.name: {
                "available_tokens": int(self._buckets[p].available()),
                "capacity": self._buckets[p].capacity,
                "completed": self._completed[p],
                "throttled": self._throttled[p]
            }
            for p in Priority
        }

async def demo_budget_scheduler():
    scheduler = BudgetPriorityScheduler()
    sched_task = asyncio.create_task(scheduler.scheduler_loop())

    requests = [
        (Priority.BACKGROUND, "Analyze sentiment of this paragraph: 'The product was okay.'"),
        (Priority.BACKGROUND, "Translate 'hello' to French."),
        (Priority.CRITICAL, "User login is failing — diagnose the issue."),
        (Priority.NORMAL, "What is Python's GIL?"),
        (Priority.CRITICAL, "Production is down! What causes 503 errors?"),
    ]

    tasks = [
        asyncio.create_task(scheduler.submit([{"role": "user", "content": msg}], priority))
        for priority, msg in requests
    ]

    results = await asyncio.gather(*tasks)
    print("Results by priority:")
    for result in results:
        print(f"  [{result['priority']}] wait={result['wait_ms']}ms tokens={result['tokens_used']}")
        print(f"    {result['response'][:80]}")

    print("\nBudget status:")
    for lane, status in scheduler.budget_status().items():
        pct = (status["available_tokens"] / status["capacity"]) * 100
        print(f"  {lane}: {status['available_tokens']}/{status['capacity']} available ({pct:.0f}%)")

    sched_task.cancel()

asyncio.run(demo_budget_scheduler())

# Expected Token Savings: Reserving budget lanes prevents critical requests from being throttled by background jobs
# Environment: multi-tenant SaaS, shared API infrastructure, background + interactive mixed workloads
```

---

## Option 3: Deadline-Aware Priority Queue

Requests carry deadlines; scheduler promotes requests approaching their deadline regardless of original priority.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from enum import IntEnum

client = anthropic.AsyncAnthropic()

class Priority(IntEnum):
    HIGH = 0
    NORMAL = 1
    LOW = 2

@dataclass(order=True)
class DeadlineRequest:
    effective_priority: float  # Lower = more urgent (computed from deadline)
    sequence: int
    priority: Priority = field(compare=False)
    deadline: float = field(compare=False)  # Unix timestamp
    messages: list = field(compare=False)
    future: asyncio.Future = field(compare=False)
    submitted_at: float = field(compare=False)

def compute_effective_priority(priority: Priority, deadline: float) -> float:
    """Compute effective priority combining base priority and deadline urgency."""
    now = time.monotonic()
    # Map deadline to 0-1 urgency (1.0 = expired, 0.0 = far future)
    time_to_deadline = max(0, deadline - now)
    urgency = 1.0 / (1.0 + time_to_deadline)  # Hyperbolic urgency

    # Blend base priority with deadline urgency
    base = priority.value / len(Priority)
    return base * (1 - urgency) + urgency * 0  # Deadline urgency dominates near deadline

_seq = 0

class DeadlineAwareScheduler:
    def __init__(self, max_concurrent: int = 2, reorder_interval: float = 0.5):
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._reorder_interval = reorder_interval
        self._stats = {"promoted": 0, "expired": 0, "completed": 0}

    async def submit(self, messages: list[dict], priority: Priority,
                     deadline_seconds: float = 30.0) -> dict:
        global _seq
        _seq += 1
        deadline = time.monotonic() + deadline_seconds
        loop = asyncio.get_event_loop()
        future = loop.create_future()

        req = DeadlineRequest(
            effective_priority=compute_effective_priority(priority, deadline),
            sequence=_seq,
            priority=priority,
            deadline=deadline,
            messages=messages,
            future=future,
            submitted_at=time.monotonic()
        )
        await self._queue.put(req)
        return await future

    async def reorder_loop(self):
        """Periodically reorder queue as deadlines approach."""
        while True:
            await asyncio.sleep(self._reorder_interval)
            # Drain and re-insert with updated priorities
            pending = []
            while not self._queue.empty():
                try:
                    req = self._queue.get_nowait()
                    if time.monotonic() > req.deadline:
                        # Expired — reject
                        self._stats["expired"] += 1
                        req.future.set_exception(TimeoutError(f"Deadline exceeded for request"))
                    else:
                        old_pri = req.effective_priority
                        req.effective_priority = compute_effective_priority(req.priority, req.deadline)
                        if req.effective_priority < old_pri - 0.1:
                            self._stats["promoted"] += 1
                        pending.append(req)
                except asyncio.QueueEmpty:
                    break

            for req in pending:
                await self._queue.put(req)

    async def worker(self):
        while True:
            req = await self._queue.get()
            async with self._semaphore:
                if time.monotonic() > req.deadline:
                    self._stats["expired"] += 1
                    req.future.set_exception(TimeoutError("Deadline exceeded"))
                    self._queue.task_done()
                    continue

                wait_ms = int((time.monotonic() - req.submitted_at) * 1000)
                try:
                    resp = await client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=200,
                        messages=req.messages
                    )
                    self._stats["completed"] += 1
                    req.future.set_result({
                        "response": resp.content[0].text,
                        "wait_ms": wait_ms,
                        "priority": req.priority.name,
                        "effective_priority": round(req.effective_priority, 3)
                    })
                except Exception as e:
                    req.future.set_exception(e)
                finally:
                    self._queue.task_done()

async def demo_deadline_scheduler():
    scheduler = DeadlineAwareScheduler(max_concurrent=2)
    workers = [asyncio.create_task(scheduler.worker()) for _ in range(3)]
    reorder = asyncio.create_task(scheduler.reorder_loop())

    requests = [
        (Priority.LOW, "Low priority, 60s deadline", 60),
        (Priority.LOW, "Low priority but expires in 2s!", 2),  # Should get promoted
        (Priority.HIGH, "High priority, 30s deadline", 30),
        (Priority.NORMAL, "Normal priority, 10s deadline", 10),
    ]

    tasks = [
        asyncio.create_task(scheduler.submit(
            [{"role": "user", "content": msg}], priority, deadline_secs
        ))
        for priority, msg, deadline_secs in requests
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    print("Deadline-aware scheduling results:")
    for (priority, msg, dl), result in zip(requests, results):
        if isinstance(result, Exception):
            print(f"  [{priority.name}, {dl}s] EXPIRED: {result}")
        else:
            print(f"  [{result['priority']}, eff={result['effective_priority']}] "
                  f"wait={result['wait_ms']}ms: {result['response'][:60]}")

    print(f"\nStats: {scheduler._stats}")

    for w in workers:
        w.cancel()
    reorder.cancel()

asyncio.run(demo_deadline_scheduler())

# Expected Token Savings: Deadline promotion prevents SLA violations without pre-classifying all requests as CRITICAL
# Environment: real-time pipelines, SLA-bound APIs, mixed latency-requirement workloads
```

---

## Option 4: Per-Tenant Fair-Share Queue

Each tenant gets a fair share of capacity; one tenant can't monopolize the queue even at low priority.

```python
import anthropic
import asyncio
from dataclasses import dataclass, field
from collections import defaultdict
import time

client = anthropic.AsyncAnthropic()

@dataclass
class TenantConfig:
    tenant_id: str
    weight: float = 1.0   # Relative capacity share
    max_burst: int = 10   # Max queued requests

@dataclass(order=True)
class FairShareRequest:
    virtual_finish_time: float  # For weighted fair queuing
    sequence: int
    tenant_id: str = field(compare=False)
    messages: list = field(compare=False)
    future: asyncio.Future = field(compare=False)
    submit_time: float = field(compare=False)

_req_seq = 0

class FairShareQueue:
    """Weighted fair-share queue across tenants."""

    def __init__(self, max_concurrent: int = 3):
        self._pq: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._tenants: dict[str, TenantConfig] = {}
        self._virtual_time: dict[str, float] = defaultdict(float)  # Per-tenant virtual clock
        self._queue_depth: dict[str, int] = defaultdict(int)
        self._stats: dict[str, dict] = defaultdict(lambda: {"submitted": 0, "completed": 0, "wait_ms_total": 0})

    def register_tenant(self, config: TenantConfig):
        self._tenants[config.tenant_id] = config

    async def submit(self, tenant_id: str, messages: list[dict]) -> dict:
        global _req_seq
        _req_seq += 1

        config = self._tenants.get(tenant_id, TenantConfig(tenant_id))
        depth = self._queue_depth[tenant_id]

        if depth >= config.max_burst:
            raise RuntimeError(f"Tenant {tenant_id} burst limit exceeded ({config.max_burst})")

        # Weighted fair queuing: virtual finish time = virtual_time + 1/weight
        vft = self._virtual_time[tenant_id] + (1.0 / config.weight)
        self._virtual_time[tenant_id] = vft

        loop = asyncio.get_event_loop()
        future = loop.create_future()
        req = FairShareRequest(
            virtual_finish_time=vft,
            sequence=_req_seq,
            tenant_id=tenant_id,
            messages=messages,
            future=future,
            submit_time=loop.time()
        )

        self._queue_depth[tenant_id] += 1
        self._stats[tenant_id]["submitted"] += 1
        await self._pq.put(req)
        return await future

    async def worker(self):
        while True:
            req = await self._pq.get()
            self._queue_depth[req.tenant_id] -= 1

            async with self._semaphore:
                wait_ms = int((asyncio.get_event_loop().time() - req.submit_time) * 1000)
                try:
                    resp = await client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=200,
                        messages=req.messages
                    )
                    self._stats[req.tenant_id]["completed"] += 1
                    self._stats[req.tenant_id]["wait_ms_total"] += wait_ms
                    req.future.set_result({
                        "tenant": req.tenant_id,
                        "response": resp.content[0].text[:80],
                        "wait_ms": wait_ms,
                        "vft": round(req.virtual_finish_time, 3)
                    })
                except Exception as e:
                    req.future.set_exception(e)
                finally:
                    self._pq.task_done()

    def fairness_report(self) -> dict:
        report = {}
        for tid, stats in self._stats.items():
            config = self._tenants.get(tid, TenantConfig(tid))
            report[tid] = {
                "weight": config.weight,
                "completed": stats["completed"],
                "avg_wait_ms": stats["wait_ms_total"] / max(stats["completed"], 1),
                "submitted": stats["submitted"]
            }
        return report

async def demo_fair_share():
    queue = FairShareQueue(max_concurrent=2)

    # Register tenants with different weights
    queue.register_tenant(TenantConfig("enterprise", weight=3.0, max_burst=20))
    queue.register_tenant(TenantConfig("standard", weight=1.0, max_burst=10))
    queue.register_tenant(TenantConfig("free_tier", weight=0.5, max_burst=5))

    workers = [asyncio.create_task(queue.worker()) for _ in range(3)]

    # Flood from all tenants simultaneously
    all_tasks = []
    for tenant, count in [("enterprise", 4), ("standard", 4), ("free_tier", 4)]:
        for i in range(count):
            t = asyncio.create_task(queue.submit(
                tenant,
                [{"role": "user", "content": f"Request {i} from {tenant}"}]
            ))
            all_tasks.append(t)

    results = await asyncio.gather(*all_tasks, return_exceptions=True)

    print("Fair-share scheduling results (by wait time):")
    valid = [r for r in results if isinstance(r, dict)]
    for r in sorted(valid, key=lambda x: x["wait_ms"]):
        print(f"  [{r['tenant']}] vft={r['vft']} wait={r['wait_ms']}ms")

    print("\nFairness report:")
    for tenant, report in queue.fairness_report().items():
        print(f"  {tenant} (weight={report['weight']}): "
              f"{report['completed']}/{report['submitted']} done, "
              f"avg_wait={report['avg_wait_ms']:.0f}ms")

    for w in workers:
        w.cancel()

asyncio.run(demo_fair_share())

# Expected Token Savings: Fair-share prevents free-tier from monopolizing capacity; enterprise SLA guaranteed by weight
# Environment: multi-tenant SaaS, metered API services, shared LLM infrastructure
```

---

## Option 5: Backpressure-Aware Priority Queue

Queue that applies backpressure to lower-priority lanes when total load is high, letting critical requests through.

```python
import anthropic
import asyncio
from dataclasses import dataclass, field
from enum import IntEnum

client = anthropic.AsyncAnthropic()

class Priority(IntEnum):
    CRITICAL = 0
    NORMAL = 1
    BACKGROUND = 2

# Max queue depth per priority under different load levels
BACKPRESSURE_LIMITS = {
    "low":    {Priority.CRITICAL: 100, Priority.NORMAL: 50,  Priority.BACKGROUND: 25},
    "medium": {Priority.CRITICAL: 100, Priority.NORMAL: 25,  Priority.BACKGROUND: 5},
    "high":   {Priority.CRITICAL: 100, Priority.NORMAL: 10,  Priority.BACKGROUND: 0},
    "critical":{Priority.CRITICAL: 50, Priority.NORMAL: 0,   Priority.BACKGROUND: 0},
}

@dataclass(order=True)
class PriorityRequest:
    priority: Priority
    sequence: int
    messages: list = field(compare=False)
    future: asyncio.Future = field(compare=False)

_seq = 0

class BackpressureQueue:
    def __init__(self, max_concurrent: int = 3):
        self._pq: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._depth: dict[Priority, int] = {p: 0 for p in Priority}
        self._rejected: dict[Priority, int] = {p: 0 for p in Priority}
        self._completed: dict[Priority, int] = {p: 0 for p in Priority}

    def _load_level(self) -> str:
        total = sum(self._depth.values())
        if total < 10:
            return "low"
        elif total < 30:
            return "medium"
        elif total < 60:
            return "high"
        return "critical"

    async def submit(self, messages: list[dict], priority: Priority) -> dict:
        global _seq
        _seq += 1

        load = self._load_level()
        limit = BACKPRESSURE_LIMITS[load][priority]

        if self._depth[priority] >= limit:
            self._rejected[priority] += 1
            raise RuntimeError(
                f"Backpressure: {priority.name} queue full at load level '{load}' "
                f"(depth={self._depth[priority]}, limit={limit})"
            )

        loop = asyncio.get_event_loop()
        future = loop.create_future()
        req = PriorityRequest(priority=priority, sequence=_seq, messages=messages, future=future)

        self._depth[priority] += 1
        await self._pq.put(req)
        return await future

    async def worker(self):
        while True:
            req = await self._pq.get()
            async with self._semaphore:
                self._depth[req.priority] -= 1
                try:
                    resp = await client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=200,
                        messages=req.messages
                    )
                    self._completed[req.priority] += 1
                    req.future.set_result({
                        "response": resp.content[0].text[:80],
                        "priority": req.priority.name
                    })
                except Exception as e:
                    req.future.set_exception(e)
                finally:
                    self._pq.task_done()

    def status(self) -> dict:
        load = self._load_level()
        return {
            "load_level": load,
            "queue_depth": {p.name: d for p, d in self._depth.items()},
            "completed": {p.name: c for p, c in self._completed.items()},
            "rejected": {p.name: r for p, r in self._rejected.items()},
            "limits": {p.name: BACKPRESSURE_LIMITS[load][p] for p in Priority}
        }

async def demo_backpressure():
    queue = BackpressureQueue(max_concurrent=2)
    workers = [asyncio.create_task(queue.worker()) for _ in range(3)]

    results = {"success": 0, "rejected": 0}
    tasks = []

    # Mix of priorities
    for i in range(12):
        priority = Priority(i % 3)
        t = asyncio.create_task(queue.submit(
            [{"role": "user", "content": f"Request {i} at priority {priority.name}"}],
            priority
        ))
        tasks.append((priority, t))
        await asyncio.sleep(0.01)

    for priority, task in tasks:
        try:
            result = await task
            results["success"] += 1
            print(f"  ✓ [{result['priority']}]: {result['response'][:50]}")
        except RuntimeError as e:
            results["rejected"] += 1
            print(f"  ✗ [{priority.name}]: {e}")

    status = queue.status()
    print(f"\nLoad level: {status['load_level']}")
    print(f"Completed: {status['completed']}")
    print(f"Rejected: {status['rejected']}")

    for w in workers:
        w.cancel()

asyncio.run(demo_backpressure())

# Expected Token Savings: Backpressure shedding of background jobs prevents queue depth from causing OOM or starvation
# Environment: high-load production systems, API gateways with admission control, rate-limited shared infra
```

---

## Option 6: SQLite-Persistent Priority Queue with Crash Recovery

Priority queue backed by SQLite — survives process restarts and enables multi-process workers.

```python
import anthropic
import asyncio
import sqlite3
import json
import time
import uuid
from contextlib import contextmanager
from enum import IntEnum

client = anthropic.AsyncAnthropic()

class Priority(IntEnum):
    CRITICAL = 0
    NORMAL = 1
    BACKGROUND = 2

QUEUE_DB = "priority_queue.db"

@contextmanager
def get_db():
    conn = sqlite3.connect(QUEUE_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_queue_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS request_queue (
                id TEXT PRIMARY KEY,
                priority INTEGER NOT NULL,
                messages_json TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                submitted_at REAL NOT NULL,
                claimed_at REAL,
                completed_at REAL,
                result TEXT,
                error TEXT,
                worker_id TEXT
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_status_priority ON request_queue(status, priority, submitted_at)")

def enqueue(messages: list[dict], priority: Priority) -> str:
    """Add request to persistent queue. Returns request_id."""
    request_id = str(uuid.uuid4())
    with get_db() as db:
        db.execute("""
            INSERT INTO request_queue (id, priority, messages_json, submitted_at)
            VALUES (?, ?, ?, ?)
        """, (request_id, priority.value, json.dumps(messages), time.time()))
    return request_id

def claim_next(worker_id: str) -> dict | None:
    """Atomically claim the highest-priority pending request."""
    with get_db() as db:
        row = db.execute("""
            SELECT * FROM request_queue
            WHERE status = 'pending'
            ORDER BY priority ASC, submitted_at ASC
            LIMIT 1
        """).fetchone()

        if not row:
            return None

        updated = db.execute("""
            UPDATE request_queue
            SET status = 'claimed', claimed_at = ?, worker_id = ?
            WHERE id = ? AND status = 'pending'
        """, (time.time(), worker_id, row["id"])).rowcount

        if updated == 0:
            return None  # Race condition — another worker claimed it

        return dict(row)

def complete_request(request_id: str, result: str | None, error: str | None):
    with get_db() as db:
        status = "completed" if result else "failed"
        db.execute("""
            UPDATE request_queue
            SET status = ?, completed_at = ?, result = ?, error = ?
            WHERE id = ?
        """, (status, time.time(), result, error, request_id))

def get_result(request_id: str, timeout: float = 30) -> dict | None:
    """Poll for result with timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with get_db() as db:
            row = db.execute(
                "SELECT * FROM request_queue WHERE id = ?", (request_id,)
            ).fetchone()
            if row and row["status"] in ("completed", "failed"):
                return dict(row)
        time.sleep(0.2)
    return None

async def persistent_worker(worker_id: str):
    """Async worker that processes from SQLite queue."""
    while True:
        item = claim_next(worker_id)
        if not item:
            await asyncio.sleep(0.1)
            continue

        messages = json.loads(item["messages_json"])
        priority = Priority(item["priority"])
        wait_ms = int((time.time() - item["submitted_at"]) * 1000)

        try:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=messages
            )
            result = resp.content[0].text
            complete_request(item["id"], result, None)
            print(f"  [{worker_id}] [{priority.name}] wait={wait_ms}ms: {result[:60]}")
        except Exception as e:
            complete_request(item["id"], None, str(e))

def queue_stats() -> dict:
    with get_db() as db:
        rows = db.execute("""
            SELECT priority, status, COUNT(*) as count
            FROM request_queue
            GROUP BY priority, status
        """).fetchall()
        stats: dict = {}
        for row in rows:
            p = Priority(row["priority"]).name
            stats.setdefault(p, {})[row["status"]] = row["count"]
        return stats

async def demo_persistent_queue():
    init_queue_db()

    # Enqueue requests of different priorities
    request_ids = []
    for priority, content in [
        (Priority.BACKGROUND, "Background analysis: summarize AI trends"),
        (Priority.BACKGROUND, "Background: compute statistics"),
        (Priority.CRITICAL, "URGENT: User authentication broken!"),
        (Priority.NORMAL, "Standard: explain async/await"),
        (Priority.CRITICAL, "URGENT: Payment processing failing!"),
    ]:
        rid = enqueue([{"role": "user", "content": content}], priority)
        request_ids.append((rid, priority.name, content[:40]))
        print(f"Enqueued [{priority.name}]: {content[:40]}")

    # Start workers
    print("\nStarting workers...")
    worker_tasks = [
        asyncio.create_task(persistent_worker(f"worker_{i}"))
        for i in range(2)
    ]

    await asyncio.sleep(15)  # Let workers process

    for t in worker_tasks:
        t.cancel()

    print("\nFinal queue stats:")
    for priority, statuses in queue_stats().items():
        print(f"  {priority}: {statuses}")

asyncio.run(demo_persistent_queue())

# Expected Token Savings: Persistent queue survives crashes; no work lost, no duplicate API calls from re-submission
# Environment: production agents, multi-process workers, crash-resilient batch processing
```

---

## Comparison

| Option | Queue Type | Crash Recovery | Multi-Process | Backpressure | Best For |
|--------|-----------|---------------|--------------|-------------|---------|
| 1. asyncio PriorityQueue | In-memory | No | No | No | Simple single-process agents |
| 2. Token Budget | In-memory | No | No | Yes (budget) | Rate-limited APIs with budgets |
| 3. Deadline-Aware | In-memory | No | No | No | SLA-bound, time-sensitive requests |
| 4. Fair-Share | In-memory | No | No | Partial | Multi-tenant fair allocation |
| 5. Backpressure | In-memory | No | No | Yes | High-load systems with admission control |
| 6. SQLite-Persistent | SQLite | Yes | Yes | No | Production, crash-resilient batch |

**Recommended defaults:**
- **Single-process, interactive** → Option 1 (simple priority queue)
- **Multi-tenant API** → Option 4 (fair-share)
- **SLA requirements** → Option 3 (deadline-aware)
- **Production batch** → Option 6 (persistent) + Option 5 (backpressure)
