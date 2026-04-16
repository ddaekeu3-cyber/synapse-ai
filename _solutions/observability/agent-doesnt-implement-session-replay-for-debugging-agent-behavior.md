---
title: "Agent Doesn't Implement Session Replay for Debugging Agent Behavior"
description: "Agents that only log final outputs cannot reproduce the exact sequence of LLM turns, tool calls, and intermediate states that led to a bad response. Implement session replay that captures every event in a session — messages, tool invocations, results, and context snapshots — in a structured, replayable format so engineers can step through any past session and reproduce the exact execution path."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-session-replay-for-debugging-agent-behavior
tags: [session-replay, debugging, event-sourcing, reproducibility, audit-trail, agent-observability]
symptoms:
  - "Cannot reproduce a bad agent response — only the final output was logged"
  - "No record of which tool results were injected into context before the LLM call"
  - "Debugging requires guessing what the agent 'saw' at each step"
  - "Intermittent failures are impossible to diagnose without step-level event logs"
  - "No way to compare what the agent did in session A vs session B for the same query"
---

## Why This Happens

Standard application logging captures inputs and outputs at system boundaries but not intermediate states. An agent session involves dozens of events — user messages, LLM turns, tool calls, result injections, context mutations — each of which influences subsequent behavior. Without a structured event log for each session, debugging a bad response requires guessing which combination of tool results and model outputs led to the problem. Session replay treats the agent as an event-sourced system: every state transition is an event, and replaying the event log recreates the exact execution context at any point in time.

## Solution 1: Session Event Types

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class SessionEventType(str, Enum):
    SESSION_START = "session_start"
    USER_MESSAGE = "user_message"
    LLM_REQUEST = "llm_request"
    LLM_RESPONSE = "llm_response"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_END = "tool_call_end"
    CONTEXT_SNAPSHOT = "context_snapshot"
    ERROR = "error"
    SESSION_END = "session_end"


@dataclass
class SessionEvent:
    event_id: str
    session_id: str
    event_type: SessionEventType
    sequence: int                    # monotonically increasing within session
    timestamp: float
    data: Dict[str, Any]
    span_id: Optional[str] = None    # link to distributed trace span
    parent_event_id: Optional[str] = None

    @classmethod
    def create(
        cls,
        session_id: str,
        event_type: SessionEventType,
        sequence: int,
        data: Dict[str, Any],
        span_id: Optional[str] = None,
        parent_event_id: Optional[str] = None,
    ) -> "SessionEvent":
        return cls(
            event_id=uuid.uuid4().hex[:16],
            session_id=session_id,
            event_type=event_type,
            sequence=sequence,
            timestamp=time.time(),
            data=data,
            span_id=span_id,
            parent_event_id=parent_event_id,
        )
```

## Solution 2: Session Event Recorder

```python
import json
from collections import defaultdict
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional


class SessionEventRecorder:
    """
    Records session events in-memory with optional file persistence.
    Each session is an ordered list of events indexed by session_id.
    """

    def __init__(
        self,
        max_sessions: int = 500,
        persist_dir: Optional[str] = None,
    ):
        self._max = max_sessions
        self._persist_dir = Path(persist_dir) if persist_dir else None
        self._sessions: Dict[str, List[SessionEvent]] = {}
        self._sequence: Dict[str, int] = defaultdict(int)
        self._lock = Lock()

        if self._persist_dir:
            self._persist_dir.mkdir(parents=True, exist_ok=True)

    def record(self, event: SessionEvent) -> None:
        with self._lock:
            if event.session_id not in self._sessions:
                if len(self._sessions) >= self._max:
                    oldest = next(iter(self._sessions))
                    del self._sessions[oldest]
                    del self._sequence[oldest]
                self._sessions[event.session_id] = []
            self._sessions[event.session_id].append(event)
            if self._persist_dir:
                self._append_to_file(event)

    def _append_to_file(self, event: SessionEvent) -> None:
        path = self._persist_dir / f"{event.session_id}.jsonl"
        line = json.dumps({
            "event_id": event.event_id,
            "session_id": event.session_id,
            "event_type": event.event_type.value,
            "sequence": event.sequence,
            "timestamp": event.timestamp,
            "data": event.data,
            "span_id": event.span_id,
        })
        with open(path, "a") as f:
            f.write(line + "\n")

    def get_session(self, session_id: str) -> List[SessionEvent]:
        with self._lock:
            return list(self._sessions.get(session_id, []))

    def session_ids(self) -> List[str]:
        with self._lock:
            return list(self._sessions.keys())
```

## Solution 3: Session Recorder Context Manager

```python
import asyncio
from typing import Any, Dict, Optional


class ActiveSessionRecorder:
    """
    High-level API for recording events during an active agent session.
    Manages the sequence counter and wraps tool calls automatically.
    """

    def __init__(self, session_id: str, recorder: SessionEventRecorder):
        self._session_id = session_id
        self._recorder = recorder
        self._seq = 0

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _emit(self, event_type: SessionEventType, data: Dict[str, Any]) -> SessionEvent:
        event = SessionEvent.create(
            session_id=self._session_id,
            event_type=event_type,
            sequence=self._next_seq(),
            data=data,
        )
        self._recorder.record(event)
        return event

    def start(self, input_data: Dict[str, Any]) -> None:
        self._emit(SessionEventType.SESSION_START, {"input": input_data})

    def user_message(self, content: str) -> None:
        self._emit(SessionEventType.USER_MESSAGE, {"content": content})

    def llm_request(self, messages: list, model: str, params: Dict[str, Any] = None) -> None:
        self._emit(SessionEventType.LLM_REQUEST, {
            "model": model,
            "message_count": len(messages),
            "params": params or {},
        })

    def llm_response(self, content: str, usage: Dict[str, Any] = None) -> None:
        self._emit(SessionEventType.LLM_RESPONSE, {
            "content_preview": content[:200],
            "content_length": len(content),
            "usage": usage or {},
        })

    def tool_call_start(self, tool_name: str, args: Dict[str, Any]) -> str:
        event = self._emit(SessionEventType.TOOL_CALL_START, {
            "tool_name": tool_name,
            "args_keys": list(args.keys()),
        })
        return event.event_id

    def tool_call_end(
        self,
        tool_name: str,
        result_preview: str,
        latency_ms: float,
        error: Optional[str] = None,
    ) -> None:
        self._emit(SessionEventType.TOOL_CALL_END, {
            "tool_name": tool_name,
            "result_preview": result_preview[:200],
            "latency_ms": latency_ms,
            "error": error,
        })

    def end(self, outcome: str = "success", error: Optional[str] = None) -> None:
        self._emit(SessionEventType.SESSION_END, {
            "outcome": outcome,
            "error": error,
            "total_events": self._seq,
        })
```

## Solution 4: Session Replay Engine

```python
from typing import Callable, List, Optional


class SessionReplayEngine:
    """
    Replays a recorded session event-by-event, calling a handler
    for each event. Supports step-through replay and filtering
    by event type for focused debugging.
    """

    def __init__(self, recorder: SessionEventRecorder):
        self._recorder = recorder

    def replay(
        self,
        session_id: str,
        handler: Callable[[SessionEvent], None],
        event_types: Optional[List[SessionEventType]] = None,
        from_sequence: int = 0,
    ) -> int:
        """
        Replays events for session_id, calling handler for each.
        Returns the number of events replayed.
        """
        events = self._recorder.get_session(session_id)
        replayed = 0
        for event in events:
            if event.sequence < from_sequence:
                continue
            if event_types and event.event_type not in event_types:
                continue
            handler(event)
            replayed += 1
        return replayed

    def summarize(self, session_id: str) -> dict:
        events = self._recorder.get_session(session_id)
        if not events:
            return {"session_id": session_id, "found": False}

        by_type: Dict[str, int] = {}
        for e in events:
            by_type[e.event_type.value] = by_type.get(e.event_type.value, 0) + 1

        duration_ms = 0.0
        if len(events) >= 2:
            duration_ms = round((events[-1].timestamp - events[0].timestamp) * 1000, 2)

        return {
            "session_id": session_id,
            "found": True,
            "total_events": len(events),
            "duration_ms": duration_ms,
            "event_counts": by_type,
        }
```

## Solution 5: Session Diff Comparator

```python
from typing import List, Tuple


class SessionDiffComparator:
    """
    Compares two session replays to identify where their execution paths diverged.
    Useful for A/B testing prompt changes or debugging regressions.
    """

    def __init__(self, recorder: SessionEventRecorder):
        self._recorder = recorder

    def diff(
        self,
        session_a: str,
        session_b: str,
    ) -> List[dict]:
        events_a = self._recorder.get_session(session_a)
        events_b = self._recorder.get_session(session_b)

        diffs = []
        max_seq = max(len(events_a), len(events_b))
        for i in range(max_seq):
            ea = events_a[i] if i < len(events_a) else None
            eb = events_b[i] if i < len(events_b) else None

            if ea is None:
                diffs.append({"sequence": i, "diff": "session_b_longer", "b_type": eb.event_type.value})
            elif eb is None:
                diffs.append({"sequence": i, "diff": "session_a_longer", "a_type": ea.event_type.value})
            elif ea.event_type != eb.event_type:
                diffs.append({
                    "sequence": i,
                    "diff": "event_type_mismatch",
                    "a_type": ea.event_type.value,
                    "b_type": eb.event_type.value,
                })
        return diffs
```

## Solution 6: Replay Anomaly Finder

```python
import time
from typing import List


class ReplayAnomalyFinder:
    """
    Scans session event logs for anomaly patterns:
    unusually long tool calls, repeated error events,
    and sessions with unexpected event sequences.
    """

    def __init__(self, recorder: SessionEventRecorder):
        self._recorder = recorder

    def find_anomalies(self, session_id: str, slow_tool_ms: float = 5000.0) -> List[dict]:
        events = self._recorder.get_session(session_id)
        anomalies = []
        tool_starts: dict = {}

        for event in events:
            if event.event_type == SessionEventType.TOOL_CALL_START:
                tool_starts[event.data.get("tool_name", "")] = event.timestamp
            elif event.event_type == SessionEventType.TOOL_CALL_END:
                tool_name = event.data.get("tool_name", "")
                latency = event.data.get("latency_ms", 0)
                if latency > slow_tool_ms:
                    anomalies.append({
                        "type": "slow_tool_call",
                        "tool_name": tool_name,
                        "latency_ms": latency,
                    })
                if event.data.get("error"):
                    anomalies.append({
                        "type": "tool_error",
                        "tool_name": tool_name,
                        "error": event.data["error"],
                    })
            elif event.event_type == SessionEventType.ERROR:
                anomalies.append({
                    "type": "session_error",
                    "data": event.data,
                })

        return anomalies
```

## Comparison

| Approach | Event Capture | File Persistence | Step Replay | Session Diff | Anomaly Detection |
|---|---|---|---|---|---|
| SessionEventRecorder | Yes (in-memory) | Yes (JSONL) | No | No | No |
| ActiveSessionRecorder | Yes (high-level) | Via recorder | No | No | No |
| SessionReplayEngine | No | No | Yes | No | No |
| SessionDiffComparator | No | No | No | Yes | No |
| ReplayAnomalyFinder | No | No | No | No | Yes |

**Best for production**: Persist session events to JSONL files named by session_id — flat files are trivially queryable with `grep` and `jq` during incidents and can be archived to S3 for long-term storage. Capture `content_preview` (first 200 chars) of LLM responses and tool results rather than full content to balance debuggability against storage cost. Use `SessionDiffComparator.diff()` when rolling out prompt changes: compare sessions before and after the change at the same sequence positions to verify the new prompt produces the expected execution path. Alert when `ReplayAnomalyFinder.find_anomalies()` returns errors for more than 5% of sessions in an hour.
