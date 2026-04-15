---
layout: solution
title: "Agent Doesn't Implement Health Check Endpoint"
category: general
description: "Agent service has no /health or /ready endpoint, so load balancers, orchestrators, and monitoring systems cannot detect when the agent is degraded, misconfigured, or unable to reach the Anthropic API."
tags: [general, health-check, observability, reliability, devops, production]
---

## Symptom

The agent container reports as healthy to Kubernetes because the process is running, but requests are silently failing because the Anthropic API key is invalid, the rate limit is exhausted, or a required tool dependency is unreachable. The load balancer keeps routing traffic to a broken instance for minutes before a human notices. After a deployment, the new version starts receiving traffic before it has finished warming up its connection pool, causing a spike in 500 errors.

## Root Cause

Developers focus on the agent's core logic and neglect to add health check endpoints. Without them, orchestration systems (Kubernetes, ECS, Docker Compose, nginx upstream checks) use the process-is-alive heuristic — which is insufficient. A process can be running while its dependencies are unreachable, its API key is revoked, or its connection pool is exhausted. A proper health check verifies that the agent can actually serve requests before declaring itself ready.

## Fix

### Option 1 — Minimal HTTP health check with FastAPI

```python
import os
import time
import asyncio
import anthropic
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

app    = FastAPI()
client = anthropic.AsyncAnthropic()

START_TIME = time.time()

@app.get("/health")
async def health() -> JSONResponse:
    """Liveness probe — is the process alive and not deadlocked?"""
    return JSONResponse({
        "status":   "ok",
        "uptime_s": round(time.time() - START_TIME, 1),
    })

@app.get("/ready")
async def ready() -> JSONResponse:
    """
    Readiness probe — can the agent actually serve requests?
    Checks API key validity with a minimal test call.
    """
    checks: dict = {}

    # 1. API key present
    checks["api_key_set"] = bool(os.getenv("ANTHROPIC_API_KEY"))

    # 2. Anthropic API reachable (minimal call)
    try:
        resp = await asyncio.wait_for(
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            ),
            timeout=5.0,
        )
        checks["anthropic_api"] = "ok"
        checks["model"]         = resp.model
    except asyncio.TimeoutError:
        checks["anthropic_api"] = "timeout"
    except anthropic.AuthenticationError:
        checks["anthropic_api"] = "auth_error"
    except Exception as e:
        checks["anthropic_api"] = f"error: {type(e).__name__}"

    all_ok = all(v in (True, "ok") for v in checks.values())
    status_code = 200 if all_ok else 503

    return JSONResponse(
        {"status": "ready" if all_ok else "degraded", "checks": checks},
        status_code=status_code,
    )

# Run with: uvicorn solution:app --host 0.0.0.0 --port 8080
# Kubernetes liveness:  GET /health  (fast, no external calls)
# Kubernetes readiness: GET /ready   (verifies API reachability)
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
```

**Expected Token Savings:** Each `/ready` call costs ~1 token (the "ping" response); the probe runs every 10-30s and prevents broken instances from receiving traffic — the cost of a single misdirected request to a broken agent far exceeds the cost of all health check calls.
**Environment:** FastAPI agents deployed on Kubernetes or any orchestrator that supports HTTP liveness/readiness probes; the liveness/readiness separation is the standard Kubernetes pattern.

---

### Option 2 — Dependency matrix health check: Anthropic + tools + config

```python
import os
import time
import asyncio
import anthropic
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app    = FastAPI()
client = anthropic.AsyncAnthropic()

async def check_anthropic() -> dict:
    t0 = time.monotonic()
    try:
        await asyncio.wait_for(
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            ),
            timeout=5.0,
        )
        return {"status": "ok", "latency_ms": round((time.monotonic() - t0) * 1000)}
    except asyncio.TimeoutError:
        return {"status": "timeout", "latency_ms": 5000}
    except anthropic.RateLimitError:
        return {"status": "rate_limited", "latency_ms": round((time.monotonic() - t0) * 1000)}
    except anthropic.AuthenticationError:
        return {"status": "auth_error", "latency_ms": 0}
    except Exception as e:
        return {"status": f"error:{type(e).__name__}", "latency_ms": 0}

async def check_database() -> dict:
    """Check database connectivity — replace with your actual DB client."""
    t0 = time.monotonic()
    try:
        # Simulate DB ping — in production: await db.execute("SELECT 1")
        await asyncio.sleep(0.002)
        return {"status": "ok", "latency_ms": round((time.monotonic() - t0) * 1000)}
    except Exception as e:
        return {"status": f"error:{type(e).__name__}", "latency_ms": 0}

async def check_config() -> dict:
    required = ["ANTHROPIC_API_KEY", "AGENT_MODEL", "DATABASE_URL"]
    missing  = [k for k in required if not os.getenv(k)]
    return {"status": "ok" if not missing else "missing", "missing_vars": missing}

@app.get("/ready")
async def ready() -> JSONResponse:
    # Run all checks concurrently
    anthropic_result, db_result, config_result = await asyncio.gather(
        check_anthropic(),
        check_database(),
        check_config(),
    )

    checks = {
        "anthropic": anthropic_result,
        "database":  db_result,
        "config":    config_result,
    }

    # Agent is ready only if all dependencies are ok
    all_ok = all(v.get("status") == "ok" for v in checks.values())

    return JSONResponse(
        {
            "status":     "ready" if all_ok else "degraded",
            "checks":     checks,
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        status_code=200 if all_ok else 503,
    )

@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
```

**Expected Token Savings:** Concurrent dependency checks complete in max(individual_check_latency) instead of sum — a 5ms DB check and 200ms API check together take 200ms, not 205ms; parallel health checks minimise the latency impact of probing all dependencies on every check interval.
**Environment:** Agents with multiple dependencies (database, vector store, external APIs); a dependency matrix health check makes it immediately obvious which dependency caused a readiness failure.

---

### Option 3 — Cached health check: avoid hitting Anthropic API on every probe

```python
import asyncio
import time
import anthropic
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app    = FastAPI()
client = anthropic.AsyncAnthropic()

# Cache health check results to avoid calling Anthropic on every probe
_cache: dict = {"result": None, "expires_at": 0.0}
_cache_lock  = asyncio.Lock()
CACHE_TTL    = 30.0   # seconds

async def get_health_status() -> dict:
    now = time.monotonic()
    async with _cache_lock:
        if _cache["result"] is not None and now < _cache["expires_at"]:
            return {**_cache["result"], "cached": True}

        # Perform actual health check
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(
                client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=1,
                    messages=[{"role": "user", "content": "ping"}],
                ),
                timeout=5.0,
            )
            result = {
                "status":      "ok",
                "latency_ms":  round((time.monotonic() - t0) * 1000),
                "last_checked": time.strftime("%H:%M:%S", time.gmtime()),
            }
        except Exception as e:
            result = {
                "status":      f"error:{type(e).__name__}",
                "latency_ms":  round((time.monotonic() - t0) * 1000),
                "last_checked": time.strftime("%H:%M:%S", time.gmtime()),
            }

        _cache["result"]     = result
        _cache["expires_at"] = now + CACHE_TTL
        return {**result, "cached": False}

@app.get("/ready")
async def ready() -> JSONResponse:
    status = await get_health_status()
    ok     = status["status"] == "ok"
    return JSONResponse({"status": "ready" if ok else "degraded", **status},
                        status_code=200 if ok else 503)

@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})

# Test the caching behaviour
async def demo() -> None:
    print("First call (no cache):")
    print(await get_health_status())
    print("Second call (should be cached):")
    print(await get_health_status())

if __name__ == "__main__":
    asyncio.run(demo())
```

**Expected Token Savings:** With 30s cache TTL and a 10s probe interval, 3 probe calls hit cache and only 1 actually calls Anthropic — a 67% reduction in health-check API calls; for 10 instances × 6 probes/min, caching reduces health-check calls from 60/min to 20/min.
**Environment:** Production agents with many replicas and frequent probe intervals; cached health checks prevent probe traffic from consuming rate limit budget or adding unnecessary API costs.

---

### Option 4 — Background health monitor with circuit breaker integration

```python
import asyncio
import time
import enum
import anthropic
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app    = FastAPI()
client = anthropic.AsyncAnthropic()

class State(enum.Enum):
    HEALTHY   = "healthy"
    DEGRADED  = "degraded"
    UNHEALTHY = "unhealthy"

class HealthMonitor:
    def __init__(self, check_interval: float = 30.0, failure_threshold: int = 3):
        self._interval  = check_interval
        self._threshold = failure_threshold
        self._state     = State.HEALTHY
        self._failures  = 0
        self._last_ok   = time.time()
        self._latency   = 0.0
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _check(self) -> bool:
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(
                client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=1,
                    messages=[{"role": "user", "content": "ping"}],
                ),
                timeout=5.0,
            )
            self._latency = (time.monotonic() - t0) * 1000
            return True
        except Exception:
            return False

    async def _run(self) -> None:
        while True:
            ok = await self._check()
            if ok:
                self._failures = 0
                self._last_ok  = time.time()
                self._state    = State.HEALTHY
            else:
                self._failures += 1
                if self._failures >= self._threshold:
                    self._state = State.UNHEALTHY
                else:
                    self._state = State.DEGRADED
            await asyncio.sleep(self._interval)

    @property
    def status(self) -> dict:
        return {
            "state":        self._state.value,
            "failures":     self._failures,
            "last_ok_ago":  round(time.time() - self._last_ok, 1),
            "latency_ms":   round(self._latency, 1),
        }

monitor = HealthMonitor(check_interval=30.0, failure_threshold=3)

@app.on_event("startup")
async def startup() -> None:
    await monitor.start()

@app.on_event("shutdown")
async def shutdown() -> None:
    await monitor.stop()

@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})

@app.get("/ready")
async def ready() -> JSONResponse:
    s       = monitor.status
    ok      = s["state"] == State.HEALTHY.value
    return JSONResponse(
        {"status": "ready" if ok else "degraded", **s},
        status_code=200 if ok else 503,
    )

@app.get("/metrics")
async def metrics() -> JSONResponse:
    """Expose health metrics for Prometheus/Grafana scraping."""
    s = monitor.status
    return JSONResponse({
        "agent_healthy":     1 if s["state"] == "healthy" else 0,
        "agent_failures":    s["failures"],
        "agent_latency_ms":  s["latency_ms"],
        "agent_last_ok_ago": s["last_ok_ago"],
    })

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
```

**Expected Token Savings:** Background monitor runs on its own cadence independent of probe frequency — probes read cached state (0 tokens) while the monitor makes periodic real API calls; decoupling probe frequency from check frequency allows high-frequency probing (every 5s) with low-frequency actual API calls (every 30s).
**Environment:** Production agents with strict SLAs; the circuit-breaker-style state machine (healthy → degraded → unhealthy) prevents cascading failures by removing unhealthy instances from rotation after consecutive failures.

---

### Option 5 — Startup probe: block traffic until agent is fully warm

```python
import asyncio
import os
import time
import anthropic
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app    = FastAPI()
client = anthropic.AsyncAnthropic()

# Startup state — set to True only after all warmup tasks complete
_STARTUP_COMPLETE = False
_STARTUP_ERROR:  str | None = None
_STARTUP_TIME:   float = 0.0

async def warmup() -> None:
    """
    Perform all expensive startup tasks before accepting traffic:
    - Validate API key
    - Pre-build connection pool
    - Load configuration from remote store
    - Warm caches
    """
    global _STARTUP_COMPLETE, _STARTUP_ERROR, _STARTUP_TIME
    t0 = time.monotonic()
    print("  [startup] beginning warmup...")

    # Step 1: validate API key
    try:
        await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
        print("  [startup] API key valid ✓")
    except anthropic.AuthenticationError as e:
        _STARTUP_ERROR = f"API key invalid: {e}"
        print(f"  [startup] FAILED: {_STARTUP_ERROR}")
        return

    # Step 2: warm the connection pool (fire 3 cheap calls concurrently)
    try:
        await asyncio.gather(*[
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            for _ in range(3)
        ])
        print("  [startup] connection pool warmed ✓")
    except Exception as e:
        print(f"  [startup] pool warmup warning: {e}")   # non-fatal

    # Step 3: validate required environment variables
    required = ["ANTHROPIC_API_KEY"]
    missing  = [k for k in required if not os.getenv(k)]
    if missing:
        _STARTUP_ERROR = f"Missing env vars: {missing}"
        print(f"  [startup] FAILED: {_STARTUP_ERROR}")
        return

    _STARTUP_TIME    = time.monotonic() - t0
    _STARTUP_COMPLETE = True
    print(f"  [startup] complete in {_STARTUP_TIME*1000:.0f}ms ✓")

@app.on_event("startup")
async def on_startup() -> None:
    asyncio.create_task(warmup())   # run warmup without blocking server start

@app.get("/startup")
async def startup_probe() -> JSONResponse:
    """Kubernetes startupProbe — blocks readiness until warmup is complete."""
    if _STARTUP_COMPLETE:
        return JSONResponse({"status": "complete", "warmup_ms": round(_STARTUP_TIME * 1000)})
    if _STARTUP_ERROR:
        return JSONResponse({"status": "failed", "error": _STARTUP_ERROR}, status_code=503)
    return JSONResponse({"status": "warming_up"}, status_code=503)

@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})

@app.get("/ready")
async def ready() -> JSONResponse:
    if not _STARTUP_COMPLETE:
        return JSONResponse({"status": "starting"}, status_code=503)
    if _STARTUP_ERROR:
        return JSONResponse({"status": "failed", "error": _STARTUP_ERROR}, status_code=503)
    return JSONResponse({"status": "ready"})

# Kubernetes probe config:
# startupProbe:  GET /startup  failureThreshold=30  periodSeconds=5  (150s max warmup)
# livenessProbe: GET /health   failureThreshold=3   periodSeconds=10
# readinessProbe:GET /ready    failureThreshold=2   periodSeconds=5

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
```

**Expected Token Savings:** Startup warmup fires 4 cheap ping calls (~4 tokens total) to pre-establish the connection pool; without warmup, the first 3-5 real requests pay the full TCP+TLS handshake overhead. The startup probe prevents traffic arriving before warmup is complete, eliminating the cold-start error spike on deployment.
**Environment:** Kubernetes deployments where rolling updates cause cold-start spikes; the three-probe pattern (startup + liveness + readiness) is the Kubernetes-recommended approach for agents with non-trivial initialisation.

---

### Option 6 — Health check as pytest fixture for CI integration testing

```python
import asyncio
import time
import pytest
import httpx
import anthropic

# ── In-process agent server for testing ──────────────────────────────────────
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

client_sdk = anthropic.AsyncAnthropic()

def build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/ready")
    async def ready() -> JSONResponse:
        try:
            await asyncio.wait_for(
                client_sdk.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=1,
                    messages=[{"role": "user", "content": "ping"}],
                ),
                timeout=5.0,
            )
            return JSONResponse({"status": "ready"})
        except Exception as e:
            return JSONResponse({"status": "degraded", "error": str(e)}, status_code=503)

    return app

# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def test_app():
    return build_app()

@pytest.fixture(scope="module")
def test_client(test_app):
    return TestClient(test_app)

def test_health_returns_200(test_client):
    r = test_client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

def test_health_is_fast(test_client):
    """Liveness probe must respond in <50ms — it should never hit the network."""
    t0 = time.perf_counter()
    test_client.get("/health")
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert elapsed_ms < 50, f"Health check too slow: {elapsed_ms:.0f}ms"

def test_ready_returns_ready_or_degraded(test_client):
    """Readiness probe must return a valid status — never a 500."""
    r = test_client.get("/ready")
    assert r.status_code in (200, 503)
    assert r.json()["status"] in ("ready", "degraded")

def test_ready_json_schema(test_client):
    """Readiness probe response must always include status field."""
    r    = test_client.get("/ready")
    body = r.json()
    assert "status" in body

# Run with: pytest solution.py -v
# These tests run in CI on every deploy to catch health endpoint regressions.
```

**Expected Token Savings:** Health check tests in CI add ~1 API call each run; they prevent silent regressions where a code change breaks the `/ready` endpoint — a broken health check means orchestrators think the agent is permanently unhealthy or healthy when it isn't, causing missed alerts or unnecessary restarts.
**Environment:** Teams with CI/CD pipelines; health endpoint tests should be part of the smoke test suite that runs after every deployment to verify the new version is observable before it fully rolls out.

---

## Comparison

| Option | Probe Type | Caches Result | Background Check | Best For |
|---|---|---|---|---|
| 1. Basic FastAPI health | liveness + readiness | No | No | Minimal working health check |
| 2. Dependency matrix | readiness (multi-dep) | No | No | Agents with multiple dependencies |
| 3. Cached health | readiness | Yes (TTL) | No | High-replica deployments, rate-limit budget |
| 4. Background monitor | all three | Yes (always) | Yes | SLA-bound services with metrics |
| 5. Startup probe | startup + liveness + readiness | N/A | Yes (warmup) | Kubernetes rolling deployments |
| 6. CI test fixture | test all probes | N/A | N/A | CI/CD pipeline health regression prevention |
