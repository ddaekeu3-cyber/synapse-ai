---
layout: solution
title: "Agent Doesn't Implement Resource Limits in Container"
category: docker
description: "Agents running without CPU and memory limits in Docker or Kubernetes can consume all host resources, starving other services and triggering OOM kills."
tags: [docker, kubernetes, resource-limits, memory, cpu, oom]
---

# Agent Doesn't Implement Resource Limits in Container

An agent without resource limits is a resource liability. A prompt that triggers a large context window, an infinite loop in a tool call, or an embedding computation spike can consume all available host memory, cause OOM kills across the machine, and take down unrelated services. Docker and Kubernetes both have straightforward limit mechanisms — they're just rarely set.

## Why This Happens

Limits aren't required to get a container running. Developers set them "later" and later never comes. Default Docker behavior has no limits. Default Kubernetes behavior has no limits unless a LimitRange is set.

---

## Option 1: Docker Compose Resource Limits

Set per-service CPU and memory limits in `docker-compose.yml` using the v3 `deploy` spec.

```yaml
# docker-compose.yml
version: "3.9"

services:
  agent:
    build: .
    image: synapse-agent:latest
    environment:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
    ports:
      - "8000:8000"
    deploy:
      resources:
        limits:
          cpus: "1.0"       # max 1 full CPU core
          memory: "512M"    # hard memory ceiling
        reservations:
          cpus: "0.25"      # guaranteed CPU share
          memory: "128M"    # guaranteed RAM
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s

  worker:
    build: .
    command: python worker.py
    environment:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
    deploy:
      resources:
        limits:
          cpus: "2.0"
          memory: "1G"
        reservations:
          cpus: "0.5"
          memory: "256M"
    restart: unless-stopped
```

```python
# agent_server.py — the containerized agent
import anthropic
from fastapi import FastAPI

app = FastAPI()
client = anthropic.Anthropic()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/run")
def run(prompt: str):
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return {"result": response.content[0].text}
```

**Expected Token Savings:** No direct savings; prevents OOM kills that would abort in-flight requests mid-generation.

**Environment:** Docker Compose; local dev and single-host production deployments.

---

## Option 2: Dockerfile Runtime Defaults with ENV Documentation

Document expected resource requirements in the Dockerfile and set runtime defaults via environment variables.

```dockerfile
# Dockerfile
FROM python:3.12-slim AS base

# Document resource requirements
LABEL org.opencontainers.image.description="Synapse AI Agent"
LABEL resource.memory.min="128Mi"
LABEL resource.memory.recommended="512Mi"
LABEL resource.cpu.min="0.25"
LABEL resource.cpu.recommended="1.0"

WORKDIR /app

RUN addgroup --system agent && adduser --system --ingroup agent agent

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=agent:agent . .

USER agent

# Limit Python's own memory behavior
ENV PYTHONUNBUFFERED=1
# Limit asyncio thread pool (default is min(32, os.cpu_count() + 4))
ENV PYTHONMAXTHREADS=4
# Gunicorn worker count — tie to container CPU limit
ENV WORKERS=2
ENV MAX_REQUESTS=1000
ENV MAX_REQUESTS_JITTER=100

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1

CMD ["sh", "-c", "gunicorn agent_server:app -w $WORKERS -k uvicorn.workers.UvicornWorker --max-requests $MAX_REQUESTS --max-requests-jitter $MAX_REQUESTS_JITTER -b 0.0.0.0:8000"]
```

```bash
# Run with explicit limits
docker run \
  --memory="512m" \
  --memory-swap="512m" \
  --cpus="1.0" \
  --memory-reservation="128m" \
  -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  -p 8000:8000 \
  synapse-agent:latest
```

**Expected Token Savings:** `--memory-swap="512m"` (equal to memory) disables swap, preventing slow OOM death from disk I/O.

**Environment:** Docker CLI; any single-container deployment.

---

## Option 3: Kubernetes Deployment with Resource Requests and Limits

Full Kubernetes manifest with `resources`, `livenessProbe`, `readinessProbe`, and HPA.

```yaml
# k8s/agent-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: synapse-agent
  labels:
    app: synapse-agent
spec:
  replicas: 2
  selector:
    matchLabels:
      app: synapse-agent
  template:
    metadata:
      labels:
        app: synapse-agent
    spec:
      containers:
        - name: agent
          image: synapse-agent:latest
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 8000
          env:
            - name: ANTHROPIC_API_KEY
              valueFrom:
                secretKeyRef:
                  name: anthropic-secret
                  key: api-key
          resources:
            requests:
              memory: "128Mi"   # guaranteed allocation
              cpu: "250m"       # 0.25 cores guaranteed
            limits:
              memory: "512Mi"   # hard cap — OOM kill if exceeded
              cpu: "1000m"      # 1 core max
          livenessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 15
            periodSeconds: 20
            failureThreshold: 3
          readinessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 5
            periodSeconds: 10
          lifecycle:
            preStop:
              exec:
                command: ["/bin/sh", "-c", "sleep 5"]
      terminationGracePeriodSeconds: 30

---
# Horizontal Pod Autoscaler
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: synapse-agent-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: synapse-agent
  minReplicas: 2
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
    - type: Resource
      resource:
        name: memory
        target:
          type: Utilization
          averageUtilization: 80

---
# LimitRange to enforce defaults cluster-wide
apiVersion: v1
kind: LimitRange
metadata:
  name: agent-limit-range
  namespace: synapse
spec:
  limits:
    - type: Container
      default:
        memory: "256Mi"
        cpu: "500m"
      defaultRequest:
        memory: "64Mi"
        cpu: "100m"
      max:
        memory: "2Gi"
        cpu: "2000m"
```

**Expected Token Savings:** K8s OOM kill on limit breach is cleaner than host-level kill; HPA scales out instead of letting one pod consume all memory.

**Environment:** Kubernetes; production multi-replica deployments.

---

## Option 4: Python-Level Memory Guard

Supplement container limits with an in-process memory monitor that gracefully shuts down the worker before the OOM killer acts.

```python
import os
import sys
import asyncio
import threading
import resource
import anthropic
from fastapi import FastAPI

MAX_MEMORY_MB = int(os.environ.get("MAX_MEMORY_MB", "400"))

app = FastAPI()
client = anthropic.AsyncAnthropic()


def get_memory_mb() -> float:
    """Get current process RSS in megabytes."""
    try:
        import resource as _resource
        usage = _resource.getrusage(_resource.RUSAGE_SELF)
        # ru_maxrss is in KB on Linux, bytes on macOS
        if sys.platform == "darwin":
            return usage.ru_maxrss / 1024 / 1024
        return usage.ru_maxrss / 1024
    except Exception:
        return 0.0


def memory_watchdog(interval: float = 5.0):
    """Background thread that exits the process if memory exceeds limit."""
    while True:
        mem = get_memory_mb()
        if mem > MAX_MEMORY_MB:
            print(
                f"[Watchdog] Memory limit exceeded: {mem:.1f}MB > {MAX_MEMORY_MB}MB. "
                f"Initiating graceful shutdown.",
                file=sys.stderr,
            )
            # Give Docker/K8s time to route traffic away before dying
            os.kill(os.getpid(), __import__("signal").SIGTERM)
            break
        threading.Event().wait(interval)


@app.on_event("startup")
async def startup():
    thread = threading.Thread(target=memory_watchdog, daemon=True)
    thread.start()
    print(f"[Watchdog] Memory limit: {MAX_MEMORY_MB}MB")


@app.get("/health")
async def health():
    mem = get_memory_mb()
    return {
        "status": "ok",
        "memory_mb": round(mem, 1),
        "limit_mb": MAX_MEMORY_MB,
    }


@app.post("/run")
async def run(prompt: str):
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return {"result": response.content[0].text}
```

**Expected Token Savings:** Graceful SIGTERM before OOM kill means in-flight requests can finish; reduces hard failures.

**Environment:** Any containerized Python agent; complement to Docker/K8s limits.

---

## Option 5: ulimit-Based Hard Limits in Entrypoint

Set `ulimit` for virtual address space in the container entrypoint to cap memory at the OS level.

```bash
#!/bin/bash
# docker-entrypoint.sh

set -euo pipefail

# Set soft and hard virtual memory limit (in KB)
MEMORY_LIMIT_MB=${MEMORY_LIMIT_MB:-512}
MEMORY_LIMIT_KB=$((MEMORY_LIMIT_MB * 1024))

echo "[entrypoint] Setting virtual memory limit: ${MEMORY_LIMIT_MB}MB"
ulimit -v $MEMORY_LIMIT_KB 2>/dev/null || echo "[entrypoint] ulimit -v not supported, skipping"

# Set open file descriptor limit
ulimit -n ${MAX_OPEN_FILES:-65536} 2>/dev/null || true

# Set max number of processes
ulimit -u ${MAX_PROCS:-256} 2>/dev/null || true

echo "[entrypoint] Resource limits:"
ulimit -a 2>/dev/null | grep -E "virtual|open files|max user processes" || true

exec "$@"
```

```dockerfile
# Dockerfile addition
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "agent_server:app", "--host", "0.0.0.0", "--port", "8000"]
```

```yaml
# docker-compose.yml addition
services:
  agent:
    build: .
    environment:
      - MEMORY_LIMIT_MB=512
      - MAX_OPEN_FILES=65536
    ulimits:
      nofile:
        soft: 65536
        hard: 65536
```

**Expected Token Savings:** OS-level enforcement independent of Docker runtime; prevents memory growth even if --memory flag is omitted.

**Environment:** Any Linux container; works even without Docker's memory limit flags.

---

## Option 6: Resource Limit Validation at Agent Startup

Verify that appropriate limits are in place at startup and refuse to start if running without any constraints.

```python
import os
import sys
import subprocess
import anthropic
from fastapi import FastAPI

app = FastAPI()
client = anthropic.Anthropic()

REQUIRE_LIMITS = os.environ.get("REQUIRE_RESOURCE_LIMITS", "true").lower() == "true"


def get_cgroup_memory_limit() -> int | None:
    """Read memory limit from cgroup v2 (Kubernetes/Docker sets this)."""
    for path in [
        "/sys/fs/cgroup/memory.max",        # cgroup v2
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",  # cgroup v1
    ]:
        try:
            with open(path) as f:
                val = f.read().strip()
                if val == "max":
                    return None  # unlimited
                limit = int(val)
                # cgroup v1 sets 9223372036854771712 for "unlimited"
                if limit > 2**62:
                    return None
                return limit
        except (FileNotFoundError, ValueError):
            continue
    return None


def get_cpu_quota() -> float | None:
    """Read CPU quota from cgroup v2."""
    try:
        with open("/sys/fs/cgroup/cpu.max") as f:
            val = f.read().strip()
            if val == "max":
                return None
            quota, period = val.split()
            return int(quota) / int(period)
    except Exception:
        return None


def validate_resource_limits():
    """Check that container limits are set; warn or fail if not."""
    issues = []

    mem_limit = get_cgroup_memory_limit()
    if mem_limit is None:
        issues.append("No memory limit detected (cgroup memory.max = unlimited)")
    else:
        mem_mb = mem_limit / 1024 / 1024
        print(f"[startup] Memory limit: {mem_mb:.0f}MB")
        if mem_mb > 4096:
            issues.append(f"Memory limit {mem_mb:.0f}MB seems too high for a single agent")

    cpu_quota = get_cpu_quota()
    if cpu_quota is None:
        issues.append("No CPU quota detected (cpu.max = unlimited)")
    else:
        print(f"[startup] CPU quota: {cpu_quota:.2f} cores")

    if issues:
        for issue in issues:
            print(f"[startup] WARNING: {issue}", file=sys.stderr)

        if REQUIRE_LIMITS:
            print(
                "[startup] FATAL: Resource limits required but not set. "
                "Set REQUIRE_RESOURCE_LIMITS=false to bypass.",
                file=sys.stderr,
            )
            sys.exit(1)


@app.on_event("startup")
async def startup():
    validate_resource_limits()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/run")
def run(prompt: str):
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return {"result": response.content[0].text}
```

**Expected Token Savings:** Prevents accidental unlimited-resource deployments; catches misconfigured K8s manifests before they cause incidents.

**Environment:** Kubernetes and Docker; containers running on shared infrastructure.

---

## Comparison

| Option | Enforcement Layer | OOM Behavior | Startup Validation | Scales Automatically |
|--------|------------------|--------------|-------------------|----------------------|
| 1. Docker Compose limits | Docker runtime | Hard OOM kill | No | No |
| 2. Dockerfile ENV + CLI flags | Docker runtime | Hard OOM kill | No | No |
| 3. Kubernetes resources + HPA | K8s scheduler | Pod OOM kill, reschedule | No | Yes (HPA) |
| 4. Python memory watchdog | Process | Graceful SIGTERM | No | No |
| 5. ulimit entrypoint | Linux kernel | SIGKILL on alloc | No | No |
| 6. Startup validation | Application | Refuse to start | Yes | No |
