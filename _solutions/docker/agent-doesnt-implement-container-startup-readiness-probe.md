---
title: "Agent Doesn't Implement Container Startup Readiness Probe"
description: "How to implement readiness probes for containerized AI agents so orchestrators only route traffic after the agent has fully initialized—including model client warmup and tool registration."
categories: [docker]
difficulty: intermediate
---

A containerized agent may start its HTTP server before it has finished initializing—loading configs, warming up the API client, registering tools, or verifying API key validity. Without a readiness probe, orchestrators route traffic to half-initialized agents, causing failed requests and confusing errors.

## Solution 1: HTTP Readiness Endpoint with Initialization Gate

Expose a `/ready` endpoint that returns 200 only after all initialization steps have completed.

```python
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Awaitable
from fastapi import FastAPI, Response
import anthropic

app = FastAPI()
client = anthropic.AsyncAnthropic()


class InitState(Enum):
    STARTING = "starting"
    READY = "ready"
    FAILED = "failed"


@dataclass
class ReadinessGate:
    state: InitState = InitState.STARTING
    checks: dict[str, bool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    ready_at: float | None = None

    @property
    def is_ready(self) -> bool:
        return self.state == InitState.READY

    def mark_check(self, name: str, passed: bool, error: str | None = None):
        self.checks[name] = passed
        if not passed and error:
            self.errors.append(f"{name}: {error}")

    def finalize(self):
        if all(self.checks.values()) and self.checks:
            self.state = InitState.READY
            self.ready_at = time.monotonic()
        else:
            self.state = InitState.FAILED


gate = ReadinessGate()


async def check_api_connectivity() -> tuple[bool, str | None]:
    """Verify the Anthropic API is reachable and the key is valid."""
    try:
        await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
        return True, None
    except Exception as e:
        return False, str(e)


async def check_tool_registry() -> tuple[bool, str | None]:
    """Verify required tools are registered and accessible."""
    # Simulate tool registration check
    await asyncio.sleep(0.05)
    return True, None


async def check_config_loaded() -> tuple[bool, str | None]:
    """Verify configuration is valid and complete."""
    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False, "ANTHROPIC_API_KEY not set"
    return True, None


INIT_CHECKS: list[tuple[str, Callable[[], Awaitable[tuple[bool, str | None]]]]] = [
    ("config_loaded", check_config_loaded),
    ("api_connectivity", check_api_connectivity),
    ("tool_registry", check_tool_registry),
]


@app.on_event("startup")
async def initialize():
    print("[startup] Beginning initialization...")
    for name, check_fn in INIT_CHECKS:
        print(f"[startup] Checking {name}...")
        passed, error = await check_fn()
        gate.mark_check(name, passed, error)
        if not passed:
            print(f"[startup] FAILED: {name}: {error}")
        else:
            print(f"[startup] OK: {name}")
    gate.finalize()
    print(f"[startup] State: {gate.state.value}")


@app.get("/ready")
async def readiness_probe(response: Response):
    if gate.is_ready:
        return {
            "status": "ready",
            "checks": gate.checks,
            "ready_at": gate.ready_at,
        }
    response.status_code = 503
    return {
        "status": gate.state.value,
        "checks": gate.checks,
        "errors": gate.errors,
    }


@app.get("/live")
async def liveness_probe():
    """Liveness probe — always returns 200 if the process is alive."""
    return {"status": "alive"}


@app.post("/chat")
async def chat(message: str, response: Response):
    if not gate.is_ready:
        response.status_code = 503
        return {"error": "Agent not ready", "state": gate.state.value}
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": message}],
    )
    return {"reply": resp.content[0].text}
```

## Solution 2: Docker HEALTHCHECK with Shell Script

Write a shell script that docker's HEALTHCHECK directive calls to determine container readiness.

```bash
#!/bin/sh
# /app/healthcheck.sh
# Used in Dockerfile: HEALTHCHECK CMD /app/healthcheck.sh

set -e

READY_URL="${READY_URL:-http://localhost:8000/ready}"
TIMEOUT="${HEALTH_TIMEOUT:-5}"

# Attempt HTTP check
STATUS=$(curl -sf --max-time "$TIMEOUT" -o /dev/null -w "%{http_code}" "$READY_URL" 2>/dev/null) || STATUS=000

if [ "$STATUS" = "200" ]; then
  exit 0
else
  echo "Readiness check failed: HTTP $STATUS" >&2
  exit 1
fi
```

```dockerfile
# Dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Expose readiness port
EXPOSE 8000

# Liveness: process-level (fast, every 10s)
# Readiness: HTTP endpoint (waits for full init)
HEALTHCHECK \
  --interval=10s \
  --timeout=5s \
  --start-period=30s \
  --retries=3 \
  CMD /app/healthcheck.sh

CMD ["uvicorn", "agent:app", "--host", "0.0.0.0", "--port", "8000"]
```

```python
# agent.py — referenced by Dockerfile above
import asyncio
import os
import time
from fastapi import FastAPI, Response
import anthropic

app = FastAPI()
client = anthropic.AsyncAnthropic()
_ready = False
_ready_at: float | None = None


@app.on_event("startup")
async def startup():
    global _ready, _ready_at
    # Warm up: verify API key works
    try:
        await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            messages=[{"role": "user", "content": "hi"}],
        )
        _ready = True
        _ready_at = time.monotonic()
        print("[ready] Agent initialized successfully")
    except Exception as e:
        print(f"[error] Initialization failed: {e}")


@app.get("/ready")
async def ready(response: Response):
    if _ready:
        return {"status": "ready", "uptime": time.monotonic() - (_ready_at or 0)}
    response.status_code = 503
    return {"status": "initializing"}
```

## Solution 3: Kubernetes Readiness and Liveness Probes

Define separate readiness and liveness probes in a Kubernetes Deployment manifest.

```yaml
# k8s-agent-deployment.yaml
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
        image: myregistry/ai-agent:latest
        ports:
        - containerPort: 8000
        env:
        - name: ANTHROPIC_API_KEY
          valueFrom:
            secretKeyRef:
              name: anthropic-secret
              key: api-key
        # Liveness: restart if process hangs (checked every 15s after 30s startup)
        livenessProbe:
          httpGet:
            path: /live
            port: 8000
          initialDelaySeconds: 10
          periodSeconds: 15
          timeoutSeconds: 5
          failureThreshold: 3
        # Readiness: don't route traffic until agent is initialized
        readinessProbe:
          httpGet:
            path: /ready
            port: 8000
          initialDelaySeconds: 5
          periodSeconds: 5
          timeoutSeconds: 5
          failureThreshold: 6       # Allow up to 30s for init (6 * 5s)
          successThreshold: 1
        # Startup: give extra time for first boot (model downloads, etc.)
        startupProbe:
          httpGet:
            path: /ready
            port: 8000
          failureThreshold: 30      # 30 * 2s = 60s startup budget
          periodSeconds: 2
        resources:
          requests:
            memory: "256Mi"
            cpu: "250m"
          limits:
            memory: "512Mi"
            cpu: "1"
```

```python
# Corresponding FastAPI app with all three probe endpoints
import asyncio
import time
from fastapi import FastAPI, Response
import anthropic

app = FastAPI()
client = anthropic.AsyncAnthropic()

_state = {
    "ready": False,
    "live": True,
    "start_time": time.monotonic(),
    "errors": [],
}


@app.on_event("startup")
async def initialize():
    try:
        # Simulate initialization work
        await asyncio.sleep(0.5)  # Config loading
        await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            messages=[{"role": "user", "content": "init"}],
        )
        _state["ready"] = True
        print("[k8s] Agent ready")
    except Exception as e:
        _state["errors"].append(str(e))
        print(f"[k8s] Init failed: {e}")


@app.get("/live")
async def liveness():
    """Always 200 unless process is stuck."""
    return {"status": "alive", "uptime": time.monotonic() - _state["start_time"]}


@app.get("/ready")
async def readiness(response: Response):
    """200 only when fully initialized."""
    if _state["ready"]:
        return {"status": "ready"}
    response.status_code = 503
    return {"status": "not_ready", "errors": _state["errors"]}
```

## Solution 4: Async Initialization Queue with Dependency Ordering

Run initialization tasks in dependency order, with parallel execution of independent tasks.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Awaitable
import anthropic

client = anthropic.AsyncAnthropic()


@dataclass
class InitTask:
    name: str
    fn: Callable[[], Awaitable[bool]]
    depends_on: list[str] = field(default_factory=list)
    timeout_seconds: float = 30.0
    required: bool = True
    status: str = "pending"   # pending | running | done | failed | skipped
    duration_ms: float = 0.0


class InitializationOrchestrator:
    def __init__(self):
        self._tasks: dict[str, InitTask] = {}
        self._errors: list[str] = []
        self._ready = asyncio.Event()

    def register(self, task: InitTask):
        self._tasks[task.name] = task

    async def _run_task(self, task: InitTask) -> bool:
        # Wait for dependencies
        for dep in task.depends_on:
            dep_task = self._tasks.get(dep)
            if dep_task and dep_task.status == "failed":
                task.status = "skipped"
                return False

        task.status = "running"
        start = time.monotonic()
        try:
            result = await asyncio.wait_for(task.fn(), timeout=task.timeout_seconds)
            task.status = "done" if result else "failed"
            task.duration_ms = (time.monotonic() - start) * 1000
            return result
        except asyncio.TimeoutError:
            task.status = "failed"
            self._errors.append(f"{task.name}: timed out after {task.timeout_seconds}s")
            return False
        except Exception as e:
            task.status = "failed"
            self._errors.append(f"{task.name}: {e}")
            return False

    async def run_all(self) -> bool:
        # Topological execution
        completed: set[str] = set()

        for _ in range(len(self._tasks) + 1):
            for name, task in self._tasks.items():
                if name in completed:
                    continue
                deps_done = all(
                    self._tasks.get(d, InitTask("", lambda: asyncio.sleep(0))).status in ("done", "skipped")
                    or d not in self._tasks
                    for d in task.depends_on
                )
                if deps_done and task.status == "pending":
                    await self._run_task(task)
                    completed.add(name)

        failed_required = [t for t in self._tasks.values() if t.required and t.status != "done"]
        if not failed_required:
            self._ready.set()
            return True
        return False

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    def status_summary(self) -> dict:
        return {
            "ready": self.is_ready,
            "tasks": {n: t.status for n, t in self._tasks.items()},
            "errors": self._errors,
        }


# Example usage
async def check_api() -> bool:
    await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1,
        messages=[{"role": "user", "content": "ping"}],
    )
    return True


async def load_tools() -> bool:
    await asyncio.sleep(0.1)
    return True


async def warm_cache() -> bool:
    await asyncio.sleep(0.05)
    return True


async def main():
    orchestrator = InitializationOrchestrator()
    orchestrator.register(InitTask("api_check", check_api, timeout_seconds=10))
    orchestrator.register(InitTask("tool_load", load_tools, depends_on=["api_check"]))
    orchestrator.register(InitTask("cache_warm", warm_cache, depends_on=["api_check"], required=False))

    ready = await orchestrator.run_all()
    print(f"Initialization {'complete' if ready else 'FAILED'}")
    print(orchestrator.status_summary())


asyncio.run(main())
```

## Solution 5: Graceful Degradation Readiness (Partial Ready)

Allow the agent to serve limited functionality while optional initialization tasks are still running.

```python
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from fastapi import FastAPI, Response
import anthropic

app = FastAPI()
client = anthropic.AsyncAnthropic()


class CapabilitySet(Enum):
    NONE = "none"
    BASIC = "basic"          # Core API only
    STANDARD = "standard"    # + tools
    FULL = "full"            # + caching, analytics


@dataclass
class GradualReadinessState:
    capability: CapabilitySet = CapabilitySet.NONE
    completed: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    start_time: float = field(default_factory=time.monotonic)


state = GradualReadinessState()


async def init_basic():
    await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1,
        messages=[{"role": "user", "content": "ping"}],
    )
    state.completed.append("api_connectivity")
    state.capability = CapabilitySet.BASIC


async def init_tools():
    await asyncio.sleep(0.2)  # Simulate tool loading
    state.completed.append("tool_registry")
    state.capability = CapabilitySet.STANDARD


async def init_optional():
    await asyncio.sleep(0.5)  # Simulate cache warmup
    state.completed.append("cache_warmup")
    state.capability = CapabilitySet.FULL


@app.on_event("startup")
async def startup():
    state.pending = ["api_connectivity", "tool_registry", "cache_warmup"]
    try:
        await init_basic()
        state.pending.remove("api_connectivity")
    except Exception as e:
        print(f"[startup] Basic init failed: {e}")
        return

    # Run remaining tasks in background
    asyncio.create_task(_continue_init())


async def _continue_init():
    await init_tools()
    state.pending.remove("tool_registry")
    await init_optional()
    state.pending.remove("cache_warmup")
    print(f"[startup] Full init complete: {state.capability.value}")


@app.get("/ready")
async def readiness(response: Response):
    if state.capability == CapabilitySet.NONE:
        response.status_code = 503
        return {"status": "not_ready", "capability": state.capability.value}
    # 200 even if not FULL — partial readiness is acceptable
    return {
        "status": "ready",
        "capability": state.capability.value,
        "completed": state.completed,
        "pending": state.pending,
        "uptime_seconds": time.monotonic() - state.start_time,
    }


@app.post("/chat")
async def chat(message: str, response: Response):
    if state.capability == CapabilitySet.NONE:
        response.status_code = 503
        return {"error": "Not ready"}
    if state.capability == CapabilitySet.BASIC:
        response.status_code = 206  # Partial content — limited features
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": message}],
    )
    return {"reply": resp.content[0].text, "capability": state.capability.value}
```

## Solution 6: Readiness Probe with Circuit Breaker

Combine the readiness gate with a circuit breaker so the agent can re-enter "not ready" state if downstream dependencies degrade.

```python
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from fastapi import FastAPI, Response
import anthropic

app = FastAPI()
client = anthropic.AsyncAnthropic()


class CircuitState(Enum):
    CLOSED = "closed"       # Healthy
    OPEN = "open"           # Failing — block requests
    HALF_OPEN = "half_open" # Testing recovery


@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = 3
    recovery_timeout: float = 30.0
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    last_failure: float = 0.0
    last_success: float = field(default_factory=time.monotonic)

    def record_success(self):
        self.failure_count = 0
        self.state = CircuitState.CLOSED
        self.last_success = time.monotonic()

    def record_failure(self):
        self.failure_count += 1
        self.last_failure = time.monotonic()
        if self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN

    def should_allow(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if time.monotonic() - self.last_failure > self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                return True  # Allow one probe
            return False
        return True  # HALF_OPEN — allow probe


api_circuit = CircuitBreaker("anthropic_api")
_init_complete = False


async def probe_api() -> bool:
    if not api_circuit.should_allow():
        return False
    try:
        await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
        api_circuit.record_success()
        return True
    except Exception:
        api_circuit.record_failure()
        return False


@app.on_event("startup")
async def startup():
    global _init_complete
    ok = await probe_api()
    if ok:
        _init_complete = True
        asyncio.create_task(periodic_health_check())


async def periodic_health_check():
    """Re-probe API health every 60 seconds."""
    while True:
        await asyncio.sleep(60)
        await probe_api()


@app.get("/ready")
async def readiness(response: Response):
    api_ok = api_circuit.state == CircuitState.CLOSED
    overall_ready = _init_complete and api_ok

    if not overall_ready:
        response.status_code = 503

    return {
        "ready": overall_ready,
        "api_circuit": {
            "state": api_circuit.state.value,
            "failures": api_circuit.failure_count,
        },
        "init_complete": _init_complete,
    }
```

## Comparison

| Solution | Orchestrator | Partial readiness | Re-probing | Best for |
|---|---|---|---|---|
| **HTTP readiness gate** | Any (HTTP) | No | No | Simple FastAPI agents |
| **Docker HEALTHCHECK** | Docker Swarm | No | Yes (automatic) | Single-container deployments |
| **Kubernetes probes** | Kubernetes | No | Yes (automatic) | K8s deployments |
| **Async init queue** | Any | No | No | Complex dependency chains |
| **Gradual degradation** | Any (HTTP) | Yes | No | Agents with optional features |
| **Circuit breaker probe** | Any (HTTP) | No | Yes (active) | High-availability production |

Start with **HTTP readiness gate** (Solution 1) — it works with any orchestrator and requires no changes to the container runtime. Add **Kubernetes probes** (Solution 3) when deploying to K8s. Use **gradual degradation** (Solution 5) when the agent can provide partial value while still initializing.
