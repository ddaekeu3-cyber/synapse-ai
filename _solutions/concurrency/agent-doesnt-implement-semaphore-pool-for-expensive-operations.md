---
title: "Agent Doesn't Implement Semaphore Pool for Expensive Operations"
slug: agent-doesnt-implement-semaphore-pool-for-expensive-operations
category: concurrency
tags: [semaphore, concurrency, rate-limiting, resource-pool, asyncio, anthropic-sdk]
description: >
  The agent issues every LLM call and tool invocation without any concurrency
  limit, allowing a burst of simultaneous requests to exhaust the API rate
  limit, overwhelm downstream services, or consume all available file
  descriptors and memory. A semaphore pool bounds concurrent expensive operations
  while letting cheap operations proceed freely.
symptoms:
  - Burst of 50 simultaneous requests triggers 429 rate-limit responses
  - Database connections exhausted when many tool calls run concurrently
  - Memory usage spikes proportionally to concurrent request count
  - No distinction between cheap (classification) and expensive (generation) operations
related_solutions:
  - agent-doesnt-implement-fair-queuing-for-concurrent-users
  - agent-doesnt-implement-load-shedding-under-overload
  - agent-doesnt-implement-request-deduplication-for-concurrent-callers
---

## Problem

`asyncio.gather(*[expensive_call() for _ in range(1000)])` will attempt 1000
concurrent LLM calls. Even if they don't all fire at once, the concurrency
burst triggers rate limits, depletes connection pools, and causes memory
spikes. A semaphore constrains parallelism to a safe level while still
allowing maximum throughput within that limit. Different operations have
different cost profiles and should have different semaphores.

---

## Solution 1 — Single Global Semaphore (Simplest)

A single `asyncio.Semaphore` limits how many LLM calls can run concurrently
across the entire process.

```python
import anthropic
import asyncio
import time

MAX_CONCURRENT_LLM = 5
_llm_sem = asyncio.Semaphore(MAX_CONCURRENT_LLM)


async def rate_limited_create(
    messages: list,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 256,
) -> str:
    async with _llm_sem:
        client = anthropic.AsyncAnthropic()
        resp = await client.messages.create(
            model=model, max_tokens=max_tokens, messages=messages
        )
        return resp.content[0].text


async def demo_global_sem():
    start = time.monotonic()
    tasks = [
        rate_limited_create([{"role": "user", "content": f"Q{i}: define caching."}])
        for i in range(12)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    elapsed = time.monotonic() - start
    ok = sum(1 for r in results if isinstance(r, str))
    print(f"[global-sem] {ok}/12 OK  elapsed={elapsed:.1f}s  (max {MAX_CONCURRENT_LLM} concurrent)")


asyncio.run(demo_global_sem())
```

---

## Solution 2 — Tiered Semaphore Pool (Model-Cost-Aware)

Different model tiers have different cost and rate-limit profiles. Haiku can
run 20 concurrent calls; Opus should be limited to 2. Use a separate semaphore
per tier.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass


@dataclass
class ModelTier:
    model:       str
    concurrency: int
    max_tokens:  int


TIERS = [
    ModelTier("claude-haiku-4-5-20251001",  concurrency=20, max_tokens=512),
    ModelTier("claude-sonnet-4-6",           concurrency=8,  max_tokens=1024),
    ModelTier("claude-opus-4-6",             concurrency=2,  max_tokens=4096),
]

_tier_sems: dict[str, asyncio.Semaphore] = {
    t.model: asyncio.Semaphore(t.concurrency) for t in TIERS
}
_tier_map: dict[str, ModelTier] = {t.model: t for t in TIERS}


async def tiered_create(
    messages: list,
    model: str = "claude-sonnet-4-6",
    max_tokens: int | None = None,
) -> str:
    sem  = _tier_sems.get(model, asyncio.Semaphore(5))
    tier = _tier_map.get(model)
    tok  = max_tokens or (tier.max_tokens if tier else 512)

    async with sem:
        client = anthropic.AsyncAnthropic()
        resp = await client.messages.create(
            model=model, max_tokens=tok, messages=messages
        )
        return resp.content[0].text


async def demo_tiered_sem():
    start = time.monotonic()
    # Mix of haiku (fast, high concurrency) and sonnet (slower, lower concurrency)
    tasks = (
        [tiered_create([{"role": "user", "content": f"H{i}: define cache."}],
                       model="claude-haiku-4-5-20251001") for i in range(10)] +
        [tiered_create([{"role": "user", "content": f"S{i}: explain CAP."}],
                       model="claude-sonnet-4-6") for i in range(4)]
    )
    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok = sum(1 for r in results if isinstance(r, str))
    elapsed = time.monotonic() - start
    print(f"[tiered-sem] {ok}/{len(results)} OK  elapsed={elapsed:.1f}s")


asyncio.run(demo_tiered_sem())
```

---

## Solution 3 — Named Operation Semaphore Pool

Maintain a registry of named semaphores — one per operation type — so that
LLM calls, DB queries, web requests, and code execution each have independent
concurrency budgets. Adding a new expensive operation is a one-line registration.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class SemaphorePool:
    _pool: dict[str, asyncio.Semaphore] = field(default_factory=dict)
    _limits: dict[str, int] = field(default_factory=dict)

    def register(self, name: str, limit: int) -> "SemaphorePool":
        self._limits[name] = limit
        self._pool[name] = asyncio.Semaphore(limit)
        return self

    def get(self, name: str) -> asyncio.Semaphore:
        if name not in self._pool:
            raise KeyError(f"Semaphore '{name}' not registered")
        return self._pool[name]

    def usage(self) -> dict[str, dict]:
        return {
            name: {
                "limit": self._limits[name],
                "available": sem._value,
                "in_use": self._limits[name] - sem._value,
            }
            for name, sem in self._pool.items()
        }


_pool = (
    SemaphorePool()
    .register("llm_haiku",   20)
    .register("llm_sonnet",   8)
    .register("llm_opus",     2)
    .register("db_query",    10)
    .register("web_request",  5)
    .register("code_exec",    3)
)


async def with_sem(operation: str):
    """Async context manager shorthand."""
    return _pool.get(operation)


async def llm_call(messages: list, model: str = "claude-sonnet-4-6") -> str:
    op_name = {
        "claude-haiku-4-5-20251001": "llm_haiku",
        "claude-sonnet-4-6":          "llm_sonnet",
        "claude-opus-4-6":            "llm_opus",
    }.get(model, "llm_sonnet")

    async with _pool.get(op_name):
        client = anthropic.AsyncAnthropic()
        resp = await client.messages.create(model=model, max_tokens=256, messages=messages)
        return resp.content[0].text


async def db_query(sql: str) -> str:
    async with _pool.get("db_query"):
        await asyncio.sleep(0.05)   # simulate DB latency
        return f"Result for: {sql}"


async def demo_pool():
    start = time.monotonic()
    tasks = (
        [llm_call([{"role": "user", "content": f"LLM {i}: define idempotency."}]) for i in range(6)] +
        [db_query(f"SELECT * FROM users WHERE id={i}") for i in range(8)]
    )
    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok = sum(1 for r in results if isinstance(r, str))
    print(f"[named-pool] {ok}/{len(results)} OK  elapsed={time.monotonic()-start:.1f}s")
    print(f"Pool usage: {_pool.usage()}")


asyncio.run(demo_pool())
```

---

## Solution 4 — Semaphore with Queue Depth Monitoring and Backpressure

Expose the semaphore's wait queue depth as a metric. When too many callers
are waiting, reject new requests early (load shedding) rather than letting
the queue grow unbounded.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class MonitoredSemaphore:
    capacity:    int
    shed_at:     int    = 20   # reject if >N callers waiting
    _sem:        asyncio.Semaphore = field(init=False)
    _waiters:    int = 0
    _in_use:     int = 0
    _shed_count: int = 0
    _total:      int = 0

    def __post_init__(self):
        self._sem = asyncio.Semaphore(self.capacity)

    async def acquire(self) -> bool:
        """Returns True if acquired, False if shed."""
        if self._waiters >= self.shed_at:
            self._shed_count += 1
            return False
        self._waiters += 1
        self._total += 1
        await self._sem.acquire()
        self._waiters -= 1
        self._in_use += 1
        return True

    def release(self) -> None:
        self._in_use -= 1
        self._sem.release()

    def stats(self) -> dict:
        return {
            "capacity":   self.capacity,
            "in_use":     self._in_use,
            "waiting":    self._waiters,
            "shed":       self._shed_count,
            "total":      self._total,
            "shed_rate":  f"{self._shed_count / max(self._total, 1):.0%}",
        }


_monitored = MonitoredSemaphore(capacity=5, shed_at=10)


async def backpressure_create(messages: list, model: str = "claude-sonnet-4-6") -> str:
    acquired = await _monitored.acquire()
    if not acquired:
        raise RuntimeError("Service overloaded — request shed. Please retry later.")
    try:
        client = anthropic.AsyncAnthropic()
        resp = await client.messages.create(model=model, max_tokens=128, messages=messages)
        return resp.content[0].text
    finally:
        _monitored.release()


async def demo_backpressure():
    # Fire 25 concurrent requests at a semaphore with capacity 5, shed at 10 waiting
    tasks = [
        backpressure_create([{"role": "user", "content": f"Q{i}: define ACID."}])
        for i in range(25)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok   = sum(1 for r in results if isinstance(r, str))
    shed = sum(1 for r in results if isinstance(r, RuntimeError))
    print(f"[backpressure] ok={ok}  shed={shed}  total=25")
    print(f"Stats: {_monitored.stats()}")


asyncio.run(demo_backpressure())
```

---

## Solution 5 — Priority Semaphore (High-Priority Work Jumps Queue)

Implement a priority queue in front of the semaphore so high-priority requests
(real-time user-facing) preempt low-priority batch work when the semaphore
is contended.

```python
import anthropic
import asyncio
import heapq
import time
from dataclasses import dataclass, field


@dataclass(order=True)
class PrioritisedWork:
    priority:  int         # lower = higher priority
    seq:       int         # tiebreaker — earlier requests win
    messages:  list = field(compare=False)
    model:     str  = field(compare=False, default="claude-sonnet-4-6")
    future:    asyncio.Future = field(compare=False, default=None)


class PrioritySemaphorePool:
    def __init__(self, concurrency: int = 5):
        self._concurrency = concurrency
        self._sem = asyncio.Semaphore(concurrency)
        self._heap: list[PrioritisedWork] = []
        self._seq = 0
        self._lock = asyncio.Lock()
        self._work_ready = asyncio.Event()

    async def submit(self, messages: list, priority: int = 5,
                     model: str = "claude-sonnet-4-6") -> str:
        loop = asyncio.get_running_loop()
        fut  = loop.create_future()
        async with self._lock:
            work = PrioritisedWork(
                priority=priority, seq=self._seq,
                messages=messages, model=model, future=fut,
            )
            self._seq += 1
            heapq.heappush(self._heap, work)
        self._work_ready.set()
        return await fut

    async def _worker(self) -> None:
        client = anthropic.AsyncAnthropic()
        while True:
            await self._work_ready.wait()
            async with self._lock:
                if not self._heap:
                    self._work_ready.clear()
                    continue
                work = heapq.heappop(self._heap)
                if not self._heap:
                    self._work_ready.clear()

            async with self._sem:
                try:
                    resp = await client.messages.create(
                        model=work.model, max_tokens=128, messages=work.messages
                    )
                    work.future.set_result(resp.content[0].text)
                except Exception as e:
                    work.future.set_exception(e)

    def start(self, n_workers: int = 3) -> None:
        for _ in range(n_workers):
            asyncio.create_task(self._worker())


_prio_pool = PrioritySemaphorePool(concurrency=4)


async def demo_priority():
    _prio_pool.start(n_workers=4)

    start = time.monotonic()
    # Mix of batch (priority 9) and real-time (priority 1) work
    tasks = (
        [_prio_pool.submit(
            [{"role": "user", "content": f"Batch {i}: explain sharding."}],
            priority=9,
        ) for i in range(5)] +
        [_prio_pool.submit(
            [{"role": "user", "content": f"RT {i}: quick answer: what is DNS?"}],
            priority=1,
        ) for i in range(3)]
    )
    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok = sum(1 for r in results if isinstance(r, str))
    print(f"[priority-sem] {ok}/{len(results)} OK  elapsed={time.monotonic()-start:.1f}s")


asyncio.run(demo_priority())
```

---

## Solution 6 — Adaptive Semaphore that Scales with Success Rate

Start with a conservative concurrency limit and automatically increase it when
the success rate is high, or decrease it when errors are detected — similar
to AIMD (Additive Increase, Multiplicative Decrease) used in TCP congestion
control.

```python
import anthropic
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class AIMDSemaphore:
    min_limit:   int = 2
    max_limit:   int = 30
    initial:     int = 5
    window:      int = 50     # outcomes to consider
    increase_at: float = 0.95  # increase if success rate > this
    decrease_at: float = 0.80  # decrease if success rate < this

    _limit:      int = field(init=False)
    _sem:        asyncio.Semaphore = field(init=False)
    _outcomes:   deque = field(default_factory=deque)
    _lock:       asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self):
        self._limit = self.initial
        self._sem   = asyncio.Semaphore(self.initial)

    def _record(self, success: bool) -> None:
        self._outcomes.append(1 if success else 0)
        if len(self._outcomes) > self.window:
            self._outcomes.popleft()

    @property
    def _success_rate(self) -> float:
        return sum(self._outcomes) / max(len(self._outcomes), 1)

    async def _maybe_adjust(self) -> None:
        async with self._lock:
            if len(self._outcomes) < 10:
                return
            sr = self._success_rate
            if sr > self.increase_at and self._limit < self.max_limit:
                self._limit += 1
                self._sem._value += 1   # add a permit
                print(f"[aimd] ↑ limit={self._limit}  sr={sr:.0%}")
            elif sr < self.decrease_at and self._limit > self.min_limit:
                self._limit = max(self.min_limit, int(self._limit * 0.7))
                print(f"[aimd] ↓ limit={self._limit}  sr={sr:.0%}")

    async def run(self, coro) -> any:
        await self._sem.acquire()
        try:
            result = await coro
            self._record(True)
            return result
        except Exception:
            self._record(False)
            raise
        finally:
            self._sem.release()
            await self._maybe_adjust()


_aimd = AIMDSemaphore(initial=5, min_limit=2, max_limit=20)


async def adaptive_create(messages: list, model: str = "claude-sonnet-4-6") -> str:
    client = anthropic.AsyncAnthropic()
    return await _aimd.run(
        client.messages.create(model=model, max_tokens=128, messages=messages)
    )


async def demo_aimd():
    # Warm up with successful calls — limit should climb
    for i in range(20):
        try:
            await adaptive_create([{"role": "user", "content": f"Q{i}: define TTL."}])
        except Exception:
            pass
    print(f"[aimd] final limit after 20 calls: {_aimd._limit}")


asyncio.run(demo_aimd())
```

---

## Comparison

| Approach | Granularity | Backpressure | Priority | Self-tuning | Complexity |
|---|---|---|---|---|---|
| Single global semaphore | Process-wide | No | No | No | Very low |
| Tiered per-model semaphore | Per model tier | No | No | No | Low |
| Named operation pool | Per operation type | No | No | No | Low |
| Monitored with load shedding | Process-wide | Yes (shed) | No | No | Medium |
| Priority semaphore queue | Per operation | No | Yes | No | Medium |
| AIMD adaptive semaphore | Process-wide | Yes (adapts) | No | Yes | High |

**Rule of thumb:**
- Any service making concurrent LLM calls → Solution 1 (global semaphore) as minimum baseline
- Multiple model tiers → Solution 2 (tiered) to match limits to each model's rate-limit quota
- Many different operation types (LLM + DB + web) → Solution 3 (named pool)
- High-traffic services → Solution 4 (monitored + shed) to prevent queue unbounded growth
- Mixed real-time + batch workloads → Solution 5 (priority) so batch never blocks user-facing requests
- Dynamic traffic patterns → Solution 6 (AIMD) for hands-off tuning as load changes
