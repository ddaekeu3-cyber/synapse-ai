---
title: "Agent Doesn't Implement Semaphore-Based Concurrency Control"
description: "Six solutions for limiting concurrent LLM calls, tool executions, and resource access using asyncio semaphores and related primitives."
difficulty: intermediate
category: concurrency
tags: [semaphore, concurrency, rate-limiting, asyncio, throttling, resource-control]
---

# Agent Doesn't Implement Semaphore-Based Concurrency Control

Without concurrency limits, agents will fire hundreds of simultaneous LLM calls on burst traffic, exhaust API rate limits, and cause cascading 429 errors. Semaphores are the right primitive for controlling how many operations run at once. These six solutions cover basic semaphores through tiered, adaptive, and per-resource variants.

## Solution 1: Basic AsyncIO Semaphore for LLM Call Throttling

Wrap every LLM call in a semaphore to cap concurrent in-flight requests.

```python
import asyncio
from anthropic import AsyncAnthropic

# Global semaphore: at most 5 concurrent LLM calls
_LLM_SEMAPHORE = asyncio.Semaphore(5)


async def bounded_llm_call(
    client: AsyncAnthropic,
    message: str,
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 1024,
) -> str:
    async with _LLM_SEMAPHORE:
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": message}],
        )
        return response.content[0].text


async def process_batch(messages: list[str]) -> list[str]:
    """Process many messages with at most 5 concurrent LLM calls."""
    client = AsyncAnthropic()
    tasks = [bounded_llm_call(client, msg) for msg in messages]
    return await asyncio.gather(*tasks)


async def demo_basic_semaphore():
    messages = [f"What is {i} squared?" for i in range(20)]
    results = await process_batch(messages)
    print(f"Processed {len(results)} messages with max 5 concurrent calls")
    for i, r in enumerate(results[:3]):
        print(f"  [{i}] {r[:60]}")
```

## Solution 2: Tiered Semaphore by Request Priority

High-priority requests get a larger concurrency budget; low-priority share a tighter cap.

```python
import asyncio
from enum import IntEnum
from anthropic import AsyncAnthropic


class Priority(IntEnum):
    HIGH = 0
    NORMAL = 1
    LOW = 2


class TieredSemaphore:
    """
    Three concurrency tiers:
    - HIGH: up to 8 concurrent (e.g., interactive user requests)
    - NORMAL: up to 5 concurrent (e.g., background tasks)
    - LOW: up to 2 concurrent (e.g., batch analytics)
    """

    def __init__(self, high: int = 8, normal: int = 5, low: int = 2):
        self._semaphores = {
            Priority.HIGH: asyncio.Semaphore(high),
            Priority.NORMAL: asyncio.Semaphore(normal),
            Priority.LOW: asyncio.Semaphore(low),
        }

    def acquire(self, priority: Priority):
        return self._semaphores[priority]


_TIERED = TieredSemaphore()


class PriorityBoundedAgent:
    def __init__(self, semaphores: TieredSemaphore = _TIERED):
        self.client = AsyncAnthropic()
        self.sem = semaphores

    async def chat(
        self,
        message: str,
        priority: Priority = Priority.NORMAL,
        model: str = "claude-haiku-4-5-20251001",
    ) -> str:
        async with self.sem.acquire(priority):
            response = await self.client.messages.create(
                model=model,
                max_tokens=1024,
                messages=[{"role": "user", "content": message}],
            )
            return response.content[0].text


async def demo_tiered():
    agent = PriorityBoundedAgent()

    # Mix high and low priority work
    high_tasks = [agent.chat(f"Hi {i}", Priority.HIGH) for i in range(10)]
    low_tasks = [agent.chat(f"Batch {i}", Priority.LOW) for i in range(5)]

    results = await asyncio.gather(*high_tasks, *low_tasks)
    print(f"Completed {len(results)} tasks across priority tiers")
```

## Solution 3: Per-Resource Semaphore Registry

Different external resources (database, file system, external API) each get their own concurrency cap.

```python
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncGenerator
from anthropic import AsyncAnthropic


@dataclass
class ResourceLimits:
    llm_calls: int = 5
    db_queries: int = 10
    file_ops: int = 3
    external_api: int = 4
    tool_executions: int = 6


class SemaphoreRegistry:
    def __init__(self, limits: ResourceLimits | None = None):
        lim = limits or ResourceLimits()
        self._sems: dict[str, asyncio.Semaphore] = {
            "llm": asyncio.Semaphore(lim.llm_calls),
            "db": asyncio.Semaphore(lim.db_queries),
            "file": asyncio.Semaphore(lim.file_ops),
            "api": asyncio.Semaphore(lim.external_api),
            "tool": asyncio.Semaphore(lim.tool_executions),
        }
        self._counts: dict[str, int] = {k: 0 for k in self._sems}
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self, resource: str) -> AsyncGenerator[None, None]:
        sem = self._sems.get(resource)
        if sem is None:
            raise ValueError(f"Unknown resource '{resource}'. Known: {list(self._sems)}")
        async with sem:
            async with self._lock:
                self._counts[resource] += 1
            try:
                yield
            finally:
                async with self._lock:
                    self._counts[resource] -= 1

    def stats(self) -> dict[str, int]:
        return dict(self._counts)


_REGISTRY = SemaphoreRegistry()


class ResourceControlledAgent:
    def __init__(self, registry: SemaphoreRegistry = _REGISTRY):
        self.client = AsyncAnthropic()
        self.registry = registry

    async def llm_call(self, message: str) -> str:
        async with self.registry.acquire("llm"):
            response = await self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{"role": "user", "content": message}],
            )
            return response.content[0].text

    async def db_query(self, query: str) -> str:
        async with self.registry.acquire("db"):
            # Simulate DB query
            await asyncio.sleep(0.1)
            return f"db_result({query})"

    async def file_read(self, path: str) -> str:
        async with self.registry.acquire("file"):
            await asyncio.sleep(0.05)
            return f"file_content({path})"

    async def process_task(self, task: str) -> dict:
        """Multi-resource task: LLM + DB + file."""
        # These respect individual resource limits
        llm_result, db_result, file_result = await asyncio.gather(
            self.llm_call(task),
            self.db_query(f"SELECT * FROM tasks WHERE name='{task}'"),
            self.file_read(f"/data/{task}.json"),
        )
        return {
            "task": task,
            "llm": llm_result[:50],
            "db": db_result,
            "file": file_result,
        }


async def demo_registry():
    agent = ResourceControlledAgent()
    tasks = [agent.process_task(f"task_{i}") for i in range(8)]
    results = await asyncio.gather(*tasks)
    print(f"Completed {len(results)} multi-resource tasks")
    print(f"Current resource usage: {agent.registry.stats()}")
```

## Solution 4: Adaptive Semaphore That Adjusts to Error Rate

Dynamically reduce the concurrency limit when error rates rise; recover when errors clear.

```python
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic, RateLimitError


@dataclass
class ErrorWindow:
    window_size: int = 20
    _results: deque = field(default_factory=lambda: deque(maxlen=20))

    def record(self, success: bool):
        self._results.append(success)

    @property
    def error_rate(self) -> float:
        if not self._results:
            return 0.0
        return 1 - sum(self._results) / len(self._results)

    @property
    def has_enough_samples(self) -> bool:
        return len(self._results) >= 5


class AdaptiveSemaphore:
    """
    Starts at max_concurrency; halves on high error rate;
    gradually recovers as errors clear.
    """

    def __init__(
        self,
        min_concurrency: int = 1,
        max_concurrency: int = 10,
        error_threshold: float = 0.3,
        check_interval: float = 5.0,
    ):
        self.min_c = min_concurrency
        self.max_c = max_concurrency
        self.error_threshold = error_threshold
        self.check_interval = check_interval

        self._current = max_concurrency
        self._sem = asyncio.Semaphore(max_concurrency)
        self._window = ErrorWindow()
        self._lock = asyncio.Lock()
        self._adjuster_task: asyncio.Task | None = None

    async def start(self):
        self._adjuster_task = asyncio.create_task(self._adjust_loop())

    async def stop(self):
        if self._adjuster_task:
            self._adjuster_task.cancel()

    async def _adjust_loop(self):
        while True:
            await asyncio.sleep(self.check_interval)
            if not self._window.has_enough_samples:
                continue
            async with self._lock:
                error_rate = self._window.error_rate
                if error_rate > self.error_threshold and self._current > self.min_c:
                    new = max(self.min_c, self._current // 2)
                    print(f"[ADAPTIVE] Error rate={error_rate:.0%} → reduce {self._current} → {new}")
                    self._current = new
                    self._sem = asyncio.Semaphore(new)
                elif error_rate < self.error_threshold / 2 and self._current < self.max_c:
                    new = min(self.max_c, self._current + 1)
                    print(f"[ADAPTIVE] Error rate={error_rate:.0%} → increase {self._current} → {new}")
                    self._current = new
                    self._sem = asyncio.Semaphore(new)

    def record_success(self):
        self._window.record(True)

    def record_error(self):
        self._window.record(False)

    async def __aenter__(self):
        await self._sem.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._sem.release()
        if exc_type is None:
            self.record_success()
        else:
            self.record_error()


class AdaptiveAgent:
    def __init__(self):
        self.client = AsyncAnthropic()
        self.semaphore = AdaptiveSemaphore(min_concurrency=1, max_concurrency=8)

    async def start(self):
        await self.semaphore.start()

    async def stop(self):
        await self.semaphore.stop()

    async def chat(self, message: str) -> str:
        async with self.semaphore:
            response = await self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{"role": "user", "content": message}],
            )
            return response.content[0].text


async def demo_adaptive():
    agent = AdaptiveAgent()
    await agent.start()
    messages = [f"Question {i}" for i in range(30)]
    results = await asyncio.gather(*[agent.chat(m) for m in messages], return_exceptions=True)
    ok = sum(1 for r in results if not isinstance(r, Exception))
    print(f"Completed {ok}/{len(messages)} requests")
    await agent.stop()
```

## Solution 5: Semaphore with Queue Position Tracking and Timeout

Add position-in-queue visibility and per-request wait-timeout so callers can fail fast instead of waiting indefinitely.

```python
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class WaitStats:
    request_id: str
    enqueued_at: float = field(default_factory=time.time)
    acquired_at: float | None = None
    released_at: float | None = None

    @property
    def wait_ms(self) -> float | None:
        if self.acquired_at:
            return (self.acquired_at - self.enqueued_at) * 1000
        return None

    @property
    def hold_ms(self) -> float | None:
        if self.acquired_at and self.released_at:
            return (self.released_at - self.acquired_at) * 1000
        return None


class TrackedSemaphore:
    def __init__(self, concurrency: int = 5):
        self._sem = asyncio.Semaphore(concurrency)
        self._concurrency = concurrency
        self._waiting: dict[str, WaitStats] = {}
        self._lock = asyncio.Lock()
        self.total_acquired = 0
        self.total_timed_out = 0

    @property
    def queue_depth(self) -> int:
        return len(self._waiting)

    async def acquire(self, timeout: float = 30.0) -> WaitStats:
        request_id = str(uuid.uuid4())[:8]
        stats = WaitStats(request_id=request_id)
        async with self._lock:
            self._waiting[request_id] = stats

        try:
            acquired = await asyncio.wait_for(self._sem.acquire(), timeout=timeout)
            stats.acquired_at = time.time()
            async with self._lock:
                self.total_acquired += 1
            return stats
        except asyncio.TimeoutError:
            async with self._lock:
                self._waiting.pop(request_id, None)
                self.total_timed_out += 1
            raise TimeoutError(
                f"Request {request_id} waited {timeout:.1f}s; queue depth was {self.queue_depth}"
            )

    def release(self, stats: WaitStats):
        stats.released_at = time.time()
        self._waiting.pop(stats.request_id, None)
        self._sem.release()

    def summary(self) -> dict:
        return {
            "concurrency_limit": self._concurrency,
            "current_queue_depth": self.queue_depth,
            "total_acquired": self.total_acquired,
            "total_timed_out": self.total_timed_out,
        }


class TrackedSemaphoreAgent:
    def __init__(self, concurrency: int = 4, wait_timeout: float = 10.0):
        self.client = AsyncAnthropic()
        self.sem = TrackedSemaphore(concurrency)
        self.wait_timeout = wait_timeout

    async def chat(self, message: str) -> tuple[str, WaitStats]:
        stats = await self.sem.acquire(timeout=self.wait_timeout)
        try:
            response = await self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{"role": "user", "content": message}],
            )
            return response.content[0].text, stats
        finally:
            self.sem.release(stats)


async def demo_tracked():
    agent = TrackedSemaphoreAgent(concurrency=3, wait_timeout=8.0)
    messages = [f"Message {i}" for i in range(12)]

    async def run_one(msg: str):
        try:
            text, stats = await agent.chat(msg)
            print(
                f"  [{stats.request_id}] wait={stats.wait_ms:.0f}ms "
                f"hold={stats.hold_ms:.0f}ms text={text[:40]}"
            )
        except TimeoutError as e:
            print(f"  TIMEOUT: {e}")

    await asyncio.gather(*[run_one(m) for m in messages])
    print(f"\nSemaphore summary: {agent.sem.summary()}")
```

## Solution 6: Weighted Semaphore for Variable-Cost Operations

Large prompts consume more capacity; weight acquisition by estimated token count so heavy requests don't fully occupy all slots.

```python
import asyncio
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


class WeightedSemaphore:
    """
    A semaphore with a total capacity (e.g., 100 token-units).
    Each acquire() takes a weighted share. Heavy requests take more capacity.
    """

    def __init__(self, capacity: int = 100):
        self._capacity = capacity
        self._available = capacity
        self._condition = asyncio.Condition()

    async def acquire(self, weight: int = 1):
        if weight > self._capacity:
            raise ValueError(f"weight={weight} exceeds total capacity={self._capacity}")
        async with self._condition:
            while self._available < weight:
                await self._condition.wait()
            self._available -= weight

    def release(self, weight: int = 1):
        asyncio.get_event_loop().call_soon_threadsafe(self._release_sync, weight)

    def _release_sync(self, weight: int):
        async def _release():
            async with self._condition:
                self._available = min(self._capacity, self._available + weight)
                self._condition.notify_all()
        asyncio.ensure_future(_release())

    async def __aenter__(self, weight: int = 1):
        self._current_weight = weight
        await self.acquire(weight)
        return self

    async def __aexit__(self, *args):
        async with self._condition:
            self._available = min(self._capacity, self._available + self._current_weight)
            self._condition.notify_all()

    @property
    def available(self) -> int:
        return self._available


@dataclass
class WeightedRequest:
    message: str
    estimated_tokens: int  # Caller's estimate of output tokens

    @property
    def weight(self) -> int:
        """Map estimated token count to capacity units (1 unit per 200 tokens)."""
        return max(1, self.estimated_tokens // 200)


class WeightedAgent:
    """
    Total capacity = 100 units.
    A small request (200 tokens) costs 1 unit.
    A large request (2000 tokens) costs 10 units.
    Max concurrent = 100 small OR 10 large OR any mix summing to ≤100.
    """

    def __init__(self, capacity: int = 100):
        self.client = AsyncAnthropic()
        self._wsem = WeightedSemaphore(capacity)

    async def chat(self, req: WeightedRequest) -> str:
        async with self._wsem:
            self._wsem._current_weight = req.weight
            await self._wsem.acquire(req.weight)
            try:
                response = await self.client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=min(req.estimated_tokens, 4096),
                    messages=[{"role": "user", "content": req.message}],
                )
                return response.content[0].text
            finally:
                async with self._wsem._condition:
                    self._wsem._available = min(
                        self._wsem._capacity,
                        self._wsem._available + req.weight
                    )
                    self._wsem._condition.notify_all()

    async def chat_simple(self, message: str, estimated_tokens: int = 500) -> str:
        req = WeightedRequest(message=message, estimated_tokens=estimated_tokens)
        weight = req.weight
        await self._wsem.acquire(weight)
        try:
            response = await self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=estimated_tokens,
                messages=[{"role": "user", "content": message}],
            )
            return response.content[0].text
        finally:
            async with self._wsem._condition:
                self._wsem._available = min(
                    self._wsem._capacity,
                    self._wsem._available + weight,
                )
                self._wsem._condition.notify_all()


async def demo_weighted():
    agent = WeightedAgent(capacity=20)
    # Mix of small and large requests
    requests = [
        ("What is 2+2?", 100),           # weight=1
        ("Write a detailed 1000-word essay on climate change.", 2000),  # weight=10
        ("Say hello.", 50),              # weight=1
        ("Explain machine learning in detail.", 1500),  # weight=7
        ("What day is it?", 100),        # weight=1
    ]
    tasks = [agent.chat_simple(msg, tokens) for msg, tokens in requests]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for (msg, _), result in zip(requests, results):
        status = "OK" if not isinstance(result, Exception) else f"ERR:{result}"
        print(f"  {msg[:40]}: {status}")
```

## Comparison Table

| Solution | Concurrency Model | Priority Support | Adaptive | Timeout Support | Best For |
|---|---|---|---|---|---|
| Basic Semaphore | Fixed global cap | No | No | No | Simple single-resource throttling |
| Tiered Semaphore | Per-priority caps | Yes (3 tiers) | No | No | Mixed-priority workloads |
| Resource Registry | Per-resource caps | No | No | No | Multi-resource agents |
| Adaptive Semaphore | Error-rate driven | No | Yes | No | Unstable upstream APIs |
| Tracked Semaphore | Fixed + queue visibility | No | No | Yes | SLA-sensitive production agents |
| Weighted Semaphore | Capacity-weighted | No | No | No | Variable-cost LLM request sizing |

**Recommended**: Use **Basic Semaphore** (Solution 1) as the default starting point — it prevents most rate-limit cascades with three lines of code. Add **Resource Registry** (Solution 3) when agents touch multiple external resources. Use **Adaptive Semaphore** (Solution 4) when upstream APIs are unstable and you can't predict safe concurrency limits statically.
