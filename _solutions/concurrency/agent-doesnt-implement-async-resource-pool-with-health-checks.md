---
layout: solution
title: "Agent Doesn't Implement Async Resource Pool with Health Checks"
category: concurrency
description: "Manage a pool of reusable async resources (connections, clients, workers) with periodic health checks, automatic eviction of unhealthy members, and backpressure."
tags: [resource-pool, health-check, connection-pool, async, backpressure, eviction]
---

# Agent Doesn't Implement Async Resource Pool with Health Checks

Agents that create a new client or connection per request pay cold-start latency on every call. Pools amortize initialization cost — but without health checks, stale or broken pool members silently fail. A proper async pool validates resources before lending them, evicts unhealthy ones, and signals backpressure when the pool is exhausted.

## Option 1: Simple Async Pool with Semaphore

```python
import asyncio
import anthropic
from contextlib import asynccontextmanager

MAX_CONCURRENT = 3
_semaphore = asyncio.Semaphore(MAX_CONCURRENT)

# Shared client (one per process — httpx manages connection pool internally)
_client = anthropic.AsyncAnthropic()


@asynccontextmanager
async def pool_client():
    async with _semaphore:
        yield _client


async def agent_call(prompt: str) -> str:
    async with pool_client() as client:
        r = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        )
        return r.content[0].text


async def main() -> None:
    prompts = [f"Count to {i}." for i in range(1, 8)]
    results = await asyncio.gather(*[agent_call(p) for p in prompts])
    for i, r in enumerate(results):
        print(f"[{i+1}] {r[:60]}")


asyncio.run(main())

# Expected Token Savings: Shared client reuses HTTP connections; no per-request handshake overhead
# Environment: Python 3.11+, asyncio; MAX_CONCURRENT should match your Anthropic rate limit tier
```

## Option 2: Pool with Checkout/Return and Health Validation

```python
import asyncio
import time
import anthropic
from dataclasses import dataclass, field

POOL_SIZE = 4
HEALTH_CHECK_INTERVAL = 30.0  # seconds


@dataclass
class PoolMember:
    client: anthropic.AsyncAnthropic
    created_at: float = field(default_factory=time.monotonic)
    last_used: float = field(default_factory=time.monotonic)
    error_count: int = 0
    healthy: bool = True


class AsyncClientPool:
    def __init__(self, size: int = POOL_SIZE) -> None:
        self._pool: list[PoolMember] = []
        self._lock = asyncio.Lock()
        self._available = asyncio.Queue()
        self._size = size

    async def initialize(self) -> None:
        for _ in range(self._size):
            member = PoolMember(client=anthropic.AsyncAnthropic())
            self._pool.append(member)
            await self._available.put(member)

    async def _health_check(self, member: PoolMember) -> bool:
        try:
            # Lightweight ping: minimal token call
            r = await asyncio.wait_for(
                member.client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=1,
                    messages=[{"role": "user", "content": "ping"}],
                ),
                timeout=5.0,
            )
            member.healthy = True
            member.error_count = 0
            return True
        except Exception:
            member.healthy = False
            member.error_count += 1
            return False

    async def checkout(self, timeout: float = 10.0) -> PoolMember:
        member = await asyncio.wait_for(self._available.get(), timeout=timeout)
        member.last_used = time.monotonic()

        # Evict unhealthy members and replace
        if not member.healthy or member.error_count >= 3:
            print(f"[POOL] Replacing unhealthy member (errors={member.error_count})")
            member = PoolMember(client=anthropic.AsyncAnthropic())

        return member

    async def checkin(self, member: PoolMember) -> None:
        await self._available.put(member)

    async def run_health_checks(self) -> None:
        while True:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
            for member in self._pool:
                ok = await self._health_check(member)
                print(f"[HEALTH] member healthy={ok}")


POOL = AsyncClientPool(size=POOL_SIZE)


async def agent_call(prompt: str) -> str:
    member = await POOL.checkout(timeout=10.0)
    try:
        r = await member.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        )
        return r.content[0].text
    except Exception as e:
        member.error_count += 1
        raise
    finally:
        await POOL.checkin(member)


async def main() -> None:
    await POOL.initialize()
    prompts = [f"What is {i} squared?" for i in range(1, 6)]
    results = await asyncio.gather(*[agent_call(p) for p in prompts])
    for r in results:
        print(r[:60])


asyncio.run(main())

# Expected Token Savings: Pool reuse eliminates connection setup; health checks prevent silent failures
# Environment: Python 3.11+; POOL_SIZE should match your concurrency budget
```

## Option 3: Resource Pool with Backpressure and Wait Queue

```python
import asyncio
import time
import anthropic
from collections import deque
from dataclasses import dataclass

POOL_SIZE = 3
MAX_QUEUE_DEPTH = 10


@dataclass
class Resource:
    client: anthropic.AsyncAnthropic
    busy: bool = False
    error_count: int = 0


class BackpressurePool:
    def __init__(self, size: int, max_queue: int) -> None:
        self._resources = [Resource(client=anthropic.AsyncAnthropic()) for _ in range(size)]
        self._waiters: deque[asyncio.Future] = deque()
        self._lock = asyncio.Lock()
        self._max_queue = max_queue

    def _find_free(self) -> Resource | None:
        return next((r for r in self._resources if not r.busy and r.error_count < 5), None)

    async def acquire(self) -> Resource:
        async with self._lock:
            resource = self._find_free()
            if resource:
                resource.busy = True
                return resource

            if len(self._waiters) >= self._max_queue:
                raise RuntimeError(f"Pool queue full ({self._max_queue} waiters) — backpressure")

            fut: asyncio.Future = asyncio.get_event_loop().create_future()
            self._waiters.append(fut)

        # Wait outside the lock
        return await fut

    async def release(self, resource: Resource) -> None:
        async with self._lock:
            resource.busy = False
            if self._waiters:
                waiter = self._waiters.popleft()
                resource.busy = True
                waiter.set_result(resource)

    async def use(self, prompt: str) -> str:
        resource = await self.acquire()
        try:
            r = await resource.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": prompt}],
            )
            return r.content[0].text
        except Exception as e:
            resource.error_count += 1
            raise
        finally:
            await self.release(resource)


POOL = BackpressurePool(size=POOL_SIZE, max_queue=MAX_QUEUE_DEPTH)


async def main() -> None:
    prompts = [f"Describe the number {i} in one sentence." for i in range(1, 9)]
    try:
        results = await asyncio.gather(*[POOL.use(p) for p in prompts])
        for r in results:
            print(r[:80])
    except RuntimeError as e:
        print(f"[BACKPRESSURE] {e}")


asyncio.run(main())

# Expected Token Savings: Backpressure prevents OOM from spawning unlimited concurrent requests
# Environment: Python 3.11+; tune MAX_QUEUE_DEPTH to match acceptable wait latency
```

## Option 4: SQLite-Tracked Pool Metrics with Auto-Scaling

```python
import asyncio
import sqlite3
import time
import anthropic
from dataclasses import dataclass

DB_PATH = "pool_metrics.db"
MIN_POOL = 2
MAX_POOL = 6
SCALE_UP_THRESHOLD = 0.8   # scale up when 80% busy
SCALE_DOWN_THRESHOLD = 0.3  # scale down when <30% busy


@dataclass
class PoolSlot:
    id: int
    client: anthropic.AsyncAnthropic
    busy: bool = False


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pool_events (
            ts REAL, event TEXT, pool_size INTEGER, busy_count INTEGER
        )
    """)
    conn.commit()
    return conn


class AutoScalingPool:
    def __init__(self) -> None:
        self._slots: list[PoolSlot] = []
        self._lock = asyncio.Lock()
        self._next_id = 0
        self._conn = init_db()

    def _add_slot(self) -> None:
        slot = PoolSlot(id=self._next_id, client=anthropic.AsyncAnthropic())
        self._slots.append(slot)
        self._next_id += 1
        print(f"[POOL] Scaled UP to {len(self._slots)} slots")
        self._log("scale_up", len(self._slots), self._busy_count())

    def _remove_slot(self) -> None:
        idle = [s for s in self._slots if not s.busy]
        if idle and len(self._slots) > MIN_POOL:
            self._slots.remove(idle[-1])
            print(f"[POOL] Scaled DOWN to {len(self._slots)} slots")
            self._log("scale_down", len(self._slots), self._busy_count())

    def _busy_count(self) -> int:
        return sum(1 for s in self._slots if s.busy)

    def _log(self, event: str, size: int, busy: int) -> None:
        self._conn.execute(
            "INSERT INTO pool_metrics VALUES (?,?,?,?)",
            (time.time(), event, size, busy),
        )
        self._conn.commit()

    async def acquire(self) -> PoolSlot:
        async with self._lock:
            if not self._slots:
                self._add_slot()

            idle = [s for s in self._slots if not s.busy]
            if not idle:
                if len(self._slots) < MAX_POOL:
                    self._add_slot()
                    idle = [self._slots[-1]]
                else:
                    # Wait for one to free up
                    pass

            if idle:
                idle[0].busy = True
                busy_ratio = self._busy_count() / len(self._slots)
                if busy_ratio >= SCALE_UP_THRESHOLD and len(self._slots) < MAX_POOL:
                    self._add_slot()
                return idle[0]

        # Spin-wait for an idle slot (simple implementation)
        while True:
            await asyncio.sleep(0.05)
            async with self._lock:
                idle = [s for s in self._slots if not s.busy]
                if idle:
                    idle[0].busy = True
                    return idle[0]

    async def release(self, slot: PoolSlot) -> None:
        async with self._lock:
            slot.busy = False
            busy_ratio = self._busy_count() / max(len(self._slots), 1)
            if busy_ratio < SCALE_DOWN_THRESHOLD:
                self._remove_slot()

    async def use(self, prompt: str) -> str:
        slot = await self.acquire()
        try:
            r = await slot.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": prompt}],
            )
            return r.content[0].text
        finally:
            await self.release(slot)


POOL = AutoScalingPool()


async def main() -> None:
    prompts = [f"Say one fact about the number {i}." for i in range(1, 7)]
    results = await asyncio.gather(*[POOL.use(p) for p in prompts])
    for r in results:
        print(r[:80])
    print(f"\n[POOL] Final size: {len(POOL._slots)}")


asyncio.run(main())

# Expected Token Savings: Auto-scaling prevents over-provisioning; metrics enable capacity planning
# Environment: Python 3.11+, SQLite3; query pool_metrics table for utilization dashboards
```

## Option 5: Pool with Periodic Health Eviction Background Task

```python
import asyncio
import time
import anthropic
from dataclasses import dataclass, field

POOL_SIZE = 4
EVICTION_INTERVAL = 60.0   # check every 60s
MAX_IDLE_AGE = 120.0       # evict members idle > 120s
MAX_ERROR_COUNT = 3


@dataclass
class PoolMember:
    client: anthropic.AsyncAnthropic
    created_at: float = field(default_factory=time.monotonic)
    last_used: float = field(default_factory=time.monotonic)
    error_count: int = 0
    in_use: bool = False


class ManagedPool:
    def __init__(self, size: int) -> None:
        self._members: list[PoolMember] = [
            PoolMember(client=anthropic.AsyncAnthropic()) for _ in range(size)
        ]
        self._lock = asyncio.Lock()
        self._size = size

    async def _eviction_loop(self) -> None:
        while True:
            await asyncio.sleep(EVICTION_INTERVAL)
            now = time.monotonic()
            async with self._lock:
                before = len(self._members)
                self._members = [
                    m for m in self._members
                    if m.in_use
                    or (m.error_count < MAX_ERROR_COUNT and now - m.last_used < MAX_IDLE_AGE)
                ]
                evicted = before - len(self._members)
                # Replenish to maintain pool size
                while len(self._members) < self._size:
                    self._members.append(PoolMember(client=anthropic.AsyncAnthropic()))
                if evicted:
                    print(f"[EVICT] Removed {evicted} unhealthy/idle members; pool={len(self._members)}")

    def start_eviction(self) -> asyncio.Task:
        return asyncio.create_task(self._eviction_loop())

    async def acquire(self, timeout: float = 10.0) -> PoolMember:
        deadline = time.monotonic() + timeout
        while True:
            async with self._lock:
                idle = [m for m in self._members if not m.in_use and m.error_count < MAX_ERROR_COUNT]
                if idle:
                    member = idle[0]
                    member.in_use = True
                    member.last_used = time.monotonic()
                    return member
            if time.monotonic() > deadline:
                raise TimeoutError("Pool acquire timed out")
            await asyncio.sleep(0.05)

    async def release(self, member: PoolMember) -> None:
        async with self._lock:
            member.in_use = False

    async def use(self, prompt: str) -> str:
        member = await self.acquire()
        try:
            r = await member.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": prompt}],
            )
            return r.content[0].text
        except Exception:
            member.error_count += 1
            raise
        finally:
            await self.release(member)


POOL = ManagedPool(size=POOL_SIZE)


async def main() -> None:
    eviction_task = POOL.start_eviction()
    prompts = [f"Name one programming language starting with letter {chr(65+i)}." for i in range(5)]
    results = await asyncio.gather(*[POOL.use(p) for p in prompts])
    for r in results:
        print(r[:80])
    eviction_task.cancel()


asyncio.run(main())

# Expected Token Savings: Eviction prevents stale clients from accumulating; no resource leak
# Environment: Python 3.11+; tune EVICTION_INTERVAL and MAX_IDLE_AGE for your traffic pattern
```

## Option 6: Pool with Prometheus-Compatible Metrics Export

```python
import asyncio
import time
import anthropic
from dataclasses import dataclass, field

# Minimal metrics registry (replace with prometheus_client in production)
METRICS: dict[str, float] = {
    "pool_size": 0,
    "pool_busy": 0,
    "pool_idle": 0,
    "pool_errors_total": 0,
    "pool_acquires_total": 0,
    "pool_timeouts_total": 0,
    "pool_avg_wait_ms": 0.0,
}
_wait_samples: list[float] = []


@dataclass
class Slot:
    client: anthropic.AsyncAnthropic
    busy: bool = False
    errors: int = 0


class InstrumentedPool:
    def __init__(self, size: int) -> None:
        self._slots = [Slot(client=anthropic.AsyncAnthropic()) for _ in range(size)]
        self._lock = asyncio.Lock()
        METRICS["pool_size"] = size

    def _update_metrics(self) -> None:
        busy = sum(1 for s in self._slots if s.busy)
        METRICS["pool_busy"] = busy
        METRICS["pool_idle"] = len(self._slots) - busy
        errs = sum(s.errors for s in self._slots)
        METRICS["pool_errors_total"] = errs
        if _wait_samples:
            METRICS["pool_avg_wait_ms"] = sum(_wait_samples) / len(_wait_samples)

    async def acquire(self, timeout: float = 10.0) -> Slot:
        start = time.monotonic()
        deadline = start + timeout
        while True:
            async with self._lock:
                idle = [s for s in self._slots if not s.busy]
                if idle:
                    idle[0].busy = True
                    wait_ms = (time.monotonic() - start) * 1000
                    _wait_samples.append(wait_ms)
                    if len(_wait_samples) > 1000:
                        _wait_samples.pop(0)
                    METRICS["pool_acquires_total"] += 1
                    self._update_metrics()
                    return idle[0]
            if time.monotonic() > deadline:
                METRICS["pool_timeouts_total"] += 1
                raise TimeoutError("Pool acquire timeout")
            await asyncio.sleep(0.02)

    async def release(self, slot: Slot) -> None:
        async with self._lock:
            slot.busy = False
            self._update_metrics()

    async def use(self, prompt: str) -> str:
        slot = await self.acquire()
        try:
            r = await slot.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": prompt}],
            )
            return r.content[0].text
        except Exception:
            slot.errors += 1
            raise
        finally:
            await self.release(slot)

    def metrics_text(self) -> str:
        """Prometheus text format output."""
        lines = []
        for k, v in METRICS.items():
            lines.append(f"# TYPE {k} gauge")
            lines.append(f"{k} {v}")
        return "\n".join(lines)


POOL = InstrumentedPool(size=4)


async def main() -> None:
    prompts = [f"What is {i} * {i+1}?" for i in range(1, 7)]
    await asyncio.gather(*[POOL.use(p) for p in prompts])
    print(POOL.metrics_text())


asyncio.run(main())

# Expected Token Savings: Metrics enable right-sizing the pool; no extra API calls
# Environment: Python 3.11+; expose metrics_text() via HTTP /metrics endpoint for Prometheus scraping
```

## Comparison

| Option | Backpressure | Health Checks | Auto-Scale | Metrics | Best For |
|--------|-------------|--------------|-----------|---------|----------|
| 1. Semaphore | Semaphore wait | No | No | No | Minimal setup |
| 2. Checkout/Return | Queue wait | Ping check | No | No | Explicit lifecycle |
| 3. Wait Queue | Queue depth cap | No | No | No | Bounded concurrency |
| 4. Auto-Scaling | Spin-wait | No | Yes | SQLite | Variable load |
| 5. Eviction Task | Acquire timeout | Eviction | No | No | Long-running agents |
| 6. Instrumented | Acquire timeout | No | No | Prometheus | Observability-first |
