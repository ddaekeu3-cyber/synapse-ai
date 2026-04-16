---
layout: solution
title: "Agent Doesn't Implement Health Check Endpoint for Agent Service"
category: reliability
description: "A health check endpoint lets orchestrators, load balancers, and monitoring systems verify that an agent service is alive, model connectivity is functional, and dependencies are reachable — enabling automatic restarts, traffic routing, and alerting without manual intervention."
tags: [reliability, health-check, monitoring, kubernetes, liveness, readiness]
---

## Problem

Without a health check endpoint, a deployed agent service appears healthy to infrastructure even when the Anthropic API is unreachable, a required tool is broken, memory is exhausted, or the agent is stuck in a loop. Kubernetes keeps routing traffic to dead pods. Load balancers serve failing replicas. On-call teams don't know something is wrong until users complain. A `/health` endpoint makes agent health observable and actionable.

## Solutions

### Option 1: Simple Liveness + Readiness Probe

```python
import anthropic
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import threading

client = anthropic.Anthropic()

# Global health state
_service_start_time = time.time()
_last_model_check: dict = {"time": 0, "ok": False, "latency_ms": 0}
_request_count = 0
_error_count = 0

def check_model_connectivity() -> dict:
    """Ping the model with a minimal request to verify connectivity."""
    t0 = time.time()
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{"role": "user", "content": "ping"}]
        )
        latency_ms = (time.time() - t0) * 1000
        return {"ok": True, "latency_ms": round(latency_ms), "error": None}
    except anthropic.APIConnectionError as e:
        return {"ok": False, "latency_ms": 0, "error": f"connection_error: {str(e)[:50]}"}
    except anthropic.AuthenticationError:
        return {"ok": False, "latency_ms": 0, "error": "auth_error: invalid_api_key"}
    except anthropic.APIStatusError as e:
        return {"ok": False, "latency_ms": 0, "error": f"api_error_{e.status_code}"}
    except Exception as e:
        return {"ok": False, "latency_ms": 0, "error": str(e)[:80]}

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global _last_model_check

        if self.path == "/health/live":
            # Liveness: is the process alive?
            # Fails only if process is completely stuck
            uptime = time.time() - _service_start_time
            payload = {
                "status": "alive",
                "uptime_seconds": round(uptime),
                "requests_served": _request_count
            }
            self._respond(200, payload)

        elif self.path == "/health/ready":
            # Readiness: can this instance serve traffic?
            # Checks model connectivity (cached 60s)
            now = time.time()
            if now - _last_model_check["time"] > 60:
                result = check_model_connectivity()
                _last_model_check = {**result, "time": now}

            model_ok = _last_model_check.get("ok", False)
            status_code = 200 if model_ok else 503
            payload = {
                "status": "ready" if model_ok else "not_ready",
                "model_check": {
                    "ok": model_ok,
                    "latency_ms": _last_model_check.get("latency_ms"),
                    "error": _last_model_check.get("error"),
                    "checked_at": _last_model_check.get("time")
                },
                "uptime_seconds": round(time.time() - _service_start_time)
            }
            self._respond(status_code, payload)

        elif self.path == "/health":
            # Combined health summary
            uptime = time.time() - _service_start_time
            error_rate = _error_count / max(_request_count, 1)
            payload = {
                "status": "ok" if error_rate < 0.1 else "degraded",
                "uptime_seconds": round(uptime),
                "requests": _request_count,
                "errors": _error_count,
                "error_rate": round(error_rate, 3)
            }
            self._respond(200, payload)

        else:
            self._respond(404, {"error": "not_found"})

    def _respond(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # Suppress default logging

def start_health_server(port: int = 8080):
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[Health] Server started on :{port}")
    return server

# Kubernetes probe configuration (add to deployment YAML):
# livenessProbe:
#   httpGet: {path: /health/live, port: 8080}
#   initialDelaySeconds: 5
#   periodSeconds: 10
# readinessProbe:
#   httpGet: {path: /health/ready, port: 8080}
#   initialDelaySeconds: 10
#   periodSeconds: 30

server = start_health_server(port=8080)
print("Health endpoints: /health, /health/live, /health/ready")

# Demonstrate health check
import urllib.request
for path in ["/health/live", "/health/ready", "/health"]:
    try:
        with urllib.request.urlopen(f"http://localhost:8080{path}", timeout=5) as resp:
            data = json.loads(resp.read())
            print(f"{path}: HTTP {resp.status} → {data}")
    except Exception as e:
        print(f"{path}: {e}")

server.shutdown()

# Expected Token Savings: 5 tokens/check (minimal ping); prevents bad pod traffic = saves failed user requests
# Environment: ANTHROPIC_API_KEY required, runs HTTP server on port 8080
```

### Option 2: Async Health Check with Dependency Tree

```python
import anthropic
import asyncio
import time
import json
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.AsyncAnthropic()

class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"

@dataclass
class ComponentHealth:
    name: str
    status: HealthStatus
    latency_ms: float
    message: str
    critical: bool = True  # If True, failure = overall unhealthy

@dataclass
class SystemHealth:
    overall: HealthStatus
    components: list[ComponentHealth]
    checked_at: float
    uptime_seconds: float

    def to_dict(self) -> dict:
        return {
            "status": self.overall.value,
            "uptime_seconds": round(self.uptime_seconds),
            "checked_at": self.checked_at,
            "components": [
                {
                    "name": c.name,
                    "status": c.status.value,
                    "latency_ms": round(c.latency_ms),
                    "message": c.message,
                    "critical": c.critical
                }
                for c in self.components
            ]
        }

START_TIME = time.time()

async def check_anthropic_api() -> ComponentHealth:
    t0 = time.time()
    try:
        await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{"role": "user", "content": "ok"}]
        )
        latency = (time.time() - t0) * 1000
        status = HealthStatus.HEALTHY if latency < 5000 else HealthStatus.DEGRADED
        return ComponentHealth("anthropic_api", status, latency,
            f"ok ({latency:.0f}ms)", critical=True)
    except Exception as e:
        return ComponentHealth("anthropic_api", HealthStatus.UNHEALTHY,
            (time.time() - t0) * 1000, str(e)[:80], critical=True)

async def check_memory() -> ComponentHealth:
    """Check if memory usage is within acceptable limits."""
    import sys
    t0 = time.time()
    try:
        # Simple heuristic: check if we can allocate
        _ = bytearray(1024 * 1024)  # 1MB test allocation
        latency = (time.time() - t0) * 1000
        return ComponentHealth("memory", HealthStatus.HEALTHY, latency,
            "allocation ok", critical=False)
    except MemoryError:
        return ComponentHealth("memory", HealthStatus.UNHEALTHY,
            (time.time() - t0) * 1000, "out_of_memory", critical=True)

async def check_tool_availability(tool_name: str, check_fn) -> ComponentHealth:
    """Generic tool availability check."""
    t0 = time.time()
    try:
        await check_fn()
        latency = (time.time() - t0) * 1000
        return ComponentHealth(tool_name, HealthStatus.HEALTHY, latency,
            "available", critical=False)
    except Exception as e:
        return ComponentHealth(tool_name, HealthStatus.DEGRADED,
            (time.time() - t0) * 1000, str(e)[:60], critical=False)

async def run_health_check() -> SystemHealth:
    """Run all health checks in parallel."""
    async def mock_db_check():
        await asyncio.sleep(0.01)  # Simulate DB ping

    async def mock_cache_check():
        await asyncio.sleep(0.005)

    checks = await asyncio.gather(
        check_anthropic_api(),
        check_memory(),
        check_tool_availability("database", mock_db_check),
        check_tool_availability("cache", mock_cache_check),
        return_exceptions=False
    )

    components = list(checks)

    # Determine overall status
    critical_failures = [c for c in components if c.critical and c.status == HealthStatus.UNHEALTHY]
    any_degraded = any(c.status == HealthStatus.DEGRADED for c in components)

    if critical_failures:
        overall = HealthStatus.UNHEALTHY
    elif any_degraded:
        overall = HealthStatus.DEGRADED
    else:
        overall = HealthStatus.HEALTHY

    return SystemHealth(
        overall=overall,
        components=components,
        checked_at=time.time(),
        uptime_seconds=time.time() - START_TIME
    )

async def main():
    print("[Health Check] Running dependency health check...")
    health = await run_health_check()
    report = health.to_dict()
    print(json.dumps(report, indent=2))

    # HTTP status mapping
    http_status = {
        HealthStatus.HEALTHY: 200,
        HealthStatus.DEGRADED: 200,   # Still serving but degraded
        HealthStatus.UNHEALTHY: 503   # Not ready to serve
    }[health.overall]
    print(f"\nHTTP Status: {http_status}")

asyncio.run(main())

# Expected Token Savings: Parallel checks add minimal overhead; prevents traffic to broken instances
# Environment: ANTHROPIC_API_KEY required, uses asyncio
```

### Option 3: FastAPI Health Endpoints with Metrics

```python
import anthropic
import time
import threading
from dataclasses import dataclass, field
from collections import deque

client = anthropic.Anthropic()

# Simulated metrics store (use Prometheus in production)
@dataclass
class AgentMetrics:
    request_count: int = 0
    error_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    latencies_ms: deque = field(default_factory=lambda: deque(maxlen=100))
    last_success_time: float = field(default_factory=time.time)
    last_error: str = ""
    start_time: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record_success(self, latency_ms: float, tokens_in: int, tokens_out: int):
        with self._lock:
            self.request_count += 1
            self.total_input_tokens += tokens_in
            self.total_output_tokens += tokens_out
            self.latencies_ms.append(latency_ms)
            self.last_success_time = time.time()

    def record_error(self, error: str):
        with self._lock:
            self.request_count += 1
            self.error_count += 1
            self.last_error = error[:100]

    def p99_latency(self) -> float:
        if not self.latencies_ms:
            return 0.0
        sorted_lats = sorted(self.latencies_ms)
        idx = int(len(sorted_lats) * 0.99)
        return sorted_lats[min(idx, len(sorted_lats) - 1)]

    def error_rate(self) -> float:
        return self.error_count / max(self.request_count, 1)

    def uptime_seconds(self) -> float:
        return time.time() - self.start_time

metrics = AgentMetrics()

def run_agent_request(prompt: str) -> str:
    """Run agent request with metrics tracking."""
    t0 = time.time()
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        latency = (time.time() - t0) * 1000
        metrics.record_success(latency, response.usage.input_tokens, response.usage.output_tokens)
        return response.content[0].text
    except Exception as e:
        metrics.record_error(str(e))
        raise

def get_health_response() -> dict:
    """FastAPI-compatible health response."""
    error_rate = metrics.error_rate()
    p99 = metrics.p99_latency()
    seconds_since_success = time.time() - metrics.last_success_time

    # Health determination
    issues = []
    if error_rate > 0.2:
        issues.append(f"high_error_rate:{error_rate:.0%}")
    if p99 > 10000:
        issues.append(f"high_p99:{p99:.0f}ms")
    if seconds_since_success > 300 and metrics.request_count > 0:
        issues.append(f"no_success_in_{int(seconds_since_success)}s")

    status = "healthy" if not issues else ("degraded" if error_rate < 0.5 else "unhealthy")
    http_code = 200 if status != "unhealthy" else 503

    return {
        "http_code": http_code,
        "body": {
            "status": status,
            "issues": issues,
            "uptime_seconds": round(metrics.uptime_seconds()),
            "metrics": {
                "requests_total": metrics.request_count,
                "errors_total": metrics.error_count,
                "error_rate": round(error_rate, 4),
                "p99_latency_ms": round(p99),
                "tokens_in_total": metrics.total_input_tokens,
                "tokens_out_total": metrics.total_output_tokens,
                "last_success_ago_seconds": round(seconds_since_success)
            },
            "last_error": metrics.last_error or None
        }
    }

# FastAPI route (pseudocode — install fastapi + uvicorn to run):
# @app.get("/health")
# async def health():
#     result = get_health_response()
#     return JSONResponse(status_code=result["http_code"], content=result["body"])

# Simulate some requests
for i, prompt in enumerate(["What is 2+2?", "Name a planet", "Hello"]):
    try:
        out = run_agent_request(prompt)
        print(f"[{i+1}] OK: {out[:50]}")
    except Exception as e:
        print(f"[{i+1}] Error: {e}")

import json
health = get_health_response()
print(f"\nHealth (HTTP {health['http_code']}):")
print(json.dumps(health['body'], indent=2))

# Expected Token Savings: 5 tokens/health check; metrics-driven auto-scaling saves over-provisioning cost
# Environment: ANTHROPIC_API_KEY required; use fastapi + uvicorn for HTTP server
```

### Option 4: Startup Probe with Warm-Up Validation

```python
import anthropic
import time
import json
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.Anthropic()

class StartupPhase(str, Enum):
    INITIALIZING = "initializing"
    WARMING_UP = "warming_up"
    READY = "ready"
    FAILED = "failed"

@dataclass
class StartupProbe:
    """Tracks agent service startup and warm-up state."""
    phase: StartupPhase = StartupPhase.INITIALIZING
    start_time: float = field(default_factory=time.time)
    warm_up_calls: int = 0
    warm_up_target: int = 3
    errors: list[str] = field(default_factory=list)
    model_latency_baseline_ms: float = 0.0
    ready_at: float = 0.0

    def is_ready(self) -> bool:
        return self.phase == StartupPhase.READY

    def startup_duration(self) -> float:
        if self.ready_at:
            return self.ready_at - self.start_time
        return time.time() - self.start_time

probe = StartupProbe()

def run_warm_up_call(call_number: int) -> dict:
    """Execute a warm-up API call to pre-heat connections and verify functionality."""
    t0 = time.time()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        messages=[{"role": "user", "content": f"warm-up {call_number}"}]
    )
    latency = (time.time() - t0) * 1000
    return {"latency_ms": latency, "tokens": response.usage.input_tokens + response.usage.output_tokens}

def run_startup_sequence():
    """Execute startup warm-up sequence before accepting traffic."""
    global probe

    print("[Startup] Beginning warm-up sequence...")
    probe.phase = StartupPhase.WARMING_UP
    latencies = []

    for i in range(probe.warm_up_target):
        try:
            result = run_warm_up_call(i + 1)
            latencies.append(result["latency_ms"])
            probe.warm_up_calls += 1
            print(f"[Startup] Warm-up {i+1}/{probe.warm_up_target}: {result['latency_ms']:.0f}ms")
        except Exception as e:
            probe.errors.append(f"warm_up_{i+1}: {str(e)[:50]}")
            if len(probe.errors) > probe.warm_up_target // 2:
                probe.phase = StartupPhase.FAILED
                print(f"[Startup] FAILED: too many errors during warm-up")
                return False

    if latencies:
        probe.model_latency_baseline_ms = sum(latencies) / len(latencies)

    probe.phase = StartupPhase.READY
    probe.ready_at = time.time()
    print(f"[Startup] Ready! Warm-up took {probe.startup_duration():.1f}s, "
          f"baseline latency: {probe.model_latency_baseline_ms:.0f}ms")
    return True

def startup_health_check() -> dict:
    """Kubernetes startupProbe endpoint."""
    if probe.phase == StartupPhase.READY:
        return {
            "http_code": 200,
            "body": {
                "status": "ready",
                "startup_duration_s": round(probe.startup_duration(), 2),
                "warm_up_calls": probe.warm_up_calls,
                "baseline_latency_ms": round(probe.model_latency_baseline_ms)
            }
        }
    elif probe.phase == StartupPhase.FAILED:
        return {
            "http_code": 503,
            "body": {"status": "failed", "errors": probe.errors}
        }
    else:
        return {
            "http_code": 503,  # Not ready yet — keep Kubernetes waiting
            "body": {
                "status": probe.phase.value,
                "warm_up_progress": f"{probe.warm_up_calls}/{probe.warm_up_target}"
            }
        }

# Kubernetes startupProbe (add to deployment YAML):
# startupProbe:
#   httpGet: {path: /health/startup, port: 8080}
#   failureThreshold: 30
#   periodSeconds: 5
# (Gives 30*5=150s for startup before killing pod)

success = run_startup_sequence()
health = startup_health_check()
print(f"\nStartup probe (HTTP {health['http_code']}):")
print(json.dumps(health['body'], indent=2))

# Expected Token Savings: Warm-up catches cold-start issues; prevents 503 errors during pod startup
# Environment: ANTHROPIC_API_KEY required
```

### Option 5: Circuit-Breaker-Aware Health Check

```python
import anthropic
import time
import json
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.Anthropic()

class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

@dataclass
class CircuitBreakerHealth:
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: float = 0.0
    last_success_time: float = field(default_factory=time.time)
    total_requests: int = 0
    total_failures: int = 0
    open_count: int = 0  # How many times circuit has opened

    FAILURE_THRESHOLD = 5
    RECOVERY_TIMEOUT = 30.0
    HALF_OPEN_SUCCESSES = 2

    def check_state_transition(self):
        if self.state == CircuitState.OPEN:
            if time.time() - self.last_failure_time > self.RECOVERY_TIMEOUT:
                self.state = CircuitState.HALF_OPEN
                self.success_count = 0
                print(f"[Circuit] OPEN → HALF_OPEN")

    def record_success(self):
        self.total_requests += 1
        self.last_success_time = time.time()
        if self.state == CircuitState.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= self.HALF_OPEN_SUCCESSES:
                self.state = CircuitState.CLOSED
                self.failure_count = 0
                print(f"[Circuit] HALF_OPEN → CLOSED")
        elif self.state == CircuitState.CLOSED:
            self.failure_count = max(0, self.failure_count - 1)

    def record_failure(self, error: str):
        self.total_requests += 1
        self.total_failures += 1
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.state == CircuitState.CLOSED and self.failure_count >= self.FAILURE_THRESHOLD:
            self.state = CircuitState.OPEN
            self.open_count += 1
            print(f"[Circuit] CLOSED → OPEN (failures: {self.failure_count})")
        elif self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.OPEN
            print(f"[Circuit] HALF_OPEN → OPEN (probe failed)")

    def health_dict(self) -> dict:
        self.check_state_transition()
        error_rate = self.total_failures / max(self.total_requests, 1)
        seconds_since_success = time.time() - self.last_success_time

        return {
            "circuit_breaker": {
                "state": self.state.value,
                "failure_count": self.failure_count,
                "open_count": self.open_count,
                "error_rate": round(error_rate, 4),
                "seconds_since_success": round(seconds_since_success),
                "recovery_timeout_s": self.RECOVERY_TIMEOUT
            },
            "accepting_traffic": self.state != CircuitState.OPEN,
            "http_status": 503 if self.state == CircuitState.OPEN else 200
        }

cb = CircuitBreakerHealth()

def call_with_circuit_breaker(prompt: str) -> str:
    cb.check_state_transition()

    if cb.state == CircuitState.OPEN:
        raise RuntimeError("Circuit breaker OPEN — service unavailable")

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}]
        )
        cb.record_success()
        return response.content[0].text
    except Exception as e:
        cb.record_failure(str(e))
        raise

def health_endpoint() -> dict:
    """Returns circuit breaker state as health info."""
    health = cb.health_dict()
    return health

# Simulate requests and monitor health
for i, prompt in enumerate(["What year is it?", "Name a color", "Hello world"]):
    try:
        out = call_with_circuit_breaker(prompt)
        print(f"[{i+1}] OK: {out[:40]}")
    except RuntimeError as e:
        print(f"[{i+1}] Blocked: {e}")
    except Exception as e:
        print(f"[{i+1}] Error: {e}")

print("\nHealth:")
print(json.dumps(health_endpoint(), indent=2))

# Expected Token Savings: Circuit breaker blocks calls during outages = 100% token savings when open
# Environment: ANTHROPIC_API_KEY required
```

### Option 6: Health Check with SLO Burn Rate Alerting

```python
import anthropic
import time
import json
from dataclasses import dataclass, field
from collections import deque

client = anthropic.Anthropic()

@dataclass
class SLOHealthChecker:
    """
    Health check that also computes SLO burn rate.
    If error budget is burning too fast, report degraded health.
    """
    slo_target: float = 0.99          # 99% success rate SLO
    window_1h_seconds: float = 3600
    window_24h_seconds: float = 86400
    burn_rate_threshold_fast: float = 14.4   # Fast burn (1h window)
    burn_rate_threshold_slow: float = 1.0     # Slow burn (24h window)

    # Rolling window storage
    events_1h: deque = field(default_factory=lambda: deque())    # (timestamp, is_success)
    events_24h: deque = field(default_factory=lambda: deque())

    start_time: float = field(default_factory=time.time)
    total_requests: int = 0
    total_errors: int = 0

    def record_event(self, success: bool):
        now = time.time()
        self.total_requests += 1
        if not success:
            self.total_errors += 1

        entry = (now, success)
        self.events_1h.append(entry)
        self.events_24h.append(entry)

        # Prune old events
        cutoff_1h = now - self.window_1h_seconds
        cutoff_24h = now - self.window_24h_seconds
        while self.events_1h and self.events_1h[0][0] < cutoff_1h:
            self.events_1h.popleft()
        while self.events_24h and self.events_24h[0][0] < cutoff_24h:
            self.events_24h.popleft()

    def _error_rate(self, window: deque) -> float:
        if not window:
            return 0.0
        errors = sum(1 for _, ok in window if not ok)
        return errors / len(window)

    def _burn_rate(self, window: deque) -> float:
        """Burn rate = actual error rate / (1 - SLO target)."""
        error_rate = self._error_rate(window)
        error_budget = 1.0 - self.slo_target
        return error_rate / max(error_budget, 1e-10)

    def health_status(self) -> dict:
        burn_1h = self._burn_rate(self.events_1h)
        burn_24h = self._burn_rate(self.events_24h)

        # SLO alert conditions
        alerts = []
        if burn_1h > self.burn_rate_threshold_fast:
            alerts.append(f"fast_burn:{burn_1h:.1f}x (1h window)")
        if burn_24h > self.burn_rate_threshold_slow:
            alerts.append(f"slow_burn:{burn_24h:.1f}x (24h window)")

        # Error budget remaining
        uptime = time.time() - self.start_time
        budget_consumed = self.total_errors / max(self.total_requests, 1) / (1 - self.slo_target)
        budget_remaining_pct = max(0, (1 - budget_consumed) * 100)

        if alerts:
            status = "unhealthy" if burn_1h > self.burn_rate_threshold_fast * 2 else "degraded"
        elif budget_remaining_pct < 10:
            status = "degraded"
        else:
            status = "healthy"

        return {
            "status": status,
            "http_code": 503 if status == "unhealthy" else 200,
            "slo": {
                "target": f"{self.slo_target:.1%}",
                "error_budget_remaining_pct": round(budget_remaining_pct, 1),
                "burn_rate_1h": round(burn_1h, 2),
                "burn_rate_24h": round(burn_24h, 2),
                "fast_burn_threshold": self.burn_rate_threshold_fast,
                "slow_burn_threshold": self.burn_rate_threshold_slow,
            },
            "alerts": alerts,
            "window_counts": {
                "1h": len(self.events_1h),
                "24h": len(self.events_24h),
            },
            "totals": {
                "requests": self.total_requests,
                "errors": self.total_errors,
                "uptime_s": round(uptime)
            }
        }

checker = SLOHealthChecker(slo_target=0.99)

def slo_tracked_call(prompt: str) -> str:
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=50,
            messages=[{"role": "user", "content": prompt}]
        )
        checker.record_event(success=True)
        return response.content[0].text
    except Exception as e:
        checker.record_event(success=False)
        raise

# Make some calls
for prompt in ["What is 5+5?", "Name a fruit", "Say hi"]:
    try:
        out = slo_tracked_call(prompt)
        print(f"OK: {out[:40]}")
    except Exception as e:
        print(f"Error: {e}")

health = checker.health_status()
print(f"\nHealth (HTTP {health['http_code']}):")
print(json.dumps(health, indent=2))

# Expected Token Savings: Burn rate alerting prevents SLO breach = avoids incident cost
# Environment: ANTHROPIC_API_KEY required
```

## Comparison

| Option | Probe Type | Dependency Checks | Metrics | Best Use Case |
|--------|-----------|------------------|---------|---------------|
| Liveness + Readiness | HTTP server | Model connectivity | Basic | Kubernetes pod health |
| Async Dependency Tree | Async parallel | Multi-component | Per-component | Microservice with many deps |
| FastAPI + Metrics | HTTP route | Model ping | P99, error rate | Production REST API services |
| Startup Probe + Warm-Up | HTTP startup | Model latency baseline | Warm-up timing | Cold-start sensitive services |
| Circuit Breaker Health | State-based | Self-contained | Circuit state | Services with model instability |
| SLO Burn Rate Health | Rolling window | Model calls | Error budget | SLO-driven on-call alerting |
