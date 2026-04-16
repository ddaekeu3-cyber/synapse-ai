---
title: "Agent Doesn't Implement Change Data Capture for State Audit Trail"
description: "Agents that mutate state without capturing before/after snapshots have no audit trail for compliance, debugging, or rollback. Implement Change Data Capture (CDC) to record every state mutation as an immutable event with before/after values, actor, timestamp, and causation chain — enabling full replay, point-in-time state reconstruction, and compliance reporting."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-change-data-capture-for-state-audit-trail
tags: [change-data-capture, audit-trail, state-mutation, observability, compliance, event-sourcing]
symptoms:
  - "Agent memory was modified and nobody knows when, by whom, or what the previous value was"
  - "Compliance audit asks for all changes to user data in the last 90 days — no such log exists"
  - "Debug session: agent produced wrong output but there's no record of what state it read"
  - "State rollback required after a bug — no snapshot of pre-bug values available"
  - "Multiple agents modify shared state with no record of which agent made which change"
---

## Why This Happens

Standard application logging captures what code ran, not what data changed. CDC fills this gap by recording every write operation as a structured event: the entity changed, the field changed, the old value, the new value, and the causal context (which agent, session, and request triggered the change). This pattern is borrowed from database CDC (Postgres logical replication, Debezium) and adapted for in-application state management.

## Solution 1: Change Event Model

```python
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

@dataclass
class ChangeEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    entity_type: str = ""        # "agent_memory" | "user_profile" | "session_state"
    entity_id: str = ""          # primary key of the changed entity
    operation: str = ""          # "create" | "update" | "delete"
    field_path: str = ""         # dot-notation path, e.g., "preferences.language"
    old_value: Any = None
    new_value: Any = None

    # Causation chain
    actor_type: str = ""         # "agent" | "user" | "system" | "tool"
    actor_id: str = ""
    session_id: str = ""
    request_id: str = ""
    causation_event_id: str = "" # event that caused this change (for chains)

    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "operation": self.operation,
            "field_path": self.field_path,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "actor_type": self.actor_type,
            "actor_id": self.actor_id,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "causation_event_id": self.causation_event_id,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)
```

## Solution 2: CDC-Aware State Store

```python
import copy
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

class CDCAwareStateStore:
    """
    In-memory state store that emits ChangeEvents on every mutation.
    Wraps dict-like state access and intercepts set/delete operations.
    """

    def __init__(
        self,
        entity_type: str,
        entity_id: str,
        event_emitter: "CDCEventEmitter",
    ):
        self._type = entity_type
        self._id = entity_id
        self._emitter = event_emitter
        self._state: Dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._state.get(key, default)

    def set(
        self,
        key: str,
        value: Any,
        actor_type: str = "system",
        actor_id: str = "",
        session_id: str = "",
        request_id: str = "",
    ) -> ChangeEvent:
        old_value = copy.deepcopy(self._state.get(key))
        self._state[key] = value

        operation = "update" if key in self._state else "create"
        event = ChangeEvent(
            entity_type=self._type,
            entity_id=self._id,
            operation=operation,
            field_path=key,
            old_value=old_value,
            new_value=copy.deepcopy(value),
            actor_type=actor_type,
            actor_id=actor_id,
            session_id=session_id,
            request_id=request_id,
        )
        self._emitter.emit(event)
        return event

    def delete(
        self,
        key: str,
        actor_type: str = "system",
        actor_id: str = "",
        session_id: str = "",
        request_id: str = "",
    ) -> Optional[ChangeEvent]:
        if key not in self._state:
            return None
        old_value = copy.deepcopy(self._state.pop(key))
        event = ChangeEvent(
            entity_type=self._type,
            entity_id=self._id,
            operation="delete",
            field_path=key,
            old_value=old_value,
            new_value=None,
            actor_type=actor_type,
            actor_id=actor_id,
            session_id=session_id,
            request_id=request_id,
        )
        self._emitter.emit(event)
        return event

    def snapshot(self) -> dict:
        return copy.deepcopy(self._state)
```

## Solution 3: CDC Event Emitter and Storage

```python
import json
import time
from typing import Callable, List, Optional

class CDCEventEmitter:
    """
    Receives ChangeEvents and fans them out to registered sinks.
    Sinks can be: append-only log files, database tables, message queues.
    """

    def __init__(self):
        self._sinks: List[Callable[[ChangeEvent], None]] = []
        self._buffer: List[ChangeEvent] = []
        self._total_emitted = 0

    def add_sink(self, sink: Callable[[ChangeEvent], None]) -> None:
        self._sinks.append(sink)

    def emit(self, event: ChangeEvent) -> None:
        self._buffer.append(event)
        self._total_emitted += 1
        for sink in self._sinks:
            try:
                sink(event)
            except Exception as exc:
                print(f"[cdc] sink error: {exc}")

    def flush(self) -> List[ChangeEvent]:
        events = list(self._buffer)
        self._buffer.clear()
        return events

    def stats(self) -> dict:
        return {
            "total_emitted": self._total_emitted,
            "buffered": len(self._buffer),
            "sinks": len(self._sinks),
        }


class CDCEventLog:
    """
    Append-only in-memory event log for CDC events.
    Supports querying by entity, actor, time range, and field path.
    """

    def __init__(self, max_events: int = 100_000):
        self._events: List[ChangeEvent] = []
        self._max = max_events

    def append(self, event: ChangeEvent) -> None:
        if len(self._events) >= self._max:
            self._events.pop(0)
        self._events.append(event)

    def query_by_entity(
        self,
        entity_type: str,
        entity_id: Optional[str] = None,
        since: Optional[float] = None,
        limit: int = 100,
    ) -> List[ChangeEvent]:
        results = [
            e for e in self._events
            if e.entity_type == entity_type
            and (entity_id is None or e.entity_id == entity_id)
            and (since is None or e.timestamp >= since)
        ]
        return results[-limit:]

    def query_by_session(
        self, session_id: str, limit: int = 200
    ) -> List[ChangeEvent]:
        return [e for e in self._events if e.session_id == session_id][-limit:]

    def query_by_actor(
        self, actor_id: str, since: Optional[float] = None, limit: int = 100
    ) -> List[ChangeEvent]:
        return [
            e for e in self._events
            if e.actor_id == actor_id
            and (since is None or e.timestamp >= since)
        ][-limit:]
```

## Solution 4: State Reconstructor

```python
import copy
from typing import Any, Dict, List, Optional

class StateReconstructor:
    """
    Reconstructs the state of an entity at any point in time by replaying
    ChangeEvents forward from the beginning or from a snapshot.
    """

    def reconstruct_at(
        self,
        entity_type: str,
        entity_id: str,
        target_timestamp: float,
        event_log: CDCEventLog,
        initial_snapshot: Optional[dict] = None,
    ) -> dict:
        """Returns the entity state as it was at target_timestamp."""
        state = copy.deepcopy(initial_snapshot or {})

        events = event_log.query_by_entity(
            entity_type, entity_id, limit=10000
        )

        for event in events:
            if event.timestamp > target_timestamp:
                break
            state = self._apply_event(state, event)

        return state

    def _apply_event(self, state: dict, event: ChangeEvent) -> dict:
        state = copy.deepcopy(state)

        if event.operation == "delete":
            state.pop(event.field_path, None)
        elif event.operation in ("create", "update"):
            # Handle nested dot-notation paths
            keys = event.field_path.split(".")
            target = state
            for key in keys[:-1]:
                if key not in target:
                    target[key] = {}
                target = target[key]
            target[keys[-1]] = copy.deepcopy(event.new_value)

        return state

    def diff_at_timestamps(
        self,
        entity_type: str,
        entity_id: str,
        t1: float,
        t2: float,
        event_log: CDCEventLog,
    ) -> List[dict]:
        """Returns the list of changes between two timestamps."""
        events = event_log.query_by_entity(entity_type, entity_id, limit=10000)
        return [
            {
                "event_id": e.event_id,
                "field": e.field_path,
                "operation": e.operation,
                "old": e.old_value,
                "new": e.new_value,
                "actor": f"{e.actor_type}:{e.actor_id}",
                "timestamp": e.timestamp,
            }
            for e in events if t1 <= e.timestamp <= t2
        ]
```

## Solution 5: Compliance Report Generator

```python
import time
from dataclasses import dataclass
from typing import List, Optional

@dataclass
class ComplianceReport:
    subject_id: str
    report_type: str    # "gdpr_access" | "audit_trail" | "change_history"
    period_start: float
    period_end: float
    events: List[dict]
    generated_at: float

class ComplianceReportGenerator:
    """
    Generates compliance reports from CDC event logs.
    Supports GDPR data access requests, audit trail exports,
    and per-actor change history reports.
    """

    def __init__(self, event_log: CDCEventLog):
        self._log = event_log

    def gdpr_data_access_report(
        self, user_id: str, since: Optional[float] = None
    ) -> ComplianceReport:
        """All state changes touching user data for a specific user."""
        events = self._log.query_by_actor(user_id, since=since, limit=10000)
        period_start = min((e.timestamp for e in events), default=time.time())
        return ComplianceReport(
            subject_id=user_id,
            report_type="gdpr_access",
            period_start=period_start,
            period_end=time.time(),
            events=[self._redact_sensitive(e.to_dict()) for e in events],
            generated_at=time.time(),
        )

    def audit_trail_report(
        self,
        entity_type: str,
        entity_id: str,
        since: float,
        until: Optional[float] = None,
    ) -> ComplianceReport:
        """Full audit trail for a specific entity."""
        until = until or time.time()
        events = self._log.query_by_entity(entity_type, entity_id, since=since)
        filtered = [e for e in events if e.timestamp <= until]
        return ComplianceReport(
            subject_id=f"{entity_type}:{entity_id}",
            report_type="audit_trail",
            period_start=since,
            period_end=until,
            events=[e.to_dict() for e in filtered],
            generated_at=time.time(),
        )

    def _redact_sensitive(self, event_dict: dict) -> dict:
        """Remove PII from values in report export."""
        for key in ("old_value", "new_value"):
            if isinstance(event_dict.get(key), str) and len(event_dict[key]) > 100:
                event_dict[key] = event_dict[key][:50] + "...[redacted]"
        return event_dict
```

## Solution 6: CDC Health Monitor

```python
import time
from dataclasses import dataclass
from typing import Dict

class CDCHealthMonitor:
    """
    Monitors CDC event throughput and detects anomalies:
    - Event burst (unusual spike in changes — possible bug or attack)
    - Sink lag (events not being consumed from buffer)
    - Missing entity coverage (entities modified outside CDC wrapper)
    """

    def __init__(
        self,
        emitter: CDCEventEmitter,
        event_log: CDCEventLog,
        burst_threshold: int = 500,   # events per minute
    ):
        self._emitter = emitter
        self._log = event_log
        self._threshold = burst_threshold
        self._last_minute_count = 0
        self._window_start = time.time()

    def check(self) -> dict:
        stats = self._emitter.stats()
        now = time.time()
        elapsed = now - self._window_start

        # Events per minute rate
        rate_per_min = (stats["total_emitted"] / max(elapsed, 1)) * 60

        alerts = []
        if rate_per_min > self._threshold:
            alerts.append({
                "type": "event_burst",
                "rate_per_minute": round(rate_per_min, 1),
                "threshold": self._threshold,
            })
        if stats["buffered"] > 1000:
            alerts.append({
                "type": "sink_lag",
                "buffered_events": stats["buffered"],
            })

        return {
            "healthy": len(alerts) == 0,
            "alerts": alerts,
            "stats": stats,
            "events_per_minute": round(rate_per_min, 1),
            "checked_at": now,
        }
```

## Comparison

| Approach | Before/After Values | Actor Tracking | Time Reconstruction | Compliance Export |
|---|---|---|---|---|
| ChangeEvent | Yes | Yes | N/A | Via to_json() |
| CDCAwareStateStore | Yes (deep copy) | Yes | No | No |
| CDCEventEmitter + Log | Yes | Yes | Via log queries | No |
| StateReconstructor | N/A (replay) | N/A | Yes | No |
| ComplianceReportGenerator | N/A (reports) | Via actor_id | Via events | Yes |
| CDCHealthMonitor | N/A | N/A | N/A | N/A |

**Best for production**: Wrap all mutable agent state in `CDCAwareStateStore`. Route all events through `CDCEventEmitter` with two sinks: in-memory `CDCEventLog` for fast local queries, and a persistent append-only store (database or S3 log) for compliance. Use `StateReconstructor` during post-incident analysis to replay state to the exact moment of failure. Run `CDCHealthMonitor` to catch event bursts that might indicate runaway mutations. Generate `ComplianceReport` on demand for GDPR access requests without touching application logic.
