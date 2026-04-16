---
title: "Agent Doesn't Implement Session Replay Event Logging"
description: "Agents that log only final outputs cannot be replayed or debugged after the fact: a user reports a bad response but there is no record of which tools were called, what context was injected, or what the LLM was asked. Implement session replay event logging that captures every agent event — user turn, tool call, tool result, LLM request, LLM response — as a structured, ordered log that can reconstruct the full session state at any point in time."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-session-replay-event-logging
tags: [session-replay, event-logging, audit-trail, debugging, replay-buffer, structured-logging]
symptoms:
  - "User reports bad agent response but no record of what tools were called"
  - "Cannot reproduce a failure because the intermediate context is not logged"
  - "LLM prompt content at time of bad response is unrecoverable"
  - "On-call engineer must ask the user to reproduce the issue manually"
  - "No ordered event stream — log entries from parallel tool calls appear out of sequence"
---

## Why This Happens

Application logs capture what happened at the infrastructure layer — HTTP requests, errors, latencies — but not the agent-layer narrative: what the user asked, how the agent decomposed it, what each tool returned, and what was finally sent to the model. Without an ordered, session-scoped event log, debugging requires guessing from output alone. Session replay logging requires assigning a monotonic sequence number to every agent event, tagging every event with a session ID, and writing events to a durable append-only log that can be read back in order.

## Solution 1: Replay Event

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class ReplayEventType(str, Enum):
    SESSION_START = "session_start"
    USER_TURN = "user_turn"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_END = "tool_call_end"
    TOOL_CALL_ERROR = "tool_call_error"
    LLM_REQUEST = "llm_request"
    LLM_RESPONSE = "llm_response"
    CONTEXT_ASSEMBLED = "context_assembled"
    AGENT_DECISION = "agent_decision"
    SESSION_END = "session_end"


@dataclass
class ReplayEvent:
    session_id: str
    event_type: ReplayEventType
    sequence: int                          # monotonic within session
    timestamp: float = field(default_factory=time.time)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    payload: Dict[str, Any] = field(default_factory=dict)
    parent_event_id: Optional[str] = None  # for nested events (tool call → result)
    duration_ms: Optional[float] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "event_type": self.event_type.value,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "event_id": self.event_id,
            "payload": self.payload,
            "parent_event_id": self.parent_event_id,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }
```

## Solution 2: Session Event Sequencer

```python
import threading
from typing import List


class SessionEventSequencer:
    """
    Maintains a monotonically increasing sequence counter per session.
    Thread-safe for concurrent tool calls within one session.
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._counter = 0
        self._lock = threading.Lock()
        self._events: List[ReplayEvent] = []

    def next_sequence(self) -> int:
        with self._lock:
            self._counter += 1
            return self._counter

    def emit(
        self,
        event_type: ReplayEventType,
        payload: dict = None,
        parent_event_id: str = None,
        duration_ms: float = None,
        error: str = None,
    ) -> ReplayEvent:
        event = ReplayEvent(
            session_id=self.session_id,
            event_type=event_type,
            sequence=self.next_sequence(),
            payload=payload or {},
            parent_event_id=parent_event_id,
            duration_ms=duration_ms,
            error=error,
        )
        with self._lock:
            self._events.append(event)
        return event

    def events(self) -> List[ReplayEvent]:
        with self._lock:
            return list(self._events)

    def event_count(self) -> int:
        with self._lock:
            return len(self._events)
```

## Solution 3: Session Replay Logger

```python
import json
import time
from pathlib import Path
from threading import Lock
from typing import Callable, Dict, List, Optional


class SessionReplayLogger:
    """
    Writes session replay events to a per-session JSONL file.
    Supports in-memory buffering with periodic flush.
    """

    def __init__(
        self,
        log_dir: str = "/tmp/agent_replay_logs",
        flush_every_n: int = 10,
        write_fn: Optional[Callable[[dict], None]] = None,
    ):
        self._dir = Path(log_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._flush_n = flush_every_n
        self._write_fn = write_fn
        self._buffers: Dict[str, List[dict]] = {}
        self._lock = Lock()

    def log(self, event: ReplayEvent) -> None:
        record = event.to_dict()
        if self._write_fn:
            self._write_fn(record)

        with self._lock:
            session_id = event.session_id
            buf = self._buffers.setdefault(session_id, [])
            buf.append(record)
            if len(buf) >= self._flush_n:
                self._flush_session(session_id)

    def flush(self, session_id: str) -> None:
        with self._lock:
            self._flush_session(session_id)

    def _flush_session(self, session_id: str) -> None:
        buf = self._buffers.get(session_id)
        if not buf:
            return
        path = self._dir / f"{session_id}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            for record in buf:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._buffers[session_id] = []

    def flush_all(self) -> None:
        with self._lock:
            for session_id in list(self._buffers.keys()):
                self._flush_session(session_id)

    def read_session(self, session_id: str) -> List[dict]:
        path = self._dir / f"{session_id}.jsonl"
        if not path.exists():
            return []
        events = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return sorted(events, key=lambda e: e["sequence"])
```

## Solution 4: Instrumented Agent Session

```python
import time
from typing import Any, Callable, Optional


class InstrumentedAgentSession:
    """
    Wraps agent session operations and emits replay events for each.
    """

    def __init__(
        self,
        session_id: str,
        sequencer: SessionEventSequencer,
        logger: SessionReplayLogger,
    ):
        self._session_id = session_id
        self._seq = sequencer
        self._logger = logger

    def start(self, metadata: dict = None) -> ReplayEvent:
        event = self._seq.emit(ReplayEventType.SESSION_START, payload=metadata or {})
        self._logger.log(event)
        return event

    def record_user_turn(self, message: str, token_count: int = 0) -> ReplayEvent:
        event = self._seq.emit(ReplayEventType.USER_TURN, payload={
            "message": message[:500],  # truncate for log safety
            "token_count": token_count,
        })
        self._logger.log(event)
        return event

    def record_tool_call(
        self,
        tool_name: str,
        args: dict,
        parent_id: str = None,
    ) -> ReplayEvent:
        event = self._seq.emit(ReplayEventType.TOOL_CALL_START, payload={
            "tool_name": tool_name,
            "args_keys": list(args.keys()),
        }, parent_event_id=parent_id)
        self._logger.log(event)
        return event

    def record_tool_result(
        self,
        tool_name: str,
        result_summary: str,
        duration_ms: float,
        parent_id: str,
        error: str = None,
    ) -> ReplayEvent:
        etype = ReplayEventType.TOOL_CALL_ERROR if error else ReplayEventType.TOOL_CALL_END
        event = self._seq.emit(etype, payload={
            "tool_name": tool_name,
            "result_preview": result_summary[:200],
        }, parent_event_id=parent_id, duration_ms=duration_ms, error=error)
        self._logger.log(event)
        return event

    def record_llm_request(self, prompt_tokens: int, context_summary: str = "") -> ReplayEvent:
        event = self._seq.emit(ReplayEventType.LLM_REQUEST, payload={
            "prompt_tokens": prompt_tokens,
            "context_summary": context_summary[:300],
        })
        self._logger.log(event)
        return event

    def record_llm_response(self, completion_tokens: int, response_preview: str, duration_ms: float) -> ReplayEvent:
        event = self._seq.emit(ReplayEventType.LLM_RESPONSE, payload={
            "completion_tokens": completion_tokens,
            "response_preview": response_preview[:300],
        }, duration_ms=duration_ms)
        self._logger.log(event)
        return event

    def end(self, reason: str = "complete") -> None:
        event = self._seq.emit(ReplayEventType.SESSION_END, payload={"reason": reason})
        self._logger.log(event)
        self._logger.flush(self._session_id)
```

## Solution 5: Session Replay Reader

```python
from typing import Dict, List, Optional


class SessionReplayReader:
    """
    Reads a session replay log and provides structured access to
    events by type, tool name, or time range for debugging.
    """

    def __init__(self, logger: SessionReplayLogger):
        self._logger = logger

    def get_timeline(self, session_id: str) -> List[dict]:
        return self._logger.read_session(session_id)

    def get_tool_calls(self, session_id: str) -> List[dict]:
        events = self._logger.read_session(session_id)
        return [e for e in events if e["event_type"] in ("tool_call_start", "tool_call_end", "tool_call_error")]

    def get_llm_exchanges(self, session_id: str) -> List[dict]:
        events = self._logger.read_session(session_id)
        return [e for e in events if e["event_type"] in ("llm_request", "llm_response")]

    def summarize(self, session_id: str) -> dict:
        events = self._logger.read_session(session_id)
        if not events:
            return {"session_id": session_id, "events": 0}
        tool_calls = [e for e in events if e["event_type"] == "tool_call_start"]
        errors = [e for e in events if e["event_type"] == "tool_call_error"]
        llm_reqs = [e for e in events if e["event_type"] == "llm_request"]
        return {
            "session_id": session_id,
            "total_events": len(events),
            "tool_calls": len(tool_calls),
            "tool_errors": len(errors),
            "llm_requests": len(llm_reqs),
            "duration_ms": round((events[-1]["timestamp"] - events[0]["timestamp"]) * 1000, 2),
            "unique_tools": list({e["payload"].get("tool_name") for e in tool_calls if e.get("payload")}),
        }
```

## Solution 6: Replay Log Retention Manager

```python
import os
import time
from pathlib import Path
from typing import List


class ReplayLogRetentionManager:
    """
    Prunes session replay logs older than the retention window.
    Prevents unbounded disk usage from accumulated replay files.
    """

    def __init__(
        self,
        log_dir: str,
        retention_days: float = 7.0,
    ):
        self._dir = Path(log_dir)
        self._retention = retention_days * 86400

    def prune(self) -> dict:
        cutoff = time.time() - self._retention
        pruned = []
        kept = []
        for path in self._dir.glob("*.jsonl"):
            if path.stat().st_mtime < cutoff:
                path.unlink()
                pruned.append(path.name)
            else:
                kept.append(path.name)
        return {
            "pruned": len(pruned),
            "kept": len(kept),
            "retention_days": self._retention / 86400,
        }

    def disk_usage_mb(self) -> float:
        total = sum(p.stat().st_size for p in self._dir.glob("*.jsonl"))
        return round(total / (1024 * 1024), 2)
```

## Comparison

| Approach | Ordered Events | Per-Session Files | Tool Call Pairing | LLM Exchange Log | Retention |
|---|---|---|---|---|---|
| SessionEventSequencer | Yes (monotonic) | No | No | No | No |
| SessionReplayLogger | Via sequencer | Yes (JSONL) | No | No | No |
| InstrumentedAgentSession | Via sequencer | Via logger | Yes (parent_id) | Yes | No |
| SessionReplayReader | No | Via logger | Via event type | Via event type | No |
| ReplayLogRetentionManager | No | No | No | No | Yes (TTL prune) |

**Best for production**: Write replay logs to JSONL (one JSON object per line) — this format is appendable, grep-friendly, and importable into any log aggregation system. Use `parent_event_id` to link tool call start and end events; this allows computing per-tool latency from the replay log without a separate metrics system. Truncate message content in `record_user_turn` and response content in `record_llm_response` to 500 characters for PII compliance — the replay log should capture the structure of what happened, not full plaintext user data. Set `retention_days=7` and run `ReplayLogRetentionManager.prune()` daily; 7 days covers almost all post-incident investigations without unbounded disk growth.
