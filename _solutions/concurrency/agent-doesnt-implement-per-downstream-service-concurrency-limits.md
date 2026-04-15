---
layout: solution
title: "Agent Doesn't Implement Per-Downstream-Service Concurrency Limits"
category: concurrency
description: "The agent applies a single global semaphore to all tool calls, allowing one slow downstream service to consume all capacity and starve fast services — or conversely, hammering a fragile service with too many concurrent requests."
tags: [concurrency, semaphore, tool-failure, rate-limiting, production]
---

## Symptom

The agent runs 20 parallel tool calls under a single semaphore of size 10. A database that can only safely handle 3 concurrent connections gets 10 simultaneous queries and starts timing out. Meanwhile, a fast in-memory cache that could handle 50 concurrent reads gets throttled to 10. Alternatively, without any semaphore, a fragile third-party API gets flooded and responds with 429 errors that cascade through the agent.

## Root Cause

Different downstream services have fundamentally different concurrency tolerances: a PostgreSQL database may support 5–20 safe concurrent queries, an Elasticsearch cluster 50, a third-party REST API 10, and a local Redis 200+. A single global semaphore treats all services identically, either over-constraining fast services or under-constraining fragile ones. Per-service semaphores let each service run at exactly its capacity.

## Fix

### Option 1 — Per-service semaphore registry

```python
import asyncio
import anthropic
import time

client = anthropic.AsyncAnthropic()

# Each service gets its own concurrency limit
SERVICE_LIMITS = {
    "postgres":      asyncio.Semaphore(5),
    "elasticsearch": asyncio.Semaphore(20),
    "third_party_api": asyncio.Semaphore(8),
    "redis":         asyncio.Semaphore(50),
    "anthropic_api": asyncio.Semaphore(10),
}

async def call_service(service: str, operation: str) -> str:
    sem = SERVICE_LIMITS.get(service, asyncio.Semaphore(5))  # default conservative
    async with sem:
        print(f"[{service}] running: {operation}")
        await asyncio.sleep(0.05)  # simulate service latency
        return f"{service}: {operation} result"

async def run_agent_tools(tasks: list[tuple[str, str]]) -> list[str]:
    """Run tool calls concurrently, each respecting its service's limit."""
    return await asyncio.gather(*[call_service(svc, op) for svc, op in tasks])

async def main():
    tool_calls = [
        ("postgres",        "SELECT users"),
        ("postgres",        "SELECT orders"),
        ("postgres",        "SELECT products"),
        ("elasticsearch",   "search logs"),
        ("elasticsearch",   "search events"),
        ("third_party_api", "fetch weather"),
        ("redis",           "get session:abc"),
        ("redis",           "get session:def"),
        ("anthropic_api",   "embed text 1"),
        ("anthropic_api",   "embed text 2"),
    ]
    start = time.monotonic()
    results = await run_agent_tools(tool_calls)
    print(f"\n[done] {len(results)} calls in {time.monotonic()-start:.2f}s")

asyncio.run(main())
```

**Expected Token Savings:** Correct concurrency limits prevent cascading failures that would otherwise require expensive retry loops; fewer 429/timeout errors means fewer wasted API calls.
**Environment:** Multi-tool agents calling heterogeneous backends; any agent using more than one downstream service.

---

### Option 2 — Service client class with built-in concurrency control

```python
import asyncio
import anthropic
import time
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class ServiceClient:
    name:       str
    max_concurrent: int
    _sem:       asyncio.Semaphore = field(init=False)
    _active:    int = field(default=0, init=False)
    _total:     int = field(default=0, init=False)

    def __post_init__(self):
        self._sem = asyncio.Semaphore(self.max_concurrent)

    async def call(self, operation: str, latency: float = 0.05) -> str:
        async with self._sem:
            self._active += 1
            self._total  += 1
            peak = self._active
            try:
                await asyncio.sleep(latency)
                return f"{self.name}:{operation}"
            finally:
                self._active -= 1

    def stats(self) -> str:
        return f"{self.name}: limit={self.max_concurrent}, total={self._total}"

# Instantiate once at startup
db        = ServiceClient("postgres",      max_concurrent=5)
search    = ServiceClient("elasticsearch", max_concurrent=20)
ext_api   = ServiceClient("external_api",  max_concurrent=8)
cache     = ServiceClient("redis",         max_concurrent=50)

async def process_user_request(user_id: str) -> dict:
    # All these run concurrently, each capped by its own service limit
    user_data, search_results, ext_data, cached = await asyncio.gather(
        db.call(f"SELECT * FROM users WHERE id={user_id}"),
        search.call(f"search events user={user_id}"),
        ext_api.call(f"GET /profile/{user_id}"),
        cache.call(f"GET session:{user_id}"),
    )
    return {"user": user_data, "search": search_results, "ext": ext_data, "cache": cached}

async def main():
    users = [f"user_{i}" for i in range(15)]
    start = time.monotonic()
    results = await asyncio.gather(*[process_user_request(u) for u in users])
    elapsed = time.monotonic() - start
    print(f"[done] {len(results)} requests in {elapsed:.2f}s")
    for svc in [db, search, ext_api, cache]:
        print(" ", svc.stats())

asyncio.run(main())
```

**Expected Token Savings:** Encapsulated service clients make limits visible in code; `stats()` surfaces actual usage for capacity planning without external tooling.
**Environment:** Structured agents with a defined set of downstream services; service-oriented architectures where each client is a long-lived singleton.

---

### Option 3 — Dynamic limit loader from config with hot reload

```python
import asyncio
import json
import os
import time
import anthropic

client = anthropic.AsyncAnthropic()

DEFAULT_LIMITS = {
    "postgres":        5,
    "redis":          50,
    "external_api":    8,
    "anthropic_embed": 15,
}

class ServicePool:
    def __init__(self, config_path: str | None = None):
        self._config_path = config_path
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._limits: dict[str, int] = {}
        self._load_limits()

    def _load_limits(self) -> None:
        limits = dict(DEFAULT_LIMITS)
        if self._config_path and os.path.exists(self._config_path):
            try:
                with open(self._config_path) as f:
                    overrides = json.load(f)
                limits.update(overrides)
                print(f"[pool] loaded limits from {self._config_path}")
            except Exception as exc:
                print(f"[pool] config load failed: {exc} — using defaults")
        self._limits = limits
        # Rebuild semaphores
        self._sems = {name: asyncio.Semaphore(limit) for name, limit in limits.items()}

    def reload(self) -> None:
        self._load_limits()
        print(f"[pool] reloaded: {self._limits}")

    async def call(self, service: str, operation: str, latency: float = 0.05) -> str:
        sem = self._sems.get(service, asyncio.Semaphore(3))  # conservative default
        async with sem:
            await asyncio.sleep(latency)
            return f"{service}:{operation}"

    def report(self) -> None:
        for name, limit in self._limits.items():
            print(f"  {name}: max_concurrent={limit}")

pool = ServicePool()

async def agent_task(task_id: int) -> list[str]:
    return await asyncio.gather(
        pool.call("postgres",        f"query_{task_id}"),
        pool.call("redis",           f"get_{task_id}"),
        pool.call("external_api",    f"fetch_{task_id}"),
        pool.call("anthropic_embed", f"embed_{task_id}"),
    )

async def main():
    pool.report()
    tasks = [agent_task(i) for i in range(10)]
    results = await asyncio.gather(*tasks)
    print(f"[done] {sum(len(r) for r in results)} operations completed")

asyncio.run(main())
```

**Expected Token Savings:** Config-driven limits let ops teams adjust concurrency without code deploys; reducing a limit during an incident prevents cascading failures while the root cause is investigated.
**Environment:** Production agents with ops team oversight; services where concurrency limits need to be tuned based on observed load patterns.

---

### Option 4 — Token bucket per service (rate + concurrency combined)

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()

class TokenBucketService:
    """Rate limiter (requests/sec) + concurrency limiter combined."""

    def __init__(self, name: str, rps: float, max_concurrent: int):
        self.name          = name
        self._rps          = rps
        self._tokens       = rps
        self._max_tokens   = rps
        self._last_refill  = time.monotonic()
        self._sem          = asyncio.Semaphore(max_concurrent)
        self._rate_lock    = asyncio.Lock()

    async def _acquire_token(self) -> None:
        """Block until a rate-limit token is available."""
        while True:
            async with self._rate_lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(self._max_tokens, self._tokens + elapsed * self._rps)
                self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
            await asyncio.sleep(1.0 / self._rps)

    async def call(self, operation: str) -> str:
        await self._acquire_token()          # rate limit
        async with self._sem:               # concurrency limit
            await asyncio.sleep(0.05)       # simulate service call
            return f"{self.name}:{operation}"

# Services with different rate and concurrency profiles
postgres   = TokenBucketService("postgres",      rps=10,  max_concurrent=5)
ext_api    = TokenBucketService("external_api",  rps=5,   max_concurrent=3)
redis      = TokenBucketService("redis",         rps=100, max_concurrent=20)

async def run_batch(n: int) -> None:
    tasks = []
    for i in range(n):
        tasks.append(postgres.call(f"q{i}"))
        tasks.append(ext_api.call(f"f{i}"))
        tasks.append(redis.call(f"g{i}"))
    start = time.monotonic()
    results = await asyncio.gather(*tasks)
    print(f"[done] {len(results)} ops in {time.monotonic()-start:.2f}s")

asyncio.run(run_batch(5))
```

**Expected Token Savings:** Combined rate + concurrency limiting prevents both burst overload and sustained throughput violations; fewer 429s and timeouts means fewer retry token expenditures.
**Environment:** Agents calling third-party APIs with both rate limits (requests/sec) and concurrency limits (simultaneous connections).

---

### Option 5 — Priority-aware per-service queue

```python
import asyncio
import heapq
import time
import anthropic
from dataclasses import dataclass, field
from typing import Any

client = anthropic.AsyncAnthropic()

@dataclass(order=True)
class PrioritizedTask:
    priority:   int          # lower = higher priority
    task_id:    int
    operation:  str          = field(compare=False)
    future:     asyncio.Future = field(compare=False, default=None)

class PriorityServiceQueue:
    def __init__(self, name: str, max_concurrent: int):
        self.name    = name
        self._sem    = asyncio.Semaphore(max_concurrent)
        self._heap:  list = []
        self._counter = 0

    async def call(self, operation: str, priority: int = 5) -> str:
        loop   = asyncio.get_event_loop()
        future = loop.create_future()
        task   = PrioritizedTask(priority, self._counter, operation, future)
        self._counter += 1
        heapq.heappush(self._heap, task)
        await self._process()
        return await future

    async def _process(self) -> None:
        if not self._heap:
            return
        # Only process if a slot is available
        if self._sem._value == 0:
            return
        task = heapq.heappop(self._heap)
        asyncio.create_task(self._execute(task))

    async def _execute(self, task: PrioritizedTask) -> None:
        async with self._sem:
            await asyncio.sleep(0.03)
            result = f"{self.name}[p{task.priority}]:{task.operation}"
            task.future.set_result(result)

db = PriorityServiceQueue("postgres", max_concurrent=4)

async def main():
    # Mix of high-priority user-facing queries and low-priority background jobs
    results = await asyncio.gather(
        db.call("user_login",    priority=1),   # P1 critical
        db.call("analytics",     priority=9),   # P9 background
        db.call("user_profile",  priority=2),   # P2 important
        db.call("report_daily",  priority=8),   # P8 background
        db.call("session_check", priority=1),   # P1 critical
    )
    for r in results:
        print(r)

asyncio.run(main())
```

**Expected Token Savings:** Priority queuing ensures critical agent tasks complete first; background analytics don't consume concurrency slots that delay user-facing requests during peak load.
**Environment:** Agents serving both interactive users and background batch jobs; multi-tenant systems where user-facing requests must be prioritised over housekeeping tasks.

---

### Option 6 — Adaptive limits: auto-adjust concurrency based on error rate

```python
import asyncio
import time
import random
import anthropic

client = anthropic.AsyncAnthropic()

class AdaptiveServiceLimit:
    """Automatically reduces concurrency on errors and recovers gradually."""

    def __init__(self, name: str, initial: int, min_limit: int = 1, max_limit: int = None):
        self.name       = name
        self._limit     = initial
        self._min       = min_limit
        self._max       = max_limit or initial * 3
        self._sem       = asyncio.Semaphore(initial)
        self._errors    = 0
        self._successes = 0
        self._lock      = asyncio.Lock()
        self._last_probe = time.monotonic()

    async def _adjust(self, success: bool) -> None:
        async with self._lock:
            if success:
                self._successes += 1
                # Recover: increase limit every 10 consecutive successes
                if self._successes >= 10 and self._limit < self._max:
                    self._limit     = min(self._max, self._limit + 1)
                    self._sem       = asyncio.Semaphore(self._limit)
                    self._successes = 0
                    print(f"[adaptive:{self.name}] ↑ limit={self._limit}")
            else:
                self._errors    += 1
                self._successes  = 0
                # Reduce: halve on 3 consecutive errors
                if self._errors >= 3 and self._limit > self._min:
                    self._limit  = max(self._min, self._limit // 2)
                    self._sem    = asyncio.Semaphore(self._limit)
                    self._errors = 0
                    print(f"[adaptive:{self.name}] ↓ limit={self._limit}")

    async def call(self, operation: str) -> str:
        async with self._sem:
            await asyncio.sleep(0.02)
            # Simulate intermittent failures
            if random.random() < 0.15:
                await self._adjust(success=False)
                raise ConnectionError(f"{self.name} overloaded")
            await self._adjust(success=True)
            return f"{self.name}:{operation}"

db = AdaptiveServiceLimit("postgres", initial=8, min_limit=2, max_limit=16)

async def main():
    errors = 0
    for batch in range(5):
        tasks = [db.call(f"op_{batch}_{i}") for i in range(10)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        batch_errors = sum(1 for r in results if isinstance(r, Exception))
        errors += batch_errors
        print(f"batch {batch}: {len(results)-batch_errors} ok, {batch_errors} errors, limit={db._limit}")
    print(f"\n[done] total errors: {errors}")

asyncio.run(main())
```

**Expected Token Savings:** Adaptive limits detect real-time service degradation and self-tune; fewer cascading failures during incidents means fewer retry-storm token expenditures.
**Environment:** Agents in dynamic environments where downstream service capacity varies (e.g., database under load); self-healing systems that must operate without manual operator intervention.

---

## Comparison

| Option | Limit Type | Dynamic | Per-Service | Priority | Async Safe | Best For |
|---|---|---|---|---|---|---|
| 1. Semaphore registry | Concurrency | No | Yes | No | Yes | Baseline per-service isolation |
| 2. Service client class | Concurrency | No | Yes | No | Yes | Structured agents with DI pattern |
| 3. Config-driven hot reload | Concurrency | Yes (manual) | Yes | No | Yes | Ops-managed production agents |
| 4. Token bucket | Rate + Concurrency | No | Yes | No | Yes | Third-party APIs with both limits |
| 5. Priority queue | Concurrency + Priority | No | Yes | Yes | Yes | Mixed user-facing + batch workloads |
| 6. Adaptive | Concurrency | Yes (auto) | Yes | No | Yes | Self-healing agents; dynamic environments |
