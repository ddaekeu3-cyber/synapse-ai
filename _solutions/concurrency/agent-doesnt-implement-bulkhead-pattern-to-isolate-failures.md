---
layout: solution
title: "Agent Doesn't Implement Bulkhead Pattern to Isolate Failures"
category: concurrency
description: "All agent operations share the same thread pool and connection resources — one slow or failing downstream service exhausts the shared pool and takes down every other operation, including unrelated ones."
tags: [concurrency, bulkhead, resilience, isolation, production]
---

## Symptom

The agent handles three types of work: real-time user queries, background summarisation jobs, and webhook processing. All three share a single `asyncio.Semaphore(20)`. When the summarisation service slows down and its tasks pile up, they occupy all 20 slots. Real-time user queries — completely unrelated — start timing out and failing. A single slow service takes down the entire agent.

## Root Cause

A bulkhead in ship design isolates compartments so that a breach in one doesn't sink the vessel. The software bulkhead pattern applies the same principle: separate resource pools (thread pools, semaphores, connection pools) for separate concerns so that exhaustion in one pool cannot propagate to others. Without bulkheads, a shared resource pool is a single point of failure that couples unrelated workloads.

## Fix

### Option 1 — Per-concern semaphores as bulkheads

```python
import asyncio
import anthropic
import time

client = anthropic.AsyncAnthropic()

# Separate semaphores per concern — exhaustion in one cannot starve the others
BULKHEADS = {
    "realtime":   asyncio.Semaphore(5),   # user-facing: small, fast
    "background": asyncio.Semaphore(10),  # batch jobs: larger, can queue
    "webhook":    asyncio.Semaphore(3),   # webhooks: isolated from both
}

async def run_in_bulkhead(concern: str, prompt: str) -> str:
    sem = BULKHEADS.get(concern, asyncio.Semaphore(3))
    async with sem:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text

async def simulate_load():
    # Even if background is fully saturated, realtime still gets through
    background_tasks = [
        run_in_bulkhead("background", f"Summarise article {i}") for i in range(20)
    ]
    realtime_tasks = [
        run_in_bulkhead("realtime", f"User query {i}") for i in range(5)
    ]

    start = time.monotonic()
    all_results = await asyncio.gather(*background_tasks, *realtime_tasks)
    elapsed = time.monotonic() - start

    print(f"[done] {len(all_results)} tasks in {elapsed:.2f}s")
    print(f"  Realtime bulkhead: {BULKHEADS['realtime']._value} free slots")
    print(f"  Background bulkhead: {BULKHEADS['background']._value} free slots")

asyncio.run(simulate_load())
```

**Expected Token Savings:** Bulkheads prevent cascading failures that would force full restarts; a restart during a batch job wastes all in-progress tokens. Isolation keeps both concerns running.
**Environment:** Multi-workload agents; any agent handling both real-time user requests and background processing.

---

### Option 2 — Bulkhead class with queue depth monitoring and rejection

```python
import asyncio
import anthropic
import time
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class Bulkhead:
    name:        str
    max_concurrent: int
    max_queue:   int = 50   # reject if queue grows beyond this
    _sem:        asyncio.Semaphore = field(init=False)
    _waiting:    int = field(default=0, init=False)
    _completed:  int = field(default=0, init=False)
    _rejected:   int = field(default=0, init=False)

    def __post_init__(self):
        self._sem = asyncio.Semaphore(self.max_concurrent)

    async def run(self, coro):
        if self._waiting >= self.max_queue:
            self._rejected += 1
            raise RuntimeError(f"[{self.name}] bulkhead queue full ({self.max_queue})")
        self._waiting += 1
        try:
            async with self._sem:
                self._waiting -= 1
                result = await coro
                self._completed += 1
                return result
        except Exception:
            self._waiting -= 1
            raise

    def stats(self) -> str:
        return (
            f"{self.name}: concurrent≤{self.max_concurrent}, "
            f"waiting={self._waiting}, done={self._completed}, rejected={self._rejected}"
        )

realtime   = Bulkhead("realtime",   max_concurrent=3, max_queue=10)
background = Bulkhead("background", max_concurrent=8, max_queue=100)

async def call_api(prompt: str) -> str:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text

async def main():
    rt_tasks = [
        realtime.run(call_api(f"User query {i}")) for i in range(5)
    ]
    bg_tasks = [
        background.run(call_api(f"Background job {i}")) for i in range(15)
    ]

    results = await asyncio.gather(*rt_tasks, *bg_tasks, return_exceptions=True)
    errors  = sum(1 for r in results if isinstance(r, Exception))
    success = len(results) - errors
    print(f"\n[results] {success} ok, {errors} errors")
    print(realtime.stats())
    print(background.stats())

asyncio.run(main())
```

**Expected Token Savings:** Queue-depth rejection prevents memory exhaustion from unbounded task accumulation; rejected tasks fail fast instead of eventually timing out after consuming a connection for minutes.
**Environment:** High-traffic agents where queue depth must be bounded; multi-tenant platforms where per-pool rejection protects against one tenant's burst starving others.

---

### Option 3 — Thread-pool bulkhead for CPU-bound tool execution

```python
import asyncio
import anthropic
from concurrent.futures import ThreadPoolExecutor
import time

client = anthropic.AsyncAnthropic()

# Separate thread pools for separate concerns
_AI_EXECUTOR    = ThreadPoolExecutor(max_workers=5,  thread_name_prefix="ai")
_TOOL_EXECUTOR  = ThreadPoolExecutor(max_workers=10, thread_name_prefix="tool")
_IO_EXECUTOR    = ThreadPoolExecutor(max_workers=20, thread_name_prefix="io")

async def run_in_pool(executor: ThreadPoolExecutor, fn, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, fn, *args)

def cpu_heavy_tool(data: str) -> str:
    """CPU-bound: runs in TOOL pool, doesn't compete with AI calls."""
    time.sleep(0.05)  # simulate processing
    return f"processed: {data[:20]}"

def file_io_tool(filename: str) -> str:
    """IO-bound: runs in IO pool."""
    time.sleep(0.02)
    return f"read: {filename}"

async def ai_call(prompt: str) -> str:
    """AI calls in AI pool — isolated from tool execution."""
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text

async def process_request(request_id: int) -> dict:
    # Each concern runs in its own thread pool — CPU tools don't block AI calls
    ai_result, tool_result, io_result = await asyncio.gather(
        ai_call(f"Analyse request {request_id}"),
        run_in_pool(_TOOL_EXECUTOR, cpu_heavy_tool, f"data_{request_id}"),
        run_in_pool(_IO_EXECUTOR,   file_io_tool,   f"file_{request_id}.txt"),
    )
    return {"id": request_id, "ai": ai_result[:40], "tool": tool_result, "io": io_result}

async def main():
    start   = time.monotonic()
    results = await asyncio.gather(*[process_request(i) for i in range(8)])
    elapsed = time.monotonic() - start
    for r in results[:3]:
        print(r)
    print(f"\n[done] {len(results)} requests in {elapsed:.2f}s")
    _AI_EXECUTOR.shutdown(wait=False)
    _TOOL_EXECUTOR.shutdown(wait=False)
    _IO_EXECUTOR.shutdown(wait=False)

asyncio.run(main())
```

**Expected Token Savings:** CPU-bound tool execution in a separate pool can't starve AI API calls; AI calls that are in-flight when CPU tools pile up complete normally rather than timing out waiting for thread availability.
**Environment:** Agents combining AI calls with CPU-heavy processing (embeddings, parsing, compression); mixed async/sync workloads.

---

### Option 4 — Bulkhead with circuit breaker per pool

```python
import asyncio
import anthropic
import time
from enum import Enum
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

class State(Enum):
    CLOSED    = "closed"
    OPEN      = "open"
    HALF_OPEN = "half-open"

@dataclass
class BulkheadWithBreaker:
    name:            str
    max_concurrent:  int
    failure_threshold: int  = 3
    recovery_timeout:  float = 10.0
    _sem:            asyncio.Semaphore = field(init=False)
    _state:          State             = field(default=State.CLOSED, init=False)
    _failures:       int               = field(default=0, init=False)
    _opened_at:      float             = field(default=0.0, init=False)

    def __post_init__(self):
        self._sem = asyncio.Semaphore(self.max_concurrent)

    def _check_breaker(self) -> bool:
        """Returns True if request should proceed."""
        if self._state == State.CLOSED:
            return True
        if self._state == State.OPEN:
            if time.monotonic() - self._opened_at >= self.recovery_timeout:
                self._state = State.HALF_OPEN
                print(f"[{self.name}] HALF-OPEN — probing")
                return True
            return False
        return True  # HALF_OPEN: allow one probe

    def _record_success(self):
        self._failures = 0
        if self._state == State.HALF_OPEN:
            self._state = State.CLOSED
            print(f"[{self.name}] CLOSED — recovered")

    def _record_failure(self):
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._state    = State.OPEN
            self._opened_at = time.monotonic()
            print(f"[{self.name}] OPEN — {self._failures} failures")

    async def run(self, coro):
        if not self._check_breaker():
            raise RuntimeError(f"[{self.name}] circuit OPEN — fast fail")
        async with self._sem:
            try:
                result = await coro
                self._record_success()
                return result
            except Exception:
                self._record_failure()
                raise

    def status(self) -> str:
        return f"{self.name}: {self._state.value}, failures={self._failures}"

search_bh = BulkheadWithBreaker("search",   max_concurrent=5,  failure_threshold=2)
ai_bh     = BulkheadWithBreaker("ai_calls", max_concurrent=10, failure_threshold=5)

async def search(query: str) -> str:
    await asyncio.sleep(0.05)
    return f"results for: {query}"

async def ai_call(prompt: str) -> str:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text

async def main():
    for i in range(8):
        results = await asyncio.gather(
            ai_bh.run(ai_call(f"Analyse {i}")),
            search_bh.run(search(f"query {i}")),
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, Exception)]
        if errors:
            print(f"[{i}] errors: {[str(e)[:50] for e in errors]}")
    print()
    print(ai_bh.status())
    print(search_bh.status())

asyncio.run(main())
```

**Expected Token Savings:** Circuit breaker on a per-pool basis means a failing search service doesn't cause AI call failures; the AI pool stays healthy and keeps processing while the search pool's breaker trips.
**Environment:** Agents calling multiple external services; any architecture where one service failing should not cascade to unrelated services.

---

### Option 5 — Named bulkhead registry with dynamic pool sizing

```python
import asyncio
import anthropic
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class PoolConfig:
    name:        str
    concurrency: int
    priority:    int = 5  # 1=highest, 10=lowest

class BulkheadRegistry:
    def __init__(self):
        self._pools: dict[str, tuple[asyncio.Semaphore, PoolConfig]] = {}

    def register(self, cfg: PoolConfig) -> None:
        self._pools[cfg.name] = (asyncio.Semaphore(cfg.concurrency), cfg)
        print(f"[registry] registered pool: {cfg.name} (concurrency={cfg.concurrency}, priority={cfg.priority})")

    def resize(self, name: str, new_concurrency: int) -> None:
        """Dynamically adjust pool size (takes effect for new acquisitions)."""
        _, cfg  = self._pools[name]
        cfg.concurrency = new_concurrency
        self._pools[name] = (asyncio.Semaphore(new_concurrency), cfg)
        print(f"[registry] resized {name} → {new_concurrency}")

    async def run(self, pool_name: str, coro):
        sem, cfg = self._pools.get(pool_name, (asyncio.Semaphore(3), PoolConfig("default", 3)))
        async with sem:
            return await coro

    def report(self) -> None:
        print("\n=== Bulkhead Registry ===")
        for name, (sem, cfg) in sorted(self._pools.items(), key=lambda x: x[1][1].priority):
            print(f"  {name} (priority={cfg.priority}): capacity={cfg.concurrency}, free={sem._value}")

registry = BulkheadRegistry()
registry.register(PoolConfig("p0_critical",    concurrency=3, priority=1))
registry.register(PoolConfig("p1_user_facing", concurrency=8, priority=2))
registry.register(PoolConfig("p2_background",  concurrency=15, priority=5))
registry.register(PoolConfig("p3_batch",       concurrency=5,  priority=9))

async def api_call(pool: str, prompt: str) -> str:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text

async def main():
    tasks = [
        registry.run("p0_critical",    api_call("p0_critical",    "Emergency alert?")),
        registry.run("p1_user_facing", api_call("p1_user_facing", "User dashboard query")),
        registry.run("p2_background",  api_call("p2_background",  "Background summarisation")),
        registry.run("p3_batch",       api_call("p3_batch",       "Nightly batch job")),
    ] * 3  # run each 3 times

    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok = sum(1 for r in results if not isinstance(r, Exception))
    print(f"\n[done] {ok}/{len(results)} succeeded")
    registry.report()

asyncio.run(main())
```

**Expected Token Savings:** Priority-aware pool sizing reserves capacity for critical workloads; batch jobs cannot consume the concurrency slots needed for real-time critical requests.
**Environment:** Multi-priority agents (critical alerts + user queries + batch); platforms where SLA tiers require resource isolation.

---

### Option 6 — Bulkhead with timeout per pool and fallback response

```python
import asyncio
import anthropic
import time
from dataclasses import dataclass, field
from typing import Any, Callable

client = anthropic.AsyncAnthropic()

@dataclass
class TimeboxedBulkhead:
    name:           str
    max_concurrent: int
    timeout_seconds: float
    fallback:       Callable[[], Any] = lambda: None
    _sem:           asyncio.Semaphore = field(init=False)
    _timeouts:      int = field(default=0, init=False)

    def __post_init__(self):
        self._sem = asyncio.Semaphore(self.max_concurrent)

    async def run(self, coro):
        async with self._sem:
            try:
                return await asyncio.wait_for(coro, timeout=self.timeout_seconds)
            except asyncio.TimeoutError:
                self._timeouts += 1
                print(f"[{self.name}] timeout after {self.timeout_seconds}s (total: {self._timeouts})")
                return self.fallback()

    def stats(self) -> str:
        return f"{self.name}: concurrency≤{self.max_concurrent}, timeouts={self._timeouts}"

# Each pool has its own timeout — batch can be slow, realtime must be fast
realtime_bh  = TimeboxedBulkhead("realtime",   max_concurrent=4,  timeout_seconds=5.0,
                                  fallback=lambda: "Response temporarily unavailable.")
background_bh = TimeboxedBulkhead("background", max_concurrent=10, timeout_seconds=30.0,
                                   fallback=lambda: None)

async def ai_call(prompt: str, delay: float = 0.0) -> str:
    if delay:
        await asyncio.sleep(delay)
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text

async def main():
    tasks = [
        realtime_bh.run(ai_call(f"User query {i}"))    for i in range(4)
    ] + [
        background_bh.run(ai_call(f"Background {i}"))  for i in range(8)
    ]

    start   = time.monotonic()
    results = await asyncio.gather(*tasks)
    elapsed = time.monotonic() - start

    ok    = sum(1 for r in results if r is not None)
    fb    = sum(1 for r in results if r == "Response temporarily unavailable.")
    empty = sum(1 for r in results if r is None)
    print(f"\n[done] {len(results)} tasks in {elapsed:.2f}s: {ok} ok, {fb} fallback, {empty} skipped")
    print(realtime_bh.stats())
    print(background_bh.stats())

asyncio.run(main())
```

**Expected Token Savings:** Per-pool timeouts prevent slow background tasks from holding semaphore slots for long periods; fallback responses let real-time users get *something* while the slow pool catches up.
**Environment:** Agents with SLA requirements; real-time user-facing endpoints that must respond within N seconds regardless of backend slowness.

---

## Comparison

| Option | Isolation Mechanism | Queue Rejection | Circuit Breaker | Timeout Per Pool | Best For |
|---|---|---|---|---|---|
| 1. Per-concern semaphores | Separate semaphores | No | No | No | Baseline bulkhead isolation |
| 2. Bulkhead class + queue | Semaphore + queue limit | Yes | No | No | High-traffic with bounded queues |
| 3. Thread pool per concern | Separate ThreadPoolExecutor | No | No | No | Mixed async + CPU-bound workloads |
| 4. Bulkhead + circuit breaker | Semaphore + state machine | No | Yes | No | Multi-service agents with failure isolation |
| 5. Named registry | Registry of semaphores | No | No | No | Multi-priority workloads; dynamic resizing |
| 6. Timeboxed bulkhead | Semaphore + wait_for | No | No | Yes | SLA-bound real-time agents |
