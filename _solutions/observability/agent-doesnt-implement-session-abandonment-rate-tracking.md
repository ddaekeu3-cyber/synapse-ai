---
title: "Agent Doesn't Implement Session Abandonment Rate Tracking"
description: "Agents that measure only successful task completions miss a critical signal: sessions where the user stopped interacting before the task finished. High abandonment rates indicate that the agent is too slow, too verbose, making too many tool calls, or producing incorrect intermediate results that cause users to give up. Implement session abandonment rate tracking that classifies sessions as completed, abandoned, or errored, and surfaces abandonment patterns by task type, latency bucket, and tool failure events."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-session-abandonment-rate-tracking
tags: [session-abandonment, user-experience, completion-rate, task-success, engagement, ux-observability]
symptoms:
  - "Completion rate looks healthy but user satisfaction is low — abandoned sessions not counted"
  - "No way to know if users are giving up during long tool-call chains"
  - "Slow responses correlated with re-sent queries not detected"
  - "Task types with high abandonment rate not distinguished from successful ones"
  - "Error sessions and abandonment sessions counted together or not at all"
---

## Why This Happens

Session completion metrics count sessions that reached a final agent response. Sessions where the user closed the window, navigated away, sent a new query before the agent finished, or simply waited too long and gave up are not represented. This creates optimistic completion rates: if 30% of sessions are abandoned, measuring only completed sessions over-reports quality by up to 43%. Abandonment tracking requires a session lifecycle model with explicit state transitions, a timeout or signal mechanism that marks sessions as abandoned when activity stops, and correlation of abandonment with preceding events (slow tool calls, error messages, excessive turns).

## Solution 1: Session Lifecycle State

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class SessionState(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    ABANDONED = "abandoned"       # user left without final response
    ERRORED = "errored"           # agent terminated due to error
    TIMED_OUT = "timed_out"       # no activity for inactivity_timeout


@dataclass
class SessionEvent:
    event_type: str    # "user_message" | "agent_response" | "tool_call" | "tool_error"
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionRecord:
    session_id: str
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    state: SessionState = SessionState.ACTIVE
    events: List[SessionEvent] = field(default_factory=list)
    task_type: str = ""
    user_id: str = ""
    final_agent_responded: bool = False
    abandonment_reason: str = ""

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.ended_at:
            return round(self.ended_at - self.started_at, 2)
        return round(time.time() - self.started_at, 2)

    @property
    def turn_count(self) -> int:
        return sum(1 for e in self.events if e.event_type == "user_message")

    @property
    def tool_error_count(self) -> int:
        return sum(1 for e in self.events if e.event_type == "tool_error")

    def add_event(self, event_type: str, **metadata) -> None:
        self.events.append(SessionEvent(event_type=event_type, metadata=metadata))
```

## Solution 2: Session Tracker

```python
import time
from threading import Lock
from typing import Dict, List, Optional


class SessionTracker:
    """
    Maintains active session records and transitions sessions
    to terminal states (completed, abandoned, errored, timed_out).
    """

    def __init__(
        self,
        inactivity_timeout_seconds: float = 120.0,
        max_sessions: int = 10000,
    ):
        self._timeout = inactivity_timeout_seconds
        self._max = max_sessions
        self._sessions: Dict[str, SessionRecord] = {}
        self._closed: List[SessionRecord] = []
        self._lock = Lock()

    def start_session(
        self, session_id: str, user_id: str = "", task_type: str = ""
    ) -> SessionRecord:
        record = SessionRecord(
            session_id=session_id, user_id=user_id, task_type=task_type
        )
        with self._lock:
            self._sessions[session_id] = record
        return record

    def record_event(self, session_id: str, event_type: str, **metadata) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session:
                session.add_event(event_type, **metadata)

    def complete_session(self, session_id: str) -> Optional[SessionRecord]:
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session:
                session.state = SessionState.COMPLETED
                session.ended_at = time.time()
                session.final_agent_responded = True
                self._closed.append(session)
            return session

    def abandon_session(self, session_id: str, reason: str = "") -> Optional[SessionRecord]:
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session:
                session.state = SessionState.ABANDONED
                session.ended_at = time.time()
                session.abandonment_reason = reason
                self._closed.append(session)
            return session

    def error_session(self, session_id: str, error: str = "") -> Optional[SessionRecord]:
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session:
                session.state = SessionState.ERRORED
                session.ended_at = time.time()
                session.abandonment_reason = error
                self._closed.append(session)
            return session

    def sweep_inactive(self) -> List[SessionRecord]:
        cutoff = time.time() - self._timeout
        timed_out = []
        with self._lock:
            inactive_ids = [
                sid for sid, s in self._sessions.items()
                if s.events and s.events[-1].timestamp < cutoff
            ]
            for sid in inactive_ids:
                session = self._sessions.pop(sid)
                session.state = SessionState.TIMED_OUT
                session.ended_at = time.time()
                session.abandonment_reason = "inactivity_timeout"
                self._closed.append(session)
                timed_out.append(session)
        return timed_out

    def active_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def closed_sessions(self, limit: int = 1000) -> List[SessionRecord]:
        with self._lock:
            return self._closed[-limit:]
```

## Solution 3: Abandonment Rate Calculator

```python
import time
from typing import Dict, List, Optional


class AbandonmentRateCalculator:
    """
    Computes abandonment rates from closed session records.
    Segments by task type, latency bucket, and preceding error events.
    """

    def calculate(
        self,
        sessions: List[SessionRecord],
        window_seconds: float = 3600.0,
    ) -> dict:
        cutoff = time.time() - window_seconds
        recent = [
            s for s in sessions
            if s.ended_at and s.ended_at >= cutoff
        ]
        if not recent:
            return {"window_seconds": window_seconds, "sessions": 0}

        completed = [s for s in recent if s.state == SessionState.COMPLETED]
        abandoned = [s for s in recent if s.state in (
            SessionState.ABANDONED, SessionState.TIMED_OUT
        )]
        errored = [s for s in recent if s.state == SessionState.ERRORED]

        abandonment_rate = len(abandoned) / max(len(recent), 1)

        # By task type
        by_task: Dict[str, dict] = {}
        for s in recent:
            t = s.task_type or "unknown"
            if t not in by_task:
                by_task[t] = {"total": 0, "abandoned": 0}
            by_task[t]["total"] += 1
            if s.state in (SessionState.ABANDONED, SessionState.TIMED_OUT):
                by_task[t]["abandoned"] += 1

        # Abandonment after tool errors
        abandoned_after_error = sum(
            1 for s in abandoned if s.tool_error_count > 0
        )

        return {
            "window_seconds": window_seconds,
            "total_sessions": len(recent),
            "completed": len(completed),
            "abandoned": len(abandoned),
            "errored": len(errored),
            "abandonment_rate_pct": round(abandonment_rate * 100, 2),
            "abandoned_after_tool_error_pct": round(
                abandoned_after_error / max(len(abandoned), 1) * 100, 1
            ),
            "by_task_type": {
                t: {
                    "abandonment_rate_pct": round(
                        v["abandoned"] / max(v["total"], 1) * 100, 1
                    ),
                    "total": v["total"],
                }
                for t, v in by_task.items()
            },
        }
```

## Solution 4: Abandonment Latency Correlator

```python
from typing import List


class AbandonmentLatencyCorrelator:
    """
    Correlates session abandonment with duration at abandonment time.
    Identifies whether slow sessions are disproportionately abandoned.
    """

    def __init__(self, latency_buckets: List[float] = None):
        self._buckets = latency_buckets or [10.0, 30.0, 60.0, 120.0, 300.0]

    def correlate(self, sessions: List[SessionRecord]) -> dict:
        bucket_labels = [f"<{int(b)}s" for b in self._buckets] + [
            f">={int(self._buckets[-1])}s"
        ]
        bucket_stats = {label: {"total": 0, "abandoned": 0} for label in bucket_labels}

        for s in sessions:
            if s.ended_at is None:
                continue
            duration = s.ended_at - s.started_at
            label = bucket_labels[-1]
            for i, threshold in enumerate(self._buckets):
                if duration < threshold:
                    label = bucket_labels[i]
                    break
            bucket_stats[label]["total"] += 1
            if s.state in (SessionState.ABANDONED, SessionState.TIMED_OUT):
                bucket_stats[label]["abandoned"] += 1

        return {
            "latency_buckets": {
                label: {
                    "total": v["total"],
                    "abandonment_rate_pct": round(
                        v["abandoned"] / max(v["total"], 1) * 100, 1
                    ),
                }
                for label, v in bucket_stats.items()
            }
        }
```

## Solution 5: Inactivity Sweep Scheduler

```python
import asyncio
import time
from typing import Optional


class InactivitySweepScheduler:
    """
    Periodically runs the session tracker sweep to transition
    inactive sessions to TIMED_OUT state without waiting for
    an explicit close signal from the application.
    """

    def __init__(
        self,
        tracker: SessionTracker,
        sweep_interval_seconds: float = 30.0,
    ):
        self._tracker = tracker
        self._interval = sweep_interval_seconds
        self._task: Optional[asyncio.Task] = None
        self._total_swept = 0

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            swept = self._tracker.sweep_inactive()
            self._total_swept += len(swept)
            await asyncio.sleep(self._interval)

    def stats(self) -> dict:
        return {
            "total_swept": self._total_swept,
            "sweep_interval_seconds": self._interval,
        }
```

## Solution 6: Session Abandonment Dashboard

```python
import time


class SessionAbandonmentDashboard:
    """
    Combines abandonment rates, latency correlation, active session count,
    and sweep statistics into a single user-experience observability view.
    """

    def __init__(
        self,
        tracker: SessionTracker,
        calculator: AbandonmentRateCalculator,
        correlator: AbandonmentLatencyCorrelator,
        sweep_scheduler: InactivitySweepScheduler,
    ):
        self._tracker = tracker
        self._calc = calculator
        self._correlator = correlator
        self._sweep = sweep_scheduler

    def render(self) -> dict:
        closed = self._tracker.closed_sessions(limit=5000)
        return {
            "generated_at": time.time(),
            "active_sessions": self._tracker.active_count(),
            "abandonment_1h": self._calc.calculate(closed, 3600.0),
            "abandonment_24h": self._calc.calculate(closed, 86400.0),
            "latency_correlation": self._correlator.correlate(closed[-500:]),
            "sweep_stats": self._sweep.stats(),
        }
```

## Comparison

| Approach | Session Lifecycle | Abandonment Classification | Latency Correlation | Inactivity Sweep | Dashboard |
|---|---|---|---|---|---|
| SessionTracker | Yes (full state machine) | Via state | No | Yes (sweep_inactive) | No |
| AbandonmentRateCalculator | No | Yes (by type/error) | No | No | No |
| AbandonmentLatencyCorrelator | No | No | Yes | No | No |
| InactivitySweepScheduler | No | No | No | Yes (async) | No |
| SessionAbandonmentDashboard | No | No | No | No | Yes |

**Best for production**: Set `inactivity_timeout_seconds=120` — two minutes of silence after the last user message reliably signals abandonment without false-positiving on users who are reading a long agent response. Distinguish `ABANDONED` (user sent a new message or closed the session explicitly) from `TIMED_OUT` (inactivity) in your reporting — timed-out sessions where the agent was still generating a response indicate slow generation, while timed-out sessions where the agent had already responded indicate the user was unsatisfied with the answer. Monitor `abandonment_rate_pct` by `task_type`: if a specific task type exceeds 40% abandonment while others are below 15%, that task type's agent behavior (verbosity, tool chain depth, accuracy) warrants targeted investigation. Correlate `abandoned_after_tool_error_pct` above 50% with specific tool names — this reliably identifies which tool failures drive users away.
