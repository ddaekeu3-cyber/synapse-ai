---
layout: solution
title: "Agent Doesn't Implement Dependency Health Cascade Detection"
description: "How to detect when one failing dependency is causing cascading health failures across multiple agents, and isolate the root cause before the entire system degrades."
tags: [reliability, cascade, dependency, health, circuit-breaker, root-cause]
difficulty: advanced
solution_count: 6
---

## Problem

A shared dependency — a database, a vector store, an embedding service, an external API — starts failing. Multiple agents independently retry, open their own circuit breakers, and emit alerts. Each agent treats it as an isolated failure. Operators receive dozens of identical alerts from different agents, all caused by one root issue. The blast radius is invisible until half the system is degraded.

```python
# Bad: each agent monitors its own health independently
class AgentA:
    async def health_check(self):
        return await self.db.ping()  # fails -> AgentA marks itself unhealthy

class AgentB:
    async def health_check(self):
        return await self.db.ping()  # fails -> AgentB marks itself unhealthy

# Result: 10 agents, 10 alerts, 0 root-cause signal — operators don't know it's the DB
```

---

## Solution 1 — Shared Dependency Health Registry

All agents report dependency health to a central registry. A cascade is detected when multiple agents report the same dependency as unhealthy within a time window.

```python
import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

@dataclass
class DependencyReport:
    dep_name: str
    agent_id: str
    healthy: bool
    latency_ms: float
    ts: float = field(default_factory=time.time)
    error: str = ""

@dataclass
class CascadeAlert:
    dep_name: str
    affected_agents: list[str]
    failure_rate: float
    detected_at: float

class DependencyHealthRegistry:
    def __init__(self, window_seconds: float = 60.0,
                 cascade_threshold: float = 0.5):
        self._window = window_seconds
        self._threshold = cascade_threshold  # fraction of agents reporting unhealthy
        self._reports: list[DependencyReport] = []
        self._known_agents: set[str] = set()
        self._lock = asyncio.Lock()
        self._cascade_callbacks: list[callable] = []

    def on_cascade(self, callback: callable) -> None:
        self._cascade_callbacks.append(callback)

    async def report(self, dep_name: str, agent_id: str,
                     healthy: bool, latency_ms: float = 0, error: str = "") -> None:
        async with self._lock:
            self._known_agents.add(agent_id)
            self._reports.append(DependencyReport(
                dep_name, agent_id, healthy, latency_ms, error=error
            ))
            # Trim old reports
            cutoff = time.time() - self._window
            self._reports = [r for r in self._reports if r.ts > cutoff]
            await self._check_cascade(dep_name)

    async def _check_cascade(self, dep_name: str) -> None:
        cutoff = time.time() - self._window
        recent = [r for r in self._reports
                  if r.dep_name == dep_name and r.ts > cutoff]
        if not recent:
            return

        # One report per agent (latest)
        latest_per_agent: dict[str, DependencyReport] = {}
        for r in recent:
            if r.agent_id not in latest_per_agent or r.ts > latest_per_agent[r.agent_id].ts:
                latest_per_agent[r.agent_id] = r

        unhealthy = [r for r in latest_per_agent.values() if not r.healthy]
        total = len(latest_per_agent)
        failure_rate = len(unhealthy) / max(total, 1)

        if failure_rate >= self._threshold and len(unhealthy) >= 2:
            alert = CascadeAlert(
                dep_name=dep_name,
                affected_agents=[r.agent_id for r in unhealthy],
                failure_rate=failure_rate,
                detected_at=time.time(),
            )
            for cb in self._cascade_callbacks:
                asyncio.create_task(cb(alert))

    def status(self) -> dict[str, Any]:
        cutoff = time.time() - self._window
        recent = [r for r in self._reports if r.ts > cutoff]
        by_dep: dict[str, list[DependencyReport]] = defaultdict(list)
        for r in recent:
            by_dep[r.dep_name].append(r)

        return {
            dep: {
                "healthy_agents": sum(1 for r in reps if r.healthy),
                "unhealthy_agents": sum(1 for r in reps if not r.healthy),
                "avg_latency_ms": sum(r.latency_ms for r in reps) / len(reps),
            }
            for dep, reps in by_dep.items()
        }

# Setup
registry = DependencyHealthRegistry(window_seconds=60, cascade_threshold=0.5)

async def on_cascade_detected(alert: CascadeAlert) -> None:
    print(
        f"CASCADE DETECTED: {alert.dep_name} is failing for "
        f"{len(alert.affected_agents)} agents "
        f"({alert.failure_rate:.0%} failure rate)\n"
        f"Affected: {alert.affected_agents}"
    )
    # In production: page on-call, open incident, suppress per-agent alerts

registry.on_cascade(on_cascade_detected)

# Each agent reports to the shared registry
async def agent_health_loop(agent_id: str, db_url: str) -> None:
    while True:
        t0 = time.monotonic()
        try:
            await ping_database(db_url)
            latency = (time.monotonic() - t0) * 1000
            await registry.report("database", agent_id, healthy=True, latency_ms=latency)
        except Exception as e:
            latency = (time.monotonic() - t0) * 1000
            await registry.report("database", agent_id, healthy=False,
                                   latency_ms=latency, error=str(e))
        await asyncio.sleep(10)
```

---

## Solution 2 — Causal Graph: Link Agent Failures to Root Dependencies

Build a causal graph where agents declare their dependencies. When an agent fails, walk the graph to identify which dependency is the likely root cause.

```python
from dataclasses import dataclass, field
from typing import Any

@dataclass
class AgentNode:
    agent_id: str
    dependencies: list[str]  # dependency names
    healthy: bool = True
    last_error: str = ""

@dataclass
class DependencyNode:
    dep_name: str
    healthy: bool = True
    affected_agents: list[str] = field(default_factory=list)

class CausalHealthGraph:
    def __init__(self):
        self._agents: dict[str, AgentNode] = {}
        self._deps: dict[str, DependencyNode] = {}

    def register_agent(self, agent_id: str, dependencies: list[str]) -> None:
        self._agents[agent_id] = AgentNode(agent_id, dependencies)
        for dep in dependencies:
            if dep not in self._deps:
                self._deps[dep] = DependencyNode(dep)

    def report_agent_failure(self, agent_id: str, error: str) -> list[str]:
        """Returns list of suspected root-cause dependencies."""
        if agent_id not in self._agents:
            return []
        self._agents[agent_id].healthy = False
        self._agents[agent_id].last_error = error

        # Find all dependencies shared by 2+ failed agents
        failed_agents = [a for a in self._agents.values() if not a.healthy]
        dep_failure_count: dict[str, int] = {}
        for agent in failed_agents:
            for dep in agent.dependencies:
                dep_failure_count[dep] = dep_failure_count.get(dep, 0) + 1

        suspects = [
            dep for dep, count in dep_failure_count.items()
            if count >= 2
        ]
        suspects.sort(key=lambda d: -dep_failure_count[d])
        return suspects

    def report_dep_failure(self, dep_name: str) -> list[str]:
        """Mark dependency unhealthy; return affected agents."""
        if dep_name in self._deps:
            self._deps[dep_name].healthy = False
        return [
            a.agent_id for a in self._agents.values()
            if dep_name in a.dependencies
        ]

    def root_cause_analysis(self) -> dict[str, Any]:
        unhealthy_deps = [d for d in self._deps.values() if not d.healthy]
        unhealthy_agents = [a for a in self._agents.values() if not a.healthy]

        if not unhealthy_deps and not unhealthy_agents:
            return {"status": "healthy"}

        # Find the dependency whose failure explains the most agent failures
        dep_explains: dict[str, list[str]] = {}
        for dep in self._deps.values():
            affected = [
                a.agent_id for a in unhealthy_agents
                if dep.dep_name in a.dependencies
            ]
            if affected:
                dep_explains[dep.dep_name] = affected

        sorted_deps = sorted(dep_explains.items(), key=lambda x: -len(x[1]))
        return {
            "probable_root_cause": sorted_deps[0][0] if sorted_deps else None,
            "affects_agents": sorted_deps[0][1] if sorted_deps else [],
            "all_dep_failures": [d.dep_name for d in unhealthy_deps],
            "all_agent_failures": [a.agent_id for a in unhealthy_agents],
        }

# Usage
graph = CausalHealthGraph()
graph.register_agent("agent-summarize", ["database", "embedding_service", "llm_api"])
graph.register_agent("agent-classify", ["database", "llm_api"])
graph.register_agent("agent-search", ["database", "vector_store"])

# Agents start failing
graph.report_agent_failure("agent-summarize", "Connection refused: database:5432")
graph.report_agent_failure("agent-classify", "Connection refused: database:5432")
graph.report_agent_failure("agent-search", "Timeout: database:5432")

rca = graph.root_cause_analysis()
print(rca)
# {"probable_root_cause": "database", "affects_agents": ["agent-summarize", "agent-classify", "agent-search"]}
```

---

## Solution 3 — Correlation-Based Cascade Detector Using Error Rate Signals

Compute the correlation between per-agent error rate spikes. High correlation across agents = shared dependency; low correlation = isolated agent issue.

```python
import asyncio
import time
import math
from collections import deque
from dataclasses import dataclass, field

@dataclass
class ErrorRateTracker:
    agent_id: str
    window: float = 60.0
    _events: deque = field(default_factory=deque)

    def record(self, is_error: bool) -> None:
        now = time.monotonic()
        self._events.append((now, is_error))
        cutoff = now - self.window
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def error_rate(self) -> float:
        if not self._events:
            return 0.0
        return sum(1 for _, e in self._events if e) / len(self._events)

def pearson_correlation(x: list[float], y: list[float]) -> float:
    n = len(x)
    if n < 2:
        return 0.0
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    num = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    den_x = math.sqrt(sum((xi - mean_x) ** 2 for xi in x))
    den_y = math.sqrt(sum((yi - mean_y) ** 2 for yi in y))
    if den_x * den_y == 0:
        return 0.0
    return num / (den_x * den_y)

class CorrelationCascadeDetector:
    def __init__(self, correlation_threshold: float = 0.8,
                 sample_interval: float = 5.0):
        self._trackers: dict[str, ErrorRateTracker] = {}
        self._history: dict[str, list[float]] = {}  # agent_id -> [error_rate_samples]
        self._corr_threshold = correlation_threshold
        self._sample_interval = sample_interval

    def get_tracker(self, agent_id: str) -> ErrorRateTracker:
        if agent_id not in self._trackers:
            self._trackers[agent_id] = ErrorRateTracker(agent_id)
            self._history[agent_id] = []
        return self._trackers[agent_id]

    async def sample_loop(self) -> None:
        """Periodically sample error rates and check for correlated spikes."""
        while True:
            await asyncio.sleep(self._sample_interval)
            for agent_id, tracker in self._trackers.items():
                rate = tracker.error_rate()
                hist = self._history[agent_id]
                hist.append(rate)
                if len(hist) > 20:
                    hist.pop(0)

            self._detect_cascade()

    def _detect_cascade(self) -> None:
        agent_ids = list(self._history.keys())
        if len(agent_ids) < 2:
            return

        for i in range(len(agent_ids)):
            for j in range(i + 1, len(agent_ids)):
                a1, a2 = agent_ids[i], agent_ids[j]
                h1 = self._history[a1]
                h2 = self._history[a2]
                if len(h1) < 5 or len(h2) < 5:
                    continue

                corr = pearson_correlation(h1[-10:], h2[-10:])
                current_a1 = self._trackers[a1].error_rate()
                current_a2 = self._trackers[a2].error_rate()

                if corr >= self._corr_threshold and current_a1 > 0.1 and current_a2 > 0.1:
                    print(
                        f"CASCADE SIGNAL: {a1} and {a2} have correlated error spikes "
                        f"(correlation={corr:.2f}, rates={current_a1:.0%}/{current_a2:.0%})\n"
                        f"Likely shared dependency failure — investigate common dependencies."
                    )

detector = CorrelationCascadeDetector(correlation_threshold=0.8)
tracker_a = detector.get_tracker("agent-a")
tracker_b = detector.get_tracker("agent-b")

# Both agents start getting errors at the same time
for _ in range(20):
    tracker_a.record(is_error=True)
    tracker_b.record(is_error=True)
```

---

## Solution 4 — Blast Radius Estimator Before Taking Action

Before a circuit breaker trips or an agent restarts, estimate the blast radius — how many other agents and users will be affected if the shared dependency is isolated.

```python
from dataclasses import dataclass
from typing import Any

@dataclass
class BlastRadiusReport:
    dep_name: str
    direct_agents: list[str]
    downstream_agents: list[str]  # agents that depend on the direct agents
    estimated_users_affected: int
    recommendation: str

class BlastRadiusEstimator:
    def __init__(self):
        # agent_id -> list of dep names
        self._agent_deps: dict[str, list[str]] = {}
        # agent_id -> list of downstream agent_ids
        self._agent_downstream: dict[str, list[str]] = {}
        # agent_id -> estimated daily active users served
        self._agent_traffic: dict[str, int] = {}

    def register(self, agent_id: str, dependencies: list[str],
                 downstream_agents: list[str] = None,
                 daily_users: int = 0) -> None:
        self._agent_deps[agent_id] = dependencies
        self._agent_downstream[agent_id] = downstream_agents or []
        self._agent_traffic[agent_id] = daily_users

    def estimate(self, dep_name: str) -> BlastRadiusReport:
        # Direct: agents that depend on this dependency
        direct = [
            aid for aid, deps in self._agent_deps.items()
            if dep_name in deps
        ]

        # Downstream: agents that depend on the direct agents
        downstream = set()
        for agent_id in direct:
            for ds in self._agent_downstream.get(agent_id, []):
                if ds not in direct:
                    downstream.add(ds)

        # User impact
        total_users = sum(self._agent_traffic.get(a, 0)
                         for a in direct + list(downstream))

        if total_users > 10000:
            recommendation = "CRITICAL: Do not isolate. Page on-call immediately."
        elif total_users > 1000:
            recommendation = "HIGH: Degrade gracefully; activate fallback before isolation."
        elif len(direct) > 5:
            recommendation = "MEDIUM: Coordinate with team before circuit-breaking."
        else:
            recommendation = "LOW: Safe to isolate; few agents affected."

        return BlastRadiusReport(
            dep_name=dep_name,
            direct_agents=direct,
            downstream_agents=list(downstream),
            estimated_users_affected=total_users,
            recommendation=recommendation,
        )

estimator = BlastRadiusEstimator()
estimator.register("agent-search", ["database", "vector_store"],
                   downstream_agents=["agent-report"], daily_users=50000)
estimator.register("agent-classify", ["database", "llm_api"], daily_users=20000)
estimator.register("agent-report", ["database"], daily_users=5000)

report = estimator.estimate("database")
print(f"Blast radius for 'database':")
print(f"  Direct agents: {report.direct_agents}")
print(f"  Downstream:    {report.downstream_agents}")
print(f"  Users at risk: {report.estimated_users_affected:,}")
print(f"  Action: {report.recommendation}")
```

---

## Solution 5 — Coordinated Circuit Breaker: Shared Trip Signal

When one agent's circuit breaker trips on a dependency, broadcast the trip signal to all other agents sharing that dependency. Avoids redundant hammering of a known-bad dependency.

```python
import asyncio
import time
from dataclasses import dataclass
from enum import Enum, auto

class CBState(Enum):
    CLOSED = auto()
    OPEN = auto()
    HALF_OPEN = auto()

@dataclass
class TripSignal:
    dep_name: str
    triggered_by: str
    tripped_at: float
    reason: str

class SharedCircuitBreakerBus:
    """Pub/sub bus for circuit breaker trip signals."""

    def __init__(self):
        self._subscribers: dict[str, list[asyncio.Queue]] = {}

    def subscribe(self, dep_name: str) -> asyncio.Queue:
        q: asyncio.Queue[TripSignal] = asyncio.Queue()
        self._subscribers.setdefault(dep_name, []).append(q)
        return q

    async def broadcast_trip(self, signal: TripSignal) -> None:
        for q in self._subscribers.get(signal.dep_name, []):
            await q.put(signal)

class CoordinatedCircuitBreaker:
    def __init__(self, agent_id: str, dep_name: str,
                 bus: SharedCircuitBreakerBus,
                 failure_threshold: int = 3,
                 reset_timeout: float = 30.0):
        self._agent_id = agent_id
        self._dep_name = dep_name
        self._bus = bus
        self._threshold = failure_threshold
        self._reset_timeout = reset_timeout
        self._state = CBState.CLOSED
        self._failures = 0
        self._opened_at: float = 0.0
        self._signal_queue = bus.subscribe(dep_name)
        asyncio.create_task(self._listen_for_signals())

    async def _listen_for_signals(self) -> None:
        """Trip immediately when another agent reports this dependency is down."""
        while True:
            signal: TripSignal = await self._signal_queue.get()
            if (signal.triggered_by != self._agent_id
                    and self._state == CBState.CLOSED):
                print(
                    f"[{self._agent_id}] Coordinated trip: {self._dep_name} "
                    f"tripped by {signal.triggered_by} — pre-emptively opening circuit"
                )
                self._state = CBState.OPEN
                self._opened_at = signal.tripped_at

    async def call(self, fn, *args, **kwargs):
        if self._state == CBState.OPEN:
            if time.monotonic() - self._opened_at > self._reset_timeout:
                self._state = CBState.HALF_OPEN
            else:
                raise RuntimeError(f"Circuit OPEN for {self._dep_name}")

        try:
            result = await fn(*args, **kwargs)
            if self._state == CBState.HALF_OPEN:
                self._state = CBState.CLOSED
                self._failures = 0
            return result
        except Exception as e:
            self._failures += 1
            if self._failures >= self._threshold or self._state == CBState.HALF_OPEN:
                self._state = CBState.OPEN
                self._opened_at = time.monotonic()
                signal = TripSignal(self._dep_name, self._agent_id,
                                    self._opened_at, str(e))
                await self._bus.broadcast_trip(signal)
                print(f"[{self._agent_id}] Circuit opened for {self._dep_name} — broadcasting")
            raise

bus = SharedCircuitBreakerBus()
cb_a = CoordinatedCircuitBreaker("agent-a", "database", bus)
cb_b = CoordinatedCircuitBreaker("agent-b", "database", bus)
# When agent-a trips, agent-b's circuit opens immediately without needing its own failures
```

---

## Solution 6 — Dependency Health Heatmap with Anomaly Scoring

Maintain a rolling heatmap of dependency health across all agents. Detect anomalies using Z-score on per-dependency error rates to distinguish cascade (global spike) from isolated failures.

```python
import asyncio
import time
import math
from collections import defaultdict, deque
from dataclasses import dataclass

@dataclass
class HealthDataPoint:
    ts: float
    agent_id: str
    dep_name: str
    error_rate: float

class DependencyHeatmap:
    def __init__(self, window_seconds: float = 120.0):
        self._window = window_seconds
        self._data: list[HealthDataPoint] = []
        self._lock = asyncio.Lock()

    async def record(self, agent_id: str, dep_name: str, error_rate: float) -> None:
        async with self._lock:
            self._data.append(HealthDataPoint(time.time(), agent_id, dep_name, error_rate))
            cutoff = time.time() - self._window
            self._data = [d for d in self._data if d.ts > cutoff]

    def _dep_rates(self, dep_name: str) -> list[float]:
        cutoff = time.time() - self._window
        # Latest rate per agent
        by_agent: dict[str, float] = {}
        for d in self._data:
            if d.dep_name == dep_name and d.ts > cutoff:
                by_agent[d.agent_id] = d.error_rate
        return list(by_agent.values())

    def zscore_anomaly(self, dep_name: str) -> dict:
        rates = self._dep_rates(dep_name)
        if len(rates) < 3:
            return {"dep": dep_name, "anomaly": False, "reason": "insufficient data"}

        mean = sum(rates) / len(rates)
        variance = sum((r - mean) ** 2 for r in rates) / len(rates)
        std = math.sqrt(variance)

        # A cascade = mean error rate is high AND std is low (all agents equally affected)
        is_cascade = mean > 0.3 and std < 0.1
        # An isolated failure = one agent has high rate, others are fine (high std)
        is_isolated = mean < 0.2 and std > 0.2

        return {
            "dep": dep_name,
            "mean_error_rate": round(mean, 3),
            "std": round(std, 3),
            "agent_count": len(rates),
            "anomaly": is_cascade,
            "pattern": "CASCADE" if is_cascade else ("ISOLATED" if is_isolated else "NORMAL"),
            "affected_agents": len([r for r in rates if r > 0.3]),
        }

    def full_report(self) -> list[dict]:
        deps = set(d.dep_name for d in self._data)
        return [self.zscore_anomaly(dep) for dep in deps]

heatmap = DependencyHeatmap(window_seconds=120)

async def agent_report_loop(agent_id: str) -> None:
    while True:
        # Simulate error rates
        import random
        rate = random.uniform(0.7, 0.9)  # DB is failing
        await heatmap.record(agent_id, "database", rate)
        await heatmap.record(agent_id, "llm_api", random.uniform(0, 0.05))
        await asyncio.sleep(5)

async def monitor_loop() -> None:
    while True:
        await asyncio.sleep(15)
        report = heatmap.full_report()
        for dep in report:
            if dep["pattern"] == "CASCADE":
                print(f"CASCADE DETECTED: {dep['dep']} — "
                      f"{dep['affected_agents']}/{dep['agent_count']} agents failing "
                      f"(mean={dep['mean_error_rate']:.0%})")
```

---

## Comparison

| Approach | Root Cause ID | Blast Radius | Auto-Coordinate | Anomaly Detection | Best For |
|---|---|---|---|---|---|
| Shared health registry | Partial | No | No | No | Centralized health aggregation |
| Causal graph | **Yes** | Partial | No | No | Structured dependency mapping |
| Correlation detector | **Yes** | No | No | **Yes** | Unknown shared dependencies |
| Blast radius estimator | Partial | **Yes** | No | No | Pre-isolation impact assessment |
| Coordinated circuit breaker | No | No | **Yes** | No | Stopping redundant retry storms |
| Heatmap + Z-score | **Yes** | Partial | No | **Yes** | Statistical cascade vs isolated |
