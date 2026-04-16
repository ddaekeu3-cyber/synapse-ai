---
layout: solution
title: "Agent Doesn't Implement Distributed Health Dashboard for Multi-Agent Systems"
category: observability
description: "Aggregate health signals from all agents, tools, and dependencies into a unified dashboard so operators can see system-wide status at a glance and detect degradation early."
tags: [observability, health, dashboard, multi-agent, monitoring, status, distributed]
---

# Agent Doesn't Implement Distributed Health Dashboard for Multi-Agent Systems

## Problem

When running multiple concurrent agents — orchestrators, subagents, tool servers, and external dependencies — health failures are invisible unless each component actively reports its status to a shared view. Without a distributed health dashboard, operators discover failures only when user-facing errors appear. By then the blast radius is large and root-cause analysis requires manual log trawling.

## Solutions

### Option 1: Centralized Health Registry with Pull-Based Checks

Each agent registers a health check function; a central registry polls them periodically and exposes a summary.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.AsyncAnthropic()


class Status(str, Enum):
    OK       = "ok"
    DEGRADED = "degraded"
    DOWN     = "down"
    UNKNOWN  = "unknown"


@dataclass
class HealthResult:
    component: str
    status: Status
    latency_ms: float
    message: str = ""
    checked_at: float = field(default_factory=time.time)


@dataclass
class HealthRegistry:
    _checks: dict = field(default_factory=dict)

    def register(self, name: str, check_fn) -> None:
        self._checks[name] = check_fn

    async def run_all(self) -> list[HealthResult]:
        results = []
        for name, fn in self._checks.items():
            start = time.monotonic()
            try:
                await asyncio.wait_for(fn(), timeout=5.0)
                latency = (time.monotonic() - start) * 1000
                results.append(HealthResult(name, Status.OK, round(latency, 1)))
            except asyncio.TimeoutError:
                results.append(HealthResult(name, Status.DOWN, 5000.0, "timeout"))
            except Exception as e:
                latency = (time.monotonic() - start) * 1000
                results.append(HealthResult(name, Status.DEGRADED, round(latency, 1), str(e)[:80]))
        return results

    def summary(self, results: list[HealthResult]) -> dict:
        by_status = {s: 0 for s in Status}
        for r in results:
            by_status[r.status] += 1
        overall = (
            Status.DOWN     if by_status[Status.DOWN] > 0 else
            Status.DEGRADED if by_status[Status.DEGRADED] > 0 else
            Status.OK
        )
        return {
            "overall": overall.value,
            "components": {r.component: {"status": r.status.value, "latency_ms": r.latency_ms, "message": r.message} for r in results},
            "counts": {s.value: v for s, v in by_status.items()},
        }


registry = HealthRegistry()


# --- register component checks ---

async def check_anthropic_api() -> None:
    await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1,
        messages=[{"role": "user", "content": "ping"}],
    )

async def check_orchestrator() -> None:
    await asyncio.sleep(0.05)   # simulate internal check

async def check_tool_server() -> None:
    await asyncio.sleep(0.02)

async def check_memory_store() -> None:
    await asyncio.sleep(0.01)

registry.register("anthropic_api",  check_anthropic_api)
registry.register("orchestrator",   check_orchestrator)
registry.register("tool_server",    check_tool_server)
registry.register("memory_store",   check_memory_store)


async def health_dashboard_loop(interval_seconds: int = 10, iterations: int = 2) -> None:
    for _ in range(iterations):
        results = await registry.run_all()
        summary = registry.summary(results)
        print(f"\n=== Health Dashboard [{time.strftime('%H:%M:%S')}] ===")
        print(f"Overall: {summary['overall'].upper()}")
        for name, info in summary["components"].items():
            icon = {"ok": "✓", "degraded": "~", "down": "✗", "unknown": "?"}.get(info["status"], "?")
            print(f"  [{icon}] {name:<20} {info['status']:<10} {info['latency_ms']:>6.1f}ms  {info['message']}")
        await asyncio.sleep(interval_seconds)


if __name__ == "__main__":
    asyncio.run(health_dashboard_loop(interval_seconds=5, iterations=2))

# Expected Token Savings: Catch degradation before users report errors; 1 ping/interval per component
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 2: Push-Based Health Beacon from Each Agent

Each agent continuously pushes its own health metrics to a shared store; the dashboard reads the store.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

BEACON_INTERVAL   = 5.0   # seconds between beacons
STALE_THRESHOLD   = 15.0  # seconds before an agent is considered dead


@dataclass
class AgentBeacon:
    agent_id: str
    status: str          # "ok" | "busy" | "error"
    request_count: int
    error_count: int
    avg_latency_ms: float
    last_error: str
    updated_at: float = field(default_factory=time.time)

    def is_stale(self) -> bool:
        return time.time() - self.updated_at > STALE_THRESHOLD

    def error_rate(self) -> float:
        return self.error_count / max(self.request_count, 1)


class HealthStore:
    def __init__(self) -> None:
        self._beacons: dict[str, AgentBeacon] = {}
        self._lock = asyncio.Lock()

    async def update(self, beacon: AgentBeacon) -> None:
        async with self._lock:
            self._beacons[beacon.agent_id] = beacon

    async def get_all(self) -> dict[str, AgentBeacon]:
        async with self._lock:
            return dict(self._beacons)

    async def dashboard(self) -> dict:
        beacons = await self.get_all()
        now = time.time()
        rows = []
        for agent_id, b in beacons.items():
            stale = b.is_stale()
            rows.append({
                "agent_id":      agent_id,
                "status":        "stale" if stale else b.status,
                "requests":      b.request_count,
                "error_rate":    f"{b.error_rate():.1%}",
                "avg_latency_ms": b.avg_latency_ms,
                "last_seen_ago": f"{now - b.updated_at:.1f}s",
                "last_error":    b.last_error,
            })
        alive   = sum(1 for r in rows if r["status"] == "ok")
        degraded = sum(1 for r in rows if r["status"] in ("busy", "error"))
        stale    = sum(1 for r in rows if r["status"] == "stale")
        return {
            "agents": rows,
            "summary": {"alive": alive, "degraded": degraded, "stale": stale, "total": len(rows)},
        }


store = HealthStore()


class SimulatedAgent:
    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self.request_count = 0
        self.error_count = 0
        self.total_latency = 0.0
        self.last_error = ""

    async def run_task(self, prompt: str) -> str:
        start = time.monotonic()
        try:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": prompt}],
            )
            self.request_count += 1
            self.total_latency += (time.monotonic() - start) * 1000
            return resp.content[0].text
        except Exception as e:
            self.error_count += 1
            self.last_error = str(e)[:60]
            raise

    async def beacon_loop(self) -> None:
        while True:
            avg_lat = self.total_latency / max(self.request_count, 1)
            err_rate = self.error_count / max(self.request_count, 1)
            status = "error" if err_rate > 0.2 else "busy" if avg_lat > 2000 else "ok"
            await store.update(AgentBeacon(
                agent_id=self.agent_id,
                status=status,
                request_count=self.request_count,
                error_count=self.error_count,
                avg_latency_ms=round(avg_lat, 1),
                last_error=self.last_error,
            ))
            await asyncio.sleep(BEACON_INTERVAL)


async def dashboard_loop(iterations: int = 2) -> None:
    for _ in range(iterations):
        await asyncio.sleep(BEACON_INTERVAL + 1)
        dash = await store.dashboard()
        print(f"\n=== Agent Health Dashboard [{time.strftime('%H:%M:%S')}] ===")
        s = dash["summary"]
        print(f"Alive={s['alive']} Degraded={s['degraded']} Stale={s['stale']} Total={s['total']}")
        for a in dash["agents"]:
            print(f"  {a['agent_id']:<20} {a['status']:<8} reqs={a['requests']} "
                  f"err={a['error_rate']} lat={a['avg_latency_ms']}ms seen={a['last_seen_ago']}")


async def main() -> None:
    agents = [SimulatedAgent(f"agent_{i:02d}") for i in range(4)]

    async def agent_work(agent: SimulatedAgent) -> None:
        beacon_task = asyncio.create_task(agent.beacon_loop())
        for i in range(5):
            try:
                await agent.run_task(f"Task {i} from {agent.agent_id}")
            except Exception:
                pass
            await asyncio.sleep(0.5)
        beacon_task.cancel()

    await asyncio.gather(
        dashboard_loop(iterations=2),
        *[agent_work(a) for a in agents],
    )


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Push model avoids poll overhead; stale detection catches silent crashes
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 3: Dependency Graph Health with Cascading Status

Model dependencies between components; a downstream failure automatically propagates degraded status upstream.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.AsyncAnthropic()


class Health(str, Enum):
    OK       = "ok"
    DEGRADED = "degraded"
    DOWN     = "down"


@dataclass
class Component:
    name: str
    depends_on: list[str] = field(default_factory=list)
    _status: Health = Health.OK
    _message: str = ""
    _latency: float = 0.0

    def set_health(self, status: Health, msg: str = "", latency: float = 0.0) -> None:
        self._status = status
        self._message = msg
        self._latency = latency

    @property
    def own_status(self) -> Health:
        return self._status


class DependencyHealthGraph:
    def __init__(self) -> None:
        self._nodes: dict[str, Component] = {}

    def add(self, component: Component) -> None:
        self._nodes[component.name] = component

    def effective_status(self, name: str, visited: set | None = None) -> Health:
        if visited is None:
            visited = set()
        if name in visited:
            return Health.OK   # cycle guard
        visited.add(name)
        node = self._nodes[name]
        own = node.own_status
        dep_statuses = [self.effective_status(dep, visited) for dep in node.depends_on]
        worst_dep = (
            Health.DOWN     if Health.DOWN     in dep_statuses else
            Health.DEGRADED if Health.DEGRADED in dep_statuses else
            Health.OK
        )
        if own == Health.DOWN or worst_dep == Health.DOWN:
            return Health.DOWN
        if own == Health.DEGRADED or worst_dep == Health.DEGRADED:
            return Health.DEGRADED
        return Health.OK

    def dashboard(self) -> dict:
        rows = {}
        for name, node in self._nodes.items():
            eff = self.effective_status(name)
            rows[name] = {
                "own_status":       node.own_status.value,
                "effective_status": eff.value,
                "depends_on":       node.depends_on,
                "message":          node._message,
                "latency_ms":       node._latency,
            }
        return rows


graph = DependencyHealthGraph()

# define component topology
graph.add(Component("anthropic_api"))
graph.add(Component("tool_server",     depends_on=["anthropic_api"]))
graph.add(Component("memory_store"))
graph.add(Component("subagent_a",      depends_on=["tool_server", "memory_store"]))
graph.add(Component("subagent_b",      depends_on=["tool_server"]))
graph.add(Component("orchestrator",    depends_on=["subagent_a", "subagent_b"]))


async def probe_anthropic() -> None:
    start = time.monotonic()
    try:
        await asyncio.wait_for(
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            ),
            timeout=4.0,
        )
        lat = (time.monotonic() - start) * 1000
        graph._nodes["anthropic_api"].set_health(Health.OK, latency=round(lat, 1))
    except asyncio.TimeoutError:
        graph._nodes["anthropic_api"].set_health(Health.DOWN, "timeout")
    except Exception as e:
        graph._nodes["anthropic_api"].set_health(Health.DEGRADED, str(e)[:60])


async def probe_all() -> None:
    await probe_anthropic()
    # simulate other checks
    graph._nodes["tool_server"].set_health(Health.OK, latency=12.0)
    graph._nodes["memory_store"].set_health(Health.OK, latency=3.0)
    graph._nodes["subagent_a"].set_health(Health.OK)
    graph._nodes["subagent_b"].set_health(Health.OK)
    graph._nodes["orchestrator"].set_health(Health.OK)


async def main() -> None:
    await probe_all()
    dash = graph.dashboard()
    print("=== Dependency Health Dashboard ===")
    icons = {"ok": "✓", "degraded": "~", "down": "✗"}
    for name, info in dash.items():
        own = icons.get(info["own_status"], "?")
        eff = icons.get(info["effective_status"], "?")
        deps = ", ".join(info["depends_on"]) or "none"
        print(f"  [{eff}] {name:<20} own={info['own_status']:<8} effective={info['effective_status']:<8} deps=[{deps}]")

    # simulate tool_server going down → subagents and orchestrator should cascade
    print("\n--- Simulating tool_server DOWN ---")
    graph._nodes["tool_server"].set_health(Health.DOWN, "connection refused")
    dash = graph.dashboard()
    for name, info in dash.items():
        eff = icons.get(info["effective_status"], "?")
        print(f"  [{eff}] {name:<20} effective={info['effective_status']}")


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Operators see one root cause instead of many cascading alerts
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 4: Time-Series Health History with Trend Detection

Store health check results over time and detect trending degradation before it becomes a full outage.

```python
import anthropic
import asyncio
import time
import statistics
from collections import deque
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

HISTORY_WINDOW   = 20   # number of data points to keep per component
PROBE_INTERVAL   = 3.0  # seconds between probes
DEGRADE_LATENCY  = 2000  # ms threshold for DEGRADED
DOWN_LATENCY     = 5000  # ms threshold for DOWN
ERROR_RATE_WARN  = 0.1   # 10% error rate = DEGRADED


@dataclass
class TimeSeriesHealth:
    component: str
    latencies: deque = field(default_factory=lambda: deque(maxlen=20))
    errors:    deque = field(default_factory=lambda: deque(maxlen=20))

    def record(self, latency_ms: float, is_error: bool) -> None:
        self.latencies.append(latency_ms)
        self.errors.append(1 if is_error else 0)

    def p95_latency(self) -> float:
        if not self.latencies:
            return 0.0
        s = sorted(self.latencies)
        idx = int(len(s) * 0.95)
        return s[min(idx, len(s) - 1)]

    def error_rate(self) -> float:
        if not self.errors:
            return 0.0
        return sum(self.errors) / len(self.errors)

    def trend(self) -> str:
        if len(self.latencies) < 4:
            return "insufficient_data"
        half = len(self.latencies) // 2
        recent = statistics.mean(list(self.latencies)[-half:])
        older  = statistics.mean(list(self.latencies)[:half])
        if recent > older * 1.5:
            return "degrading"
        if recent < older * 0.7:
            return "improving"
        return "stable"

    def status(self) -> str:
        p95 = self.p95_latency()
        err = self.error_rate()
        if p95 >= DOWN_LATENCY or err >= 0.5:
            return "down"
        if p95 >= DEGRADE_LATENCY or err >= ERROR_RATE_WARN:
            return "degraded"
        return "ok"


class TimeSeriesDashboard:
    def __init__(self) -> None:
        self._series: dict[str, TimeSeriesHealth] = {}

    def track(self, component: str) -> TimeSeriesHealth:
        if component not in self._series:
            self._series[component] = TimeSeriesHealth(component)
        return self._series[component]

    def print_dashboard(self) -> None:
        print(f"\n=== Health Trend Dashboard [{time.strftime('%H:%M:%S')}] ===")
        icons = {"ok": "✓", "degraded": "~", "down": "✗"}
        trends = {"degrading": "↑", "improving": "↓", "stable": "→", "insufficient_data": "?"}
        for comp, ts in self._series.items():
            st  = ts.status()
            tr  = ts.trend()
            p95 = ts.p95_latency()
            err = ts.error_rate()
            icon = icons.get(st, "?")
            tarr = trends.get(tr, "?")
            print(f"  [{icon}]{tarr} {comp:<20} {st:<10} p95={p95:>6.0f}ms err={err:.1%} trend={tr}")


dashboard = TimeSeriesDashboard()


async def probe_component(component: str, probe_fn) -> None:
    series = dashboard.track(component)
    start = time.monotonic()
    try:
        await asyncio.wait_for(probe_fn(), timeout=5.0)
        lat = (time.monotonic() - start) * 1000
        series.record(lat, is_error=False)
    except asyncio.TimeoutError:
        series.record(5000.0, is_error=True)
    except Exception:
        lat = (time.monotonic() - start) * 1000
        series.record(lat, is_error=True)


async def probe_api() -> None:
    await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1,
        messages=[{"role": "user", "content": "ping"}],
    )


async def probe_fast() -> None:
    await asyncio.sleep(0.01)


async def main() -> None:
    for i in range(6):
        await asyncio.gather(
            probe_component("anthropic_api", probe_api),
            probe_component("tool_server",   probe_fast),
            probe_component("memory_store",  probe_fast),
        )
        dashboard.print_dashboard()
        await asyncio.sleep(PROBE_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Trend detection catches gradual degradation before full failure
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 5: HTTP Health Endpoint with JSON Status Page

Expose a `/health` HTTP endpoint that aggregates all component statuses and returns machine-readable JSON for load balancers and monitoring systems.

```python
import anthropic
import asyncio
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import threading
from dataclasses import dataclass, field

client = anthropic.Anthropic()

CACHE_TTL_SECONDS = 10   # re-run checks at most once per 10s


@dataclass
class HealthCache:
    result: dict = field(default_factory=dict)
    updated_at: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def is_fresh(self) -> bool:
        return time.time() - self.updated_at < CACHE_TTL_SECONDS

    def update(self, result: dict) -> None:
        with self._lock:
            self.result = result
            self.updated_at = time.time()

    def get(self) -> dict:
        with self._lock:
            return dict(self.result)


cache = HealthCache()


def check_anthropic() -> dict:
    start = time.monotonic()
    try:
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
        return {"status": "ok", "latency_ms": round((time.monotonic() - start) * 1000, 1)}
    except Exception as e:
        return {"status": "down", "error": str(e)[:60], "latency_ms": round((time.monotonic() - start) * 1000, 1)}


def check_tool_server() -> dict:
    time.sleep(0.01)
    return {"status": "ok", "latency_ms": 10.0}


def check_memory() -> dict:
    time.sleep(0.005)
    return {"status": "ok", "latency_ms": 5.0}


CHECKS = {
    "anthropic_api": check_anthropic,
    "tool_server":   check_tool_server,
    "memory_store":  check_memory,
}


def run_checks() -> dict:
    components = {}
    for name, fn in CHECKS.items():
        components[name] = fn()
    statuses = [c["status"] for c in components.values()]
    overall = "down" if "down" in statuses else "degraded" if "degraded" in statuses else "ok"
    return {
        "status":     overall,
        "timestamp":  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "components": components,
    }


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/health", "/healthz"):
            self.send_response(404)
            self.end_headers()
            return

        if not cache.is_fresh():
            result = run_checks()
            cache.update(result)
        else:
            result = cache.get()

        body = json.dumps(result, indent=2).encode()
        status_code = 200 if result["status"] == "ok" else 503
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass   # suppress default access log


def start_health_server(port: int = 8080) -> HTTPServer:
    server = HTTPServer(("", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Health endpoint: http://localhost:{port}/health")
    return server


if __name__ == "__main__":
    server = start_health_server(port=8080)
    # run a check immediately and print result
    result = run_checks()
    cache.update(result)
    print(json.dumps(result, indent=2))
    # keep server alive briefly for demo
    time.sleep(2)
    server.shutdown()

# Expected Token Savings: Single probe per TTL window regardless of how many load balancers poll
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 6: Async Multi-Agent Health Aggregator with Alerting

Poll all agents and dependencies in parallel, aggregate results, and fire alerts when status changes.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Awaitable

client = anthropic.AsyncAnthropic()

CHECK_INTERVAL   = 8.0   # seconds between full sweeps
ALERT_COOLDOWN   = 30.0  # seconds before re-alerting same component


@dataclass
class ComponentCheck:
    name: str
    probe: Callable[[], Awaitable[None]]
    timeout: float = 5.0
    tags: list[str] = field(default_factory=list)


@dataclass
class AlertState:
    last_alerted: float = 0.0
    last_status: str = "ok"

    def should_alert(self, new_status: str) -> bool:
        changed = new_status != self.last_status
        cooled  = time.time() - self.last_alerted > ALERT_COOLDOWN
        return changed or (new_status != "ok" and cooled)


class HealthAggregator:
    def __init__(self) -> None:
        self._components: list[ComponentCheck] = []
        self._alert_states: dict[str, AlertState] = {}
        self._history: list[dict] = []
        self._alert_handlers: list[Callable] = []

    def register(self, component: ComponentCheck) -> None:
        self._components.append(component)
        self._alert_states[component.name] = AlertState()

    def on_alert(self, handler: Callable) -> None:
        self._alert_handlers.append(handler)

    async def _probe_one(self, component: ComponentCheck) -> dict:
        start = time.monotonic()
        try:
            await asyncio.wait_for(component.probe(), timeout=component.timeout)
            lat = (time.monotonic() - start) * 1000
            return {"name": component.name, "status": "ok", "latency_ms": round(lat, 1), "tags": component.tags}
        except asyncio.TimeoutError:
            return {"name": component.name, "status": "down", "latency_ms": component.timeout * 1000, "error": "timeout", "tags": component.tags}
        except Exception as e:
            lat = (time.monotonic() - start) * 1000
            return {"name": component.name, "status": "degraded", "latency_ms": round(lat, 1), "error": str(e)[:60], "tags": component.tags}

    async def sweep(self) -> dict:
        results = await asyncio.gather(*[self._probe_one(c) for c in self._components])
        now = time.time()

        for r in results:
            state = self._alert_states[r["name"]]
            if state.should_alert(r["status"]):
                for handler in self._alert_handlers:
                    await handler(r, state.last_status)
                state.last_alerted = now
                state.last_status = r["status"]

        statuses = [r["status"] for r in results]
        overall = "down" if "down" in statuses else "degraded" if "degraded" in statuses else "ok"
        snapshot = {
            "timestamp":  now,
            "overall":    overall,
            "components": {r["name"]: r for r in results},
        }
        self._history.append(snapshot)
        if len(self._history) > 100:
            self._history.pop(0)
        return snapshot

    def print_dashboard(self, snapshot: dict) -> None:
        icons = {"ok": "✓", "degraded": "~", "down": "✗"}
        print(f"\n=== Multi-Agent Health [{time.strftime('%H:%M:%S')}] overall={snapshot['overall'].upper()} ===")
        for name, info in snapshot["components"].items():
            icon = icons.get(info["status"], "?")
            tags = " ".join(f"[{t}]" for t in info.get("tags", []))
            err  = f" err={info['error']}" if "error" in info else ""
            print(f"  [{icon}] {name:<22} {info['status']:<10} {info['latency_ms']:>6.1f}ms {tags}{err}")


# --- define components ---

aggregator = HealthAggregator()


async def ping_api() -> None:
    await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1,
        messages=[{"role": "user", "content": "ping"}],
    )

async def fast_check() -> None:
    await asyncio.sleep(0.01)

aggregator.register(ComponentCheck("anthropic_api",  ping_api,   timeout=5.0, tags=["external"]))
aggregator.register(ComponentCheck("orchestrator",   fast_check, timeout=2.0, tags=["internal"]))
aggregator.register(ComponentCheck("subagent_pool",  fast_check, timeout=2.0, tags=["internal"]))
aggregator.register(ComponentCheck("tool_server",    fast_check, timeout=2.0, tags=["internal"]))
aggregator.register(ComponentCheck("memory_store",   fast_check, timeout=1.0, tags=["storage"]))


# --- alert handler ---
async def log_alert(component: dict, prev_status: str) -> None:
    print(f"  *** ALERT: {component['name']} {prev_status} → {component['status']} ***")


aggregator.on_alert(log_alert)


async def main() -> None:
    for _ in range(2):
        snapshot = await aggregator.sweep()
        aggregator.print_dashboard(snapshot)
        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Parallel probes complete in max(single_probe_time); alerting avoids noise floods
# Environment: ANTHROPIC_API_KEY must be set
```

---

## Comparison

| Option | Model | Alert Support | Dependency Graph | Trend Detection | Best For |
|--------|-------|--------------|-----------------|----------------|----------|
| 1 | Pull-based registry | No | No | No | Simple multi-component systems |
| 2 | Push beacons | Stale detection | No | No | Long-running agent processes |
| 3 | Dependency graph | Cascade propagation | Yes | No | Complex dependency topologies |
| 4 | Time-series + trends | Trend alerts | No | Yes | Gradual degradation detection |
| 5 | HTTP `/health` endpoint | Via load balancer | No | No | Cloud/k8s health check integration |
| 6 | Async aggregator + alerts | Full | No | No | Production multi-agent platforms |
