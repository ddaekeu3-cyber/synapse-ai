---
layout: solution
title: "Agent Doesn't Use Semaphores to Limit Shared Resource Access"
category: concurrency
description: "Agents that launch many async tasks without semaphores overwhelm shared resources — database connections, external APIs, file handles — causing cascading failures."
tags: [concurrency, semaphore, asyncio, resource-limits, throttle, python]
---

# Agent Doesn't Use Semaphores to Limit Shared Resource Access

When an agent spawns many concurrent tasks, each task may independently acquire connections to a database, open file handles, or call an external API. Without a semaphore, the number of simultaneous accesses is unbounded. At scale this exhausts connection pools, triggers 429s from external APIs, or causes OOM from too many open handles — often silently.

## Why This Happens

`asyncio.gather()` makes parallelism trivially easy. Developers write `await asyncio.gather(*tasks)` without considering that 100 tasks might simultaneously hit a rate-limited API or a database with a 10-connection pool.

---

## Option 1: Basic asyncio.Semaphore for Concurrent API Calls

Limit the number of simultaneous Claude API calls using a semaphore.

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

# Max concurrent calls to Claude API
MAX_CONCURRENT_LLM_CALLS = 5
_semaphore = asyncio.Semaphore(MAX_CONCURRENT_LLM_CALLS)


async def call_llm(prompt: str, model: str = "claude-haiku-4-5-20251001") -> str:
    """Rate-limited Claude API call using semaphore."""
    async with _semaphore:
        response = await client.messages.create(
            model=model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text


async def process_batch(prompts: list[str]) -> list[str]:
    """Process many prompts with bounded concurrency."""
    tasks = [call_llm(p) for p in prompts]
    return await asyncio.gather(*tasks)


if __name__ == "__main__":
    import time

    prompts = [f"Summarize point #{i}: Lorem ipsum..." for i in range(20)]

    start = time.monotonic()
    results = asyncio.run(process_batch(prompts))
    elapsed = time.monotonic() - start

    print(f"Processed {len(results)} prompts in {elapsed:.1f}s")
    print(f"Max concurrent: {MAX_CONCURRENT_LLM_CALLS}")
    print(f"Sample: {results[0][:80]}")
```

**Expected Token Savings:** Prevents 429 rate limit errors that waste requests; reduces failed retries.

**Environment:** Any async agent making parallel Claude API calls; essential for batch processing.

---

## Option 2: Per-Resource Semaphore Registry

Maintain separate semaphores for different resource types (LLM API, database, filesystem) with configurable limits.

```python
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import anthropic

client = anthropic.AsyncAnthropic()


@dataclass
class ResourceLimits:
    llm_calls: int = 5
    db_connections: int = 10
    file_handles: int = 20
    external_api_calls: int = 3


class SemaphoreRegistry:
    def __init__(self, limits: ResourceLimits):
        self._semaphores: dict[str, asyncio.Semaphore] = {
            "llm": asyncio.Semaphore(limits.llm_calls),
            "db": asyncio.Semaphore(limits.db_connections),
            "fs": asyncio.Semaphore(limits.file_handles),
            "external_api": asyncio.Semaphore(limits.external_api_calls),
        }
        self._usage: dict[str, int] = {k: 0 for k in self._semaphores}

    @asynccontextmanager
    async def acquire(self, resource: str):
        sem = self._semaphores.get(resource)
        if sem is None:
            raise ValueError(f"Unknown resource: {resource}")
        async with sem:
            self._usage[resource] = self._usage[resource] + 1
            try:
                yield
            finally:
                self._usage[resource] = self._usage[resource] - 1

    def status(self) -> dict:
        return {
            name: {
                "active": self._usage[name],
                "limit": sem._value + self._usage[name],
            }
            for name, sem in self._semaphores.items()
        }


# Global registry
registry = SemaphoreRegistry(ResourceLimits(llm_calls=5, db_connections=10))


async def call_llm_guarded(prompt: str) -> str:
    async with registry.acquire("llm"):
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text


async def read_db_record(record_id: str) -> dict:
    async with registry.acquire("db"):
        await asyncio.sleep(0.05)  # simulate DB query
        return {"id": record_id, "data": "..."}


async def pipeline(items: list[str]) -> list[str]:
    async def process_one(item: str) -> str:
        record = await read_db_record(item)
        summary = await call_llm_guarded(f"Summarize: {record}")
        return summary

    results = await asyncio.gather(*[process_one(i) for i in items])
    print("Resource status:", registry.status())
    return results


if __name__ == "__main__":
    items = [f"item-{i}" for i in range(15)]
    asyncio.run(pipeline(items))
```

**Expected Token Savings:** Coordinated limits across resource types; prevents DB pool exhaustion AND LLM rate limits simultaneously.

**Environment:** Pipelines mixing LLM calls with database and external API access.

---

## Option 3: Semaphore with Timeout and Fallback

Acquire a semaphore with a timeout; if unavailable, use a cached or degraded response instead of waiting indefinitely.

```python
import asyncio
import time
from typing import Any
import anthropic

client = anthropic.AsyncAnthropic()

SEMAPHORE = asyncio.Semaphore(3)
ACQUIRE_TIMEOUT = 2.0  # seconds to wait for semaphore

# Simple in-memory response cache
_cache: dict[str, tuple[str, float]] = {}
CACHE_TTL = 300.0


def get_cached(key: str) -> str | None:
    if key in _cache:
        value, ts = _cache[key]
        if time.time() - ts < CACHE_TTL:
            return value
    return None


def set_cached(key: str, value: str):
    _cache[key] = (value, time.time())


async def call_llm_with_fallback(prompt: str) -> tuple[str, str]:
    """
    Returns (result, source) where source is 'llm', 'cache', or 'degraded'.
    """
    cache_key = prompt[:100]
    cached = get_cached(cache_key)

    try:
        # Try to acquire semaphore within timeout
        await asyncio.wait_for(SEMAPHORE.acquire(), timeout=ACQUIRE_TIMEOUT)
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            result = response.content[0].text
            set_cached(cache_key, result)
            return result, "llm"
        finally:
            SEMAPHORE.release()

    except asyncio.TimeoutError:
        # Semaphore not available within timeout
        if cached:
            return cached, "cache"
        return "Service busy — please retry shortly.", "degraded"


async def demo():
    prompts = [f"What is {i} * {i}?" for i in range(10)]

    results = await asyncio.gather(*[
        call_llm_with_fallback(p) for p in prompts
    ])

    for p, (result, source) in zip(prompts, results):
        print(f"[{source:8s}] {p[:30]} -> {result[:50]}")


if __name__ == "__main__":
    asyncio.run(demo())
```

**Expected Token Savings:** Cache hits avoid LLM calls entirely; degraded fallback prevents cascading wait storms under load.

**Environment:** Production agents with strict SLA requirements; high-concurrency endpoints.

---

## Option 4: Weighted Semaphore for Priority Tasks

Use separate semaphore pools for high-priority and low-priority tasks so interactive users are never blocked by batch jobs.

```python
import asyncio
import anthropic
from dataclasses import dataclass
from enum import Enum

client = anthropic.AsyncAnthropic()


class Priority(Enum):
    HIGH = "high"    # interactive user requests
    LOW = "low"      # background batch jobs


@dataclass
class TieredSemaphores:
    """
    High-priority tasks get MAX_TOTAL slots.
    Low-priority tasks are limited to BATCH_LIMIT slots,
    leaving headroom for interactive traffic.
    """
    max_total: int = 10
    batch_limit: int = 3

    def __post_init__(self):
        # Total concurrency cap (shared by all tasks)
        self._total = asyncio.Semaphore(self.max_total)
        # Additional cap for batch/low-priority tasks
        self._batch = asyncio.Semaphore(self.batch_limit)

    async def acquire(self, priority: Priority):
        if priority == Priority.LOW:
            # Batch tasks must acquire both semaphores
            await self._batch.acquire()
            await self._total.acquire()
        else:
            # High-priority only needs the total cap
            await self._total.acquire()

    def release(self, priority: Priority):
        self._total.release()
        if priority == Priority.LOW:
            self._batch.release()

    def active_high(self) -> int:
        return self.max_total - self._total._value - (self.batch_limit - self._batch._value)

    def active_low(self) -> int:
        return self.batch_limit - self._batch._value


tiers = TieredSemaphores(max_total=10, batch_limit=3)


async def call_llm_tiered(prompt: str, priority: Priority) -> str:
    await tiers.acquire(priority)
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
    finally:
        tiers.release(priority)


async def demo():
    # Mix of interactive (high) and batch (low) requests
    high_tasks = [
        call_llm_tiered(f"Interactive query {i}", Priority.HIGH)
        for i in range(5)
    ]
    low_tasks = [
        call_llm_tiered(f"Batch job {i}", Priority.LOW)
        for i in range(10)
    ]

    results = await asyncio.gather(*high_tasks, *low_tasks)
    print(f"Completed {len(results)} tasks")
    print(f"Active high: {tiers.active_high()}, active low: {tiers.active_low()}")


if __name__ == "__main__":
    asyncio.run(demo())
```

**Expected Token Savings:** Interactive users always get slots; batch jobs don't starve the main user experience.

**Environment:** Agents serving both interactive and batch workloads simultaneously.

---

## Option 5: Context-Variable Scoped Semaphore per Request

Use `contextvars` to give each request its own per-user semaphore, preventing one user from monopolizing shared resources.

```python
import asyncio
import contextvars
from collections import defaultdict
import anthropic
from fastapi import FastAPI, Request

client = anthropic.AsyncAnthropic()
app = FastAPI()

# Per-user semaphores: user_id -> Semaphore
_user_semaphores: dict[str, asyncio.Semaphore] = defaultdict(
    lambda: asyncio.Semaphore(2)  # 2 concurrent calls per user
)

# Global semaphore (overall system limit)
_global_semaphore = asyncio.Semaphore(20)

_current_user: contextvars.ContextVar[str] = contextvars.ContextVar("current_user")


async def call_llm_per_user(prompt: str) -> str:
    """Rate-limit per user AND globally."""
    user_id = _current_user.get("anonymous")
    user_sem = _user_semaphores[user_id]

    async with user_sem:
        async with _global_semaphore:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text


@app.post("/agent/run")
async def run_agent(request: Request, prompt: str, user_id: str = "anon"):
    token = _current_user.set(user_id)
    try:
        result = await call_llm_per_user(prompt)
        return {"result": result, "user": user_id}
    except Exception as e:
        return {"error": str(e), "user": user_id}
    finally:
        _current_user.reset(token)


@app.get("/semaphore-status")
def semaphore_status():
    return {
        "global_available": _global_semaphore._value,
        "per_user": {
            uid: sem._value
            for uid, sem in list(_user_semaphores.items())[:10]
        },
    }
```

**Expected Token Savings:** Prevents single-user storms from consuming all API quota; fair resource sharing across tenants.

**Environment:** Multi-tenant FastAPI agents; SaaS platforms with per-user rate limiting.

---

## Option 6: Semaphore Pool with Monitoring and Alerting

Track semaphore wait times and alert when contention becomes a problem.

```python
import asyncio
import time
import statistics
from contextlib import asynccontextmanager
from collections import deque
import anthropic

client = anthropic.AsyncAnthropic()


class MonitoredSemaphore:
    """asyncio.Semaphore with wait-time tracking and alerting."""

    def __init__(
        self,
        value: int,
        name: str = "semaphore",
        alert_threshold_ms: float = 500.0,
        history_size: int = 100,
    ):
        self._sem = asyncio.Semaphore(value)
        self.name = name
        self._alert_threshold = alert_threshold_ms
        self._wait_times: deque[float] = deque(maxlen=history_size)
        self._total_acquired = 0
        self._total_waited = 0

    @asynccontextmanager
    async def acquire(self):
        start = time.monotonic()
        async with self._sem:
            wait_ms = (time.monotonic() - start) * 1000
            self._wait_times.append(wait_ms)
            self._total_acquired += 1

            if wait_ms > self._alert_threshold:
                self._total_waited += 1
                print(
                    f"[ALERT] {self.name}: high contention — "
                    f"waited {wait_ms:.0f}ms (threshold: {self._alert_threshold:.0f}ms) | "
                    f"contention rate: {self.contention_rate:.1%}"
                )
            yield

    @property
    def contention_rate(self) -> float:
        if self._total_acquired == 0:
            return 0.0
        return self._total_waited / self._total_acquired

    @property
    def p50_wait_ms(self) -> float:
        if not self._wait_times:
            return 0.0
        return statistics.median(self._wait_times)

    @property
    def p95_wait_ms(self) -> float:
        if len(self._wait_times) < 2:
            return 0.0
        sorted_times = sorted(self._wait_times)
        idx = int(len(sorted_times) * 0.95)
        return sorted_times[idx]

    def stats(self) -> dict:
        return {
            "name": self.name,
            "total_acquired": self._total_acquired,
            "contention_rate": f"{self.contention_rate:.1%}",
            "p50_wait_ms": round(self.p50_wait_ms, 1),
            "p95_wait_ms": round(self.p95_wait_ms, 1),
        }


llm_semaphore = MonitoredSemaphore(
    value=5,
    name="llm_api",
    alert_threshold_ms=300.0,
)


async def call_llm(prompt: str) -> str:
    async with llm_semaphore.acquire():
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text


async def demo():
    prompts = [f"Count to {i}" for i in range(20)]
    await asyncio.gather(*[call_llm(p) for p in prompts])
    print("\nSemaphore stats:", llm_semaphore.stats())


if __name__ == "__main__":
    asyncio.run(demo())
```

**Expected Token Savings:** High contention alert fires before rate limits do; gives you time to add capacity or reduce concurrency.

**Environment:** Production agents; pairs with metrics systems (Prometheus, Datadog).

---

## Comparison

| Option | Scope | Priority Support | Monitoring | Fallback |
|--------|-------|-----------------|------------|----------|
| 1. Basic semaphore | Global | No | No | No |
| 2. Per-resource registry | Per resource type | No | Status check | No |
| 3. Timeout + fallback | Global | No | No | Cache/degraded |
| 4. Weighted/tiered | High vs low priority | Yes | Basic | No |
| 5. Per-user scoped | Per user + global | No | No | No |
| 6. Monitored semaphore | Global | No | P50/P95 alerts | No |
