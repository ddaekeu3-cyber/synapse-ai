---
title: "Agent Doesn't Implement Session Replay for Debugging Agent Failures"
description: "Agents that log only final outcomes make failure reproduction nearly impossible: when a user reports wrong behavior, engineers cannot reconstruct what the agent saw, what tools it called, what responses it received, or which decision branch it took. Implement session replay that records a complete, deterministic event log per conversation so any session can be replayed, inspected, and diffed against a corrected run."
date: 2026-04-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-session-replay-for-debugging-agent-failures
tags: [session-replay, debugging, event-sourcing, deterministic-replay, failure-reproduction, audit-trail]
symptoms:
  - "Cannot reproduce a reported agent failure — no record of what the agent saw"
  - "Tool call arguments and responses are not persisted — only final answers are logged"
  - "Engineers must ask users to reproduce issues manually to debug"
  - "No way to diff two sessions to find where behavior diverged"
  - "Post-incident analysis relies on user descriptions rather than recorded evidence"
---

## Why This Happens

Agents are stateful, multi-step processes where behavior at step N depends on all prior steps. Logging only the final output is equivalent to logging only the return value of a function — useless for debugging the logic inside. Session replay requires event sourcing: every agent action (LLM call, tool dispatch, tool response, context mutation) is recorded as an immutable event with a sequence number and timestamp. A replay engine can reconstruct the full session state at any point in time by replaying the event log, allowing engineers to step through the session, inject mock tool responses, and test whether a fix produces the correct outcome.

## Solution 1: Session Event Model

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class EventType(str, Enum):
    SESSION_START = "session_start"
    USER_MESSAGE = "user_message"
    LLM_REQUEST = "llm_request"
    LLM_RESPONSE = "llm_response"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TOOL_ERROR = "tool_error"
    CONTEXT_SNAPSHOT = "context_snapshot"
    AGENT_DECISION = "agent_decision"
    SESSION_END = "session_end"


@dataclass
class SessionEvent:
    event_id: str
    session_id: str
    sequence: int
    event_type: EventType
    timestamp: float
    payload: Dict[str, Any]
    metadata: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def create(
        session_id: str,
        sequence: int,
        event_type: EventType,
        payload: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "SessionEvent":
        return SessionEvent(
            event_id=str(uuid.uuid4()),
            session_id=session_id,
            sequence=sequence,
            event_type=event_type,
            timestamp=time.time(),
            payload=payload,
            metadata=metadata or {},
        )
```

## Solution 2: Session Event Recorder

```python
import json
from pathlib import Path
from threading import Lock
from typing import List, Optional


class SessionEventRecorder:
    """
    Records session events to a JSONL file, one event per line.
    Each session gets its own file for isolated replay.
    """

    def __init__(self, storage_dir: str = "/tmp/agent_sessions"):
        self._dir = Path(storage_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._sequences: dict = {}
        self._lock = Lock()

    def _session_path(self, session_id: str) -> Path:
        return self._dir / f"{session_id}.jsonl"

    def _next_seq(self, session_id: str) -> int:
        with self._lock:
            seq = self._sequences.get(session_id, 0)
            self._sequences[session_id] = seq + 1
            return seq

    def record(
        self,
        session_id: str,
        event_type: EventType,
        payload: dict,
        metadata: Optional[dict] = None,
    ) -> SessionEvent:
        seq = self._next_seq(session_id)
        event = SessionEvent.create(
            session_id=session_id,
            sequence=seq,
            event_type=event_type,
            payload=self._sanitize(payload),
            metadata=metadata or {},
        )
        with self._lock:
            with self._session_path(session_id).open("a") as f:
                f.write(json.dumps({
                    "event_id": event.event_id,
                    "session_id": event.session_id,
                    "sequence": event.sequence,
                    "event_type": event.event_type.value,
                    "timestamp": event.timestamp,
                    "payload": event.payload,
                    "metadata": event.metadata,
                }) + "\n")
        return event

    @staticmethod
    def _sanitize(payload: dict) -> dict:
        """Remove values too large to store inline."""
        result = {}
        for k, v in payload.items():
            if isinstance(v, str) and len(v) > 8000:
                result[k] = v[:8000] + f"...[truncated {len(v) - 8000} chars]"
            else:
                result[k] = v
        return result

    def load_session(self, session_id: str) -> List[SessionEvent]:
        path = self._session_path(session_id)
        if not path.exists():
            return []
        events = []
        for line in path.read_text().splitlines():
            try:
                data = json.loads(line)
                events.append(SessionEvent(
                    event_id=data["event_id"],
                    session_id=data["session_id"],
                    sequence=data["sequence"],
                    event_type=EventType(data["event_type"]),
                    timestamp=data["timestamp"],
                    payload=data["payload"],
                    metadata=data.get("metadata", {}),
                ))
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
        return sorted(events, key=lambda e: e.sequence)

    def list_sessions(self) -> List[str]:
        return [p.stem for p in self._dir.glob("*.jsonl")]
```

## Solution 3: Instrumented Agent Session Context

```python
import time
from typing import Any, Dict, Optional


class InstrumentedAgentSession:
    """
    Context object passed through the agent that records every significant
    action to the session recorder. Attach to a session at request start.
    """

    def __init__(self, session_id: str, recorder: SessionEventRecorder):
        self._session_id = session_id
        self._recorder = recorder
        self._recorder.record(session_id, EventType.SESSION_START, {
            "session_id": session_id,
            "started_at": time.time(),
        })

    @property
    def session_id(self) -> str:
        return self._session_id

    def record_user_message(self, content: str, metadata: Optional[dict] = None) -> None:
        self._recorder.record(self._session_id, EventType.USER_MESSAGE, {
            "content": content,
            "length": len(content),
        }, metadata)

    def record_llm_request(self, model: str, messages: list, tools: list) -> None:
        self._recorder.record(self._session_id, EventType.LLM_REQUEST, {
            "model": model,
            "message_count": len(messages),
            "tool_count": len(tools),
            "messages_summary": [
                {"role": m.get("role"), "length": len(str(m.get("content", "")))}
                for m in messages
            ],
        })

    def record_llm_response(self, response_text: str, stop_reason: str, usage: dict) -> None:
        self._recorder.record(self._session_id, EventType.LLM_RESPONSE, {
            "response_preview": response_text[:500],
            "stop_reason": stop_reason,
            "usage": usage,
        })

    def record_tool_call(self, tool_name: str, args: dict) -> None:
        self._recorder.record(self._session_id, EventType.TOOL_CALL, {
            "tool_name": tool_name,
            "args": args,
        })

    def record_tool_result(self, tool_name: str, result: Any, latency_ms: float) -> None:
        self._recorder.record(self._session_id, EventType.TOOL_RESULT, {
            "tool_name": tool_name,
            "result_preview": str(result)[:1000],
            "latency_ms": round(latency_ms, 2),
        })

    def record_tool_error(self, tool_name: str, error: str, latency_ms: float) -> None:
        self._recorder.record(self._session_id, EventType.TOOL_ERROR, {
            "tool_name": tool_name,
            "error": error,
            "latency_ms": round(latency_ms, 2),
        })

    def record_session_end(self, outcome: str, total_ms: float) -> None:
        self._recorder.record(self._session_id, EventType.SESSION_END, {
            "outcome": outcome,
            "total_ms": round(total_ms, 2),
        })
```

## Solution 4: Session Replay Engine

```python
from typing import Any, Callable, Dict, List, Optional


class SessionReplayEngine:
    """
    Replays a recorded session by stepping through events in sequence.
    Allows injecting mock tool responses for hypothesis testing.
    """

    def __init__(self, recorder: SessionEventRecorder):
        self._recorder = recorder

    def replay(
        self,
        session_id: str,
        tool_mock_fn: Optional[Callable[[str, dict], Any]] = None,
        stop_at_sequence: Optional[int] = None,
    ) -> List[dict]:
        events = self._recorder.load_session(session_id)
        replay_log = []

        for event in events:
            if stop_at_sequence is not None and event.sequence > stop_at_sequence:
                break

            entry = {
                "sequence": event.sequence,
                "event_type": event.event_type.value,
                "timestamp": event.timestamp,
            }

            if event.event_type == EventType.TOOL_CALL and tool_mock_fn:
                tool_name = event.payload.get("tool_name", "")
                args = event.payload.get("args", {})
                mock_result = tool_mock_fn(tool_name, args)
                entry["mock_result"] = str(mock_result)[:500]
                entry["intercepted"] = True
            else:
                entry["payload_summary"] = {
                    k: str(v)[:200] for k, v in event.payload.items()
                }

            replay_log.append(entry)

        return replay_log

    def diff_sessions(self, session_a: str, session_b: str) -> List[dict]:
        """Compare two sessions event-by-event to find where they diverged."""
        events_a = self._recorder.load_session(session_a)
        events_b = self._recorder.load_session(session_b)
        diffs = []
        for i, (ea, eb) in enumerate(zip(events_a, events_b)):
            if ea.event_type != eb.event_type or ea.payload != eb.payload:
                diffs.append({
                    "sequence": i,
                    "session_a": {"type": ea.event_type.value, "payload": ea.payload},
                    "session_b": {"type": eb.event_type.value, "payload": eb.payload},
                })
        if len(events_a) != len(events_b):
            diffs.append({
                "sequence": "length_mismatch",
                "session_a_events": len(events_a),
                "session_b_events": len(events_b),
            })
        return diffs
```

## Solution 5: Session Summary Extractor

```python
from typing import Dict, List, Optional


class SessionSummaryExtractor:
    """
    Produces a human-readable summary of a recorded session for
    quick triage without full replay.
    """

    def __init__(self, recorder: SessionEventRecorder):
        self._recorder = recorder

    def summarize(self, session_id: str) -> dict:
        events = self._recorder.load_session(session_id)
        if not events:
            return {"session_id": session_id, "found": False}

        tool_calls: List[dict] = []
        tool_errors: List[dict] = []
        llm_calls = 0
        total_tokens = 0

        for event in events:
            if event.event_type == EventType.TOOL_CALL:
                tool_calls.append({
                    "tool": event.payload.get("tool_name"),
                    "seq": event.sequence,
                })
            elif event.event_type == EventType.TOOL_ERROR:
                tool_errors.append({
                    "tool": event.payload.get("tool_name"),
                    "error": event.payload.get("error", "")[:100],
                    "seq": event.sequence,
                })
            elif event.event_type == EventType.LLM_REQUEST:
                llm_calls += 1
            elif event.event_type == EventType.LLM_RESPONSE:
                usage = event.payload.get("usage", {})
                total_tokens += usage.get("total_tokens", 0)

        start_ts = events[0].timestamp if events else 0
        end_ts = events[-1].timestamp if events else 0

        return {
            "session_id": session_id,
            "event_count": len(events),
            "duration_seconds": round(end_ts - start_ts, 2),
            "llm_calls": llm_calls,
            "tool_calls": len(tool_calls),
            "tool_errors": len(tool_errors),
            "total_tokens": total_tokens,
            "tools_used": list({tc["tool"] for tc in tool_calls}),
            "errors": tool_errors,
        }
```

## Solution 6: Session Replay Dashboard

```python
import time
from typing import List, Optional


class SessionReplayDashboard:
    """
    Provides an operational view of recent sessions with failure filtering
    and replay access for debugging.
    """

    def __init__(
        self,
        recorder: SessionEventRecorder,
        extractor: SessionSummaryExtractor,
        replay_engine: SessionReplayEngine,
    ):
        self._recorder = recorder
        self._extractor = extractor
        self._replay = replay_engine

    def list_failed_sessions(self, last_n: int = 20) -> List[dict]:
        sessions = self._recorder.list_sessions()[-last_n:]
        summaries = [self._extractor.summarize(sid) for sid in sessions]
        return [s for s in summaries if s.get("tool_errors")]

    def inspect(self, session_id: str) -> dict:
        summary = self._extractor.summarize(session_id)
        events = self._recorder.load_session(session_id)
        timeline = [
            {
                "seq": e.sequence,
                "type": e.event_type.value,
                "ts": e.timestamp,
                "preview": str(e.payload)[:150],
            }
            for e in events
        ]
        return {
            "summary": summary,
            "timeline": timeline,
        }

    def render(self) -> dict:
        failed = self.list_failed_sessions()
        return {
            "generated_at": time.time(),
            "total_sessions_stored": len(self._recorder.list_sessions()),
            "sessions_with_errors": len(failed),
            "recent_failures": failed[:5],
        }
```

## Comparison

| Approach | Event Recording | JSONL Persistence | Replay with Mocks | Session Diff | Summary/Triage |
|---|---|---|---|---|---|
| SessionEventRecorder | Yes (typed) | Yes | No | No | No |
| InstrumentedAgentSession | Via recorder | Via recorder | No | No | No |
| SessionReplayEngine | No | Via recorder | Yes | Yes (diff) | No |
| SessionSummaryExtractor | No | Via recorder | No | No | Yes |
| SessionReplayDashboard | No | No | Via engine | No | Via extractor |

**Best for production**: Store session JSONL files with a 7-day TTL — most debugging happens within 48 hours of a report, and indefinite storage balloons disk usage. Index sessions by user ID and outcome so failed sessions are queryable without scanning all files. Use `SessionReplayEngine.diff_sessions()` to compare a failing session against a known-good session from the same user: the first diverging event is almost always the root cause. When replaying with `tool_mock_fn`, inject the corrected tool response to verify that the fix produces the expected final output before deploying.
