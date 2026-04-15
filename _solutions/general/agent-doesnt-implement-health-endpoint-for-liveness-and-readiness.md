---
layout: solution
title: "Agent Doesn't Implement Health Endpoint for Liveness and Readiness"
category: general
description: "Production AI agents lack /health, /live, and /ready endpoints, making it impossible for load balancers, Kubernetes, or monitoring systems to detect crashes, dependency failures, or warm-up state. This causes silent traffic routing to dead agents."
tags: [health-check, liveness, readiness, kubernetes, monitoring, production, fastapi]
---

## Problem

AI agents deployed in production rarely expose structured health endpoints. Without `/health`, `/live`, and `/ready` endpoints, Kubernetes liveness probes restart healthy pods unnecessarily, load balancers route to uninitialized agents, and dependency failures (DB down, API key expired) go undetected until user complaints arrive. A crashed agent that still holds a socket appears healthy to the network layer.

## Solutions

### Option 1: Simple HTTP Health Handler with Anthropic Ping

```python
import anthropic
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
import json

client = anthropic.Anthropic()
_last_api_check: dict = {"ok": False, "checked_at": 0.0}
_lock = threading.Lock()

def _refresh_api_health() -> bool:
    """Ping Anthropic with a minimal call to confirm API key is valid."""
    try:
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
        return True
    except Exception:
        return False

def get_api_health(ttl: float = 60.0) -> bool:
    now = time.time()
    with _lock:
        if now - _last_api_check["checked_at"] > ttl:
            _last_api_check["ok"] = _refresh_api_health()
            _last_api_check["checked_at"] = now
        return _last_api_check["ok"]

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/live":
            # Liveness: process is running
            body = json.dumps({"status": "alive"}).encode()
            self.send_response(200)
        elif self.path in ("/health", "/ready"):
            # Readiness: dependencies are reachable
            api_ok = get_api_health()
            body = json.dumps({"status": "ok" if api_ok else "degraded", "anthropic_api": api_ok}).encode()
            self.send_response(200 if api_ok else 503)
        else:
            body = b"not found"
            self.send_response(404)

        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # suppress default access logs

def start_health_server(port: int = 8080):
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"Health server listening on :{port}")
    return server

if __name__ == "__main__":
    start_health_server()
    # Agent main loop
    while True:
        time.sleep(5)

# Expected Token Savings: ~1 token per health check (haiku ping); TTL prevents constant billing
# Environment: any Python agent; Kubernetes liveness + readiness probe compatible
```

### Option 2: FastAPI Health Router with Dependency Checks

```python
import asyncio
import time
from contextlib import asynccontextmanager
import anthropic
from fastapi import FastAPI, Response
from pydantic import BaseModel

class HealthStatus(BaseModel):
    status: str
    checks: dict[str, bool]
    uptime_seconds: float

_start_time = time.time()
_client = anthropic.AsyncAnthropic()

async def check_anthropic() -> bool:
    try:
        await asyncio.wait_for(
            _client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            ),
            timeout=5.0,
        )
        return True
    except Exception:
        return False

# Cache so we don't hammer Anthropic on every /health probe
_cache: dict = {"result": None, "expires": 0.0}
_cache_lock = asyncio.Lock()

async def cached_anthropic_check(ttl: float = 30.0) -> bool:
    async with _cache_lock:
        if time.time() > _cache["expires"]:
            _cache["result"] = await check_anthropic()
            _cache["expires"] = time.time() + ttl
        return _cache["result"]

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm-up: mark ready only after initial API check passes
    app.state.ready = await check_anthropic()
    yield
    app.state.ready = False

app = FastAPI(lifespan=lifespan)

@app.get("/live")
async def liveness():
    """Always 200 while the process is alive."""
    return {"status": "alive"}

@app.get("/ready", response_model=HealthStatus)
async def readiness(response: Response):
    """503 until warm-up completes and dependencies pass."""
    api_ok = await cached_anthropic_check()
    ready = getattr(app.state, "ready", False) and api_ok
    response.status_code = 200 if ready else 503
    return HealthStatus(
        status="ready" if ready else "not_ready",
        checks={"anthropic_api": api_ok, "warmup_complete": app.state.ready},
        uptime_seconds=time.time() - _start_time,
    )

@app.get("/health", response_model=HealthStatus)
async def health(response: Response):
    """Alias for /ready with extended info."""
    return await readiness(response)

# Expected Token Savings: TTL-cached ping (1 token per 30s window regardless of probe frequency)
# Environment: FastAPI agents; Kubernetes httpGet probes on /live and /ready
```

### Option 3: Health State Machine with Graceful Start/Stop

```python
import anthropic
import asyncio
import time
from enum import Enum
from dataclasses import dataclass, field

class AgentState(Enum):
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    STOPPED = "stopped"

@dataclass
class HealthReport:
    state: AgentState
    checks: dict[str, bool] = field(default_factory=dict)
    message: str = ""
    uptime: float = 0.0

    @property
    def http_status(self) -> int:
        return 200 if self.state in (AgentState.READY, AgentState.DEGRADED) else 503

class AgentHealthMonitor:
    def __init__(self):
        self._state = AgentState.STARTING
        self._start_time = time.time()
        self._checks: dict[str, bool] = {}
        self._client = anthropic.AsyncAnthropic()
        self._stop_event = asyncio.Event()

    async def _probe_anthropic(self) -> bool:
        try:
            await asyncio.wait_for(
                self._client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=1,
                    messages=[{"role": "user", "content": "ok"}],
                ),
                timeout=8.0,
            )
            return True
        except Exception:
            return False

    async def run_checks(self):
        self._checks["anthropic_api"] = await self._probe_anthropic()
        all_ok = all(self._checks.values())
        if self._state == AgentState.STOPPING:
            pass  # don't flip state during shutdown
        elif all_ok:
            self._state = AgentState.READY
        else:
            self._state = AgentState.DEGRADED

    async def background_loop(self, interval: float = 45.0):
        """Continuously refresh health state."""
        while not self._stop_event.is_set():
            await self.run_checks()
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._stop_event.wait()), timeout=interval
                )
            except asyncio.TimeoutError:
                pass

    def report(self) -> HealthReport:
        return HealthReport(
            state=self._state,
            checks=dict(self._checks),
            message=f"Agent in state {self._state.value}",
            uptime=time.time() - self._start_time,
        )

    async def shutdown(self):
        self._state = AgentState.STOPPING
        self._stop_event.set()
        await self._client.close()
        self._state = AgentState.STOPPED

async def main():
    monitor = AgentHealthMonitor()
    # Initial check before accepting traffic
    await monitor.run_checks()
    print("Startup report:", monitor.report())

    # Background health loop
    asyncio.create_task(monitor.background_loop())

    await asyncio.sleep(5)
    print("Runtime report:", monitor.report())
    await monitor.shutdown()

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: 1 token per 45s interval regardless of probe load
# Environment: asyncio agents; integrates with any HTTP framework via monitor.report()
```

### Option 4: SQLite-Backed Health History with Trend Detection

```python
import anthropic
import asyncio
import sqlite3
import time
import json
from pathlib import Path

DB_PATH = Path("/tmp/agent_health.db")
client = anthropic.AsyncAnthropic()

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS health_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at REAL NOT NULL,
            check_name TEXT NOT NULL,
            passed INTEGER NOT NULL,
            latency_ms REAL NOT NULL
        )
    """)
    con.commit()
    con.close()

async def probe_anthropic() -> tuple[bool, float]:
    t0 = time.time()
    try:
        await asyncio.wait_for(
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1,
                messages=[{"role": "user", "content": "ok"}],
            ),
            timeout=10.0,
        )
        return True, (time.time() - t0) * 1000
    except Exception:
        return False, (time.time() - t0) * 1000

def record_check(name: str, passed: bool, latency_ms: float):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO health_checks (checked_at, check_name, passed, latency_ms) VALUES (?,?,?,?)",
        (time.time(), name, int(passed), latency_ms),
    )
    con.commit()
    con.close()

def get_health_trend(name: str, window: int = 10) -> dict:
    """Return pass rate and p95 latency over last N checks."""
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT passed, latency_ms FROM health_checks WHERE check_name=? ORDER BY id DESC LIMIT ?",
        (name, window),
    ).fetchall()
    con.close()
    if not rows:
        return {"pass_rate": 0.0, "p95_latency_ms": None, "sample_count": 0}
    passes = sum(r[0] for r in rows)
    latencies = sorted(r[1] for r in rows)
    p95_idx = max(0, int(len(latencies) * 0.95) - 1)
    return {
        "pass_rate": passes / len(rows),
        "p95_latency_ms": latencies[p95_idx],
        "sample_count": len(rows),
    }

async def full_health_report() -> dict:
    passed, latency = await probe_anthropic()
    record_check("anthropic_api", passed, latency)
    trend = get_health_trend("anthropic_api")
    status = "ok" if passed and trend["pass_rate"] >= 0.8 else "degraded"
    return {
        "status": status,
        "latest_check": {"passed": passed, "latency_ms": round(latency, 1)},
        "trend": trend,
    }

async def main():
    init_db()
    report = await full_health_report()
    print(json.dumps(report, indent=2))

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: 1 token per check; trend detection avoids false alarms from transient spikes
# Environment: long-running agents; SQLite trend distinguishes flaps from real outages
```

### Option 5: Multi-Dependency Health with Parallel Probes

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass
from typing import Callable, Awaitable

@dataclass
class Probe:
    name: str
    check: Callable[[], Awaitable[bool]]
    critical: bool = True  # critical=False → degraded not down
    timeout: float = 5.0

async def _run_probe(probe: Probe) -> tuple[str, bool, float]:
    t0 = time.time()
    try:
        result = await asyncio.wait_for(probe.check(), timeout=probe.timeout)
    except Exception:
        result = False
    return probe.name, result, (time.time() - t0) * 1000

async def run_all_probes(probes: list[Probe]) -> dict:
    results = await asyncio.gather(*[_run_probe(p) for p in probes])
    checks = {name: {"passed": passed, "latency_ms": round(ms, 1)} for name, passed, ms in results}
    name_to_probe = {p.name: p for p in probes}

    critical_fail = any(
        not checks[p.name]["passed"] for p in probes if p.critical
    )
    non_critical_fail = any(
        not checks[p.name]["passed"] for p in probes if not p.critical
    )

    if critical_fail:
        status, http = "down", 503
    elif non_critical_fail:
        status, http = "degraded", 200
    else:
        status, http = "ok", 200

    return {"status": status, "http_status": http, "checks": checks}

_client = anthropic.AsyncAnthropic()

async def anthropic_probe() -> bool:
    await _client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1,
        messages=[{"role": "user", "content": "ok"}],
    )
    return True

async def disk_probe() -> bool:
    import shutil
    usage = shutil.disk_usage("/")
    return (usage.free / usage.total) > 0.05  # >5% free

async def memory_probe() -> bool:
    import resource
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_maxrss < 2 * 1024 * 1024  # < 2 GB RSS

PROBES = [
    Probe("anthropic_api", anthropic_probe, critical=True, timeout=10.0),
    Probe("disk_space", disk_probe, critical=False, timeout=1.0),
    Probe("memory", memory_probe, critical=False, timeout=1.0),
]

async def main():
    report = await run_all_probes(PROBES)
    import json
    print(json.dumps(report, indent=2))

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: 1 token per Anthropic probe; non-critical probes add zero API cost
# Environment: multi-dependency agents; parallel probes complete in max(individual_timeout) not sum
```

### Option 6: Kubernetes-Ready Health Server with Startup Probe

```python
import anthropic
import asyncio
import json
import time
from aiohttp import web

_client = anthropic.AsyncAnthropic()
_ready = False
_start_time = time.time()
_last_probe: dict = {"ok": False, "at": 0.0}
_PROBE_TTL = 30.0

async def _probe() -> bool:
    now = time.time()
    if now - _last_probe["at"] < _PROBE_TTL:
        return _last_probe["ok"]
    try:
        await asyncio.wait_for(
            _client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1,
                messages=[{"role": "user", "content": "ok"}],
            ),
            timeout=8.0,
        )
        _last_probe.update(ok=True, at=now)
    except Exception:
        _last_probe.update(ok=False, at=now)
    return _last_probe["ok"]

async def live_handler(request: web.Request) -> web.Response:
    """Kubernetes livenessProbe: 200 = process alive, never restart unless 500."""
    return web.json_response({"alive": True})

async def ready_handler(request: web.Request) -> web.Response:
    """Kubernetes readinessProbe: 503 = remove from load balancer."""
    global _ready
    api_ok = await _probe()
    _ready = api_ok
    body = {"ready": _ready, "api": api_ok, "uptime": round(time.time() - _start_time, 1)}
    return web.json_response(body, status=200 if _ready else 503)

async def startup_handler(request: web.Request) -> web.Response:
    """Kubernetes startupProbe: 503 until initial probe passes."""
    api_ok = await _probe()
    body = {"started": api_ok}
    return web.json_response(body, status=200 if api_ok else 503)

async def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/live", live_handler)
    app.router.add_get("/ready", ready_handler)
    app.router.add_get("/startup", startup_handler)
    return app

async def main():
    app = await build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    print("Health server up on :8080")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: TTL prevents probe-per-request billing; 1 token per 30s window
# Environment: Kubernetes deployments; configure startupProbe + livenessProbe + readinessProbe separately
```

## Comparison

| Option | Framework | Probe Type | Caching | Kubernetes Fit |
|--------|-----------|-----------|---------|---------------|
| 1 — Simple HTTP + threading | stdlib | Anthropic ping | TTL lock | Basic (httpGet) |
| 2 — FastAPI router | FastAPI | Anthropic ping | asyncio TTL | Full (livenessProbe + readinessProbe) |
| 3 — State machine | asyncio | Anthropic ping | Background loop | Full (via report()) |
| 4 — SQLite trend history | asyncio + sqlite3 | Anthropic ping | SQLite rows | Trend-aware (avoids flap restarts) |
| 5 — Multi-dependency parallel | asyncio | Anthropic + disk + memory | None | Partial (parallel probe timing) |
| 6 — aiohttp + startup probe | aiohttp | Anthropic ping | TTL dict | Full (startup + live + ready) |
