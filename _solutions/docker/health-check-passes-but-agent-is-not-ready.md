---
layout: solution
title: "Health Check Passes But Agent Is Not Ready — Misleading Liveness Probe"
category: docker
description: "Container starts. Health check returns 200 OK. Load balancer sends traffic. Agent crashes or returns errors because the model client isn't initialized, the database connection pool isn't warm, or the embedding index hasn't loaded. Health check passes too early."
tags: [docker, health-check, readiness, liveness, kubernetes, probe, warmup, startup, k8s]
---

# Health Check Passes But Agent Is Not Ready — Misleading Liveness Probe

## Symptom
- Container starts, health check returns 200 immediately, traffic routed in
- First N requests fail with `ModelNotInitialized`, `ConnectionPoolNotReady`, or `NullPointerException`
- Agent loads a large embedding index — takes 30 seconds — but health check passes in 2 seconds
- Kubernetes marks pod as Ready before model weights are loaded
- Rolling deploy causes errors: new pod reports healthy before old pod's traffic drains
- `/health` returns 200 but `/api/chat` returns 500 for first minute after deploy

## Root Cause
There are two distinct concepts that are often conflated into a single health endpoint:
- **Liveness**: is the process alive and not deadlocked? (restart if no)
- **Readiness**: is the service ready to handle traffic? (remove from load balancer if no)

A simple `GET /health → 200` check answers the liveness question but not the readiness question. If the agent needs 30 seconds to load a model, warm a connection pool, or build an index, traffic routed during that window will fail. The fix is to track initialization state explicitly and expose a separate readiness endpoint that returns non-200 until all components are ready.

## Fix

### Option 1: Separate liveness and readiness endpoints

```python
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from fastapi import FastAPI, Response

class ComponentStatus(Enum):
    NOT_STARTED = "not_started"
    INITIALIZING = "initializing"
    READY = "ready"
    FAILED = "failed"

@dataclass
class ReadinessTracker:
    """
    Track initialization state for all agent components.
    Only reports ready when ALL required components are ready.
    """
    _components: dict[str, ComponentStatus] = field(default_factory=dict)
    _start_time: float = field(default_factory=time.monotonic)
    _ready_time: float | None = None

    def register(self, name: str):
        """Register a component that must be ready before serving traffic"""
        self._components[name] = ComponentStatus.NOT_STARTED

    def mark_initializing(self, name: str):
        self._components[name] = ComponentStatus.INITIALIZING
        print(f"[readiness] {name}: initializing...")

    def mark_ready(self, name: str):
        self._components[name] = ComponentStatus.READY
        elapsed = time.monotonic() - self._start_time
        print(f"[readiness] {name}: ready ({elapsed:.1f}s after start)")
        if self.is_ready and self._ready_time is None:
            self._ready_time = time.monotonic()
            total = self._ready_time - self._start_time
            print(f"[readiness] ALL components ready — serving traffic ({total:.1f}s startup)")

    def mark_failed(self, name: str, error: str):
        self._components[name] = ComponentStatus.FAILED
        print(f"[readiness] {name}: FAILED — {error}")

    @property
    def is_ready(self) -> bool:
        if not self._components:
            return False
        return all(s == ComponentStatus.READY for s in self._components.values())

    @property
    def status_detail(self) -> dict:
        return {
            "ready": self.is_ready,
            "components": {k: v.value for k, v in self._components.items()},
            "uptime_seconds": round(time.monotonic() - self._start_time, 1)
        }

# Global readiness tracker
readiness = ReadinessTracker()
readiness.register("anthropic_client")
readiness.register("database_pool")
readiness.register("embedding_index")

app = FastAPI()

@app.get("/health")  # Liveness — always 200 if process is alive
async def liveness():
    return {"status": "alive", "pid": __import__("os").getpid()}

@app.get("/ready")  # Readiness — 503 until all components ready
async def readiness_check(response: Response):
    if readiness.is_ready:
        return readiness.status_detail
    response.status_code = 503
    return readiness.status_detail

@app.on_event("startup")
async def startup():
    asyncio.create_task(initialize_components())

async def initialize_components():
    """Initialize all components after server starts accepting connections"""

    # 1. Anthropic client
    readiness.mark_initializing("anthropic_client")
    try:
        import anthropic
        client = anthropic.Anthropic()
        # Warm up: make a minimal test call
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1
        )
        readiness.mark_ready("anthropic_client")
    except Exception as e:
        readiness.mark_failed("anthropic_client", str(e))
        return

    # 2. Database connection pool
    readiness.mark_initializing("database_pool")
    try:
        import asyncpg
        pool = await asyncpg.create_pool(dsn=__import__("os").environ["DATABASE_URL"], min_size=2)
        await pool.execute("SELECT 1")  # Verify connections work
        readiness.mark_ready("database_pool")
    except Exception as e:
        readiness.mark_failed("database_pool", str(e))
        return

    # 3. Embedding index (expensive — can take 30+ seconds)
    readiness.mark_initializing("embedding_index")
    try:
        index = await load_embedding_index()  # Your heavy initialization here
        readiness.mark_ready("embedding_index")
    except Exception as e:
        readiness.mark_failed("embedding_index", str(e))
```

### Option 2: Kubernetes probe configuration

```yaml
# kubernetes deployment — separate liveness and readiness probes
apiVersion: apps/v1
kind: Deployment
metadata:
  name: agent
spec:
  replicas: 2
  template:
    spec:
      containers:
      - name: agent
        image: my-agent:latest
        ports:
        - containerPort: 8000

        # Liveness: is the process alive? Restart if not.
        livenessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 10   # Give process time to start
          periodSeconds: 15
          failureThreshold: 3       # Restart after 3 consecutive failures
          timeoutSeconds: 5

        # Readiness: is the agent ready to serve traffic? Remove from LB if not.
        readinessProbe:
          httpGet:
            path: /ready
            port: 8000
          initialDelaySeconds: 5    # Start checking early
          periodSeconds: 5          # Check frequently
          failureThreshold: 60      # 60 × 5s = 5 minutes max wait
          successThreshold: 1       # Ready as soon as 1 check passes
          timeoutSeconds: 10

        # Startup: don't kill slow-starting containers
        startupProbe:
          httpGet:
            path: /ready
            port: 8000
          initialDelaySeconds: 0
          periodSeconds: 5
          failureThreshold: 60      # 5 minutes total startup time allowed
          timeoutSeconds: 10

        resources:
          requests:
            memory: "2Gi"
            cpu: "500m"
          limits:
            memory: "4Gi"
            cpu: "2000m"

---
# Service — only routes to Ready pods
apiVersion: v1
kind: Service
metadata:
  name: agent-service
spec:
  selector:
    app: agent
  ports:
  - port: 80
    targetPort: 8000
  # Kubernetes automatically excludes non-Ready pods from service endpoints
```

### Option 3: Docker Compose health check with dependency ordering

```yaml
# docker-compose.yml — health check ordering
services:
  agent:
    image: my-agent:latest
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/ready"]
      interval: 5s
      timeout: 10s
      retries: 60          # 60 × 5s = 5 minutes max
      start_period: 10s    # Don't count failures in first 10s
    depends_on:
      redis:
        condition: service_healthy
      postgres:
        condition: service_healthy
    environment:
      - STARTUP_WARMUP=true

  redis:
    image: redis:7-alpine
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 3s
      retries: 10

  postgres:
    image: postgres:15
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 3s
      retries: 10

  # Nginx only starts routing when agent is healthy
  nginx:
    image: nginx:alpine
    depends_on:
      agent:
        condition: service_healthy
    ports:
    - "80:80"
```

### Option 4: Readiness gate with timeout and retry

```python
import asyncio
import httpx
import sys

async def wait_for_readiness(
    url: str = "http://localhost:8000/ready",
    timeout_seconds: int = 300,
    poll_interval: float = 2.0
) -> bool:
    """
    Wait for service to become ready.
    Use this in startup scripts, integration tests, or deploy pipelines.
    """
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    attempt = 0

    async with httpx.AsyncClient() as client:
        while asyncio.get_event_loop().time() < deadline:
            attempt += 1
            try:
                response = await client.get(url, timeout=5)
                if response.status_code == 200:
                    data = response.json()
                    print(f"Service ready after {attempt} checks: {data}")
                    return True
                else:
                    data = response.json()
                    not_ready = [k for k, v in data.get("components", {}).items() if v != "ready"]
                    print(f"Not ready yet (attempt {attempt}). Waiting on: {not_ready}")
            except (httpx.ConnectError, httpx.TimeoutException):
                print(f"Attempt {attempt}: service not yet accepting connections")

            await asyncio.sleep(poll_interval)

    print(f"Timeout after {timeout_seconds}s — service never became ready")
    return False

# In deploy scripts:
if __name__ == "__main__":
    ready = asyncio.run(wait_for_readiness())
    sys.exit(0 if ready else 1)
```

### Option 5: Component initialization with circuit breaker

```python
import asyncio
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI

class AgentComponents:
    """Central registry for all agent components with initialization tracking"""

    def __init__(self):
        self._initialized: dict[str, bool] = {}
        self._errors: dict[str, str] = {}
        self._init_times: dict[str, float] = {}

    def is_ready(self) -> bool:
        return bool(self._initialized) and all(self._initialized.values())

    def get_status(self) -> dict:
        return {
            "ready": self.is_ready(),
            "components": {
                name: {
                    "ready": ready,
                    "init_time_ms": round(self._init_times.get(name, 0) * 1000),
                    "error": self._errors.get(name)
                }
                for name, ready in self._initialized.items()
            }
        }

    async def initialize(self, name: str, init_fn, required: bool = True):
        """Initialize a named component, tracking time and errors"""
        start = time.monotonic()
        try:
            result = await init_fn()
            elapsed = time.monotonic() - start
            self._initialized[name] = True
            self._init_times[name] = elapsed
            print(f"[startup] {name} initialized in {elapsed*1000:.0f}ms")
            return result
        except Exception as e:
            elapsed = time.monotonic() - start
            self._initialized[name] = False
            self._errors[name] = str(e)
            self._init_times[name] = elapsed
            print(f"[startup] {name} FAILED after {elapsed*1000:.0f}ms: {e}")
            if required:
                raise RuntimeError(f"Required component '{name}' failed to initialize: {e}") from e
            return None

components = AgentComponents()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup — initialize all components
    await components.initialize("model_client", init_model_client)
    await components.initialize("vector_store", init_vector_store)
    await components.initialize("cache", init_cache, required=False)  # Optional
    print("[startup] All components initialized — ready to serve")
    yield
    # Shutdown — cleanup
    print("[shutdown] Cleaning up components")

app = FastAPI(lifespan=lifespan)

@app.get("/ready")
async def ready(response):
    status = components.get_status()
    if not status["ready"]:
        response.status_code = 503
    return status
```

### Option 6: Warmup request — pre-heat model inference before marking ready

```python
import anthropic
import asyncio
import time

async def warmup_model_client(client: anthropic.AsyncAnthropic) -> float:
    """
    Send a minimal warmup request before marking service ready.
    Ensures first real request doesn't pay cold-start latency.
    Returns time taken in seconds.
    """
    start = time.monotonic()
    try:
        await client.messages.create(
            model="claude-haiku-4-5-20251001",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
            system="Reply with exactly: ok"
        )
        elapsed = time.monotonic() - start
        print(f"[warmup] Model client warmed up in {elapsed:.2f}s")
        return elapsed
    except Exception as e:
        raise RuntimeError(f"Model warmup failed: {e}") from e

async def warmup_embedding_model(model) -> float:
    """Warmup embedding model with a test sentence"""
    start = time.monotonic()
    _ = model.encode(["warmup sentence for inference graph initialization"])
    elapsed = time.monotonic() - start
    print(f"[warmup] Embedding model warmed up in {elapsed:.2f}s")
    return elapsed

# In startup sequence:
async def full_warmup():
    client = anthropic.AsyncAnthropic()
    tasks = [
        warmup_model_client(client),
        warmup_embedding_model(embedding_model),
    ]
    times = await asyncio.gather(*tasks)
    total = sum(times)
    print(f"[warmup] All warmup complete in {total:.2f}s total")
    return client
```

## Liveness vs. Readiness vs. Startup Probe Comparison

| Probe | Question | Action on Failure | Use For |
|---|---|---|---|
| Liveness | Is the process alive? | Restart container | Deadlocks, OOM, infinite loops |
| Readiness | Can it handle traffic? | Remove from load balancer | Model loading, DB connections, index warmup |
| Startup | Has it finished starting? | Delay liveness/readiness checks | Slow-starting containers (replaces high initialDelaySeconds) |

## Common Readiness Blockers and Their Typical Duration

| Component | Typical Warmup Time | Notes |
|---|---|---|
| HTTP server bind | < 1s | Usually instant |
| Anthropic API client | 1–3s | First request pays TLS handshake |
| Database connection pool | 1–5s | Depends on pool size and DB location |
| Redis connection | < 1s | Usually fast |
| Embedding model load | 5–60s | Depends on model size |
| FAISS / vector index | 10–120s | Depends on index size |
| LLM model weights (local) | 30–300s | GPU loading time |

## Expected Token Savings
Errors during agent startup → user retries → agent must re-explain failure: ~5,000 tokens per bad deploy
Ready-gate prevents traffic until agent is initialized: 0 startup errors, 0 user-visible failures

## Environment
- Any containerized agent deployed with Kubernetes, Docker Compose, or any orchestrator with health probes; critical for agents with heavy initialization (model loading, vector stores, DB pools)
- Source: direct experience; readiness/liveness conflation is the most common cause of errors in the first minute after every deploy
