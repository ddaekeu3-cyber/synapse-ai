---
layout: solution
title: "Agent Doesn't Implement Event Sourcing for Agent State"
category: concurrency
description: "Agents that mutate state in-place lose audit trails, can't replay execution, and have no recovery path after failures. Event sourcing stores every state change as an immutable event, enabling time-travel debugging, full audit logs, and deterministic replay."
tags: [event-sourcing, state-management, audit-trail, replay, concurrency, sqlite, recovery]
---

# Agent Doesn't Implement Event Sourcing for Agent State

## Problem

Agents that overwrite state in-place have no history. When something goes wrong — a hallucinated tool call, an unexpected loop, a mid-run crash — there's no way to understand what happened or recover gracefully. Event sourcing solves this by treating every state mutation as an immutable event appended to a log. Current state is derived by replaying events from the beginning (or from a snapshot).

**Symptoms:**
- Can't explain how the agent reached a given state
- Crashes leave agent in inconsistent, unrecoverable state
- No audit trail for compliance or debugging
- State bugs are non-reproducible without event history
- Agent loops can't be detected without comparing past states

---

## Option 1: SQLite Append-Only Event Log with State Projection

```python
import anthropic
import sqlite3
import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any

@dataclass
class AgentEvent:
    event_id: str
    session_id: str
    sequence: int
    event_type: str
    payload: dict
    timestamp: float = field(default_factory=time.time)

class EventStore:
    def __init__(self, db_path: str = "agent_events.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                timestamp REAL NOT NULL,
                UNIQUE(session_id, sequence)
            )
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_session ON events(session_id, sequence)")
        self.conn.commit()

    def append(self, session_id: str, event_type: str, payload: dict) -> AgentEvent:
        cursor = self.conn.execute(
            "SELECT COALESCE(MAX(sequence), -1) + 1 FROM events WHERE session_id = ?",
            (session_id,)
        )
        sequence = cursor.fetchone()[0]
        event = AgentEvent(
            event_id=str(uuid.uuid4()),
            session_id=session_id,
            sequence=sequence,
            event_type=event_type,
            payload=payload
        )
        self.conn.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
            (event.event_id, event.session_id, event.sequence,
             event.event_type, json.dumps(event.payload), event.timestamp)
        )
        self.conn.commit()
        return event

    def replay(self, session_id: str, up_to_sequence: int = None) -> list[AgentEvent]:
        query = "SELECT * FROM events WHERE session_id = ?"
        params = [session_id]
        if up_to_sequence is not None:
            query += " AND sequence <= ?"
            params.append(up_to_sequence)
        query += " ORDER BY sequence"
        rows = self.conn.execute(query, params).fetchall()
        return [AgentEvent(*row[:6], timestamp=row[5]) for row in rows]

def project_state(events: list[AgentEvent]) -> dict:
    """Derive current state by replaying events."""
    state = {"messages": [], "tool_calls": [], "status": "idle", "context": {}}
    for event in events:
        if event.event_type == "message_added":
            state["messages"].append(event.payload)
        elif event.event_type == "tool_called":
            state["tool_calls"].append(event.payload)
        elif event.event_type == "context_updated":
            state["context"].update(event.payload)
        elif event.event_type == "status_changed":
            state["status"] = event.payload["status"]
    return state

def run_event_sourced_agent(user_query: str):
    client = anthropic.Anthropic()
    store = EventStore()
    session_id = str(uuid.uuid4())

    # Record session start
    store.append(session_id, "status_changed", {"status": "running"})
    store.append(session_id, "message_added", {"role": "user", "content": user_query})

    tools = [{
        "name": "search_knowledge",
        "description": "Search the knowledge base",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"]
        }
    }]

    messages = [{"role": "user", "content": user_query}]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        tools=tools,
        messages=messages
    )

    # Record every tool use as an event
    for block in response.content:
        if block.type == "tool_use":
            store.append(session_id, "tool_called", {
                "tool": block.name,
                "input": block.input,
                "tool_use_id": block.id
            })
            # Simulate tool result
            tool_result = f"Result for: {block.input.get('query', '')}"
            store.append(session_id, "context_updated", {
                f"tool_result_{block.id}": tool_result
            })

    store.append(session_id, "message_added", {
        "role": "assistant",
        "content": response.content[0].text if response.content[0].type == "text" else "[tool use]"
    })
    store.append(session_id, "status_changed", {"status": "complete"})

    # Project final state from events
    events = store.replay(session_id)
    final_state = project_state(events)
    print(f"Session {session_id}: {len(events)} events, status={final_state['status']}")
    print(f"Tool calls made: {len(final_state['tool_calls'])}")
    return session_id, final_state

session_id, state = run_event_sourced_agent("What are the latest AI research trends?")

# Expected Token Savings: ~0% (overhead only) — value is in auditability and replay
# Environment: SQLite available, single-process or multi-process with WAL mode
```

---

## Option 2: In-Memory Event Bus with Snapshot Optimization

```python
import anthropic
import json
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

@dataclass
class Event:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event_type: str = ""
    payload: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    sequence: int = 0

class InMemoryEventStore:
    def __init__(self, snapshot_interval: int = 10):
        self._streams: dict[str, list[Event]] = defaultdict(list)
        self._snapshots: dict[str, tuple[int, dict]] = {}  # (sequence, state)
        self._subscribers: list[Callable] = []
        self.snapshot_interval = snapshot_interval

    def append(self, stream_id: str, event_type: str, payload: dict) -> Event:
        sequence = len(self._streams[stream_id])
        event = Event(event_type=event_type, payload=payload, sequence=sequence)
        self._streams[stream_id].append(event)
        # Notify subscribers
        for sub in self._subscribers:
            sub(stream_id, event)
        # Auto-snapshot at intervals
        if sequence % self.snapshot_interval == 0 and sequence > 0:
            self._take_snapshot(stream_id)
        return event

    def _take_snapshot(self, stream_id: str):
        events = self._streams[stream_id]
        state = self._replay_events(events)
        self._snapshots[stream_id] = (len(events) - 1, state)
        print(f"[Snapshot] Stream {stream_id[:8]} at seq {len(events)-1}")

    def _replay_events(self, events: list[Event], from_state: dict = None) -> dict:
        state = from_state or {"messages": [], "tool_results": {}, "iteration": 0, "done": False}
        for event in events:
            if event.event_type == "iteration_started":
                state["iteration"] = event.payload["iteration"]
            elif event.event_type == "message_sent":
                state["messages"].append(event.payload)
            elif event.event_type == "tool_result_stored":
                state["tool_results"][event.payload["tool_use_id"]] = event.payload["result"]
            elif event.event_type == "agent_finished":
                state["done"] = True
        return state

    def get_state(self, stream_id: str) -> dict:
        """Efficient state projection using latest snapshot."""
        if stream_id in self._snapshots:
            snap_seq, snap_state = self._snapshots[stream_id]
            events_after = self._streams[stream_id][snap_seq + 1:]
            return self._replay_events(events_after, dict(snap_state))
        return self._replay_events(self._streams[stream_id])

    def subscribe(self, callback: Callable):
        self._subscribers.append(callback)

    def time_travel(self, stream_id: str, to_sequence: int) -> dict:
        events = self._streams[stream_id][:to_sequence + 1]
        return self._replay_events(events)

def run_snapshotted_agent(user_query: str, max_iterations: int = 3):
    client = anthropic.Anthropic()
    store = InMemoryEventStore(snapshot_interval=5)
    stream_id = str(uuid.uuid4())

    # Monitor events in real-time
    event_log = []
    def monitor(sid, event):
        if sid == stream_id:
            event_log.append(f"[{event.sequence}] {event.event_type}")
    store.subscribe(monitor)

    tools = [{
        "name": "calculate",
        "description": "Perform a calculation",
        "input_schema": {
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"]
        }
    }]

    messages = [{"role": "user", "content": user_query}]
    store.append(stream_id, "message_sent", {"role": "user", "content": user_query})

    for iteration in range(max_iterations):
        store.append(stream_id, "iteration_started", {"iteration": iteration})
        state = store.get_state(stream_id)

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            store.append(stream_id, "agent_finished", {"final_answer": response.content[0].text})
            break

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = f"42 (computed from: {block.input.get('expression', '')})"
                store.append(stream_id, "tool_result_stored", {
                    "tool_use_id": block.id,
                    "tool": block.name,
                    "result": result
                })
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result
                })

        messages.append({"role": "assistant", "content": response.content})
        if tool_results:
            messages.append({"role": "user", "content": tool_results})

    final_state = store.get_state(stream_id)
    print(f"Events recorded: {event_log}")
    print(f"Iterations: {final_state['iteration']}, Done: {final_state['done']}")

    # Time travel: what did the agent know at sequence 2?
    historical = store.time_travel(stream_id, 2)
    print(f"State at sequence 2: iteration={historical['iteration']}")

run_snapshotted_agent("Calculate the sum of the first 10 prime numbers")

# Expected Token Savings: ~0% — value is in instant state reconstruction from snapshots
# Environment: Single-process, in-memory; add Redis streams for distributed
```

---

## Option 3: Immutable Event Replay with Deterministic Agent Re-execution

```python
import anthropic
import json
import hashlib
import time
import uuid
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Iterator

@dataclass
class AgentEvent:
    event_id: str
    session_id: str
    sequence: int
    event_type: str
    payload: dict
    timestamp: float

class FileEventLog:
    """Append-only JSONL file event log — survives process restarts."""

    def __init__(self, log_dir: str = "/tmp/agent_events"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)

    def _log_path(self, session_id: str) -> Path:
        return self.log_dir / f"{session_id}.jsonl"

    def append(self, session_id: str, event_type: str, payload: dict) -> AgentEvent:
        log_path = self._log_path(session_id)
        sequence = sum(1 for _ in self._read_raw(session_id))
        event = AgentEvent(
            event_id=str(uuid.uuid4()),
            session_id=session_id,
            sequence=sequence,
            event_type=event_type,
            payload=payload,
            timestamp=time.time()
        )
        with open(log_path, "a") as f:
            f.write(json.dumps(asdict(event)) + "\n")
        return event

    def _read_raw(self, session_id: str) -> Iterator[dict]:
        log_path = self._log_path(session_id)
        if not log_path.exists():
            return
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def replay(self, session_id: str) -> list[AgentEvent]:
        return [AgentEvent(**raw) for raw in self._read_raw(session_id)]

    def list_sessions(self) -> list[str]:
        return [p.stem for p in self.log_dir.glob("*.jsonl")]

def compute_replay_hash(events: list[AgentEvent]) -> str:
    """Deterministic hash of event sequence for integrity verification."""
    content = json.dumps([{"type": e.event_type, "payload": e.payload} for e in events])
    return hashlib.sha256(content.encode()).hexdigest()[:16]

def replay_agent_execution(session_id: str, log: FileEventLog):
    """Replay a past agent session to reproduce its behavior."""
    events = log.replay(session_id)
    print(f"\n=== Replaying session {session_id[:8]} ({len(events)} events) ===")
    print(f"Integrity hash: {compute_replay_hash(events)}")

    messages_to_replay = []
    tool_calls_to_replay = []

    for event in events:
        if event.event_type == "message_recorded":
            messages_to_replay.append(event.payload)
            print(f"  [{event.sequence}] Message: {event.payload['role']} - {str(event.payload.get('content', ''))[:60]}")
        elif event.event_type == "tool_call_recorded":
            tool_calls_to_replay.append(event.payload)
            print(f"  [{event.sequence}] Tool: {event.payload['tool']}({event.payload['input']})")
        elif event.event_type == "llm_response_recorded":
            print(f"  [{event.sequence}] LLM response recorded (stop_reason={event.payload.get('stop_reason')})")

    return messages_to_replay, tool_calls_to_replay

def run_replayable_agent(user_query: str):
    client = anthropic.Anthropic()
    log = FileEventLog()
    session_id = str(uuid.uuid4())

    log.append(session_id, "session_started", {
        "query": user_query,
        "model": "claude-haiku-4-5-20251001"
    })

    tools = [{
        "name": "lookup_fact",
        "description": "Look up a fact",
        "input_schema": {
            "type": "object",
            "properties": {"topic": {"type": "string"}},
            "required": ["topic"]
        }
    }]

    messages = [{"role": "user", "content": user_query}]
    log.append(session_id, "message_recorded", {"role": "user", "content": user_query})

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=768,
        tools=tools,
        messages=messages
    )

    # Record full LLM response for replay
    log.append(session_id, "llm_response_recorded", {
        "stop_reason": response.stop_reason,
        "content": [{"type": b.type, "text": b.text if b.type == "text" else None} for b in response.content]
    })

    for block in response.content:
        if block.type == "tool_use":
            log.append(session_id, "tool_call_recorded", {
                "tool": block.name,
                "input": block.input,
                "tool_use_id": block.id
            })

    if response.content and response.content[0].type == "text":
        log.append(session_id, "message_recorded", {
            "role": "assistant",
            "content": response.content[0].text
        })

    log.append(session_id, "session_completed", {"total_events": None})

    print(f"Session {session_id[:8]} recorded. Replaying now...\n")
    replay_agent_execution(session_id, log)
    return session_id

run_replayable_agent("Tell me about quantum computing")

# Expected Token Savings: ~0% for original run; ~100% token savings during replay (no LLM calls)
# Environment: Local filesystem; replace with S3/GCS for distributed systems
```

---

## Option 4: Event-Driven State Machine with Transition Guards

```python
import anthropic
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

class AgentState(Enum):
    IDLE = "idle"
    THINKING = "thinking"
    TOOL_CALLING = "tool_calling"
    AWAITING_RESULT = "awaiting_result"
    RESPONDING = "responding"
    COMPLETE = "complete"
    ERROR = "error"

@dataclass
class StateTransitionEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    from_state: str = ""
    to_state: str = ""
    trigger: str = ""
    payload: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

class EventDrivenStateMachine:
    VALID_TRANSITIONS = {
        AgentState.IDLE: [AgentState.THINKING],
        AgentState.THINKING: [AgentState.TOOL_CALLING, AgentState.RESPONDING, AgentState.ERROR],
        AgentState.TOOL_CALLING: [AgentState.AWAITING_RESULT, AgentState.ERROR],
        AgentState.AWAITING_RESULT: [AgentState.THINKING, AgentState.ERROR],
        AgentState.RESPONDING: [AgentState.COMPLETE, AgentState.ERROR],
        AgentState.COMPLETE: [],
        AgentState.ERROR: [AgentState.IDLE],  # Allow reset
    }

    def __init__(self):
        self.current_state = AgentState.IDLE
        self.history: list[StateTransitionEvent] = []
        self.guards: dict[tuple, list[Callable]] = {}

    def add_guard(self, from_state: AgentState, to_state: AgentState, guard: Callable):
        key = (from_state, to_state)
        self.guards.setdefault(key, []).append(guard)

    def transition(self, to_state: AgentState, trigger: str, payload: dict = None) -> bool:
        if to_state not in self.VALID_TRANSITIONS.get(self.current_state, []):
            print(f"[FSM] Invalid transition: {self.current_state} -> {to_state}")
            return False

        # Run guards
        for guard in self.guards.get((self.current_state, to_state), []):
            if not guard(self.current_state, to_state, payload or {}):
                print(f"[FSM] Guard blocked transition: {self.current_state} -> {to_state}")
                return False

        event = StateTransitionEvent(
            from_state=self.current_state.value,
            to_state=to_state.value,
            trigger=trigger,
            payload=payload or {}
        )
        self.history.append(event)
        self.current_state = to_state
        print(f"[FSM] {event.from_state} -> {event.to_state} (trigger={trigger})")
        return True

    def get_audit_trail(self) -> list[dict]:
        return [
            {
                "seq": i,
                "from": e.from_state,
                "to": e.to_state,
                "trigger": e.trigger,
                "time": e.timestamp
            }
            for i, e in enumerate(self.history)
        ]

def run_state_machine_agent(user_query: str):
    client = anthropic.Anthropic()
    fsm = EventDrivenStateMachine()

    # Guard: don't allow TOOL_CALLING if already called 5+ tools
    tool_call_count = [0]
    def tool_limit_guard(from_state, to_state, payload):
        if tool_call_count[0] >= 5:
            print("[Guard] Tool call limit reached")
            return False
        return True

    fsm.add_guard(AgentState.THINKING, AgentState.TOOL_CALLING, tool_limit_guard)

    tools = [{
        "name": "web_search",
        "description": "Search the web for information",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"]
        }
    }]

    fsm.transition(AgentState.THINKING, "user_message_received", {"query": user_query})
    messages = [{"role": "user", "content": user_query}]

    for _ in range(5):  # Max iterations
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )

        if response.stop_reason == "tool_use":
            allowed = fsm.transition(AgentState.TOOL_CALLING, "llm_requested_tool")
            if not allowed:
                # Guard blocked it — force completion
                fsm.transition(AgentState.RESPONDING, "tool_limit_enforced")
                break

            tool_call_count[0] += 1
            fsm.transition(AgentState.AWAITING_RESULT, "tool_dispatched",
                          {"tool_count": tool_call_count[0]})

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = f"Search result for: {block.input.get('query', '')}"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result
                    })

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
            fsm.transition(AgentState.THINKING, "tool_result_received")

        else:
            fsm.transition(AgentState.RESPONDING, "llm_final_response")
            fsm.transition(AgentState.COMPLETE, "response_delivered")
            break

    print("\nAudit trail:")
    for entry in fsm.get_audit_trail():
        print(f"  [{entry['seq']}] {entry['from']} -> {entry['to']} ({entry['trigger']})")

run_state_machine_agent("What are the top 3 programming languages in 2024?")

# Expected Token Savings: ~0% on tokens; ~100% reduction in invalid state bugs via transition guards
# Environment: Single-process; guards enforce business rules without extra LLM calls
```

---

## Option 5: Distributed Event Sourcing with Optimistic Concurrency Control

```python
import anthropic
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class VersionedEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    aggregate_id: str = ""
    version: int = 0
    event_type: str = ""
    payload: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

class OptimisticEventStore:
    """Prevents concurrent write conflicts via version checking."""

    def __init__(self):
        self._streams: dict[str, list[VersionedEvent]] = {}
        self._lock = asyncio.Lock()

    async def append(self, aggregate_id: str, event_type: str,
                    payload: dict, expected_version: int) -> VersionedEvent:
        async with self._lock:
            current = self._streams.get(aggregate_id, [])
            current_version = len(current) - 1

            if current_version != expected_version:
                raise ValueError(
                    f"Concurrency conflict on {aggregate_id}: "
                    f"expected version {expected_version}, got {current_version}"
                )

            event = VersionedEvent(
                aggregate_id=aggregate_id,
                version=current_version + 1,
                event_type=event_type,
                payload=payload
            )
            self._streams.setdefault(aggregate_id, []).append(event)
            return event

    async def load(self, aggregate_id: str, from_version: int = 0) -> list[VersionedEvent]:
        return self._streams.get(aggregate_id, [])[from_version:]

    async def current_version(self, aggregate_id: str) -> int:
        return len(self._streams.get(aggregate_id, [])) - 1

class AgentAggregate:
    """Aggregate root that applies events to reconstruct state."""

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self.version = -1
        self.messages: list[dict] = []
        self.tool_calls: list[dict] = []
        self.status: str = "idle"
        self.pending_changes: list[tuple] = []  # (event_type, payload)

    def apply(self, event: VersionedEvent):
        self.version = event.version
        if event.event_type == "AgentStarted":
            self.status = "running"
            self.messages.append({"role": "user", "content": event.payload["query"]})
        elif event.event_type == "ToolUsed":
            self.tool_calls.append(event.payload)
        elif event.event_type == "ResponseGenerated":
            self.messages.append({"role": "assistant", "content": event.payload["text"]})
            self.status = "complete"

    def start(self, query: str):
        self.pending_changes.append(("AgentStarted", {"query": query}))

    def use_tool(self, tool: str, input_data: dict, result: str):
        self.pending_changes.append(("ToolUsed", {
            "tool": tool, "input": input_data, "result": result
        }))

    def generate_response(self, text: str):
        self.pending_changes.append(("ResponseGenerated", {"text": text}))

async def run_optimistic_agent(user_query: str):
    client = anthropic.AsyncAnthropic()
    store = OptimisticEventStore()
    agent_id = str(uuid.uuid4())
    aggregate = AgentAggregate(agent_id)

    # Load existing state (empty for new agent)
    events = await store.load(agent_id)
    for event in events:
        aggregate.apply(event)

    aggregate.start(user_query)

    # Persist with optimistic concurrency
    for event_type, payload in aggregate.pending_changes:
        event = await store.append(agent_id, event_type, payload, aggregate.version)
        aggregate.apply(event)
    aggregate.pending_changes.clear()

    tools = [{
        "name": "fetch_data",
        "description": "Fetch data from a source",
        "input_schema": {
            "type": "object",
            "properties": {"source": {"type": "string"}},
            "required": ["source"]
        }
    }]

    messages = [{"role": "user", "content": user_query}]
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        tools=tools,
        messages=messages
    )

    for block in response.content:
        if block.type == "tool_use":
            result = f"Data from {block.input.get('source', 'unknown')}"
            aggregate.use_tool(block.name, block.input, result)

    if response.content and response.content[0].type == "text":
        aggregate.generate_response(response.content[0].text)

    # Persist remaining changes
    for event_type, payload in aggregate.pending_changes:
        event = await store.append(agent_id, event_type, payload, aggregate.version)
        aggregate.apply(event)
    aggregate.pending_changes.clear()

    all_events = await store.load(agent_id)
    print(f"Agent {agent_id[:8]}: {len(all_events)} events, version={aggregate.version}")
    print(f"Status: {aggregate.status}, Tool calls: {len(aggregate.tool_calls)}")

asyncio.run(run_optimistic_agent("Fetch the latest sales data and summarize it"))

# Expected Token Savings: ~0% — prevents data corruption in concurrent agent scenarios
# Environment: asyncio required; replace dict store with PostgreSQL + FOR UPDATE SKIP LOCKED
```

---

## Option 6: Event Sourcing with CQRS Read Model Projection

```python
import anthropic
import json
import time
import uuid
import sqlite3
from dataclasses import dataclass, field, asdict
from typing import Optional

# === Write Side: Events ===

@dataclass
class DomainEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    event_type: str = ""
    payload: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

class WriteModel:
    """Append-only event store (write side of CQRS)."""

    def __init__(self, db_path: str = "/tmp/agent_cqrs.db"):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS domain_events (
                event_id TEXT PRIMARY KEY,
                session_id TEXT,
                event_type TEXT,
                payload TEXT,
                timestamp REAL
            )
        """)
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_sess ON domain_events(session_id, timestamp)")
        self.db.commit()
        self._projectors: list = []

    def emit(self, session_id: str, event_type: str, payload: dict) -> DomainEvent:
        event = DomainEvent(session_id=session_id, event_type=event_type, payload=payload)
        self.db.execute(
            "INSERT INTO domain_events VALUES (?, ?, ?, ?, ?)",
            (event.event_id, event.session_id, event.event_type,
             json.dumps(event.payload), event.timestamp)
        )
        self.db.commit()
        # Sync projections after each event
        for proj in self._projectors:
            proj.handle(event)
        return event

    def register_projector(self, projector):
        self._projectors.append(projector)

# === Read Side: Projections ===

class SessionSummaryProjection:
    """Optimized read model for session summaries."""

    def __init__(self, db_path: str = "/tmp/agent_cqrs.db"):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS session_summaries (
                session_id TEXT PRIMARY KEY,
                user_query TEXT,
                tool_call_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'running',
                response_preview TEXT,
                updated_at REAL
            )
        """)
        self.db.commit()

    def handle(self, event: DomainEvent):
        if event.event_type == "SessionCreated":
            self.db.execute(
                "INSERT OR REPLACE INTO session_summaries VALUES (?, ?, 0, 'running', NULL, ?)",
                (event.session_id, event.payload.get("query", ""), event.timestamp)
            )
        elif event.event_type == "ToolExecuted":
            self.db.execute(
                "UPDATE session_summaries SET tool_call_count = tool_call_count + 1, updated_at = ? WHERE session_id = ?",
                (event.timestamp, event.session_id)
            )
        elif event.event_type == "ResponseGenerated":
            preview = event.payload.get("text", "")[:100]
            self.db.execute(
                "UPDATE session_summaries SET status = 'complete', response_preview = ?, updated_at = ? WHERE session_id = ?",
                (preview, event.timestamp, event.session_id)
            )
        self.db.commit()

    def get(self, session_id: str) -> Optional[dict]:
        row = self.db.execute(
            "SELECT * FROM session_summaries WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row:
            cols = ["session_id", "user_query", "tool_call_count", "status", "response_preview", "updated_at"]
            return dict(zip(cols, row))
        return None

    def list_recent(self, limit: int = 5) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM session_summaries ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        cols = ["session_id", "user_query", "tool_call_count", "status", "response_preview", "updated_at"]
        return [dict(zip(cols, row)) for row in rows]

def run_cqrs_agent(user_query: str):
    client = anthropic.Anthropic()

    # Wire up CQRS
    write_model = WriteModel()
    read_model = SessionSummaryProjection()
    write_model.register_projector(read_model)

    session_id = str(uuid.uuid4())
    write_model.emit(session_id, "SessionCreated", {"query": user_query})

    tools = [{
        "name": "analyze",
        "description": "Analyze data or text",
        "input_schema": {
            "type": "object",
            "properties": {"data": {"type": "string"}},
            "required": ["data"]
        }
    }]

    messages = [{"role": "user", "content": user_query}]
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=768,
        tools=tools,
        messages=messages
    )

    tool_results = []
    for block in response.content:
        if block.type == "tool_use":
            result = f"Analysis of: {block.input.get('data', '')}"
            write_model.emit(session_id, "ToolExecuted", {
                "tool": block.name,
                "input": block.input,
                "result": result
            })
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result
            })

    # Get final response if tools were used
    final_text = ""
    if tool_results:
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})
        final_response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=messages
        )
        final_text = final_response.content[0].text if final_response.content else ""
    else:
        final_text = response.content[0].text if response.content else ""

    write_model.emit(session_id, "ResponseGenerated", {"text": final_text})

    # Query read model — no need to replay events
    summary = read_model.get(session_id)
    print(f"\nRead model (no event replay needed):")
    print(f"  Session: {summary['session_id'][:8]}")
    print(f"  Query: {summary['user_query'][:60]}")
    print(f"  Tools used: {summary['tool_call_count']}")
    print(f"  Status: {summary['status']}")
    print(f"  Response: {summary['response_preview']}")

    print(f"\nRecent sessions:")
    for s in read_model.list_recent(3):
        print(f"  [{s['status']}] {s['user_query'][:40]} (tools={s['tool_call_count']})")

run_cqrs_agent("Analyze the performance metrics from last quarter")

# Expected Token Savings: ~0% on write path; read model avoids event replay overhead at query time
# Environment: SQLite for single-node; PostgreSQL + event bus (Kafka/Redis Streams) for distributed CQRS
```

---

## Comparison

| Option | Storage | Replay Speed | Concurrency Safe | Snapshot | Best For |
|--------|---------|--------------|-----------------|----------|----------|
| SQLite Append-Only | Durable | O(n) events | WAL mode | No | Audit trail, compliance |
| In-Memory + Snapshot | Ephemeral | O(n/k) | Single-process | Yes | Fast state reconstruction |
| File JSONL Log | Durable | O(n) | Single-writer | No | Simple replay, debugging |
| State Machine FSM | Ephemeral | O(1) | Single-process | N/A | Enforcing valid transitions |
| Optimistic Concurrency | Ephemeral | O(n) | Async-safe | No | Distributed concurrent agents |
| CQRS Read Model | Durable | O(1) query | Per-projector | Auto | High-read, analytics, dashboards |

**Recommendation:** Start with **Option 1** (SQLite append-only) for auditability and recovery. Add **Option 2** snapshot optimization when event count exceeds 1,000. Use **Option 6** (CQRS) when you need fast queries across many sessions without replaying individual event streams.
