---
title: "Agent Doesn't Implement Session Replay for Debugging"
description: "Agents that only log final outcomes make it impossible to reconstruct what happened during a failed or unexpected session: which tools were called, what their arguments and results were, what the LLM said at each turn, and how long each step took. Without session replay capability, debugging requires reproducing issues from scratch. Implement structured session event recording that captures the full turn-by-turn sequence and supports replaying or inspecting any session post-hoc."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-session-replay-for-debugging
tags: [session-replay, debugging, event-recording, turn-tracing, incident-investigation, observability]
symptoms:
  - "Cannot reconstruct what happened during a reported failed session"
  - "Bug reports require users to reproduce the issue because no session record exists"
  - "Tool arguments and results at time of failure are not preserved anywhere"
  - "LLM responses mid-session are lost — only the final answer is logged"
  - "No way to compare two sessions to find what differed between a passing and failing run"
---

## Why This Happens

Application logs capture events at a system level — requests in, responses out, errors thrown. They do not capture the conversational structure of an agent session: the sequence of LLM turns, the tool calls within each turn, the arguments passed, results received, and the time spent at each step. Without this structure, a bug report that says "the agent gave the wrong answer on question X" requires a full reproduction from scratch. Structured session event recording treats the agent session as a first-class audit artifact: each event is timestamped and typed, events are linked to a session and turn, and the full sequence can be replayed or diffed against another session.

## Solution 1: Session Event

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class SessionEventType(str, Enum):
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    USER_MESSAGE = "user_message"
    LLM_REQUEST = "llm_request"
    LLM_RESPONSE = "llm_response"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_END = "tool_call_end"
    TOOL_CALL_ERROR = "tool_call_error"
    CONTEXT_ASSEMBLED = "context_assembled"
    ERROR = "error"


@dataclass
class SessionEvent:
    event_id: str
    session_id: str
    turn_index: int
    event_type: SessionEventType
    timestamp: float
    payload: Dict[str, Any]
    duration_ms: Optional[float] = None   # set for paired start/end events
    parent_event_id: Optional[str] = None

    @staticmethod
    def make_id() -> str:
        import uuid
        return uuid.uuid4().hex[:12]
```

## Solution 2: Session Event Recorder

```python
import json
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional


class SessionEventRecorder:
    """
    Records all events for an agent session to an in-memory buffer
    and optionally flushes to a JSONL file for durable storage.
    """

    def __init__(
        self,
        session_id: str,
        output_path: Optional[str] = None,
        max_payload_chars: int = 4000,
    ):
        self._session_id = session_id
        self._path = Path(output_path) if output_path else None
        self._max_chars = max_payload_chars
        self._lock = threading.Lock()
        self._events: List[SessionEvent] = []
        self._open_spans: Dict[str, float] = {}   # event_id -> start_time

    def record(self, event: SessionEvent) -> None:
        event.payload = self._truncate_payload(event.payload)
        with self._lock:
            self._events.append(event)
        if self._path:
            self._flush_one(event)

    def start_span(self, event_id: str) -> None:
        self._open_spans[event_id] = time.time()

    def end_span(self, event_id: str) -> Optional[float]:
        start = self._open_spans.pop(event_id, None)
        if start is None:
            return None
        return round((time.time() - start) * 1000, 2)

    def _truncate_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = {}
        for k, v in payload.items():
            if isinstance(v, str) and len(v) > self._max_chars:
                result[k] = v[: self._max_chars] + f"...[truncated {len(v) - self._max_chars} chars]"
            else:
                result[k] = v
        return result

    def _flush_one(self, event: SessionEvent) -> None:
        record = {
            "event_id": event.event_id,
            "session_id": event.session_id,
            "turn_index": event.turn_index,
            "event_type": event.event_type.value,
            "timestamp": event.timestamp,
            "payload": event.payload,
            "duration_ms": event.duration_ms,
            "parent_event_id": event.parent_event_id,
        }
        with self._path.open("a") as f:
            f.write(json.dumps(record) + "\n")

    def all_events(self) -> List[SessionEvent]:
        with self._lock:
            return list(self._events)
```

## Solution 3: Session Replay Reader

```python
import json
from pathlib import Path
from typing import List, Optional


class SessionReplayReader:
    """
    Reads a recorded session from a JSONL file and reconstructs
    the turn-by-turn event sequence for debugging inspection.
    """

    @staticmethod
    def load(path: str) -> List[SessionEvent]:
        events = []
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            events.append(SessionEvent(
                event_id=data["event_id"],
                session_id=data["session_id"],
                turn_index=data["turn_index"],
                event_type=SessionEventType(data["event_type"]),
                timestamp=data["timestamp"],
                payload=data["payload"],
                duration_ms=data.get("duration_ms"),
                parent_event_id=data.get("parent_event_id"),
            ))
        return events

    @staticmethod
    def turns(events: List[SessionEvent]) -> Dict[int, List[SessionEvent]]:
        from collections import defaultdict
        turns: Dict[int, List[SessionEvent]] = defaultdict(list)
        for e in events:
            turns[e.turn_index].append(e)
        return dict(sorted(turns.items()))

    @staticmethod
    def tool_calls(events: List[SessionEvent]) -> List[dict]:
        starts = {
            e.event_id: e for e in events
            if e.event_type == SessionEventType.TOOL_CALL_START
        }
        ends = {
            e.parent_event_id: e for e in events
            if e.event_type in (SessionEventType.TOOL_CALL_END, SessionEventType.TOOL_CALL_ERROR)
            and e.parent_event_id
        }
        result = []
        for eid, start in starts.items():
            end = ends.get(eid)
            result.append({
                "turn_index": start.turn_index,
                "tool_name": start.payload.get("tool_name"),
                "args": start.payload.get("args"),
                "result": end.payload.get("result") if end else None,
                "error": end.payload.get("error") if end and end.event_type == SessionEventType.TOOL_CALL_ERROR else None,
                "duration_ms": end.duration_ms if end else None,
                "timestamp": start.timestamp,
            })
        return sorted(result, key=lambda x: x["timestamp"])
```

## Solution 4: Instrumented Agent Turn Wrapper

```python
import time
from typing import Any, Callable, Dict, Optional


class InstrumentedAgentTurnWrapper:
    """
    Wraps a single agent turn with event recording for LLM calls,
    tool calls, and context assembly. Returns the turn result with
    a full event trace attached.
    """

    def __init__(self, recorder: SessionEventRecorder):
        self._recorder = recorder

    def _event(
        self,
        turn_index: int,
        event_type: SessionEventType,
        payload: Dict[str, Any],
        parent_id: Optional[str] = None,
        duration_ms: Optional[float] = None,
    ) -> SessionEvent:
        e = SessionEvent(
            event_id=SessionEvent.make_id(),
            session_id=self._recorder._session_id,
            turn_index=turn_index,
            event_type=event_type,
            timestamp=time.time(),
            payload=payload,
            duration_ms=duration_ms,
            parent_event_id=parent_id,
        )
        self._recorder.record(e)
        return e

    async def record_tool_call(
        self,
        turn_index: int,
        tool_name: str,
        args: Dict[str, Any],
        tool_fn: Callable,
    ) -> Any:
        start_event = self._event(
            turn_index,
            SessionEventType.TOOL_CALL_START,
            {"tool_name": tool_name, "args": args},
        )
        self._recorder.start_span(start_event.event_id)
        try:
            result = await tool_fn(**args)
            duration_ms = self._recorder.end_span(start_event.event_id)
            self._event(
                turn_index,
                SessionEventType.TOOL_CALL_END,
                {"tool_name": tool_name, "result": str(result)[:2000]},
                parent_id=start_event.event_id,
                duration_ms=duration_ms,
            )
            return result
        except Exception as exc:
            duration_ms = self._recorder.end_span(start_event.event_id)
            self._event(
                turn_index,
                SessionEventType.TOOL_CALL_ERROR,
                {"tool_name": tool_name, "error": str(exc)},
                parent_id=start_event.event_id,
                duration_ms=duration_ms,
            )
            raise

    def record_llm_response(
        self, turn_index: int, response_preview: str, token_count: int, latency_ms: float
    ) -> None:
        self._event(
            turn_index,
            SessionEventType.LLM_RESPONSE,
            {"preview": response_preview[:500], "token_count": token_count},
            duration_ms=latency_ms,
        )
```

## Solution 5: Session Diff Comparator

```python
from typing import List, Tuple


class SessionDiffComparator:
    """
    Compares two recorded sessions event-by-event to surface structural
    differences — useful for comparing a passing and a failing run of the
    same prompt to isolate what changed.
    """

    def compare(
        self,
        events_a: List[SessionEvent],
        events_b: List[SessionEvent],
    ) -> dict:
        tool_calls_a = self._tool_call_sequence(events_a)
        tool_calls_b = self._tool_call_sequence(events_b)

        diffs = []
        max_len = max(len(tool_calls_a), len(tool_calls_b))
        for i in range(max_len):
            call_a = tool_calls_a[i] if i < len(tool_calls_a) else None
            call_b = tool_calls_b[i] if i < len(tool_calls_b) else None
            if call_a != call_b:
                diffs.append({
                    "position": i,
                    "session_a": call_a,
                    "session_b": call_b,
                })

        return {
            "total_turns_a": max(e.turn_index for e in events_a) + 1 if events_a else 0,
            "total_turns_b": max(e.turn_index for e in events_b) + 1 if events_b else 0,
            "tool_call_count_a": len(tool_calls_a),
            "tool_call_count_b": len(tool_calls_b),
            "differences": diffs,
            "identical": len(diffs) == 0,
        }

    @staticmethod
    def _tool_call_sequence(events: List[SessionEvent]) -> List[str]:
        return [
            e.payload.get("tool_name", "unknown")
            for e in events
            if e.event_type == SessionEventType.TOOL_CALL_START
        ]
```

## Solution 6: Session Replay Store

```python
import time
from pathlib import Path
from typing import Dict, List, Optional


class SessionReplayStore:
    """
    Manages replay files for multiple sessions. Provides listing,
    retrieval, and cleanup of session recordings.
    """

    def __init__(self, base_dir: str, max_age_seconds: float = 86400 * 7):
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)
        self._max_age = max_age_seconds

    def path_for(self, session_id: str) -> str:
        return str(self._base / f"{session_id}.jsonl")

    def list_sessions(self) -> List[dict]:
        sessions = []
        for p in self._base.glob("*.jsonl"):
            stat = p.stat()
            sessions.append({
                "session_id": p.stem,
                "size_bytes": stat.st_size,
                "modified_at": stat.st_mtime,
            })
        return sorted(sessions, key=lambda x: -x["modified_at"])

    def load(self, session_id: str) -> List[SessionEvent]:
        path = self.path_for(session_id)
        return SessionReplayReader.load(path)

    def purge_old(self) -> int:
        cutoff = time.time() - self._max_age
        removed = 0
        for p in self._base.glob("*.jsonl"):
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        return removed
```

## Comparison

| Approach | Event Recording | Durable Storage | Turn Reconstruction | Tool Call Trace | Session Diff |
|---|---|---|---|---|---|
| SessionEventRecorder | Yes (in-memory + JSONL) | Yes (JSONL) | No | No | No |
| SessionReplayReader | No | No | Yes (by turn) | Yes (start+end pairs) | No |
| InstrumentedAgentTurnWrapper | Via recorder | Via recorder | No | Yes (async wrapping) | No |
| SessionDiffComparator | No | No | No | No | Yes |
| SessionReplayStore | No | Yes (multi-session) | Via reader | Via reader | No |

**Best for production**: Write session replay files to a separate low-cost object store (S3, GCS) rather than primary application storage — they are write-once, rarely read, and can be large. Retain for 30 days by default and extend retention only for sessions flagged by users or support tickets. Use `SessionDiffComparator` as a first debugging step when a bug is reported: load the failing session and a recent passing session for the same prompt, compare tool call sequences, and the first divergence point almost always identifies the root cause. Keep `max_payload_chars=4000` to avoid bloating replay files with full document content while preserving enough context for debugging.
