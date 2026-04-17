---
title: "Agent Doesn't Implement Session Replay Event Logging"
description: "Agents that log only final outcomes cannot be replayed for debugging: when a session produces an unexpected result, engineers cannot reconstruct the exact sequence of user inputs, tool calls, LLM responses, and state transitions that led to it. Implement session replay event logging that records every agent event in a structured, ordered log that can be replayed deterministically to reproduce any session."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-session-replay-event-logging
tags: [session-replay, event-logging, audit-trail, deterministic-replay, debugging, structured-events]
symptoms:
  - "Unexpected agent behavior cannot be reproduced — no record of intermediate steps"
  - "Debugging requires attaching a debugger to a live session — cannot post-mortem analyze"
  - "Log entries show only tool call results, not the agent reasoning that preceded them"
  - "Event timestamps not preserved — cannot determine order of concurrent events"
  - "Different event types (user input, tool call, LLM response) mixed in a single untyped log"
---

## Why This Happens

Standard logging captures what happened at a moment in time but not the causal chain that produced it. An agent session is a stateful sequence: user input → LLM response → tool calls → tool results → next LLM call. Without recording each step as a structured event with a monotone sequence number, causal order, and full payload, the session cannot be replayed. Session replay requires an append-only event log per session, typed event schemas for each event kind, and a replay engine that reprocesses the events in order to reconstruct the agent's state at any point in the session.

## Solution 1: Session Event Types

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class SessionEventKind(str, Enum):
    SESSION_START = "session_start"
    USER_INPUT = "user_input"
    LLM_REQUEST = "llm_request"
    LLM_RESPONSE = "llm_response"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_RESULT = "tool_call_result"
    TOOL_CALL_ERROR = "tool_call_error"
    STATE_TRANSITION = "state_transition"
    AGENT_DECISION = "agent_decision"
    ERROR = "error"
    SESSION_END = "session_end"


@dataclass
class SessionEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4())[:16])
    session_id: str = ""
    sequence: int = 0              # monotone counter within session
    kind: SessionEventKind = SessionEventKind.USER_INPUT
    payload: Dict[str, Any] = field(default_factory=dict)
    parent_event_id: Optional[str] = None   # causal parent
    timestamp: float = field(default_factory=time.time)
    duration_ms: Optional[float] = None     # for events with duration
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "session_id": self.session_id,
            "seq": self.sequence,
            "kind": self.kind.value,
            "payload": self.payload,
            "parent_event_id": self.parent_event_id,
            "ts": round(self.timestamp, 6),
            "duration_ms": self.duration_ms,
            "tags": self.tags,
        }
```

## Solution 2: Session Event Log

```python
import json
import time
from threading import Lock
from typing import Dict, Iterator, List, Optional


class SessionEventLog:
    """
    Append-only, ordered event log for a single session.
    Events are assigned monotone sequence numbers.
    Supports serialization for persistence and replay.
    """

    def __init__(self, session_id: str):
        self._session_id = session_id
        self._events: List[SessionEvent] = []
        self._lock = Lock()
        self._seq = 0

    def append(
        self,
        kind: SessionEventKind,
        payload: dict,
        parent_event_id: Optional[str] = None,
        duration_ms: Optional[float] = None,
        tags: List[str] = None,
    ) -> SessionEvent:
        with self._lock:
            self._seq += 1
            event = SessionEvent(
                session_id=self._session_id,
                sequence=self._seq,
                kind=kind,
                payload=payload,
                parent_event_id=parent_event_id,
                duration_ms=duration_ms,
                tags=tags or [],
            )
            self._events.append(event)
            return event

    def events_since(self, sequence: int = 0) -> List[SessionEvent]:
        with self._lock:
            return [e for e in self._events if e.sequence > sequence]

    def to_json(self) -> str:
        with self._lock:
            return json.dumps({
                "session_id": self._session_id,
                "event_count": len(self._events),
                "events": [e.to_dict() for e in self._events],
            }, indent=2)

    def event_count(self) -> int:
        with self._lock:
            return len(self._events)

    def duration_s(self) -> float:
        with self._lock:
            if len(self._events) < 2:
                return 0.0
            return round(self._events[-1].timestamp - self._events[0].timestamp, 3)
```

## Solution 3: Session Event Log Registry

```python
import time
from threading import Lock
from typing import Dict, List, Optional


class SessionEventLogRegistry:
    """
    Manages event logs for all active sessions.
    Supports retrieval by session ID and cleanup of expired sessions.
    """

    def __init__(self, ttl_seconds: float = 86400.0, max_sessions: int = 10000):
        self._logs: Dict[str, SessionEventLog] = {}
        self._created_at: Dict[str, float] = {}
        self._lock = Lock()
        self._ttl = ttl_seconds
        self._max = max_sessions

    def create(self, session_id: str) -> SessionEventLog:
        log = SessionEventLog(session_id)
        with self._lock:
            self._logs[session_id] = log
            self._created_at[session_id] = time.time()
            self._evict_if_needed()
        return log

    def get(self, session_id: str) -> Optional[SessionEventLog]:
        with self._lock:
            return self._logs.get(session_id)

    def _evict_if_needed(self) -> None:
        now = time.time()
        expired = [
            sid for sid, ts in self._created_at.items()
            if now - ts > self._ttl
        ]
        for sid in expired:
            self._logs.pop(sid, None)
            self._created_at.pop(sid, None)

        if len(self._logs) > self._max:
            oldest = sorted(self._created_at.items(), key=lambda x: x[1])
            for sid, _ in oldest[:len(self._logs) - self._max]:
                self._logs.pop(sid, None)
                self._created_at.pop(sid, None)

    def active_session_count(self) -> int:
        with self._lock:
            return len(self._logs)
```

## Solution 4: Instrumented Agent Logger

```python
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional


class InstrumentedAgentLogger:
    """
    Convenience wrapper that records standard agent lifecycle events
    to a SessionEventLog with minimal boilerplate at call sites.
    """

    def __init__(self, log: SessionEventLog):
        self._log = log

    def session_start(self, metadata: dict = None) -> SessionEvent:
        return self._log.append(
            SessionEventKind.SESSION_START,
            payload={"metadata": metadata or {}},
        )

    def user_input(self, text: str, parent_id: str = None) -> SessionEvent:
        return self._log.append(
            SessionEventKind.USER_INPUT,
            payload={"text": text[:2000]},
            parent_event_id=parent_id,
        )

    def llm_request(self, model_id: str, prompt_tokens: int, parent_id: str = None) -> SessionEvent:
        return self._log.append(
            SessionEventKind.LLM_REQUEST,
            payload={"model_id": model_id, "prompt_tokens": prompt_tokens},
            parent_event_id=parent_id,
        )

    def llm_response(
        self, model_id: str, output_tokens: int, latency_ms: float, parent_id: str = None
    ) -> SessionEvent:
        return self._log.append(
            SessionEventKind.LLM_RESPONSE,
            payload={"model_id": model_id, "output_tokens": output_tokens},
            parent_event_id=parent_id,
            duration_ms=latency_ms,
        )

    @asynccontextmanager
    async def tool_call(self, tool_name: str, args: dict, parent_id: str = None) -> AsyncIterator[dict]:
        start_event = self._log.append(
            SessionEventKind.TOOL_CALL_START,
            payload={"tool_name": tool_name, "args_keys": list(args.keys())},
            parent_event_id=parent_id,
        )
        start = time.time()
        ctx = {"start_event_id": start_event.event_id}
        try:
            yield ctx
            latency_ms = round((time.time() - start) * 1000, 2)
            result = ctx.get("result")
            self._log.append(
                SessionEventKind.TOOL_CALL_RESULT,
                payload={"tool_name": tool_name, "result_type": type(result).__name__},
                parent_event_id=start_event.event_id,
                duration_ms=latency_ms,
            )
        except Exception as exc:
            latency_ms = round((time.time() - start) * 1000, 2)
            self._log.append(
                SessionEventKind.TOOL_CALL_ERROR,
                payload={"tool_name": tool_name, "error": str(exc)[:300]},
                parent_event_id=start_event.event_id,
                duration_ms=latency_ms,
            )
            raise

    def session_end(self, outcome: str = "completed", parent_id: str = None) -> SessionEvent:
        return self._log.append(
            SessionEventKind.SESSION_END,
            payload={"outcome": outcome, "duration_s": self._log.duration_s()},
            parent_event_id=parent_id,
        )
```

## Solution 5: Session Replay Reader

```python
import json
from typing import Iterator, List, Optional


class SessionReplayReader:
    """
    Reads a serialized session event log and replays events in order.
    Useful for debugging, postmortem analysis, and test fixture generation.
    """

    def __init__(self, log_json: str):
        data = json.loads(log_json)
        self._session_id = data["session_id"]
        raw_events = data.get("events", [])
        self._events = sorted(raw_events, key=lambda e: e["seq"])

    def events_of_kind(self, kind: str) -> List[dict]:
        return [e for e in self._events if e["kind"] == kind]

    def replay(self) -> Iterator[dict]:
        for event in self._events:
            yield event

    def tool_calls(self) -> List[dict]:
        return self.events_of_kind("tool_call_start")

    def errors(self) -> List[dict]:
        return self.events_of_kind("error") + self.events_of_kind("tool_call_error")

    def summary(self) -> dict:
        by_kind: dict = {}
        for e in self._events:
            by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
        durations = [e["duration_ms"] for e in self._events if e.get("duration_ms")]
        return {
            "session_id": self._session_id,
            "event_count": len(self._events),
            "by_kind": by_kind,
            "errors": len(self.errors()),
            "mean_tool_duration_ms": round(sum(durations) / max(len(durations), 1), 2),
        }
```

## Solution 6: Session Replay Dashboard

```python
import time


class SessionReplayDashboard:
    """
    Combines registry stats with per-session event breakdowns.
    """

    def __init__(self, registry: SessionEventLogRegistry):
        self._registry = registry

    def render(self, sample_session_ids: List[str] = None) -> dict:
        samples = {}
        for sid in (sample_session_ids or [])[:5]:
            log = self._registry.get(sid)
            if log:
                samples[sid] = {
                    "event_count": log.event_count(),
                    "duration_s": log.duration_s(),
                }
        return {
            "generated_at": time.time(),
            "active_sessions": self._registry.active_session_count(),
            "sample_sessions": samples,
        }

from typing import List
```

## Comparison

| Approach | Ordered Events | Typed Events | Causal Links | JSON Export | Replay |
|---|---|---|---|---|---|
| SessionEventLog | Yes (seq counter) | Yes (enum) | Yes (parent_id) | Yes | No |
| SessionEventLogRegistry | No | No | No | No | No |
| InstrumentedAgentLogger | Via log | Via log | Via log | Via log | No |
| SessionReplayReader | No | No | No | No | Yes |
| SessionReplayDashboard | No | No | No | No | Via reader |

**Best for production**: Store serialized session event logs in object storage (S3, GCS) keyed by session ID with a 30-day retention policy — they are invaluable for postmortem analysis and test fixture generation. Use `parent_event_id` to build a causal graph: LLM responses should point to the LLM request, tool results should point to the tool call start. Truncate large payloads (LLM response text > 2000 chars) before logging to avoid log storage bloat — store the first 2000 chars and a `truncated=true` flag. Use `SessionReplayReader` in unit tests by replaying a captured production session and asserting on the event sequence.
