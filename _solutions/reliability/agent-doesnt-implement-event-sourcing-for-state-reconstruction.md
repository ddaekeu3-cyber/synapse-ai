---
title: "Agent Doesn't Implement Event Sourcing for State Reconstruction"
description: "AI agents that store only current state lose the history of how they reached that state, making debugging, replay, and rollback impossible. Event sourcing records every state-changing action as an immutable event and derives current state by replaying the event log. Agents can rewind to any past point, reproduce bugs exactly, and reconstruct state after a crash without checkpointing."
date: 2025-02-18
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-event-sourcing-for-state-reconstruction
tags:
  - event-sourcing
  - state-reconstruction
  - replay
  - audit-trail
  - reliability
  - immutable-log
  - debugging
symptoms:
  - "After a crash the agent cannot determine which tasks were completed before the failure"
  - "No way to reproduce a bug because the state that caused it was overwritten"
  - "Rollback to a previous state requires restoring from a backup, not replaying events"
  - "The agent's current state is stored as mutable fields with no change history"
  - "Debugging requires guessing what sequence of tool calls produced the current broken state"
---

## Problem

Mutable state stored as current-value-only discards history. When an agent updates `task.status = "failed"`, the previous status, the transition timestamp, and the reason are gone unless explicitly logged. Event sourcing inverts this: the agent appends an immutable `TaskFailed` event to a log, and current state is always derived by replaying all events from the beginning (or from a snapshot). The log becomes the source of truth — it enables full replay for debugging, partial replay for rollback, and incremental snapshot creation to keep replay time bounded.

---

## Solution 1: AgentEvent — Immutable Event Record

```python
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class AgentEvent:
    """
    Immutable record of a single state-changing action.
    All agent state transitions are represented as events;
    the event log is the source of truth.

    Fields:
        event_id:   Unique identifier for this event
        event_type: Domain action name (e.g. "TaskClaimed", "ToolCallSucceeded")
        aggregate_id: The entity this event belongs to (session ID, task ID)
        sequence:   Monotonically increasing sequence number within the aggregate
        payload:    Event-specific data
        agent_id:   Which agent instance produced this event
        occurred_at: Wall-clock time the event happened
    """

    event_id: str
    event_type: str
    aggregate_id: str
    sequence: int
    payload: Dict[str, Any]
    agent_id: str = ""
    occurred_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, event_type: str,
                aggregate_id: str,
                sequence: int,
                payload: Dict[str, Any],
                agent_id: str = "",
                **metadata) -> "AgentEvent":
        return cls(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            aggregate_id=aggregate_id,
            sequence=sequence,
            payload=payload,
            agent_id=agent_id,
            occurred_at=time.time(),
            metadata=metadata,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "aggregate_id": self.aggregate_id,
            "sequence": self.sequence,
            "payload": self.payload,
            "agent_id": self.agent_id,
            "occurred_at": self.occurred_at,
            "metadata": self.metadata,
        }
```

---

## Solution 2: EventStore — Append-Only Event Log with Optimistic Concurrency

```python
import logging
import threading
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class OptimisticConcurrencyError(Exception):
    def __init__(self, aggregate_id: str, expected: int, actual: int):
        super().__init__(
            f"Concurrency conflict for '{aggregate_id}': "
            f"expected sequence {expected}, got {actual}"
        )


class EventStore:
    """
    Append-only, in-memory event store with optimistic concurrency control.
    Events are appended only if the aggregate's last sequence matches the
    expected sequence, preventing concurrent writers from silently overwriting.

    In production, back this with an append-only database table
    (Postgres with a UNIQUE constraint on (aggregate_id, sequence)).

    Usage:
        store = EventStore()
        store.append(AgentEvent.create("SessionStarted", "sess-001", 0, {...}))
        store.append(AgentEvent.create("TaskClaimed",    "sess-001", 1, {...}))
        events = store.load("sess-001")
    """

    def __init__(self):
        self._events: List[AgentEvent] = []
        self._lock = threading.Lock()
        self._subscribers: List[Callable[[AgentEvent], None]] = []

    def append(self, event: AgentEvent,
                expected_sequence: Optional[int] = None):
        with self._lock:
            existing = [e for e in self._events
                         if e.aggregate_id == event.aggregate_id]
            last_seq = existing[-1].sequence if existing else -1

            if expected_sequence is not None and last_seq != expected_sequence:
                raise OptimisticConcurrencyError(
                    event.aggregate_id, expected_sequence, last_seq
                )

            self._events.append(event)
            logger.debug(
                "event_appended type=%s aggregate=%s seq=%d",
                event.event_type, event.aggregate_id, event.sequence,
            )

        for sub in self._subscribers:
            try:
                sub(event)
            except Exception as exc:
                logger.warning("event_subscriber_error: %s", exc)

    def load(self, aggregate_id: str,
              from_sequence: int = 0,
              to_sequence: Optional[int] = None) -> List[AgentEvent]:
        with self._lock:
            events = [
                e for e in self._events
                if e.aggregate_id == aggregate_id
                and e.sequence >= from_sequence
                and (to_sequence is None or e.sequence <= to_sequence)
            ]
        return sorted(events, key=lambda e: e.sequence)

    def load_by_type(self, event_type: str) -> List[AgentEvent]:
        with self._lock:
            return [e for e in self._events if e.event_type == event_type]

    def subscribe(self, handler: Callable[[AgentEvent], None]):
        self._subscribers.append(handler)

    def all_aggregate_ids(self) -> List[str]:
        with self._lock:
            return list({e.aggregate_id for e in self._events})

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            type_counts: Dict[str, int] = {}
            for e in self._events:
                type_counts[e.event_type] = type_counts.get(e.event_type, 0) + 1
            return {
                "total_events": len(self._events),
                "aggregates": len(self.all_aggregate_ids()),
                "event_types": type_counts,
            }
```

---

## Solution 3: StateProjector — Reconstruct State by Replaying Events

```python
import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class StateProjector:
    """
    Reconstructs the current state of an aggregate by replaying its
    event log through registered event handlers. Each event type maps
    to a pure function that applies the event's payload to the state.

    Usage:
        projector = StateProjector()

        @projector.on("SessionStarted")
        def apply_started(state, payload):
            return {**state, "status": "active", "started_at": payload["ts"]}

        @projector.on("TaskClaimed")
        def apply_claimed(state, payload):
            tasks = state.get("tasks", {})
            tasks[payload["task_id"]] = "claimed"
            return {**state, "tasks": tasks}

        events = store.load("sess-001")
        current_state = projector.project(events)
    """

    def __init__(self, initial_state: Optional[Dict[str, Any]] = None):
        self._handlers: Dict[str, Callable] = {}
        self._initial = initial_state or {}

    def on(self, event_type: str):
        """Decorator to register an event handler."""
        def decorator(fn: Callable) -> Callable:
            self._handlers[event_type] = fn
            return fn
        return decorator

    def project(self, events: List[AgentEvent],
                 initial: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        state = dict(initial or self._initial)
        for event in sorted(events, key=lambda e: e.sequence):
            handler = self._handlers.get(event.event_type)
            if handler:
                try:
                    state = handler(state, event.payload)
                    state["_last_sequence"] = event.sequence
                    state["_last_event_type"] = event.event_type
                except Exception as exc:
                    logger.error(
                        "projection_error event=%s seq=%d: %s",
                        event.event_type, event.sequence, exc,
                    )
            else:
                logger.debug(
                    "projection_unhandled_event type=%s", event.event_type
                )
        return state

    def project_at(self, events: List[AgentEvent],
                    sequence: int) -> Dict[str, Any]:
        """Reconstruct state as it was after the given sequence number."""
        filtered = [e for e in events if e.sequence <= sequence]
        return self.project(filtered)
```

---

## Solution 4: EventSourcedAggregate — Base Class for Event-Sourced Entities

```python
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class EventSourcedAggregate:
    """
    Base class for event-sourced domain objects. Subclasses define apply()
    handlers and call emit() to record state changes. Current state is
    always derived from the event history, never set directly.

    Usage:
        class AgentSession(EventSourcedAggregate):
            def start(self, agent_id: str):
                self.emit("SessionStarted", {"agent_id": agent_id})

            def claim_task(self, task_id: str):
                if self.state.get("status") != "active":
                    raise ValueError("Session not active")
                self.emit("TaskClaimed", {"task_id": task_id})

            def apply_SessionStarted(self, payload):
                self.state["status"] = "active"
                self.state["agent_id"] = payload["agent_id"]

            def apply_TaskClaimed(self, payload):
                self.state.setdefault("tasks", []).append(payload["task_id"])

        session = AgentSession("sess-001", agent_id="agent-A")
        session.start("agent-A")
        session.claim_task("task-001")
        events = session.pending_events()
    """

    def __init__(self, aggregate_id: str, agent_id: str = ""):
        self.aggregate_id = aggregate_id
        self.agent_id = agent_id
        self.state: Dict[str, Any] = {}
        self._sequence = -1
        self._pending: List[AgentEvent] = []

    def emit(self, event_type: str, payload: Dict[str, Any]):
        self._sequence += 1
        event = AgentEvent.create(
            event_type=event_type,
            aggregate_id=self.aggregate_id,
            sequence=self._sequence,
            payload=payload,
            agent_id=self.agent_id,
        )
        self._apply(event)
        self._pending.append(event)

    def _apply(self, event: AgentEvent):
        handler_name = f"apply_{event.event_type}"
        handler = getattr(self, handler_name, None)
        if handler:
            handler(event.payload)
        else:
            logger.debug(
                "aggregate_unhandled_event type=%s aggregate=%s",
                event.event_type, self.aggregate_id,
            )

    def load_from_history(self, events: List[AgentEvent]):
        for event in sorted(events, key=lambda e: e.sequence):
            self._apply(event)
            self._sequence = event.sequence
        self._pending.clear()

    def pending_events(self) -> List[AgentEvent]:
        events = list(self._pending)
        self._pending.clear()
        return events

    def version(self) -> int:
        return self._sequence
```

---

## Solution 5: SnapshotStore — Bound Replay Time with Periodic Snapshots

```python
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class Snapshot:
    aggregate_id: str
    sequence: int
    state: Dict[str, Any]
    created_at: float


class SnapshotStore:
    """
    Stores periodic state snapshots to bound event replay time.
    Instead of replaying all 10,000 events from the beginning,
    load the last snapshot and replay only events after its sequence.

    Usage:
        snap_store = SnapshotStore(snapshot_every=100)
        projector = StateProjector()

        # Load with snapshot optimization:
        state, from_seq = snap_store.latest(aggregate_id)
        events = event_store.load(aggregate_id, from_sequence=from_seq)
        current = projector.project(events, initial=state)

        # Periodically save snapshots:
        if snap_store.should_snapshot(aggregate_id, current_sequence):
            snap_store.save(aggregate_id, current_sequence, current_state)
    """

    def __init__(self, snapshot_every: int = 100):
        self._snapshots: Dict[str, List[Snapshot]] = {}
        self._every = snapshot_every

    def save(self, aggregate_id: str,
              sequence: int,
              state: Dict[str, Any]):
        snap = Snapshot(
            aggregate_id=aggregate_id,
            sequence=sequence,
            state=dict(state),
            created_at=time.time(),
        )
        if aggregate_id not in self._snapshots:
            self._snapshots[aggregate_id] = []
        self._snapshots[aggregate_id].append(snap)
        # Keep only last 3 snapshots per aggregate
        self._snapshots[aggregate_id] = self._snapshots[aggregate_id][-3:]
        logger.info(
            "snapshot_saved aggregate=%s sequence=%d", aggregate_id, sequence
        )

    def latest(self, aggregate_id: str) -> Tuple[Dict[str, Any], int]:
        """Returns (latest_snapshot_state, from_sequence). from_sequence=0 if none."""
        snaps = self._snapshots.get(aggregate_id, [])
        if not snaps:
            return {}, 0
        latest = max(snaps, key=lambda s: s.sequence)
        return dict(latest.state), latest.sequence + 1

    def should_snapshot(self, aggregate_id: str,
                         current_sequence: int) -> bool:
        _, last_snap_seq = self.latest(aggregate_id)
        events_since = current_sequence - max(last_snap_seq - 1, -1)
        return events_since >= self._every
```

---

## Solution 6: EventSourcedAgentSession — Full Event-Sourced Agent State

```python
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class EventSourcedAgentSession(EventSourcedAggregate):
    """
    Full event-sourced session that records every agent action as an event
    and reconstructs state by replaying. Supports rollback, replay, and
    point-in-time state inspection.

    Usage:
        store = EventStore()
        snap_store = SnapshotStore()

        session = EventSourcedAgentSession.load("sess-001", store, snap_store)
        session.record_tool_call("web_search", {"query": "AI"}, success=True)
        session.record_tool_call("db_query", {"sql": "SELECT ..."}, success=False)

        for event in session.pending_events():
            store.append(event, expected_sequence=event.sequence - 1)

        # Replay to see state at any point:
        state_at_turn_3 = session.state_at(sequence=3)
    """

    def __init__(self, aggregate_id: str, agent_id: str = ""):
        super().__init__(aggregate_id, agent_id)
        self._event_store: Optional[EventStore] = None
        self._snap_store: Optional[SnapshotStore] = None

    @classmethod
    def load(cls, aggregate_id: str,
              event_store: EventStore,
              snap_store: Optional[SnapshotStore] = None,
              agent_id: str = "") -> "EventSourcedAgentSession":
        session = cls(aggregate_id, agent_id)
        session._event_store = event_store
        session._snap_store = snap_store

        if snap_store:
            initial_state, from_seq = snap_store.latest(aggregate_id)
            session.state = initial_state
            session._sequence = from_seq - 1
        else:
            from_seq = 0

        events = event_store.load(aggregate_id, from_sequence=from_seq)
        session.load_from_history(events)
        return session

    def record_tool_call(self, tool_name: str,
                          params: Dict[str, Any],
                          success: bool,
                          result_summary: str = ""):
        event_type = "ToolCallSucceeded" if success else "ToolCallFailed"
        self.emit(event_type, {
            "tool": tool_name,
            "params_keys": list(params.keys()),
            "result_summary": result_summary[:200],
        })

    def record_message(self, role: str, content_length: int):
        self.emit("MessageAdded", {"role": role, "chars": content_length})

    def apply_ToolCallSucceeded(self, payload):
        tools = self.state.setdefault("tool_calls", [])
        tools.append({"tool": payload["tool"], "status": "ok"})

    def apply_ToolCallFailed(self, payload):
        tools = self.state.setdefault("tool_calls", [])
        tools.append({"tool": payload["tool"], "status": "failed"})
        self.state["last_failure"] = payload["tool"]

    def apply_MessageAdded(self, payload):
        self.state["message_count"] = self.state.get("message_count", 0) + 1
        self.state["total_chars"] = (
            self.state.get("total_chars", 0) + payload["chars"]
        )

    def state_at(self, sequence: int) -> Dict[str, Any]:
        """Reconstruct state as it was after a given sequence number."""
        if not self._event_store:
            return {}
        events = self._event_store.load(self.aggregate_id)
        projector = StateProjector()
        projector.on("ToolCallSucceeded")(
            lambda s, p: {**s, "tool_calls": s.get("tool_calls", []) + [p]}
        )
        projector.on("ToolCallFailed")(
            lambda s, p: {**s, "last_failure": p.get("tool")}
        )
        return projector.project_at(events, sequence)

    def full_history(self) -> List[Dict[str, Any]]:
        if not self._event_store:
            return []
        return [e.to_dict() for e in self._event_store.load(self.aggregate_id)]
```

---

## Comparison

| Approach | Immutable Log | Replay | Rollback | Snapshots | Domain Model | Integrated |
|---|---|---|---|---|---|---|
| **AgentEvent** | Yes | No | No | No | No | No |
| **EventStore** | Yes | No | No | No | No | No |
| **StateProjector** | No | Yes | Yes | No | No | No |
| **EventSourcedAggregate** | Yes | Yes | No | No | Yes | No |
| **SnapshotStore** | No | Bounded | No | Yes | No | No |
| **EventSourcedAgentSession** | Yes | Yes | Yes | Yes | Yes | Yes |

**Key insight**: event sourcing's replay capability is the primary reliability benefit for agents — when an agent crashes mid-task, replaying its event log reconstructs exactly what was completed, what failed, and what was in-flight. The tradeoff is write amplification (every state change becomes an append) and growing replay time (mitigated by snapshots every 100–500 events). Use optimistic concurrency (`expected_sequence`) when multiple agent instances can write to the same aggregate; without it, two instances can append events at the same sequence number and diverge silently.
