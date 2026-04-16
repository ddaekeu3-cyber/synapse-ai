---
title: "Agent Doesn't Implement Agent Lifecycle Event Tracking"
description: "Agents without lifecycle event instrumentation have no record of when they were created, how long they ran, when they were suspended or resumed, and what caused them to terminate. Implement agent lifecycle tracking to capture state transitions (created → active → suspended → terminated), duration in each state, and termination reasons — enabling operational visibility, billing accuracy, and post-mortem analysis."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-agent-lifecycle-event-tracking
tags: [lifecycle-tracking, agent-state-machine, observability, operational-metrics, session-duration, telemetry]
symptoms:
  - "No record of how long agent sessions actually ran — billing estimates are guesses"
  - "Agent terminated unexpectedly but there's no event recording when or why"
  - "Cannot answer 'how many agents are currently active' from operational dashboards"
  - "Orphaned agent sessions accumulate with no visibility into their state"
  - "Post-mortem analysis impossible because lifecycle transitions were never recorded"
---

## Why This Happens

Agent frameworks create and destroy agent instances without emitting structured lifecycle events. Unlike HTTP requests that have clear start/end points in access logs, agent sessions span minutes to hours and transition through multiple states. Without explicit lifecycle instrumentation, operational questions — how many agents are running, how long do sessions last on average, what fraction terminate with errors — cannot be answered from telemetry. Lifecycle event tracking adds a thin instrumentation layer that records every state transition as a structured event, enabling duration metrics, state distribution dashboards, and root-cause analysis of abnormal terminations.

## Solution 1: Agent Lifecycle State Machine

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

class AgentState(str, Enum):
    CREATED = "created"
    INITIALIZING = "initializing"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    RESUMING = "resuming"
    COMPLETING = "completing"
    TERMINATED = "terminated"
    ERROR = "error"

VALID_TRANSITIONS = {
    AgentState.CREATED:      {AgentState.INITIALIZING, AgentState.ERROR},
    AgentState.INITIALIZING: {AgentState.ACTIVE, AgentState.ERROR},
    AgentState.ACTIVE:       {AgentState.SUSPENDED, AgentState.COMPLETING, AgentState.ERROR},
    AgentState.SUSPENDED:    {AgentState.RESUMING, AgentState.TERMINATED, AgentState.ERROR},
    AgentState.RESUMING:     {AgentState.ACTIVE, AgentState.ERROR},
    AgentState.COMPLETING:   {AgentState.TERMINATED, AgentState.ERROR},
    AgentState.ERROR:        {AgentState.TERMINATED},
    AgentState.TERMINATED:   set(),
}

@dataclass
class StateTransition:
    from_state: AgentState
    to_state: AgentState
    timestamp: float = field(default_factory=time.time)
    reason: str = ""
    duration_in_from_ms: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass
class AgentLifecycleRecord:
    agent_id: str
    agent_type: str
    session_id: str = ""
    user_id: str = ""
    current_state: AgentState = AgentState.CREATED
    created_at: float = field(default_factory=time.time)
    terminated_at: Optional[float] = None
    termination_reason: str = ""
    transitions: List[StateTransition] = field(default_factory=list)
    state_durations_ms: Dict[str, float] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.current_state in (AgentState.TERMINATED, AgentState.ERROR)

    @property
    def total_duration_ms(self) -> float:
        end = self.terminated_at or time.time()
        return (end - self.created_at) * 1000

    @property
    def active_duration_ms(self) -> float:
        return self.state_durations_ms.get(AgentState.ACTIVE.value, 0.0)
```

## Solution 2: Lifecycle Event Emitter

```python
import time
from typing import Callable, List, Optional

class AgentLifecycleEventEmitter:
    """
    Manages state transitions for a single agent lifecycle record.
    Validates transitions, computes duration in each state, and fires
    event handlers on every transition.
    """

    def __init__(self, record: AgentLifecycleRecord):
        self._record = record
        self._state_entered_at = time.time()
        self._handlers: List[Callable[[AgentLifecycleRecord, StateTransition], None]] = []

    def add_handler(
        self, handler: Callable[[AgentLifecycleRecord, StateTransition], None]
    ) -> None:
        self._handlers.append(handler)

    def transition(
        self,
        to_state: AgentState,
        reason: str = "",
        metadata: dict = None,
    ) -> StateTransition:
        from_state = self._record.current_state
        allowed = VALID_TRANSITIONS.get(from_state, set())
        if to_state not in allowed:
            raise ValueError(
                f"invalid transition {from_state} → {to_state}; "
                f"allowed: {[s.value for s in allowed]}"
            )

        now = time.time()
        duration_ms = (now - self._state_entered_at) * 1000

        # Accumulate time in the from_state
        key = from_state.value
        self._record.state_durations_ms[key] = (
            self._record.state_durations_ms.get(key, 0.0) + duration_ms
        )

        transition = StateTransition(
            from_state=from_state,
            to_state=to_state,
            timestamp=now,
            reason=reason,
            duration_in_from_ms=duration_ms,
            metadata=metadata or {},
        )
        self._record.transitions.append(transition)
        self._record.current_state = to_state
        self._state_entered_at = now

        if to_state in (AgentState.TERMINATED, AgentState.ERROR):
            self._record.terminated_at = now
            self._record.termination_reason = reason

        for handler in self._handlers:
            try:
                handler(self._record, transition)
            except Exception as exc:
                print(f"[lifecycle] handler error: {exc}")

        return transition
```

## Solution 3: Lifecycle Registry

```python
import asyncio
import time
from collections import defaultdict
from typing import Dict, Iterator, List, Optional

class AgentLifecycleRegistry:
    """
    Central registry of all agent lifecycle records.
    Tracks active, suspended, and recently terminated agents.
    Supports querying by state, user, session, and agent type.
    """

    def __init__(self, completed_retention_seconds: float = 3600.0):
        self._emitters: Dict[str, AgentLifecycleEventEmitter] = {}
        self._records: Dict[str, AgentLifecycleRecord] = {}
        self._retention = completed_retention_seconds
        self._total_created = 0
        self._total_terminated = 0

    def create(
        self,
        agent_id: str,
        agent_type: str,
        session_id: str = "",
        user_id: str = "",
        metadata: dict = None,
    ) -> AgentLifecycleEventEmitter:
        record = AgentLifecycleRecord(
            agent_id=agent_id,
            agent_type=agent_type,
            session_id=session_id,
            user_id=user_id,
            metadata=metadata or {},
        )
        emitter = AgentLifecycleEventEmitter(record)
        self._records[agent_id] = record
        self._emitters[agent_id] = emitter
        self._total_created += 1
        return emitter

    def get_emitter(self, agent_id: str) -> Optional[AgentLifecycleEventEmitter]:
        return self._emitters.get(agent_id)

    def get_record(self, agent_id: str) -> Optional[AgentLifecycleRecord]:
        return self._records.get(agent_id)

    def active_agents(self, agent_type: Optional[str] = None) -> List[AgentLifecycleRecord]:
        return [
            r for r in self._records.values()
            if r.current_state == AgentState.ACTIVE
            and (agent_type is None or r.agent_type == agent_type)
        ]

    def by_state(self) -> Dict[str, int]:
        counts: Dict[str, int] = defaultdict(int)
        for r in self._records.values():
            counts[r.current_state.value] += 1
        return dict(counts)

    def purge_completed(self) -> int:
        cutoff = time.time() - self._retention
        to_purge = [
            aid for aid, r in self._records.items()
            if r.is_terminal and r.terminated_at and r.terminated_at < cutoff
        ]
        for aid in to_purge:
            del self._records[aid]
            self._emitters.pop(aid, None)
        self._total_terminated += len(to_purge)
        return len(to_purge)

    def summary(self) -> dict:
        return {
            "total_created": self._total_created,
            "total_purged": self._total_terminated,
            "current_states": self.by_state(),
            "active_count": len(self.active_agents()),
        }
```

## Solution 4: Lifecycle Duration Analyzer

```python
import statistics
import time
from collections import defaultdict
from typing import Dict, List, Optional

class LifecycleDurationAnalyzer:
    """
    Computes duration statistics across completed agent lifecycles.
    Reports: median session duration, active time ratio, suspension rate,
    error termination rate — grouped by agent type.
    """

    def __init__(self, registry: AgentLifecycleRegistry):
        self._registry = registry

    def analyze(
        self,
        agent_type: Optional[str] = None,
        since_seconds: float = 3600.0,
    ) -> dict:
        cutoff = time.time() - since_seconds
        records = [
            r for r in self._registry._records.values()
            if r.is_terminal
            and (r.terminated_at or 0) >= cutoff
            and (agent_type is None or r.agent_type == agent_type)
        ]
        if not records:
            return {"sample_count": 0}

        durations_ms = [r.total_duration_ms for r in records]
        active_ms = [r.active_duration_ms for r in records]
        active_ratios = [
            a / max(d, 1) for a, d in zip(active_ms, durations_ms)
        ]
        error_count = sum(1 for r in records if r.current_state == AgentState.ERROR)
        suspended = sum(
            1 for r in records
            if any(t.from_state == AgentState.ACTIVE and t.to_state == AgentState.SUSPENDED
                   for t in r.transitions)
        )

        return {
            "sample_count": len(records),
            "total_duration_ms": {
                "p50": round(statistics.median(durations_ms), 1),
                "p95": round(sorted(durations_ms)[int(len(durations_ms) * 0.95)], 1),
                "mean": round(statistics.mean(durations_ms), 1),
            },
            "active_time_ratio": {
                "mean": round(statistics.mean(active_ratios), 4),
                "p50": round(statistics.median(active_ratios), 4),
            },
            "error_rate": round(error_count / len(records), 4),
            "suspension_rate": round(suspended / len(records), 4),
        }
```

## Solution 5: Orphan Agent Detector

```python
import time
from dataclasses import dataclass
from typing import List

@dataclass
class OrphanCandidate:
    agent_id: str
    agent_type: str
    current_state: AgentState
    last_transition_age_seconds: float
    reason: str

class OrphanAgentDetector:
    """
    Detects agents that have been stuck in a non-terminal state for too long.
    An agent in ACTIVE state for 2 hours without transitions is likely orphaned.
    Orphaned agents consume resources and may hold locks or connections.
    """

    def __init__(
        self,
        registry: AgentLifecycleRegistry,
        max_active_seconds: float = 7200.0,
        max_suspended_seconds: float = 86400.0,
        max_initializing_seconds: float = 300.0,
    ):
        self._registry = registry
        self._thresholds = {
            AgentState.ACTIVE: max_active_seconds,
            AgentState.SUSPENDED: max_suspended_seconds,
            AgentState.INITIALIZING: max_initializing_seconds,
            AgentState.RESUMING: 300.0,
            AgentState.COMPLETING: 600.0,
        }

    def detect(self) -> List[OrphanCandidate]:
        now = time.time()
        orphans = []

        for record in self._registry._records.values():
            if record.is_terminal:
                continue
            threshold = self._thresholds.get(record.current_state)
            if threshold is None:
                continue

            last_transition_at = (
                record.transitions[-1].timestamp if record.transitions else record.created_at
            )
            age = now - last_transition_at
            if age > threshold:
                orphans.append(OrphanCandidate(
                    agent_id=record.agent_id,
                    agent_type=record.agent_type,
                    current_state=record.current_state,
                    last_transition_age_seconds=round(age, 1),
                    reason=f"stuck in {record.current_state.value} for {age:.0f}s > {threshold}s",
                ))

        return orphans
```

## Solution 6: Lifecycle Dashboard

```python
import time
from typing import Optional

class AgentLifecycleDashboard:
    """
    Renders a unified operational view of the agent fleet lifecycle status.
    """

    def __init__(
        self,
        registry: AgentLifecycleRegistry,
        analyzer: LifecycleDurationAnalyzer,
        orphan_detector: OrphanAgentDetector,
    ):
        self._registry = registry
        self._analyzer = analyzer
        self._orphan_detector = orphan_detector

    def render(self) -> dict:
        state_dist = self._registry.by_state()
        orphans = self._orphan_detector.detect()
        duration_analysis = self._analyzer.analyze(since_seconds=3600.0)

        active = self._registry.active_agents()
        top_types = {}
        for r in active:
            top_types[r.agent_type] = top_types.get(r.agent_type, 0) + 1

        return {
            "generated_at": time.time(),
            "fleet_summary": self._registry.summary(),
            "state_distribution": state_dist,
            "active_by_type": top_types,
            "orphaned_agents": [
                {
                    "agent_id": o.agent_id,
                    "state": o.current_state.value,
                    "stuck_seconds": o.last_transition_age_seconds,
                    "reason": o.reason,
                }
                for o in orphans
            ],
            "duration_stats_1h": duration_analysis,
            "alerts": [
                f"orphaned agent: {o.agent_id} ({o.reason})"
                for o in orphans
            ],
        }
```

## Comparison

| Approach | State Machine | Duration Tracking | Orphan Detection | Fleet Dashboard |
|---|---|---|---|---|
| AgentLifecycleRecord | Via transitions list | Yes (per state) | No | No |
| AgentLifecycleEventEmitter | Yes (validated) | Yes (accumulates) | No | No |
| AgentLifecycleRegistry | Via emitter | Via records | No | Partial |
| LifecycleDurationAnalyzer | No | Yes (statistics) | No | No |
| OrphanAgentDetector | No | Via last transition | Yes | No |
| AgentLifecycleDashboard | No | Via analyzer | Via detector | Yes |

**Best for production**: Create one `AgentLifecycleRecord` per agent instance via `AgentLifecycleRegistry.create()` at instantiation. Emit transitions through `AgentLifecycleEventEmitter.transition()` at every state change — instrument init completion, tool call start/end, suspension triggers, and termination. Register event handlers that publish transitions to your metrics pipeline (Prometheus, Datadog) with labels `agent_type` and `from_state`. Run `OrphanAgentDetector.detect()` every 5 minutes and alert on results — orphaned agents are the most common source of resource leaks in long-running agent deployments.
