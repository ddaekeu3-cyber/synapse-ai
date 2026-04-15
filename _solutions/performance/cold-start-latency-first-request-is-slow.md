---
layout: solution
title: "Cold Start Latency — First Request Is Slow After Idle Period"
category: performance
description: "Agent is idle for 30 minutes. User sends a message. The first response takes 8 seconds instead of the usual 1.5 seconds. TCP connections were closed. Connection pools drained. Model client needs to re-establish TLS. Database pool is empty. Users notice the lag."
tags: [performance, cold-start, latency, connection-pool, warmup, keepalive, tcp, ttfb, startup]
---

# Cold Start Latency — First Request Is Slow After Idle Period

## Symptom
- First message after 30 minutes of idle takes 8 seconds; subsequent messages take 1.5 seconds
- Users notice a slow first response every morning when the agent hasn't been used overnight
- Metrics show p50 latency is fine but p99 is 5–8× worse — cold starts dominate the tail
- Database connection pool drains during idle — first query pays full connection setup cost
- TLS handshake adds 300–500ms to first HTTPS request after keepalive timeout
- Kubernetes pod restarts trigger 20–30 second cold starts for every new pod

## Root Cause
Cold start latency comes from deferred initialization: TCP connections that were idle beyond `keepalive_timeout` are closed by the server or load balancer, forcing a new TLS handshake. Database connection pools drain when idle. DNS caches expire. JIT compilation warm-up (in some runtimes) takes time. The first request after an idle period pays all these costs simultaneously. The fix is to pre-warm connections proactively and maintain them across idle periods.

## Fix

### Option 1: Connection pre-warming at startup

```python
import asyncio
import httpx
import anthropic
import time
import os

async def warm_up_connections() -> dict[str, float]:
    """
    Pre-warm all connections at agent startup.
    Call this before accepting any user traffic.
    Returns timing for each connection type.
    """
    timings = {}

    # 1. Warm Anthropic API connection
    start = time.monotonic()
    client = anthropic.AsyncAnthropic()
    try:
        await client.messages.create(
            model="claude-haiku-4-5-20251001",
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            system="Reply: ok"
        )
        timings["anthropic_api"] = time.monotonic() - start
        print(f"Anthropic API warmed up in {timings['anthropic_api']*1000:.0f}ms")
    except Exception as e:
        print(f"Anthropic warmup failed: {e}")

    # 2. Warm database connections
    start = time.monotonic()
    try:
        import asyncpg
        pool = await asyncpg.create_pool(
            dsn=os.environ["DATABASE_URL"],
            min_size=3,    # Pre-create 3 connections
            max_size=10,
            command_timeout=10
        )
        await pool.execute("SELECT 1")  # Verify connections work
        timings["database"] = time.monotonic() - start
        print(f"Database pool warmed up in {timings['database']*1000:.0f}ms")
    except Exception as e:
        print(f"Database warmup failed: {e}")

    # 3. Warm HTTP client connections to external APIs
    start = time.monotonic()
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10.0),
        limits=httpx.Limits(max_keepalive_connections=10, max_connections=20)
    ) as client:
        try:
            # HEAD request — cheap, just establishes TLS
            await client.head("https://api.example.com/health")
            timings["external_api"] = time.monotonic() - start
            print(f"External API connection warmed up in {timings['external_api']*1000:.0f}ms")
        except Exception as e:
            print(f"External API warmup failed (non-critical): {e}")

    total = sum(timings.values())
    print(f"All connections warmed up in {total*1000:.0f}ms total")
    return timings

# Call at agent startup — before readiness probe returns 200:
async def startup():
    timings = await warm_up_connections()
    # Only mark ready after warmup
    mark_ready()
```

### Option 2: Persistent HTTP client with keepalive

```python
import httpx
import asyncio
from contextlib import asynccontextmanager

class PersistentHTTPClient:
    """
    Single shared HTTP client with connection pooling and keepalive.
    Maintains connections across requests — no cold start per request.
    """

    def __init__(
        self,
        keepalive_expiry: float = 30.0,   # Keep connections for 30s idle
        max_keepalive: int = 20,
        max_connections: int = 100,
        ping_interval: float = 25.0        # Ping before keepalive expires
    ):
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0),
            limits=httpx.Limits(
                max_keepalive_connections=max_keepalive,
                max_connections=max_connections,
                keepalive_expiry=keepalive_expiry
            ),
            http2=True  # HTTP/2 multiplexing reduces connection overhead
        )
        self._ping_interval = ping_interval
        self._keepalive_hosts: set[str] = set()
        self._ping_task: asyncio.Task | None = None

    async def start(self):
        """Start background keepalive pinger"""
        self._ping_task = asyncio.create_task(self._keepalive_loop())
        print(f"HTTP client started with {self._ping_interval}s keepalive")

    async def stop(self):
        if self._ping_task:
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
        await self._client.aclose()

    def register_host(self, base_url: str):
        """Register a host to keep alive with periodic pings"""
        self._keepalive_hosts.add(base_url)

    async def _keepalive_loop(self):
        """Send HEAD requests to prevent connection idle timeout"""
        while True:
            await asyncio.sleep(self._ping_interval)
            for base_url in self._keepalive_hosts:
                try:
                    await self._client.head(f"{base_url}/health", timeout=5.0)
                except Exception:
                    pass  # Keepalive failures are non-critical

    async def get(self, url: str, **kwargs) -> httpx.Response:
        return await self._client.get(url, **kwargs)

    async def post(self, url: str, **kwargs) -> httpx.Response:
        return await self._client.post(url, **kwargs)

# Global client — shared across all requests:
http = PersistentHTTPClient(keepalive_expiry=60.0, ping_interval=30.0)

async def startup():
    await http.start()
    http.register_host("https://api.example.com")
    http.register_host("https://auth.example.com")
```

### Option 3: Database connection pool with min_size and keepalive

```python
import asyncpg
import asyncio
import os

class WarmDatabasePool:
    """
    Database connection pool that stays warm during idle periods.
    """

    def __init__(
        self,
        dsn: str,
        min_size: int = 3,
        max_size: int = 10,
        keepalive_interval: float = 60.0,  # Ping every 60s
        max_idle_time: float = 300.0        # Close idle connections after 5min
    ):
        self.dsn = dsn
        self.min_size = min_size
        self.max_size = max_size
        self.keepalive_interval = keepalive_interval
        self._pool: asyncpg.Pool | None = None
        self._keepalive_task: asyncio.Task | None = None

    async def initialize(self):
        """Create pool with min connections pre-established"""
        self._pool = await asyncpg.create_pool(
            dsn=self.dsn,
            min_size=self.min_size,
            max_size=self.max_size,
            command_timeout=30,
            # Server-side keepalive (PostgreSQL)
            server_settings={
                "keepalives": "1",
                "keepalives_idle": "60",
                "keepalives_interval": "10",
                "keepalives_count": "3",
            }
        )
        print(f"DB pool initialized: {self.min_size} min / {self.max_size} max connections")

        # Verify connections work:
        async with self._pool.acquire() as conn:
            version = await conn.fetchval("SELECT version()")
            print(f"DB connected: {version[:50]}")

        # Start keepalive loop
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def _keepalive_loop(self):
        """Keep minimum connections alive with periodic queries"""
        while True:
            await asyncio.sleep(self.keepalive_interval)
            try:
                async with self._pool.acquire(timeout=5.0) as conn:
                    await conn.execute("SELECT 1")  # Minimal keepalive query
            except Exception as e:
                print(f"DB keepalive failed: {e}")

    async def acquire(self, timeout: float = 10.0):
        """Acquire a connection — from warm pool, no cold start"""
        return self._pool.acquire(timeout=timeout)

    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool

db = WarmDatabasePool(
    dsn=os.environ["DATABASE_URL"],
    min_size=3,
    keepalive_interval=60.0
)
```

### Option 4: Anthropic client with connection reuse

```python
import anthropic
import asyncio
import time
import os

class WarmAnthropicClient:
    """
    Anthropic client wrapper that maintains a warm connection.
    Sends periodic lightweight requests to prevent connection idle timeout.
    """

    def __init__(
        self,
        model: str = "claude-sonnet-4-6",
        keepalive_interval: float = 300.0  # Every 5 minutes
    ):
        self.model = model
        self.keepalive_interval = keepalive_interval
        self._client = anthropic.AsyncAnthropic(
            api_key=os.environ["ANTHROPIC_API_KEY"],
            # Longer connection timeout — maintain persistent connections
            timeout=anthropic.Timeout(
                connect=5.0,
                read=300.0,
                write=30.0,
                pool=10.0
            ),
            max_retries=2
        )
        self._last_request_time = 0.0
        self._keepalive_task: asyncio.Task | None = None

    async def start(self):
        """Warm up and start keepalive"""
        await self._warmup()
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def _warmup(self):
        """Initial warmup request"""
        start = time.monotonic()
        try:
            await self._client.messages.create(
                model="claude-haiku-4-5-20251001",  # Cheapest model for warmup
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=1
            )
            elapsed = time.monotonic() - start
            self._last_request_time = time.monotonic()
            print(f"Anthropic client warmed up in {elapsed*1000:.0f}ms")
        except Exception as e:
            print(f"Warmup failed (non-critical): {e}")

    async def _keepalive_loop(self):
        """Prevent connection idle timeout"""
        while True:
            await asyncio.sleep(self.keepalive_interval)
            idle_time = time.monotonic() - self._last_request_time
            if idle_time > self.keepalive_interval * 0.8:
                try:
                    await self._client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        messages=[{"role": "user", "content": "ping"}],
                        max_tokens=1
                    )
                    self._last_request_time = time.monotonic()
                except Exception:
                    pass  # Non-critical

    async def create(self, **kwargs) -> anthropic.types.Message:
        """Create a message — always fast because connection is warm"""
        self._last_request_time = time.monotonic()
        return await self._client.messages.create(**kwargs)

claude = WarmAnthropicClient()
```

### Option 5: Kubernetes startup and readiness probes for warm pools

```yaml
# Kubernetes deployment — ensure warm pools before traffic
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      containers:
      - name: agent
        image: my-agent:latest

        # Startup probe — allow up to 3 minutes for warmup
        startupProbe:
          httpGet:
            path: /ready
            port: 8000
          initialDelaySeconds: 5
          periodSeconds: 5
          failureThreshold: 36   # 36 × 5s = 3 minutes max warmup

        # Readiness — only route traffic when connections are warm
        readinessProbe:
          httpGet:
            path: /ready
            port: 8000
          periodSeconds: 10
          failureThreshold: 3

        # Liveness — restart if process is dead
        livenessProbe:
          httpGet:
            path: /health
            port: 8000
          periodSeconds: 30
          failureThreshold: 3

        # Pre-stop hook — drain connections gracefully before shutdown
        lifecycle:
          preStop:
            exec:
              command: ["/bin/sh", "-c", "sleep 5"]  # Allow in-flight requests to complete

        env:
        - name: WARMUP_ON_START
          value: "true"
        - name: DB_POOL_MIN_SIZE
          value: "3"
        - name: HTTP_KEEPALIVE_SECONDS
          value: "60"
```

### Option 6: Latency monitoring — detect and alert on cold starts

```python
import time
import statistics
from dataclasses import dataclass, field

@dataclass
class LatencyMonitor:
    """
    Track request latency and detect cold start patterns.
    Alerts when first-request latency significantly exceeds steady-state.
    """
    window_size: int = 100
    cold_start_threshold_multiplier: float = 3.0

    _latencies: list[float] = field(default_factory=list)
    _cold_starts_detected: int = 0
    _last_request_time: float = field(default_factory=time.monotonic)
    _idle_threshold: float = 300.0  # 5 minutes = likely cold start

    def record(self, latency_seconds: float) -> dict:
        now = time.monotonic()
        idle_time = now - self._last_request_time
        self._last_request_time = now

        is_cold_start = idle_time > self._idle_threshold
        self._latencies.append(latency_seconds)
        if len(self._latencies) > self.window_size:
            self._latencies.pop(0)

        result = {
            "latency_ms": round(latency_seconds * 1000),
            "idle_before_seconds": round(idle_time),
            "is_cold_start": is_cold_start
        }

        if len(self._latencies) >= 10:
            median = statistics.median(self._latencies[:-1])  # Exclude this request
            if latency_seconds > median * self.cold_start_threshold_multiplier:
                self._cold_starts_detected += 1
                result["cold_start_detected"] = True
                result["median_latency_ms"] = round(median * 1000)
                result["slowdown_factor"] = round(latency_seconds / median, 1)
                print(
                    f"Cold start detected: {result['latency_ms']}ms vs "
                    f"{result['median_latency_ms']}ms median "
                    f"({result['slowdown_factor']}× slower, idle {round(idle_time)}s)"
                )

        return result

    @property
    def stats(self) -> dict:
        if not self._latencies:
            return {}
        return {
            "p50_ms": round(statistics.median(self._latencies) * 1000),
            "p95_ms": round(sorted(self._latencies)[int(len(self._latencies) * 0.95)] * 1000),
            "p99_ms": round(sorted(self._latencies)[int(len(self._latencies) * 0.99)] * 1000),
            "cold_starts": self._cold_starts_detected,
            "sample_count": len(self._latencies)
        }

latency_monitor = LatencyMonitor()

async def monitored_request(user_message: str) -> str:
    start = time.monotonic()
    result = await process_message(user_message)
    elapsed = time.monotonic() - start

    metrics = latency_monitor.record(elapsed)
    return result
```

## Cold Start Sources and Mitigations

| Source | Cold Start Cost | Mitigation |
|---|---|---|
| TLS handshake | 100–500ms | HTTP keepalive, connection pool |
| TCP connection | 50–200ms | Connection pool with min_size |
| DNS lookup | 10–100ms | DNS caching, fixed /etc/hosts |
| DB connection | 100–500ms | Connection pool, min_size ≥ 1 |
| Model API first call | 200–800ms | Warmup request at startup |
| Kubernetes pod start | 10–60s | Pre-warmed images, startup probes |
| Python module import | 500ms–5s | Eager imports at startup |

## Expected Token Savings
User retries slow first message → double processing: ~3,000 tokens per cold-start retry
Warm connections → consistent sub-2s latency: 0 cold-start retries

## Environment
- Any agent deployed as a service with idle periods; critical for agents deployed on Kubernetes, serverless, or any platform that scales down during inactivity
- Source: direct experience; cold start latency is the most common performance complaint from users of agents that aren't consistently loaded
