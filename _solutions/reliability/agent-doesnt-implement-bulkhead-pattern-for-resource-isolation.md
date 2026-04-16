---
title: "Agent Doesn't Implement Bulkhead Pattern for Resource Isolation"
description: "Partition agent resources into isolated pools so that one failing or slow component cannot exhaust resources needed by other components."
category: reliability
difficulty: advanced
tags: [bulkhead, reliability, asyncio, semaphore, isolation, resilience]
---

# Agent Doesn't Implement Bulkhead Pattern for Resource Isolation

## Problem

When all agent operations share a single thread pool, API connection pool, or token budget, one slow or failing component (e.g., a web scraping tool) starves all other components (e.g., the main LLM calls). The bulkhead pattern partitions resources into isolated pools — just like watertight compartments in a ship — so that failures in one pool don't sink the whole vessel.

---

## Option 1: Semaphore-Based Resource Pools

```python
import asyncio
import anthropic
import time
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class ResourcePool:
    name: str
    capacity: int
    timeout_s: float = 10.0
    _sem: asyncio.Semaphore = field(init=False)
    _waiters: int = 0
    _active: int = 0
    _rejected: int = 0

    def __post_init__(self):
        self._sem = asyncio.Semaphore(self.capacity)

    async def acquire(self) -> bool:
        """Try to acquire a slot. Returns False if timed out."""
        self._waiters += 1
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=self.timeout_s)
            self._waiters -= 1
            self._active += 1
            return True
        except asyncio.TimeoutError:
            self._waiters -= 1
            self._rejected += 1
            return False

    def release(self):
        self._sem.release()
        self._active -= 1

    def stats(self) -> dict:
        return {
            "name": self.name,
            "capacity": self.capacity,
            "active": self._active,
            "waiters": self._waiters,
            "rejected": self._rejected,
        }

# Separate pools for different resource types
POOLS = {
    "llm_primary": ResourcePool("llm_primary", capacity=5, timeout_s=15.0),
    "llm_batch": ResourcePool("llm_batch", capacity=2, timeout_s=30.0),
    "tool_web": ResourcePool("tool_web", capacity=3, timeout_s=10.0),
    "tool_db": ResourcePool("tool_db", capacity=8, timeout_s=5.0),
}

async def call_with_bulkhead(pool_name: str, coro) -> any:
    pool = POOLS[pool_name]
    acquired = await pool.acquire()
    if not acquired:
        raise RuntimeError(f"[BULKHEAD] {pool_name} pool exhausted — request rejected")
    try:
        return await coro
    finally:
        pool.release()

async def primary_llm_call(prompt: str) -> str:
    async def _call():
        resp = await client.messages.create(
            model="claude-sonnet-4-6", max_tokens=512,
            messages=[{"role": "user", "content": prompt}]
        )
        return resp.content[0].text

    return await call_with_bulkhead("llm_primary", _call())

async def batch_llm_call(prompt: str) -> str:
    async def _call():
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=256,
            messages=[{"role": "user", "content": prompt}]
        )
        return resp.content[0].text

    return await call_with_bulkhead("llm_batch", _call())

async def simulated_web_tool(url: str) -> str:
    async def _fetch():
        await asyncio.sleep(0.5)  # simulate network
        return f"Content from {url}"
    return await call_with_bulkhead("tool_web", _fetch())

async def main():
    # Saturate web pool, primary LLM should still work
    web_tasks = [asyncio.create_task(simulated_web_tool(f"http://site{i}.com")) for i in range(10)]
    llm_result = await primary_llm_call("What is a bulkhead pattern?")
    print(f"[PRIMARY LLM] {llm_result[:80]}")

    results = await asyncio.gather(*web_tasks, return_exceptions=True)
    errors = sum(1 for r in results if isinstance(r, RuntimeError))
    print(f"[WEB POOL] {len(results)-errors} succeeded, {errors} rejected")

    for pool in POOLS.values():
        print(f"Stats: {pool.stats()}")

asyncio.run(main())
```

---

## Option 2: Thread-Pool Bulkheads with Priority Queues

```python
import asyncio
import anthropic
from dataclasses import dataclass, field
from enum import IntEnum
import heapq
import time

client = anthropic.AsyncAnthropic()

class Priority(IntEnum):
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3

@dataclass
class WorkItem:
    priority: Priority
    submitted_at: float
    coro_factory: callable
    future: asyncio.Future

    def __lt__(self, other):
        return (self.priority, self.submitted_at) < (other.priority, other.submitted_at)

@dataclass
class PriorityBulkhead:
    name: str
    workers: int
    max_queue: int = 50
    _heap: list = field(default_factory=list)
    _active: int = 0
    _rejected: int = 0
    _worker_tasks: list = field(default_factory=list)
    _new_work: asyncio.Event = field(default_factory=asyncio.Event)

    async def start(self):
        for _ in range(self.workers):
            self._worker_tasks.append(asyncio.create_task(self._worker()))

    async def _worker(self):
        while True:
            await self._new_work.wait()
            while self._heap:
                item = heapq.heappop(self._heap)
                self._active += 1
                try:
                    result = await item.coro_factory()
                    if not item.future.done():
                        item.future.set_result(result)
                except Exception as e:
                    if not item.future.done():
                        item.future.set_exception(e)
                finally:
                    self._active -= 1
            self._new_work.clear()

    async def submit(self, coro_factory: callable, priority: Priority = Priority.NORMAL) -> asyncio.Future:
        if len(self._heap) >= self.max_queue:
            self._rejected += 1
            raise RuntimeError(f"[BULKHEAD:{self.name}] Queue full — rejecting request")
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        item = WorkItem(priority=priority, submitted_at=time.time(), coro_factory=coro_factory, future=fut)
        heapq.heappush(self._heap, item)
        self._new_work.set()
        return fut

    def stats(self) -> dict:
        return {"name": self.name, "active": self._active, "queued": len(self._heap), "rejected": self._rejected}

llm_bulkhead = PriorityBulkhead(name="llm", workers=3, max_queue=20)
tool_bulkhead = PriorityBulkhead(name="tools", workers=5, max_queue=30)

async def main():
    await llm_bulkhead.start()
    await tool_bulkhead.start()

    async def make_llm_call(msg: str):
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=100,
            messages=[{"role": "user", "content": msg}]
        )
        return resp.content[0].text

    # Submit critical and normal priority work
    critical_fut = await llm_bulkhead.submit(lambda: make_llm_call("CRITICAL: system status"), Priority.CRITICAL)
    normal_futs = [await llm_bulkhead.submit(lambda: make_llm_call(f"normal query {i}"), Priority.NORMAL) for i in range(5)]

    result = await critical_fut
    print(f"[CRITICAL] {result[:60]}")
    print(f"Stats: {llm_bulkhead.stats()}")

    for task in llm_bulkhead._worker_tasks:
        task.cancel()

asyncio.run(main())
```

---

## Option 3: Token Budget Bulkheads (Cost Isolation)

```python
import asyncio
import anthropic
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class TokenBudgetPool:
    name: str
    max_tokens_per_minute: int
    _used_this_minute: int = 0
    _minute_start: float = field(default_factory=lambda: __import__("time").time())
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def check_and_reserve(self, estimated_tokens: int) -> bool:
        import time
        async with self._lock:
            now = time.time()
            if now - self._minute_start >= 60.0:
                self._used_this_minute = 0
                self._minute_start = now
            if self._used_this_minute + estimated_tokens > self.max_tokens_per_minute:
                return False
            self._used_this_minute += estimated_tokens
            return True

    async def record_actual(self, actual_tokens: int, estimated_tokens: int):
        async with self._lock:
            # Correct the reservation
            correction = actual_tokens - estimated_tokens
            self._used_this_minute = max(0, self._used_this_minute + correction)

    def utilization(self) -> float:
        return self._used_this_minute / self.max_tokens_per_minute

# Each feature/team gets its own token budget
BUDGETS = {
    "user_chat": TokenBudgetPool("user_chat", max_tokens_per_minute=20000),
    "background_analysis": TokenBudgetPool("background_analysis", max_tokens_per_minute=5000),
    "admin_tools": TokenBudgetPool("admin_tools", max_tokens_per_minute=10000),
}

async def budgeted_call(pool_name: str, prompt: str, model: str = "claude-haiku-4-5-20251001", max_tokens: int = 512) -> str:
    pool = BUDGETS[pool_name]
    estimated = len(prompt.split()) + max_tokens
    allowed = await pool.check_and_reserve(estimated)
    if not allowed:
        raise RuntimeError(f"[TOKEN BULKHEAD] {pool_name} budget exhausted ({pool.utilization():.0%} used)")

    resp = await client.messages.create(model=model, max_tokens=max_tokens, messages=[{"role": "user", "content": prompt}])
    actual = resp.usage.input_tokens + resp.usage.output_tokens
    await pool.record_actual(actual, estimated)
    return resp.content[0].text

async def main():
    # Background tasks can't starve user chat
    bg_tasks = [asyncio.create_task(budgeted_call("background_analysis", f"Analyze topic {i}")) for i in range(3)]
    user_result = await budgeted_call("user_chat", "Hello! How can I help you today?")
    print(f"[USER CHAT] {user_result[:60]}")

    results = await asyncio.gather(*bg_tasks, return_exceptions=True)
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            print(f"[BG {i}] Rejected: {r}")
        else:
            print(f"[BG {i}] {str(r)[:50]}")

    for name, pool in BUDGETS.items():
        print(f"[{name}] utilization={pool.utilization():.1%}")

asyncio.run(main())
```

---

## Option 4: Circuit-Breaker + Bulkhead Combination

```python
import asyncio
import anthropic
import time
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.AsyncAnthropic()

class CircuitState(Enum):
    CLOSED = "closed"       # normal operation
    OPEN = "open"           # rejecting all requests
    HALF_OPEN = "half_open" # testing recovery

@dataclass
class BulkheadCircuit:
    name: str
    capacity: int            # max concurrent
    failure_threshold: int   # failures to open circuit
    recovery_timeout_s: float = 30.0
    _sem: asyncio.Semaphore = field(init=False)
    _state: CircuitState = CircuitState.CLOSED
    _failures: int = 0
    _last_failure_at: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self):
        self._sem = asyncio.Semaphore(self.capacity)

    async def call(self, coro) -> any:
        async with self._lock:
            if self._state == CircuitState.OPEN:
                if time.time() - self._last_failure_at >= self.recovery_timeout_s:
                    self._state = CircuitState.HALF_OPEN
                    print(f"[{self.name}] Circuit → HALF_OPEN")
                else:
                    raise RuntimeError(f"[{self.name}] Circuit OPEN — fast-fail")

        try:
            acquired = await asyncio.wait_for(self._sem.acquire(), timeout=5.0)
        except asyncio.TimeoutError:
            raise RuntimeError(f"[{self.name}] Bulkhead full")

        try:
            result = await coro
            async with self._lock:
                if self._state == CircuitState.HALF_OPEN:
                    self._state = CircuitState.CLOSED
                    self._failures = 0
                    print(f"[{self.name}] Circuit → CLOSED (recovered)")
            return result
        except Exception as e:
            async with self._lock:
                self._failures += 1
                self._last_failure_at = time.time()
                if self._failures >= self.failure_threshold:
                    self._state = CircuitState.OPEN
                    print(f"[{self.name}] Circuit → OPEN after {self._failures} failures")
            raise
        finally:
            self._sem.release()

circuits = {
    "primary_llm": BulkheadCircuit("primary_llm", capacity=4, failure_threshold=3),
    "web_scraper": BulkheadCircuit("web_scraper", capacity=2, failure_threshold=2, recovery_timeout_s=10.0),
}

async def resilient_llm_call(prompt: str) -> str:
    circuit = circuits["primary_llm"]
    async def _call():
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        return resp.content[0].text
    return await circuit.call(_call())

async def main():
    results = await asyncio.gather(
        *[resilient_llm_call(f"Query {i}") for i in range(6)],
        return_exceptions=True
    )
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            print(f"[{i}] FAIL: {r}")
        else:
            print(f"[{i}] OK: {str(r)[:50]}")

asyncio.run(main())
```

---

## Option 5: Per-Tenant Resource Isolation

```python
import asyncio
import anthropic
from dataclasses import dataclass, field
from collections import defaultdict

client = anthropic.AsyncAnthropic()

@dataclass
class TenantPool:
    tenant_id: str
    max_concurrent: int = 3
    max_queue: int = 10
    _sem: asyncio.Semaphore = field(init=False)
    _queue_size: int = 0
    _total_calls: int = 0
    _rejected: int = 0

    def __post_init__(self):
        self._sem = asyncio.Semaphore(self.max_concurrent)

    async def execute(self, coro) -> any:
        if self._queue_size >= self.max_queue:
            self._rejected += 1
            raise RuntimeError(f"Tenant {self.tenant_id} queue full")
        self._queue_size += 1
        try:
            async with self._sem:
                self._queue_size -= 1
                self._total_calls += 1
                return await coro
        except Exception:
            self._queue_size -= 1
            raise

class MultiTenantBulkhead:
    def __init__(self):
        self._pools: dict[str, TenantPool] = {}
        self._lock = asyncio.Lock()

    async def get_or_create_pool(self, tenant_id: str, max_concurrent: int = 3) -> TenantPool:
        async with self._lock:
            if tenant_id not in self._pools:
                self._pools[tenant_id] = TenantPool(tenant_id=tenant_id, max_concurrent=max_concurrent)
            return self._pools[tenant_id]

    async def call(self, tenant_id: str, prompt: str) -> str:
        pool = await self.get_or_create_pool(tenant_id)
        async def _call():
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=200,
                messages=[{"role": "user", "content": prompt}]
            )
            return resp.content[0].text
        return await pool.execute(_call())

    def report(self) -> dict:
        return {tid: {"calls": p._total_calls, "rejected": p._rejected} for tid, p in self._pools.items()}

bulkhead = MultiTenantBulkhead()

async def main():
    # Tenant A floods with requests — Tenant B should not be affected
    tenant_a_tasks = [asyncio.create_task(bulkhead.call("tenant_a", f"A query {i}")) for i in range(15)]
    tenant_b_result = await bulkhead.call("tenant_b", "Tenant B priority query")
    print(f"[TENANT B] {tenant_b_result[:60]}")

    a_results = await asyncio.gather(*tenant_a_tasks, return_exceptions=True)
    a_ok = sum(1 for r in a_results if not isinstance(r, Exception))
    a_rej = sum(1 for r in a_results if isinstance(r, Exception))
    print(f"[TENANT A] {a_ok} succeeded, {a_rej} rejected")
    print(f"Report: {bulkhead.report()}")

asyncio.run(main())
```

---

## Option 6: Adaptive Bulkhead with Auto-Scaling Capacity

```python
import asyncio
import anthropic
import time
import statistics
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class AdaptiveBulkhead:
    name: str
    min_capacity: int = 1
    max_capacity: int = 10
    _capacity: int = 3
    _sem: asyncio.Semaphore = field(init=False)
    _latencies: list = field(default_factory=list)
    _error_window: list = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    target_latency_ms: float = 1500.0
    scale_up_threshold: float = 0.7   # scale up if error rate > 70%
    scale_down_threshold: float = 0.1  # scale down if error rate < 10%

    def __post_init__(self):
        self._sem = asyncio.Semaphore(self._capacity)

    async def _adjust_capacity(self):
        """Adjust pool size based on recent error rate and latency."""
        async with self._lock:
            if len(self._error_window) < 5:
                return
            error_rate = sum(self._error_window[-20:]) / min(len(self._error_window), 20)
            avg_latency = statistics.mean(self._latencies[-10:]) if self._latencies else 0

            old_cap = self._capacity
            if error_rate > self.scale_up_threshold and self._capacity > self.min_capacity:
                # High error rate → reduce capacity to shed load
                self._capacity = max(self.min_capacity, self._capacity - 1)
            elif error_rate < self.scale_down_threshold and avg_latency < self.target_latency_ms:
                # Low error rate + fast → increase capacity
                self._capacity = min(self.max_capacity, self._capacity + 1)

            if self._capacity != old_cap:
                # Rebuild semaphore with new capacity
                self._sem = asyncio.Semaphore(self._capacity)
                print(f"[{self.name}] Capacity adjusted: {old_cap} → {self._capacity} (err_rate={error_rate:.0%})")

    async def execute(self, coro) -> any:
        t0 = time.time()
        try:
            acquired = await asyncio.wait_for(self._sem.acquire(), timeout=5.0)
        except asyncio.TimeoutError:
            async with self._lock:
                self._error_window.append(1)
            raise RuntimeError(f"[{self.name}] Bulkhead timeout")

        try:
            result = await coro
            latency = (time.time() - t0) * 1000
            async with self._lock:
                self._latencies.append(latency)
                self._error_window.append(0)
            return result
        except Exception:
            async with self._lock:
                self._error_window.append(1)
            raise
        finally:
            self._sem.release()
            asyncio.create_task(self._adjust_capacity())

adaptive = AdaptiveBulkhead(name="adaptive_llm", min_capacity=1, max_capacity=8)

async def main():
    async def call(i: int) -> str:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=50,
            messages=[{"role": "user", "content": f"Query {i}"}]
        )
        return resp.content[0].text

    # Send bursts of requests
    results = await asyncio.gather(
        *[adaptive.execute(call(i)) for i in range(20)],
        return_exceptions=True
    )
    ok = sum(1 for r in results if not isinstance(r, Exception))
    print(f"Completed: {ok}/20, Final capacity: {adaptive._capacity}")

asyncio.run(main())
```

---

## Comparison

| Option | Isolation Mechanism | Priority Support | Adaptive | Best For |
|--------|-------------------|-----------------|----------|----------|
| 1 – Semaphore Pools | Per-component semaphores | No | No | Simple resource partitioning |
| 2 – Priority Queues | Per-pool priority heap | Yes | No | Mixed-priority workloads |
| 3 – Token Budgets | Per-feature token quotas | No | No | Cost isolation by feature/team |
| 4 – Circuit + Bulkhead | Semaphore + circuit breaker | No | No | Failure-prone external dependencies |
| 5 – Per-Tenant | Per-tenant semaphores | No | No | Multi-tenant SaaS agents |
| 6 – Adaptive | Semaphore with auto-scale | No | Yes | Variable-load production systems |

**Recommendation:** Use Option 1 for most agents — separate semaphore pools for LLM calls vs tool calls prevents one type from starving the other. Combine with Option 4's circuit breaker for external tool calls. Use Option 5 in multi-tenant environments where one customer's burst traffic must not degrade others.
