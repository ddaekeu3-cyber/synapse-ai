---
title: "Agent Doesn't Implement Topology Change Detection for Distributed Agents"
description: "Distributed agent systems that don't detect topology changes — agents joining, leaving, crashing, or being replaced — route work to stale endpoints, retry to dead agents, and fail to rebalance load after recovery. Implement topology change detection using heartbeat-based membership, event-driven change notification, and automatic routing table invalidation."
date: 2026-04-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-topology-change-detection-for-distributed-agents
tags: [topology, distributed-agents, membership, heartbeat, routing, observability]
symptoms:
  - "Orchestrator sends tasks to an agent that crashed 30 seconds ago — no detection until timeout"
  - "New agent instance starts but receives no work because routing tables are not updated"
  - "After a rolling deploy, 20% of requests still route to old agent versions"
  - "No visibility into which agent instances are alive, their load, or their capabilities"
  - "Manual intervention required to update routing when agent instances scale up or down"
---

## Why This Happens

Distributed agent systems often use static configuration (hardcoded endpoint lists, fixed DNS names) or stale service registry entries to route work. When an agent crashes, its entry in the routing table remains valid until TTL expires or the next manual refresh. Topology change detection solves this with active heartbeating: each agent emits liveness signals on a regular interval; the orchestrator tracks last-seen timestamps and marks agents as suspect or dead when heartbeats stop arriving. Change events (join, leave, update) trigger routing table recomputation without manual intervention.

## Solution 1: Agent Membership Record

```python
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

@dataclass
class AgentMembershipRecord:
    agent_id: str
    agent_type: str           # "executor" | "retriever" | "planner" etc.
    endpoint: str             # host:port or queue name
    capabilities: List[str]   # what this agent can do
    version: str = ""
    zone: str = ""            # availability zone / region

    # Liveness tracking
    last_heartbeat: float = field(default_factory=time.time)
    heartbeat_interval_seconds: float = 10.0
    status: str = "alive"    # "alive" | "suspect" | "dead"
    consecutive_missed: int = 0

    # Load snapshot (from heartbeat payload)
    in_flight_tasks: int = 0
    cpu_percent: float = 0.0
    memory_mb: float = 0.0

    joined_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_stale(self, suspect_multiplier: float = 2.0) -> bool:
        age = time.time() - self.last_heartbeat
        return age > self.heartbeat_interval_seconds * suspect_multiplier

    def is_dead(self, dead_multiplier: float = 5.0) -> bool:
        age = time.time() - self.last_heartbeat
        return age > self.heartbeat_interval_seconds * dead_multiplier

    def update_heartbeat(self, payload: Dict[str, Any]) -> None:
        self.last_heartbeat = time.time()
        self.consecutive_missed = 0
        self.status = "alive"
        self.in_flight_tasks = payload.get("in_flight_tasks", self.in_flight_tasks)
        self.cpu_percent = payload.get("cpu_percent", self.cpu_percent)
        self.memory_mb = payload.get("memory_mb", self.memory_mb)
        self.version = payload.get("version", self.version)
```

## Solution 2: Membership Registry

```python
import asyncio
import time
from typing import Callable, Dict, List, Optional

class AgentMembershipRegistry:
    """
    Maintains the live membership view of all agents in the topology.
    Receives heartbeats and registration events from agents.
    Marks agents as suspect or dead when heartbeats stop arriving.
    Fires change callbacks on topology events.
    """

    def __init__(
        self,
        suspect_multiplier: float = 2.0,
        dead_multiplier: float = 5.0,
        check_interval_seconds: float = 5.0,
    ):
        self._members: Dict[str, AgentMembershipRecord] = {}
        self._suspect_mult = suspect_multiplier
        self._dead_mult = dead_multiplier
        self._check_interval = check_interval_seconds
        self._change_handlers: List[Callable[[str, str, AgentMembershipRecord], None]] = []
        # change_handlers receive (event_type, agent_id, record)
        # event_type: "joined" | "heartbeat" | "suspect" | "dead" | "recovered" | "left"
        self._lock = asyncio.Lock()

    def add_change_handler(
        self, handler: Callable[[str, str, AgentMembershipRecord], None]
    ) -> None:
        self._change_handlers.append(handler)

    def _notify(self, event_type: str, agent_id: str, record: AgentMembershipRecord) -> None:
        for handler in self._change_handlers:
            try:
                handler(event_type, agent_id, record)
            except Exception as exc:
                print(f"[membership] handler error: {exc}")

    async def register(self, record: AgentMembershipRecord) -> None:
        async with self._lock:
            is_new = record.agent_id not in self._members
            self._members[record.agent_id] = record
        event = "joined" if is_new else "recovered"
        self._notify(event, record.agent_id, record)

    async def heartbeat(self, agent_id: str, payload: Dict) -> bool:
        async with self._lock:
            member = self._members.get(agent_id)
            if not member:
                return False
            was_suspect = member.status in ("suspect", "dead")
            member.update_heartbeat(payload)
        if was_suspect:
            self._notify("recovered", agent_id, member)
        else:
            self._notify("heartbeat", agent_id, member)
        return True

    async def deregister(self, agent_id: str) -> None:
        async with self._lock:
            member = self._members.pop(agent_id, None)
        if member:
            self._notify("left", agent_id, member)

    async def run_health_check(self) -> Dict[str, int]:
        """Mark agents as suspect or dead based on missed heartbeats."""
        counts = {"suspect": 0, "dead": 0}
        async with self._lock:
            for agent_id, member in self._members.items():
                old_status = member.status
                if member.is_dead(self._dead_mult):
                    member.status = "dead"
                    member.consecutive_missed += 1
                elif member.is_stale(self._suspect_mult):
                    member.status = "suspect"
                    member.consecutive_missed += 1
                if old_status != member.status:
                    counts[member.status] = counts.get(member.status, 0) + 1
                    self._notify(member.status, agent_id, member)
        return counts

    def alive_members(self, agent_type: Optional[str] = None) -> List[AgentMembershipRecord]:
        return [
            m for m in self._members.values()
            if m.status == "alive"
            and (agent_type is None or m.agent_type == agent_type)
        ]

    def topology_snapshot(self) -> dict:
        members = list(self._members.values())
        return {
            "total": len(members),
            "alive": sum(1 for m in members if m.status == "alive"),
            "suspect": sum(1 for m in members if m.status == "suspect"),
            "dead": sum(1 for m in members if m.status == "dead"),
            "by_type": {
                t: sum(1 for m in members if m.agent_type == t and m.status == "alive")
                for t in {m.agent_type for m in members}
            },
        }
```

## Solution 3: Topology Change Event Bus

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, List, Optional

@dataclass
class TopologyChangeEvent:
    event_id: str
    event_type: str    # "joined" | "left" | "suspect" | "dead" | "recovered" | "capability_change"
    agent_id: str
    agent_type: str
    endpoint: str
    capabilities: List[str]
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

class TopologyChangeEventBus:
    """
    Async event bus for topology change events.
    Subscribers receive events filtered by event_type or agent_type.
    Routing tables, load balancers, and dashboards subscribe here.
    """

    def __init__(self):
        self._queues: Dict[str, asyncio.Queue] = {}
        self._sync_handlers: List[Callable[[TopologyChangeEvent], None]] = []
        self._event_history: List[TopologyChangeEvent] = []
        self._max_history = 1000

    def subscribe(self, subscriber_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._queues[subscriber_id] = q
        return q

    def add_sync_handler(self, handler: Callable[[TopologyChangeEvent], None]) -> None:
        self._sync_handlers.append(handler)

    async def publish(self, event: TopologyChangeEvent) -> None:
        if len(self._event_history) >= self._max_history:
            self._event_history.pop(0)
        self._event_history.append(event)

        for handler in self._sync_handlers:
            try:
                handler(event)
            except Exception as exc:
                print(f"[topology_bus] sync handler error: {exc}")

        for sub_id, queue in self._queues.items():
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                print(f"[topology_bus] queue full for subscriber {sub_id}")

    def recent_events(
        self,
        event_types: Optional[List[str]] = None,
        since_seconds: float = 300.0,
    ) -> List[TopologyChangeEvent]:
        cutoff = time.time() - since_seconds
        return [
            e for e in self._event_history
            if e.timestamp >= cutoff
            and (event_types is None or e.event_type in event_types)
        ]
```

## Solution 4: Topology-Aware Router

```python
import random
import time
from typing import Callable, Dict, List, Optional

class TopologyAwareRouter:
    """
    Routes tasks to alive agents using topology events to keep
    routing tables current. Supports capability-based routing,
    zone-aware routing, and load-weighted selection.
    """

    def __init__(self, registry: AgentMembershipRegistry):
        self._registry = registry
        self._route_cache: Dict[str, List[str]] = {}   # capability -> agent_ids
        self._cache_ts: float = 0.0
        self._cache_ttl = 5.0   # rebuild routing table every 5s

    def _rebuild_cache(self) -> None:
        cache: Dict[str, List[str]] = {}
        for member in self._registry.alive_members():
            for cap in member.capabilities:
                cache.setdefault(cap, []).append(member.agent_id)
        self._route_cache = cache
        self._cache_ts = time.time()

    def _get_route_cache(self) -> Dict[str, List[str]]:
        if time.time() - self._cache_ts > self._cache_ttl:
            self._rebuild_cache()
        return self._route_cache

    def route(
        self,
        required_capability: str,
        strategy: str = "least_loaded",
        preferred_zone: Optional[str] = None,
    ) -> Optional[AgentMembershipRecord]:
        cache = self._get_route_cache()
        candidate_ids = cache.get(required_capability, [])
        if not candidate_ids:
            return None

        candidates = [
            m for m in self._registry.alive_members()
            if m.agent_id in candidate_ids
            and (preferred_zone is None or m.zone == preferred_zone or True)
        ]
        if not candidates:
            return None

        if strategy == "least_loaded":
            return min(candidates, key=lambda m: m.in_flight_tasks)
        elif strategy == "round_robin":
            return random.choice(candidates)
        elif strategy == "zone_affine":
            zoned = [m for m in candidates if m.zone == preferred_zone]
            return min(zoned or candidates, key=lambda m: m.in_flight_tasks)
        return candidates[0]

    def invalidate(self) -> None:
        self._cache_ts = 0.0
```

## Solution 5: Topology Drift Detector

```python
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List

@dataclass
class TopologySnapshot:
    timestamp: float
    alive_count: int
    by_type: Dict[str, int]
    agent_ids: frozenset

class TopologyDriftDetector:
    """
    Detects when the topology has drifted significantly from a baseline.
    Alerts on: sudden membership drops (cascade failure), unexpected agent type
    imbalances, or rapid join/leave churn (sign of crashing loops).
    """

    def __init__(self, history_size: int = 60):
        self._history: Deque[TopologySnapshot] = deque(maxlen=history_size)
        self._alerts: List[dict] = []

    def record(self, registry: AgentMembershipRegistry) -> TopologySnapshot:
        snap_data = registry.topology_snapshot()
        members = registry.alive_members()
        snap = TopologySnapshot(
            timestamp=time.time(),
            alive_count=snap_data["alive"],
            by_type=snap_data["by_type"],
            agent_ids=frozenset(m.agent_id for m in members),
        )
        self._history.append(snap)
        self._check_drift(snap)
        return snap

    def _check_drift(self, current: TopologySnapshot) -> None:
        if len(self._history) < 3:
            return
        recent = list(self._history)[-5:]
        avg_alive = sum(s.alive_count for s in recent[:-1]) / max(len(recent) - 1, 1)

        if avg_alive > 0 and current.alive_count < avg_alive * 0.7:
            self._alerts.append({
                "type": "membership_drop",
                "from": avg_alive,
                "to": current.alive_count,
                "drop_pct": round((1 - current.alive_count / avg_alive) * 100, 1),
                "timestamp": current.timestamp,
            })

        # Detect churn: many agents leaving and rejoining
        if len(self._history) >= 2:
            prev = list(self._history)[-2]
            churn = len(current.agent_ids.symmetric_difference(prev.agent_ids))
            total = max(len(current.agent_ids | prev.agent_ids), 1)
            if churn / total > 0.3:
                self._alerts.append({
                    "type": "high_churn",
                    "churn_count": churn,
                    "churn_rate": round(churn / total, 3),
                    "timestamp": current.timestamp,
                })

    def recent_alerts(self, window_seconds: float = 300.0) -> List[dict]:
        cutoff = time.time() - window_seconds
        return [a for a in self._alerts if a["timestamp"] >= cutoff]
```

## Solution 6: Topology Dashboard

```python
import time
from typing import Dict

class TopologyDashboard:
    """
    Renders a unified topology health view for monitoring systems.
    Combines membership registry, event history, drift alerts, and router stats.
    """

    def __init__(
        self,
        registry: AgentMembershipRegistry,
        event_bus: TopologyChangeEventBus,
        drift_detector: TopologyDriftDetector,
        router: TopologyAwareRouter,
    ):
        self._registry = registry
        self._bus = event_bus
        self._drift = drift_detector
        self._router = router

    def render(self) -> dict:
        snapshot = self._registry.topology_snapshot()
        members = self._registry.alive_members()
        recent_events = self._bus.recent_events(since_seconds=300.0)
        drift_alerts = self._drift.recent_alerts(window_seconds=300.0)

        return {
            "generated_at": time.time(),
            "topology": snapshot,
            "recent_changes": [
                {
                    "type": e.event_type,
                    "agent_id": e.agent_id,
                    "agent_type": e.agent_type,
                    "ts": e.timestamp,
                }
                for e in recent_events[-20:]
            ],
            "drift_alerts": drift_alerts,
            "agent_load": [
                {
                    "agent_id": m.agent_id,
                    "type": m.agent_type,
                    "zone": m.zone,
                    "in_flight": m.in_flight_tasks,
                    "cpu_pct": m.cpu_percent,
                    "age_seconds": round(time.time() - m.joined_at, 1),
                    "last_heartbeat_age": round(time.time() - m.last_heartbeat, 1),
                }
                for m in sorted(members, key=lambda m: m.in_flight_tasks, reverse=True)
            ],
            "healthy": snapshot["dead"] == 0 and len(drift_alerts) == 0,
        }
```

## Comparison

| Approach | Liveness Detection | Change Events | Routing Update | Drift Detection |
|---|---|---|---|---|
| AgentMembershipRecord | Via heartbeat age | No | No | No |
| AgentMembershipRegistry | Yes (suspect/dead) | Via callbacks | No | No |
| TopologyChangeEventBus | No | Yes (async) | Via subscribers | No |
| TopologyAwareRouter | Via registry | Via invalidation | Yes (TTL cache) | No |
| TopologyDriftDetector | No | No | No | Yes (drop/churn) |
| TopologyDashboard | Via registry | Via bus | Via router | Via detector |

**Best for production**: Have every agent emit heartbeats every 10 seconds containing current load metrics. Run `AgentMembershipRegistry.run_health_check()` every 5 seconds to update suspect/dead status. Publish all status changes to `TopologyChangeEventBus`. Subscribe `TopologyAwareRouter` to the bus and call `router.invalidate()` on any "dead" or "left" event for immediate route table rebuild. Run `TopologyDriftDetector.record()` every minute to catch cascade failures before they become user-visible. Expose `TopologyDashboard.render()` as an internal health endpoint queried by your monitoring stack.
