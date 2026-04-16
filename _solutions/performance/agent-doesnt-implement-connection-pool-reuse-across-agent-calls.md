---
title: "Agent Doesn't Implement Connection Pool Reuse Across Agent Calls"
description: "Creating a new HTTP client or API client on every agent call wastes time on TLS handshakes, TCP setup, and connection negotiation. Connection pool reuse keeps connections alive across calls, dramatically reducing per-request overhead for high-throughput agent deployments."
difficulty: intermediate
category: performance
tags: [performance, connection-pool, http, reuse, latency, throughput, httpx]
---

## Problem

Agents that instantiate `AsyncAnthropic()` inside every function call, or create a new `httpx.AsyncClient()` per request, pay the full cost of TLS negotiation, TCP handshake, and HTTP/2 stream setup on every invocation. At 100+ requests/minute, this overhead dominates. Connection pooling keeps underlying TCP connections alive across calls, reducing latency by 50-200ms per request.

```python
# BAD: new client per call — full TLS handshake every time
async def handle_request(prompt: str) -> str:
    client = AsyncAnthropic()  # new connection every call
    result = await client.messages.create(...)
    return result
# At 100 req/min: ~20 seconds/min wasted on handshakes
```

## Solution 1: Module-Level Singleton Client

Share one client instance across all calls within a process.

```python
import asyncio
import atexit
from anthropic import AsyncAnthropic

# Single shared client — connection pool maintained across all calls
_shared_client: AsyncAnthropic | None = None

def get_client() -> AsyncAnthropic:
    global _shared_client
    if _shared_client is None:
        _shared_client = AsyncAnthropic(
            # httpx connection pool defaults: max_connections=100, max_keepalive=20
            http_client=None  # uses default httpx settings
        )
    return _shared_client

async def close_client():
    global _shared_client
    if _shared_client is not None:
        await _shared_client.close()
        _shared_client = None

# Register cleanup on process exit
def _sync_close():
    import asyncio as _asyncio
    try:
        loop = _asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(close_client())
        else:
            loop.run_until_complete(close_client())
    except Exception:
        pass

atexit.register(_sync_close)

async def complete(prompt: str, model: str = "claude-haiku-4-5-20251001") -> str:
    """All calls share the same underlying connection pool."""
    client = get_client()
    response = await client.messages.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text if response.content else ""

async def main():
    import time
    # All these calls reuse connections from the pool
    prompts = [f"What is {topic}?" for topic in ["Python", "asyncio", "HTTP/2", "TLS", "connection pools"]]

    start = time.time()
    results = await asyncio.gather(*[complete(p) for p in prompts])
    elapsed = time.time() - start

    print(f"5 concurrent calls in {elapsed:.2f}s (reusing connection pool)")
    for p, r in zip(prompts, results):
        print(f"  {p[:30]}: {r[:60]}...")

asyncio.run(main())
```

## Solution 2: Explicit httpx Pool Configuration

Configure the connection pool size, keepalive, and timeouts explicitly for your workload.

```python
import asyncio
import httpx
from anthropic import AsyncAnthropic

def create_pooled_client(
    max_connections: int = 50,
    max_keepalive_connections: int = 20,
    keepalive_expiry: float = 30.0,
    connect_timeout: float = 5.0,
    read_timeout: float = 60.0,
) -> AsyncAnthropic:
    """
    Create an Anthropic client with a tuned connection pool.

    max_connections: max total connections (concurrent requests limit)
    max_keepalive_connections: idle connections kept alive for reuse
    keepalive_expiry: seconds before idle connection is closed
    """
    limits = httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=max_keepalive_connections,
        keepalive_expiry=keepalive_expiry,
    )
    timeout = httpx.Timeout(
        connect=connect_timeout,
        read=read_timeout,
        write=10.0,
        pool=5.0,  # time to wait for a connection from the pool
    )
    http_client = httpx.AsyncClient(limits=limits, timeout=timeout)
    return AsyncAnthropic(http_client=http_client)

# Pool configurations for different workload types
POOL_CONFIGS = {
    "low_latency":     dict(max_connections=20, max_keepalive_connections=10, keepalive_expiry=60.0),
    "high_throughput": dict(max_connections=100, max_keepalive_connections=50, keepalive_expiry=30.0),
    "batch":           dict(max_connections=200, max_keepalive_connections=100, keepalive_expiry=120.0),
    "conservative":    dict(max_connections=10, max_keepalive_connections=5, keepalive_expiry=15.0),
}

class PooledClientManager:
    def __init__(self, profile: str = "high_throughput"):
        config = POOL_CONFIGS.get(profile, POOL_CONFIGS["high_throughput"])
        self._client = create_pooled_client(**config)
        self._profile = profile
        print(f"[Pool] Created client with profile: {profile}, config: {config}")

    async def complete(self, prompt: str, model: str = "claude-haiku-4-5-20251001") -> str:
        response = await self._client.messages.create(
            model=model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text if response.content else ""

    async def close(self):
        await self._client.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

async def main():
    async with PooledClientManager(profile="high_throughput") as mgr:
        tasks = [
            mgr.complete("What is HTTP connection pooling?"),
            mgr.complete("Explain TLS handshake overhead."),
            mgr.complete("What is keepalive in HTTP?"),
        ]
        results = await asyncio.gather(*tasks)
        for r in results:
            print(r[:120])

asyncio.run(main())
```

## Solution 3: Per-Concurrency-Level Pool Sizing

Automatically size the connection pool based on measured concurrency.

```python
import asyncio
import time
from collections import deque
from anthropic import AsyncAnthropic
import httpx

class AdaptivePoolClient:
    """Connection pool that sizes itself based on observed concurrency."""

    def __init__(self, initial_size: int = 10, max_size: int = 100):
        self._max_size = max_size
        self._current_size = initial_size
        self._active_requests = 0
        self._peak_concurrent: deque[int] = deque(maxlen=100)
        self._client: AsyncAnthropic | None = None
        self._lock = asyncio.Lock()
        self._create_client(initial_size)

    def _create_client(self, pool_size: int):
        limits = httpx.Limits(
            max_connections=pool_size,
            max_keepalive_connections=pool_size // 2,
            keepalive_expiry=30.0,
        )
        http_client = httpx.AsyncClient(limits=limits)
        self._client = AsyncAnthropic(http_client=http_client)
        self._current_size = pool_size

    async def _maybe_resize(self):
        if not self._peak_concurrent:
            return
        peak = max(self._peak_concurrent)
        recommended = min(int(peak * 1.5) + 5, self._max_size)

        if recommended > self._current_size * 1.5 or recommended < self._current_size * 0.5:
            async with self._lock:
                if self._client:
                    await self._client.close()
                self._create_client(recommended)
                print(f"[Pool] Resized: {self._current_size} connections (peak={peak})")

    async def complete(self, prompt: str) -> str:
        self._active_requests += 1
        self._peak_concurrent.append(self._active_requests)
        try:
            assert self._client is not None
            response = await self._client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}]
            )
            return response.content[0].text if response.content else ""
        finally:
            self._active_requests -= 1
            if len(self._peak_concurrent) % 20 == 0:
                await self._maybe_resize()

    async def close(self):
        if self._client:
            await self._client.close()

async def main():
    pool = AdaptivePoolClient(initial_size=5, max_size=50)
    try:
        # Simulate varying concurrency
        prompts = [f"Tell me about topic {i}" for i in range(15)]
        results = await asyncio.gather(*[pool.complete(p) for p in prompts])
        print(f"Completed {len(results)} requests")
        print(f"Pool size: {pool._current_size}, Peak: {max(pool._peak_concurrent) if pool._peak_concurrent else 0}")
    finally:
        await pool.close()

asyncio.run(main())
```

## Solution 4: Connection Pool with Circuit Breaker

Combine connection reuse with circuit breaking to prevent pool exhaustion during outages.

```python
import asyncio
import time
from enum import Enum
from anthropic import AsyncAnthropic
import httpx

class CircuitState(Enum):
    CLOSED = "closed"       # normal operation
    OPEN = "open"           # failing, reject requests
    HALF_OPEN = "half_open" # testing if recovered

class PooledCircuitBreakerClient:
    def __init__(
        self,
        pool_size: int = 30,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
    ):
        limits = httpx.Limits(
            max_connections=pool_size,
            max_keepalive_connections=pool_size // 2,
        )
        self._client = AsyncAnthropic(http_client=httpx.AsyncClient(limits=limits))
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._failure_threshold = failure_threshold
        self._last_failure_time: float = 0
        self._recovery_timeout = recovery_timeout
        self._success_count = 0

    def _should_allow_request(self) -> bool:
        if self._state == CircuitState.CLOSED:
            return True
        if self._state == CircuitState.OPEN:
            if time.time() - self._last_failure_time > self._recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                self._success_count = 0
                print(f"[Circuit] HALF_OPEN — testing recovery")
                return True
            return False
        # HALF_OPEN: allow one request at a time
        return True

    def _on_success(self):
        if self._state == CircuitState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= 3:
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                print(f"[Circuit] CLOSED — recovered")
        elif self._state == CircuitState.CLOSED:
            self._failure_count = max(0, self._failure_count - 1)

    def _on_failure(self):
        self._failure_count += 1
        self._last_failure_time = time.time()
        if self._failure_count >= self._failure_threshold:
            self._state = CircuitState.OPEN
            print(f"[Circuit] OPEN — too many failures ({self._failure_count})")

    async def complete(self, prompt: str) -> str:
        if not self._should_allow_request():
            raise RuntimeError(f"Circuit breaker OPEN — rejecting request")

        try:
            response = await self._client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}]
            )
            self._on_success()
            return response.content[0].text if response.content else ""
        except Exception as e:
            self._on_failure()
            raise

    @property
    def state(self) -> str:
        return self._state.value

    async def close(self):
        await self._client.close()

async def main():
    client = PooledCircuitBreakerClient(pool_size=20, failure_threshold=3)
    try:
        result = await client.complete("What is a circuit breaker pattern?")
        print(f"[{client.state}] {result[:200]}")
    finally:
        await client.close()

asyncio.run(main())
```

## Solution 5: Multi-Provider Pool with Load Distribution

Maintain connection pools to multiple providers and distribute load across them.

```python
import asyncio
import time
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic
import httpx

@dataclass
class ProviderPool:
    name: str
    client: AsyncAnthropic
    weight: float = 1.0
    active_connections: int = 0
    total_requests: int = 0
    total_latency: float = 0.0
    errors: int = 0

    @property
    def avg_latency(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.total_latency / self.total_requests

    @property
    def error_rate(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.errors / self.total_requests

class MultiProviderPoolManager:
    def __init__(self, providers: list[ProviderPool]):
        self._providers = providers

    def _least_loaded(self) -> ProviderPool:
        """Select provider with fewest active connections, weighted by capacity."""
        return min(
            self._providers,
            key=lambda p: p.active_connections / max(p.weight, 0.01)
        )

    async def complete(self, prompt: str, model: str = "claude-haiku-4-5-20251001") -> str:
        provider = self._least_loaded()
        provider.active_connections += 1
        provider.total_requests += 1
        start = time.time()

        try:
            response = await provider.client.messages.create(
                model=model,
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}]
            )
            provider.total_latency += time.time() - start
            return response.content[0].text if response.content else ""
        except Exception:
            provider.errors += 1
            raise
        finally:
            provider.active_connections -= 1

    def stats(self) -> list[dict]:
        return [{
            "name": p.name,
            "requests": p.total_requests,
            "avg_latency_ms": round(p.avg_latency * 1000, 1),
            "error_rate": round(p.error_rate, 3),
            "active": p.active_connections,
        } for p in self._providers]

    async def close_all(self):
        await asyncio.gather(*[p.client.close() for p in self._providers])

def make_pool(name: str, pool_size: int, weight: float = 1.0) -> ProviderPool:
    limits = httpx.Limits(
        max_connections=pool_size,
        max_keepalive_connections=pool_size // 2,
    )
    client = AsyncAnthropic(http_client=httpx.AsyncClient(limits=limits))
    return ProviderPool(name=name, client=client, weight=weight)

async def main():
    # Two pools: primary (larger) and secondary (smaller)
    manager = MultiProviderPoolManager([
        make_pool("primary", pool_size=30, weight=2.0),
        make_pool("secondary", pool_size=10, weight=1.0),
    ])

    try:
        prompts = [f"Explain concept {i}" for i in range(6)]
        results = await asyncio.gather(*[manager.complete(p) for p in prompts])
        print(f"Completed {len(results)} requests")
        for stat in manager.stats():
            print(f"  {stat}")
    finally:
        await manager.close_all()

asyncio.run(main())
```

## Solution 6: Connection Pool Metrics and Health Monitor

Monitor pool utilization and alert when approaching exhaustion.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
import httpx
from dataclasses import dataclass, field
from collections import deque

@dataclass
class PoolMetrics:
    samples: deque = field(default_factory=lambda: deque(maxlen=1000))
    pool_exhaustion_events: int = 0
    total_wait_time: float = 0.0
    request_count: int = 0

    def record(self, wait_time: float, exhausted: bool = False):
        self.samples.append({"wait": wait_time, "ts": time.time()})
        self.total_wait_time += wait_time
        self.request_count += 1
        if exhausted:
            self.pool_exhaustion_events += 1

    @property
    def avg_wait_ms(self) -> float:
        if not self.request_count:
            return 0.0
        return (self.total_wait_time / self.request_count) * 1000

    @property
    def p95_wait_ms(self) -> float:
        if not self.samples:
            return 0.0
        waits = sorted(s["wait"] for s in self.samples)
        idx = int(len(waits) * 0.95)
        return waits[min(idx, len(waits)-1)] * 1000

class MonitoredPoolClient:
    def __init__(self, pool_size: int = 20, warning_threshold: float = 0.8):
        self._pool_size = pool_size
        self._warning_threshold = warning_threshold
        self._metrics = PoolMetrics()
        self._active = 0
        self._semaphore = asyncio.Semaphore(pool_size)

        limits = httpx.Limits(
            max_connections=pool_size,
            max_keepalive_connections=pool_size // 2,
            keepalive_expiry=30.0,
        )
        self._client = AsyncAnthropic(http_client=httpx.AsyncClient(limits=limits))

    @property
    def utilization(self) -> float:
        return self._active / self._pool_size

    async def complete(self, prompt: str) -> str:
        wait_start = time.time()
        exhausted = False

        if self.utilization >= self._warning_threshold:
            exhausted = True
            print(f"[Pool Warning] Utilization at {self.utilization:.0%} ({self._active}/{self._pool_size})")

        async with self._semaphore:
            wait_time = time.time() - wait_start
            self._metrics.record(wait_time, exhausted)
            self._active += 1
            try:
                response = await self._client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=256,
                    messages=[{"role": "user", "content": prompt}]
                )
                return response.content[0].text if response.content else ""
            finally:
                self._active -= 1

    def report(self) -> dict:
        return {
            "pool_size": self._pool_size,
            "active_now": self._active,
            "utilization": f"{self.utilization:.0%}",
            "total_requests": self._metrics.request_count,
            "avg_pool_wait_ms": round(self._metrics.avg_wait_ms, 2),
            "p95_pool_wait_ms": round(self._metrics.p95_wait_ms, 2),
            "pool_exhaustion_events": self._metrics.pool_exhaustion_events,
        }

    async def close(self):
        await self._client.close()

async def main():
    pool = MonitoredPoolClient(pool_size=5, warning_threshold=0.7)
    try:
        # Burst of 10 concurrent requests into a pool of 5
        prompts = [f"Brief answer: what is topic {i}?" for i in range(10)]
        results = await asyncio.gather(*[pool.complete(p) for p in prompts])
        print(f"Completed: {len(results)}")
        print(f"Pool report: {pool.report()}")
    finally:
        await pool.close()

asyncio.run(main())
```

## Comparison

| Approach | Setup Complexity | Pool Control | Observability | Best For |
|---|---|---|---|---|
| Module-Level Singleton | Minimal | Default httpx | None | Simple scripts, single-process |
| Explicit httpx Config | Low | Full | None | Tuned production workloads |
| Adaptive Pool Sizing | Medium | Auto-scaling | Basic | Variable load patterns |
| Pool + Circuit Breaker | Medium | Full + safety | Medium | Reliability-critical services |
| Multi-Provider Pool | High | Per-provider | Medium | Multi-provider load balancing |
| Monitored Pool | Medium | Full | High | Observability-first production |

**Rule of thumb**: Start with a module-level singleton (one line). Add explicit pool size config once you know your concurrency profile. Add monitoring when you need to debug latency or detect pool exhaustion in production.
