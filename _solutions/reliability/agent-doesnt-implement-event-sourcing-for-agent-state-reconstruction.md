---
title: "Agent Doesn't Implement Event Sourcing for Agent State Reconstruction"
description: "Agents that mutate state in place without an event log cannot recover mid-task after a crash, cannot audit the exact sequence of decisions that led to an outcome, and cannot replay a task from a checkpoint. Implement event sourcing that appends every state-changing action as an immutable event, allowing state to be reconstructed at any point in time and tasks to be resumed after interruption."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-event-sourcing-for-agent-state-reconstruction
tags: [event-sourcing, state-reconstruction, crash-recovery, replay, audit-trail, checkpointing]
symptoms:
  - "Agent crash mid-task requires restarting from scratch with no recovery option"
  - "Cannot determine which tool call or decision caused an incorrect final state"
  - "No audit trail of the exact sequence of agent actions for a completed task"
  - "State is a dict that is mutated in place — no history of what it looked like before each step"
  - "Task replay for debugging is impossible because the original inputs are gone"
---

## Why This Happens

Most agents maintain state as a mutable object that is updated after each step. When the process crashes, the state is lost and the task must restart from the beginning. Event sourcing inverts this: the canonical record is the append-only event log, and current state is derived by replaying events from the beginning (or from a snapshot). This makes crash recovery trivial — resume by loading the log and replaying — and makes audit possible — the log is the exact record of what happened.

## Solution 1: Agent Event Model

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class AgentEventType(str, Enum):
    TASK_STARTED = "task_started"
    LLM_CALL_COMPLETED = "llm_call_completed"
    TOOL_CALL_DISPATCHED = "tool_call_dispatched"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    TOOL_CALL_FAILED = "tool_call_failed"
    CONTEXT_UPDATED = "context_updated"
    DECISION_MADE = "decision_made"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    CHECKPOINT_SAVED = "checkpoint_saved"


@dataclass
class AgentEvent:
    event_id: str
    task_id: str
    sequence_number: int          # monotonic per task
    event_type: AgentEventType
    payload: Dict[str, Any]
    occurred_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "task_id": self.task_id,
            "sequence_number": self.sequence_number,
            "event_type": self.event_type.value,
            "payload": self.payload,
            "occurred_at": self.occurred_at,
            "metadata": self.metadata,
        }
```

## Solution 2: Append-Only Event Store

```python
import json
import secrets
import time
from pathlib import Path
from threading import Lock
from typing import List, Optional


class AppendOnlyEventStore:
    """
    Persists agent events to a JSONL file per task.
    Supports loading all events for a task for replay or audit.
    """

    def __init__(self, base_dir: str = "/tmp/agent_events"):
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)
        self._locks: dict = {}
        self._global_lock = Lock()

    def _task_path(self, task_id: str) -> Path:
        safe_id = task_id.replace("/", "_").replace("..", "_")
        return self._base / f"{safe_id}.jsonl"

    def _lock_for(self, task_id: str) -> Lock:
        with self._global_lock:
            if task_id not in self._locks:
                self._locks[task_id] = Lock()
            return self._locks[task_id]

    def append(self, event: AgentEvent) -> None:
        with self._lock_for(event.task_id):
            with self._task_path(event.task_id).open("a") as f:
                f.write(json.dumps(event.to_dict()) + "\n")

    def load(self, task_id: str) -> List[AgentEvent]:
        path = self._task_path(task_id)
        if not path.exists():
            return []
        events = []
        with self._lock_for(task_id):
            for line in path.read_text().splitlines():
                try:
                    d = json.loads(line)
                    events.append(AgentEvent(
                        event_id=d["event_id"],
                        task_id=d["task_id"],
                        sequence_number=d["sequence_number"],
                        event_type=AgentEventType(d["event_type"]),
                        payload=d["payload"],
                        occurred_at=d.get("occurred_at", 0.0),
                        metadata=d.get("metadata", {}),
                    ))
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
        return sorted(events, key=lambda e: e.sequence_number)

    def last_sequence_number(self, task_id: str) -> int:
        events = self.load(task_id)
        return events[-1].sequence_number if events else -1
```

## Solution 3: Event-Sourced Agent State

```python
from typing import Any, Dict, List, Optional


class EventSourcedAgentState:
    """
    Derives current agent state by replaying events.
    Maintains a snapshot cache to avoid full replay on every access.
    """

    def __init__(self, task_id: str):
        self._task_id = task_id
        self._state: Dict[str, Any] = {}
        self._last_applied_seq = -1

    def apply(self, event: AgentEvent) -> None:
        if event.sequence_number <= self._last_applied_seq:
            return
        self._apply_event(event)
        self._last_applied_seq = event.sequence_number

    def _apply_event(self, event: AgentEvent) -> None:
        if event.event_type == AgentEventType.TASK_STARTED:
            self._state = {
                "status": "running",
                "task_id": self._task_id,
                "input": event.payload.get("input"),
                "tool_results": {},
                "decisions": [],
                "step_count": 0,
            }
        elif event.event_type == AgentEventType.TOOL_CALL_COMPLETED:
            tool_name = event.payload.get("tool_name", "")
            self._state.setdefault("tool_results", {})[tool_name] = event.payload.get("result")
            self._state["step_count"] = self._state.get("step_count", 0) + 1
        elif event.event_type == AgentEventType.TOOL_CALL_FAILED:
            self._state.setdefault("failures", []).append(event.payload)
        elif event.event_type == AgentEventType.DECISION_MADE:
            self._state.setdefault("decisions", []).append(event.payload.get("decision"))
        elif event.event_type in (AgentEventType.TASK_COMPLETED, AgentEventType.TASK_FAILED):
            self._state["status"] = event.event_type.value.split("_")[-1]
            self._state["outcome"] = event.payload.get("outcome")

    @classmethod
    def from_events(cls, task_id: str, events: List[AgentEvent]) -> "EventSourcedAgentState":
        state = cls(task_id)
        for event in events:
            state.apply(event)
        return state

    def snapshot(self) -> dict:
        return dict(self._state)
```

## Solution 4: Event-Sourcing Agent Controller

```python
import secrets
import time
from typing import Any, Callable, Dict, Optional


class EventSourcingAgentController:
    """
    Wraps agent actions with event emission. Every state-changing
    action produces an event that is appended to the store.
    Supports resuming from a previous event log.
    """

    def __init__(
        self,
        task_id: str,
        store: AppendOnlyEventStore,
    ):
        self._task_id = task_id
        self._store = store
        self._seq = store.last_sequence_number(task_id) + 1

    def _emit(self, event_type: AgentEventType, payload: dict) -> AgentEvent:
        event = AgentEvent(
            event_id=secrets.token_hex(8),
            task_id=self._task_id,
            sequence_number=self._seq,
            event_type=event_type,
            payload=payload,
        )
        self._store.append(event)
        self._seq += 1
        return event

    def start_task(self, input_data: Any) -> AgentEvent:
        return self._emit(AgentEventType.TASK_STARTED, {"input": input_data})

    async def run_tool(self, tool_name: str, fn: Callable, **kwargs: Any) -> Any:
        self._emit(AgentEventType.TOOL_CALL_DISPATCHED,
                   {"tool_name": tool_name, "args": list(kwargs.keys())})
        start = time.time()
        try:
            result = await fn(**kwargs)
            latency_ms = round((time.time() - start) * 1000, 2)
            self._emit(AgentEventType.TOOL_CALL_COMPLETED,
                       {"tool_name": tool_name, "result": str(result)[:500],
                        "latency_ms": latency_ms})
            return result
        except Exception as exc:
            self._emit(AgentEventType.TOOL_CALL_FAILED,
                       {"tool_name": tool_name, "error": str(exc)})
            raise

    def record_decision(self, decision: str, rationale: str = "") -> None:
        self._emit(AgentEventType.DECISION_MADE,
                   {"decision": decision, "rationale": rationale})

    def complete_task(self, outcome: Any) -> None:
        self._emit(AgentEventType.TASK_COMPLETED, {"outcome": str(outcome)[:500]})

    def fail_task(self, reason: str) -> None:
        self._emit(AgentEventType.TASK_FAILED, {"reason": reason})
```

## Solution 5: Task Replay Manager

```python
from typing import Any, Callable, List, Optional


class TaskReplayManager:
    """
    Reconstructs agent state up to a given sequence number.
    Used to debug task failures by inspecting state at any point in the history.
    """

    def __init__(self, store: AppendOnlyEventStore):
        self._store = store

    def reconstruct_at(self, task_id: str, up_to_seq: int) -> EventSourcedAgentState:
        events = [
            e for e in self._store.load(task_id)
            if e.sequence_number <= up_to_seq
        ]
        return EventSourcedAgentState.from_events(task_id, events)

    def reconstruct_latest(self, task_id: str) -> EventSourcedAgentState:
        events = self._store.load(task_id)
        return EventSourcedAgentState.from_events(task_id, events)

    def find_failure_point(self, task_id: str) -> Optional[dict]:
        events = self._store.load(task_id)
        for event in events:
            if event.event_type in (AgentEventType.TOOL_CALL_FAILED, AgentEventType.TASK_FAILED):
                state = self.reconstruct_at(task_id, event.sequence_number - 1)
                return {
                    "failure_event": event.to_dict(),
                    "state_before_failure": state.snapshot(),
                    "sequence_number": event.sequence_number,
                }
        return None

    def event_timeline(self, task_id: str) -> List[dict]:
        return [e.to_dict() for e in self._store.load(task_id)]
```

## Solution 6: Event Sourcing Dashboard

```python
import time


class EventSourcingDashboard:
    """
    Provides operational visibility into event-sourced tasks:
    active tasks, failure points, and event volume.
    """

    def __init__(
        self,
        store: AppendOnlyEventStore,
        replay_manager: TaskReplayManager,
    ):
        self._store = store
        self._replay = replay_manager

    def task_summary(self, task_id: str) -> dict:
        events = self._store.load(task_id)
        if not events:
            return {"task_id": task_id, "status": "not_found"}
        state = EventSourcedAgentState.from_events(task_id, events)
        failure = self._replay.find_failure_point(task_id)
        by_type: dict = {}
        for e in events:
            t = e.event_type.value
            by_type[t] = by_type.get(t, 0) + 1
        return {
            "task_id": task_id,
            "total_events": len(events),
            "current_state": state.snapshot(),
            "events_by_type": by_type,
            "failure_point": failure,
            "generated_at": time.time(),
        }
```

## Comparison

| Approach | Event Log | State Replay | Crash Recovery | Failure Diagnosis | Audit Trail |
|---|---|---|---|---|---|
| AppendOnlyEventStore | Yes (JSONL) | No | Yes (load log) | No | Yes |
| EventSourcedAgentState | No | Yes (apply events) | Via store | No | No |
| EventSourcingAgentController | Yes (emit) | No | Via controller | No | No |
| TaskReplayManager | Via store | Yes (up-to-seq) | Yes | Yes | Via timeline |
| EventSourcingDashboard | No | No | No | Via replay | Yes |

**Best for production**: Append events synchronously before executing the action — the event log is only useful for recovery if it is written before the action, not after. Use `TaskReplayManager.find_failure_point()` as the first debugging step for any failed task: it returns the exact state before the failure occurred, eliminating the need to reproduce the bug interactively. Implement periodic snapshots (every 20 events) to bound replay time on long-running tasks — `EventSourcedAgentState.from_events()` is O(n) in event count, so snapshots keep reconstruction fast. Store event logs in an object store (S3, GCS) for long-term retention; local file storage is acceptable for development but loses data on instance replacement.
