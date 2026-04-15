---
layout: solution
title: "Agent Doesn't Implement Health Check in Dockerfile"
category: docker
description: "AI agent containers have no HEALTHCHECK instruction, so orchestrators like Kubernetes and Docker Swarm cannot distinguish a running-but-broken container from a healthy one, causing silent service degradation."
tags: [docker, healthcheck, kubernetes, liveness, readiness, fastapi, monitoring]
---

# Agent Doesn't Implement Health Check in Dockerfile

## Problem

A container can be running (`docker ps` shows "Up 5 hours") while the process inside is deadlocked, the Anthropic client is rate-limited into permanent backoff, or the database connection pool is exhausted. Without a `HEALTHCHECK`, orchestrators keep routing traffic to these zombie containers. Adding a proper health check enables automatic restarts and traffic shifting before users notice the failure.

## Solutions

### Option 1: Minimal HTTP HEALTHCHECK in Dockerfile

```dockerfile
# Dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# ─── Health Check ─────────────────────────────────────────────────────────────
# Probe the /health endpoint every 30s.
# Allow 40s for startup before first check (agent loads models, connects to DB).
# Mark unhealthy after 3 consecutive failures.
HEALTHCHECK \
    --interval=30s \
    --timeout=10s \
    --start-period=40s \
    --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

```python
# main.py — minimal /health endpoint
from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
async def health():
    """Lightweight liveness probe — just confirms the process is alive."""
    return {"status": "ok"}
```

```bash
# Check health status from host:
docker inspect --format='{{.State.Health.Status}}' <container_id>
# Output: healthy | unhealthy | starting
```

**Expected Token Savings:** Not applicable — infrastructure configuration
**Environment:** Docker + curl (or wget)

---

### Option 2: Deep Health Check — Verify Agent Dependencies

```python
# api/health.py
"""
Tiered health check:
  /health/live   — is the process alive? (liveness probe)
  /health/ready  — can it serve traffic? (readiness probe)
  /health/deep   — are all dependencies healthy? (admin/debug only)
"""
import asyncio
import os
import time
from enum import Enum
from typing import Optional
import anthropic
import asyncpg
import redis.asyncio as aioredis
from fastapi import APIRouter, Response

router = APIRouter()

_start_time = time.time()


class CheckStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"


async def _check_anthropic() -> tuple[CheckStatus, str]:
    """Verify Anthropic API key is valid with a minimal models list call."""
    try:
        client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        # Use a tiny message to verify connectivity — not a free operation,
        # so only call this in the deep check, not every liveness probe.
        await asyncio.wait_for(
            client.models.list(),
            timeout=5.0,
        )
        return CheckStatus.OK, "reachable"
    except asyncio.TimeoutError:
        return CheckStatus.FAILED, "timeout after 5s"
    except anthropic.AuthenticationError:
        return CheckStatus.FAILED, "invalid API key"
    except Exception as e:
        return CheckStatus.DEGRADED, str(e)[:100]


async def _check_database() -> tuple[CheckStatus, str]:
    """Verify database connectivity with a SELECT 1."""
    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(os.environ["DATABASE_URL"]),
            timeout=5.0,
        )
        await conn.execute("SELECT 1")
        await conn.close()
        return CheckStatus.OK, "connected"
    except asyncio.TimeoutError:
        return CheckStatus.FAILED, "connection timeout"
    except Exception as e:
        return CheckStatus.FAILED, str(e)[:100]


async def _check_redis() -> tuple[CheckStatus, str]:
    """Verify Redis connectivity with a PING."""
    try:
        r = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
        pong = await asyncio.wait_for(r.ping(), timeout=3.0)
        await r.aclose()
        return (CheckStatus.OK, "pong") if pong else (CheckStatus.FAILED, "no pong")
    except asyncio.TimeoutError:
        return CheckStatus.FAILED, "timeout"
    except Exception as e:
        return CheckStatus.FAILED, str(e)[:100]


@router.get("/health/live")
async def liveness(response: Response):
    """
    Liveness probe: is the process running?
    Fast, no external calls. Use for Docker HEALTHCHECK and K8s livenessProbe.
    """
    return {"status": "alive", "uptime_seconds": int(time.time() - _start_time)}


@router.get("/health/ready")
async def readiness(response: Response):
    """
    Readiness probe: can the service handle traffic?
    Checks DB and Redis (required for request handling), skips Anthropic (slow).
    Use for K8s readinessProbe.
    """
    db_status, db_msg = await _check_database()
    redis_status, redis_msg = await _check_redis()

    all_ok = db_status == CheckStatus.OK and redis_status == CheckStatus.OK
    if not all_ok:
        response.status_code = 503

    return {
        "ready": all_ok,
        "checks": {
            "database": {"status": db_status, "detail": db_msg},
            "redis": {"status": redis_status, "detail": redis_msg},
        },
    }


@router.get("/health/deep")
async def deep_health(response: Response):
    """
    Deep health: checks all dependencies including Anthropic.
    Expensive — do not use as a frequent probe. Admin/debug use only.
    """
    db_status, db_msg = await _check_database()
    redis_status, redis_msg = await _check_redis()
    ai_status, ai_msg = await _check_anthropic()

    statuses = [db_status, redis_status, ai_status]
    if CheckStatus.FAILED in statuses:
        overall = CheckStatus.FAILED
        response.status_code = 503
    elif CheckStatus.DEGRADED in statuses:
        overall = CheckStatus.DEGRADED
        response.status_code = 207
    else:
        overall = CheckStatus.OK

    return {
        "status": overall,
        "uptime_seconds": int(time.time() - _start_time),
        "checks": {
            "database": {"status": db_status, "detail": db_msg},
            "redis": {"status": redis_status, "detail": redis_msg},
            "anthropic_api": {"status": ai_status, "detail": ai_msg},
        },
    }
```

```dockerfile
# Dockerfile — use the fast liveness endpoint for HEALTHCHECK
HEALTHCHECK \
    --interval=30s \
    --timeout=10s \
    --start-period=45s \
    --retries=3 \
    CMD curl -f http://localhost:8000/health/live || exit 1
```

**Expected Token Savings:** Not applicable — reliability infrastructure
**Environment:** `pip install fastapi asyncpg redis anthropic`

---

### Option 3: wget-Based HEALTHCHECK (No curl Required)

```dockerfile
# Dockerfile — use wget when curl is not installed in slim images
FROM python:3.12-slim

# wget is included in python:slim; curl is not
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

HEALTHCHECK \
    --interval=30s \
    --timeout=10s \
    --start-period=40s \
    --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:8000/health || exit 1

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

```python
# Alternative: use Python itself as the health check script (no curl/wget needed)
# scripts/healthcheck.py
import sys
import urllib.request
import urllib.error

try:
    with urllib.request.urlopen("http://localhost:8000/health", timeout=5) as resp:
        if resp.status == 200:
            sys.exit(0)
        else:
            print(f"Health check returned HTTP {resp.status}", file=sys.stderr)
            sys.exit(1)
except urllib.error.URLError as e:
    print(f"Health check failed: {e}", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f"Health check error: {e}", file=sys.stderr)
    sys.exit(1)
```

```dockerfile
# Dockerfile — Python-based healthcheck (zero extra binaries)
HEALTHCHECK \
    --interval=30s \
    --timeout=10s \
    --start-period=40s \
    --retries=3 \
    CMD python scripts/healthcheck.py
```

**Expected Token Savings:** Not applicable — portability improvement
**Environment:** stdlib Python

---

### Option 4: Kubernetes Liveness + Readiness + Startup Probes

```yaml
# k8s/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ai-agent
spec:
  replicas: 3
  selector:
    matchLabels:
      app: ai-agent
  template:
    metadata:
      labels:
        app: ai-agent
    spec:
      containers:
        - name: agent
          image: your-registry/ai-agent:latest
          ports:
            - containerPort: 8000

          # ── Startup Probe ─────────────────────────────────────────────────
          # Give the container up to 60s to start before liveness kicks in.
          # Prevents premature restarts during slow model/DB initialization.
          startupProbe:
            httpGet:
              path: /health/live
              port: 8000
            failureThreshold: 12      # 12 × 5s = 60s max startup window
            periodSeconds: 5

          # ── Liveness Probe ────────────────────────────────────────────────
          # Restart the container if it stops responding.
          # Fast endpoint — no external calls.
          livenessProbe:
            httpGet:
              path: /health/live
              port: 8000
            initialDelaySeconds: 0   # startupProbe handles the initial delay
            periodSeconds: 15
            timeoutSeconds: 5
            failureThreshold: 3

          # ── Readiness Probe ───────────────────────────────────────────────
          # Remove from load balancer if dependencies are unavailable.
          # Uses the /health/ready endpoint (checks DB + Redis, not Anthropic).
          readinessProbe:
            httpGet:
              path: /health/ready
              port: 8000
            periodSeconds: 10
            timeoutSeconds: 5
            failureThreshold: 3
            successThreshold: 1

          resources:
            requests:
              cpu: "250m"
              memory: "512Mi"
            limits:
              cpu: "1000m"
              memory: "2Gi"
```

```python
# api/health.py — simplified ready check for K8s readinessProbe
from fastapi import APIRouter, Response
import asyncpg, os, asyncio

router = APIRouter()

@router.get("/health/live")
async def live():
    return {"status": "alive"}

@router.get("/health/ready")
async def ready(response: Response):
    try:
        conn = await asyncio.wait_for(asyncpg.connect(os.environ["DATABASE_URL"]), timeout=3)
        await conn.execute("SELECT 1")
        await conn.close()
        return {"ready": True}
    except Exception as e:
        response.status_code = 503
        return {"ready": False, "error": str(e)}
```

**Expected Token Savings:** Not applicable — Kubernetes deployment configuration
**Environment:** Kubernetes + FastAPI

---

### Option 5: Docker Compose Health Check with Dependency Ordering

```yaml
# docker-compose.yml
version: "3.9"

services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: agentdb
      POSTGRES_USER: agent
      POSTGRES_PASSWORD: secret
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U agent -d agentdb"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 10s

  redis:
    image: redis:7-alpine
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 3s
      retries: 3
      start_period: 5s

  agent:
    build: .
    ports:
      - "8000:8000"
    environment:
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
      DATABASE_URL: postgresql://agent:secret@postgres/agentdb
      REDIS_URL: redis://redis:6379/0
    healthcheck:
      test: ["CMD", "python", "scripts/healthcheck.py"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 45s
    # Only start agent after postgres AND redis are healthy
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy

  nginx:
    image: nginx:alpine
    ports:
      - "80:80"
    depends_on:
      agent:
        condition: service_healthy  # Only start nginx after agent is healthy
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
```

```bash
# Start and wait for all services to be healthy before returning
docker compose up --wait

# Check all service health statuses
docker compose ps --format json | python -c "
import sys, json
for line in sys.stdin:
    s = json.loads(line)
    print(f\"{s['Service']:15} {s['Health']:10} {s['Status']}\")
"
```

**Expected Token Savings:** Not applicable — local dev + CI orchestration
**Environment:** Docker Compose v2

---

### Option 6: Health Check with Graceful Degradation Reporting

```python
# api/health.py
"""
Health check that reports degraded state (not just binary ok/fail).
Allows monitoring systems to page on "degraded" before full outage.
Also provides structured JSON for alerting rules.
"""
import asyncio
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, asdict
from typing import Optional
import anthropic
from fastapi import FastAPI, Response


@dataclass
class ComponentHealth:
    name: str
    status: str          # "ok" | "degraded" | "failed"
    latency_ms: float
    message: str
    critical: bool       # If True, failure makes whole service unhealthy


@dataclass
class HealthReport:
    status: str          # "ok" | "degraded" | "unhealthy"
    timestamp: float
    uptime_seconds: float
    version: str
    components: list[ComponentHealth] = field(default_factory=list)

    def to_http_status(self) -> int:
        return {
            "ok": 200,
            "degraded": 207,
            "unhealthy": 503,
        }.get(self.status, 503)


_start_time = time.time()
VERSION = os.environ.get("APP_VERSION", "unknown")


async def check_component(
    name: str,
    coro,
    critical: bool = True,
    timeout: float = 5.0,
) -> ComponentHealth:
    """Run a health check coroutine and return structured ComponentHealth."""
    start = time.perf_counter()
    try:
        message = await asyncio.wait_for(coro, timeout=timeout)
        latency_ms = (time.perf_counter() - start) * 1000
        return ComponentHealth(name=name, status="ok", latency_ms=latency_ms, message=message, critical=critical)
    except asyncio.TimeoutError:
        latency_ms = (time.perf_counter() - start) * 1000
        return ComponentHealth(name=name, status="failed", latency_ms=latency_ms, message=f"timeout after {timeout}s", critical=critical)
    except Exception as e:
        latency_ms = (time.perf_counter() - start) * 1000
        status = "degraded" if not critical else "failed"
        return ComponentHealth(name=name, status=status, latency_ms=latency_ms, message=str(e)[:120], critical=critical)


async def _ping_database() -> str:
    import asyncpg
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    result = await conn.fetchval("SELECT version()")
    await conn.close()
    return f"connected: {result[:40]}"


async def _ping_redis() -> str:
    import redis.asyncio as aioredis
    r = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    await r.ping()
    await r.aclose()
    return "pong"


async def _ping_anthropic() -> str:
    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    models = await client.models.list()
    return f"api reachable, {len(models.data)} models"


app = FastAPI()


@app.get("/health")
async def health(response: Response):
    """Full health check with structured degradation reporting."""
    components = await asyncio.gather(
        check_component("database", _ping_database(), critical=True, timeout=5),
        check_component("redis", _ping_redis(), critical=False, timeout=3),  # degraded, not fatal
        check_component("anthropic_api", _ping_anthropic(), critical=True, timeout=8),
    )

    # Determine overall status
    has_critical_failure = any(c.status == "failed" and c.critical for c in components)
    has_any_failure = any(c.status in ("failed", "degraded") for c in components)

    if has_critical_failure:
        overall = "unhealthy"
    elif has_any_failure:
        overall = "degraded"
    else:
        overall = "ok"

    report = HealthReport(
        status=overall,
        timestamp=time.time(),
        uptime_seconds=time.time() - _start_time,
        version=VERSION,
        components=list(components),
    )

    response.status_code = report.to_http_status()
    return asdict(report)
```

```dockerfile
# Dockerfile — points at the single /health endpoint
HEALTHCHECK \
    --interval=30s \
    --timeout=15s \
    --start-period=60s \
    --retries=3 \
    CMD python scripts/healthcheck.py
```

**Expected Token Savings:** Not applicable — observability infrastructure
**Environment:** `pip install fastapi asyncpg redis anthropic`

---

## Comparison Table

| Option | HEALTHCHECK Method | Dependency Checks | K8s Probes | Graceful Degraded | Complexity |
|--------|--------------------|-------------------|------------|-------------------|------------|
| 1: Minimal curl | `curl -f /health` | None (liveness only) | No | No | Minimal |
| 2: Deep checks | `curl -f /health/live` | DB + Redis + Anthropic | Via YAML | Partial (tiered) | Medium |
| 3: wget / Python | `python healthcheck.py` | None (liveness only) | No | No | Minimal |
| 4: K8s probes | K8s httpGet | DB + Redis | Yes (all 3 types) | Via readiness | Medium |
| 5: Compose deps | `python healthcheck.py` | All services | No | Via depends_on | Medium |
| 6: Degraded report | `python healthcheck.py` | DB + Redis + API | Via YAML | Yes (207 status) | High |
