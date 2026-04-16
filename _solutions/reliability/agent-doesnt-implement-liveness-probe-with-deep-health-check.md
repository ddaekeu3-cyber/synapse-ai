---
title: "Agent Doesn't Implement Liveness Probe with Deep Health Check"
description: "AI agents expose only a shallow /health ping that returns 200 even when the model API, database, or tool backends are unreachable, causing silent failures in production."
category: reliability
difficulty: intermediate
tags: [health-check, liveness, readiness, kubernetes, monitoring, fastapi, asyncio]
---

# Agent Doesn't Implement Liveness Probe with Deep Health Check

## Problem

A basic `/health` endpoint that always returns `{"status": "ok"}` is a liveness lie. Kubernetes restarts a pod only if the liveness probe fails — but your agent may be up while the Anthropic API, Redis, or a downstream tool service is completely unreachable. Deep health checks verify real dependencies and distinguish *liveness* (is the process alive?) from *readiness* (can it serve traffic?).

## Solution 1: Separate /liveness and /readiness Endpoints

Kubernetes uses both probes differently. Liveness failing restarts the pod; readiness failing removes it from the load balancer.

```python
import asyncio
import time
from fastapi import FastAPI, Response
import httpx
import redis.asyncio as aioredis

app = FastAPI()

# Startup time for readiness gate
_start_time = time.monotonic()
WARMUP_SECONDS = 10

redis_pool = aioredis.ConnectionPool.from_url("redis://localhost:6379")

async def check_redis() -> tuple[bool, str]:
    try:
        r = aioredis.Redis(connection_pool=redis_pool)
        await asyncio.wait_for(r.ping(), timeout=2.0)
        return True, "ok"
    except Exception as e:
        return False, str(e)

async def check_model_api() -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get("https://api.anthropic.com/v1/models")
            return resp.status_code < 500, f"http_{resp.status_code}"
    except Exception as e:
        return False, str(e)

@app.get("/healthz/live")
async def liveness():
    """Minimal check: is the Python process responsive?"""
    return {"status": "alive", "uptime_s": round(time.monotonic() - _start_time, 1)}

@app.get("/healthz/ready")
async def readiness(response: Response):
    """Deep check: can we actually serve traffic?"""
    redis_ok, redis_msg = await check_redis()
    api_ok, api_msg = await check_model_api()

    warmed_up = (time.monotonic() - _start_time) >= WARMUP_SECONDS
    all_ok = redis_ok and api_ok and warmed_up

    if not all_ok:
        response.status_code = 503

    return {
        "status": "ready" if all_ok else "not_ready",
        "checks": {
            "redis": {"ok": redis_ok, "detail": redis_msg},
            "model_api": {"ok": api_ok, "detail": api_msg},
            "warmed_up": warmed_up,
        },
    }
```

Kubernetes config:
```yaml
livenessProbe:
  httpGet:
    path: /healthz/live
    port: 8000
  initialDelaySeconds: 5
  periodSeconds: 10

readinessProbe:
  httpGet:
    path: /healthz/ready
    port: 8000
  initialDelaySeconds: 15
  periodSeconds: 5
  failureThreshold: 3
```

**When to use**: Any Kubernetes-deployed agent service. Always separate liveness from readiness.

---

## Solution 2: Async Parallel Dependency Checks with Timeout Budget

Run all dependency checks concurrently so a single slow check doesn't block the probe response.

```python
import asyncio
from dataclasses import dataclass, field
from typing import Callable, Awaitable
import time

@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    latency_ms: float

async def run_check(name: str, fn: Callable[[], Awaitable[bool]], timeout: float) -> CheckResult:
    t0 = time.monotonic()
    try:
        ok = await asyncio.wait_for(fn(), timeout=timeout)
        return CheckResult(name=name, ok=ok, detail="ok", latency_ms=(time.monotonic()-t0)*1000)
    except asyncio.TimeoutError:
        return CheckResult(name=name, ok=False, detail=f"timeout>{timeout}s", latency_ms=timeout*1000)
    except Exception as e:
        return CheckResult(name=name, ok=False, detail=str(e)[:120], latency_ms=(time.monotonic()-t0)*1000)

CHECKS: dict[str, tuple[Callable, float]] = {
    "redis":         (lambda: check_redis_ping(), 2.0),
    "postgres":      (lambda: check_db_query(), 3.0),
    "model_api":     (lambda: check_anthropic_reachable(), 5.0),
    "tool_registry": (lambda: check_tool_service(), 2.0),
    "vector_store":  (lambda: check_qdrant_ping(), 2.0),
}

async def deep_health_check() -> tuple[bool, list[CheckResult]]:
    tasks = [
        run_check(name, fn, timeout)
        for name, (fn, timeout) in CHECKS.items()
    ]
    results: list[CheckResult] = await asyncio.gather(*tasks)
    all_ok = all(r.ok for r in results)
    return all_ok, results

from fastapi import FastAPI, Response
app = FastAPI()

@app.get("/healthz")
async def health(response: Response):
    all_ok, results = await deep_health_check()
    if not all_ok:
        response.status_code = 503
    return {
        "status": "ok" if all_ok else "degraded",
        "checks": {r.name: {"ok": r.ok, "detail": r.detail, "ms": round(r.latency_ms, 1)} for r in results},
    }
```

**When to use**: Agents with 3+ dependencies. Total probe time = max(individual timeouts) not sum.

---

## Solution 3: Cached Health Check to Prevent Probe Storms

Probes running every 5 seconds can themselves create load on dependencies. Cache results with a short TTL.

```python
import asyncio
import time
from functools import wraps
from typing import Any

class CachedHealthChecker:
    def __init__(self, ttl: float = 5.0):
        self._ttl = ttl
        self._cache: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, key: str, fn) -> Any:
        now = time.monotonic()
        if key in self._cache:
            ts, value = self._cache[key]
            if now - ts < self._ttl:
                return value  # serve from cache

        # Acquire per-key lock to prevent thundering herd
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        async with self._locks[key]:
            # Double-check after acquiring lock
            if key in self._cache:
                ts, value = self._cache[key]
                if now - ts < self._ttl:
                    return value
            result = await fn()
            self._cache[key] = (time.monotonic(), result)
            return result

_checker = CachedHealthChecker(ttl=5.0)

async def cached_redis_check():
    return await _checker.get("redis", check_redis_ping)

async def cached_api_check():
    return await _checker.get("model_api", check_anthropic_reachable)

from fastapi import FastAPI, Response
app = FastAPI()

@app.get("/healthz")
async def health(response: Response):
    redis_ok = await cached_redis_check()
    api_ok = await cached_api_check()
    if not (redis_ok and api_ok):
        response.status_code = 503
    return {"redis": redis_ok, "api": api_ok}
```

**When to use**: High-frequency probes or when dependency checks themselves are expensive (e.g., DB query).

---

## Solution 4: Circuit-Breaker-Aware Health Check

Report degraded status when a circuit breaker is open, without hammering the broken dependency.

```python
import asyncio
import time
from enum import Enum

class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

class HealthAwareCircuitBreaker:
    def __init__(self, name: str, failure_threshold: int = 3, recovery_timeout: float = 30.0):
        self.name = name
        self._failures = 0
        self._threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._state = CircuitState.CLOSED
        self._opened_at: float | None = None
        self._last_success: float = time.monotonic()

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if time.monotonic() - self._opened_at > self._recovery_timeout:
                return CircuitState.HALF_OPEN
        return self._state

    def health_status(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "failures": self._failures,
            "last_success_s_ago": round(time.monotonic() - self._last_success, 1),
        }

    async def call(self, fn):
        if self.state == CircuitState.OPEN:
            raise RuntimeError(f"Circuit {self.name} is OPEN")
        try:
            result = await fn()
            self._failures = 0
            self._last_success = time.monotonic()
            self._state = CircuitState.CLOSED
            return result
        except Exception:
            self._failures += 1
            if self._failures >= self._threshold:
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()
            raise

redis_cb = HealthAwareCircuitBreaker("redis", failure_threshold=3)
api_cb = HealthAwareCircuitBreaker("anthropic_api", failure_threshold=2)

from fastapi import FastAPI, Response
app = FastAPI()

@app.get("/healthz")
async def health(response: Response):
    redis_health = redis_cb.health_status()
    api_health = api_cb.health_status()
    all_closed = all(
        h["state"] == "closed" for h in [redis_health, api_health]
    )
    if not all_closed:
        response.status_code = 503
    return {
        "status": "ok" if all_closed else "degraded",
        "dependencies": [redis_health, api_health],
    }
```

**When to use**: When health checks should reflect circuit breaker state rather than probing broken services repeatedly.

---

## Solution 5: Startup Health Gate — Block Traffic Until Ready

Block readiness until all bootstrapping tasks complete (model pre-warming, cache loading, connection pool fill).

```python
import asyncio
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Response

class StartupGate:
    def __init__(self):
        self._ready = asyncio.Event()
        self._checks: dict[str, bool] = {}
        self._errors: dict[str, str] = {}

    def mark_done(self, check: str, ok: bool, error: str = ""):
        self._checks[check] = ok
        if not ok:
            self._errors[check] = error
        if all(self._checks.values()) and self._checks:
            self._ready.set()

    async def wait_ready(self, timeout: float = 60.0):
        await asyncio.wait_for(self._ready.wait(), timeout=timeout)

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    def status(self) -> dict:
        return {"ready": self.is_ready, "checks": self._checks, "errors": self._errors}

gate = StartupGate()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Run startup checks concurrently
    async def init_redis():
        try:
            await connect_redis()
            gate.mark_done("redis", True)
        except Exception as e:
            gate.mark_done("redis", False, str(e))

    async def init_model_pool():
        try:
            await warm_model_connections(count=5)
            gate.mark_done("model_pool", True)
        except Exception as e:
            gate.mark_done("model_pool", False, str(e))

    async def init_vector_store():
        try:
            await load_vector_index()
            gate.mark_done("vector_store", True)
        except Exception as e:
            gate.mark_done("vector_store", False, str(e))

    await asyncio.gather(init_redis(), init_model_pool(), init_vector_store())
    yield  # app runs

app = FastAPI(lifespan=lifespan)

@app.get("/healthz/ready")
async def readiness(response: Response):
    if not gate.is_ready:
        response.status_code = 503
    return gate.status()

# Placeholder stubs
async def connect_redis(): await asyncio.sleep(0.1)
async def warm_model_connections(count): await asyncio.sleep(0.5)
async def load_vector_index(): await asyncio.sleep(1.0)
```

**When to use**: Agents with significant startup time (index loading, connection pool fill, model pre-warming).

---

## Solution 6: Health Check with Degraded Mode and SLO Tracking

Distinguish full health from degraded (some features unavailable) and track SLO compliance over a rolling window.

```python
import asyncio
import time
from collections import deque
from enum import Enum

class HealthLevel(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"

class SLOTracker:
    """Track success rate over a rolling window."""
    def __init__(self, window_seconds: float = 60.0):
        self._window = window_seconds
        self._events: deque[tuple[float, bool]] = deque()

    def record(self, ok: bool):
        now = time.monotonic()
        self._events.append((now, ok))
        cutoff = now - self._window
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    @property
    def success_rate(self) -> float:
        if not self._events:
            return 1.0
        return sum(1 for _, ok in self._events if ok) / len(self._events)

class DeepHealthMonitor:
    def __init__(self):
        self._trackers: dict[str, SLOTracker] = {
            "redis": SLOTracker(),
            "model_api": SLOTracker(),
            "tool_service": SLOTracker(),
        }
        self._last_values: dict[str, bool] = {}

    def record(self, service: str, ok: bool):
        self._trackers[service].record(ok)
        self._last_values[service] = ok

    def overall_level(self) -> HealthLevel:
        critical = {"redis", "model_api"}
        rates = {k: t.success_rate for k, t in self._trackers.items()}
        if any(rates[s] < 0.5 for s in critical if s in rates):
            return HealthLevel.UNHEALTHY
        if any(r < 0.95 for r in rates.values()):
            return HealthLevel.DEGRADED
        return HealthLevel.HEALTHY

    def report(self) -> dict:
        return {
            "level": self.overall_level().value,
            "services": {
                name: {
                    "current": self._last_values.get(name),
                    "success_rate_1m": round(t.success_rate, 3),
                }
                for name, t in self._trackers.items()
            },
        }

monitor = DeepHealthMonitor()

async def background_probe_loop():
    """Continuous background probes; health endpoint reads from monitor."""
    while True:
        monitor.record("redis", await check_redis_ping())
        monitor.record("model_api", await check_anthropic_reachable())
        monitor.record("tool_service", await check_tool_service())
        await asyncio.sleep(10)

from fastapi import FastAPI, Response
app = FastAPI()

@app.on_event("startup")
async def start_probes():
    asyncio.create_task(background_probe_loop())

@app.get("/healthz")
async def health(response: Response):
    report = monitor.report()
    if report["level"] == HealthLevel.UNHEALTHY.value:
        response.status_code = 503
    elif report["level"] == HealthLevel.DEGRADED.value:
        response.status_code = 207  # Multi-Status
    return report

# Stubs
async def check_redis_ping() -> bool: return True
async def check_anthropic_reachable() -> bool: return True
async def check_tool_service() -> bool: return True
```

**When to use**: Production systems where you want granular SLO visibility and can route traffic based on degraded state.

---

## Comparison

| Solution | Probe Type | Caching | K8s Ready | SLO Tracking | Best For |
|---|---|---|---|---|---|
| Separate live/ready | Both | No | Yes | No | Kubernetes baseline |
| Parallel async checks | Readiness | No | Yes | No | Multi-dependency agents |
| Cached checks | Readiness | Yes (TTL) | Yes | No | High-frequency probes |
| Circuit-breaker aware | Readiness | Via CB | Yes | No | Agents with circuit breakers |
| Startup gate | Readiness | N/A | Yes | No | Long-startup agents |
| Degraded mode + SLO | Both | Via background | Yes | Yes | Production SLO monitoring |

**Rule of thumb**: Always implement both `/live` (always fast) and `/ready` (deep checks, cached). Never let `/live` depend on external services.
