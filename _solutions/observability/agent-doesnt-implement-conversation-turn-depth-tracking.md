---
title: "Agent Doesn't Implement Conversation Turn Depth Tracking"
description: "Agents that do not track conversation turn depth have no signal for detecting runaway multi-turn loops, measuring task complexity, or correlating turn count with user satisfaction and cost. Without turn depth metrics, engineers cannot identify sessions where the agent got stuck in a clarification loop, failed to resolve a request within a reasonable number of turns, or produced unexpectedly long conversations that consumed excessive tokens. Implement conversation turn depth tracking with anomaly detection and per-turn cost attribution."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-conversation-turn-depth-tracking
tags: [turn-depth, conversation-tracking, loop-detection, multi-turn, session-analytics, turn-cost]
symptoms:
  - "No record of how many turns a session took before the user abandoned or the task completed"
  - "Agent enters clarification loops that consume 20+ turns before timing out — no alert fires"
  - "Cannot correlate session turn count with user satisfaction or task completion rate"
  - "Token cost per session is unknown because turns are not individually attributed"
  - "No maximum turn limit enforced — loops run until context window is exhausted"
---

## Why This Happens

Turn count is typically implicit: the conversation history list grows with each exchange, but the agent does not record the count as a metric or check it against a threshold. Without explicit tracking, there is no signal for a 30-turn session that indicates the agent is stuck versus a legitimately complex 30-turn research task. Turn depth tracking adds a counter to each session, records cost and latency per turn, checks the count against configured limits, and emits structured events so dashboards can show turn-count distributions, identify anomalous sessions, and correlate depth with outcomes.

## Solution 1: Turn Record

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class TurnRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class TurnRecord:
    session_id: str
    turn_number: int           # 1-indexed, increments on each user message
    role: TurnRole
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls_made: int = 0
    cost_usd: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def latency_ms(self) -> Optional[float]:
        if self.ended_at is None:
            return None
        return round((self.ended_at - self.started_at) * 1000, 2)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens
```

## Solution 2: Turn Depth Tracker

```python
import time
from typing import Dict, List, Optional


class TurnDepthTracker:
    """
    Tracks turn count and per-turn records for a single session.
    Enforces an optional max turn limit and fires a callback when exceeded.
    """

    def __init__(
        self,
        session_id: str,
        max_turns: Optional[int] = 50,
        on_limit_exceeded=None,
    ):
        self._session_id = session_id
        self._max_turns = max_turns
        self._on_limit = on_limit_exceeded
        self._turns: List[TurnRecord] = []
        self._current_turn: Optional[TurnRecord] = None
        self._started_at = time.time()

    @property
    def turn_count(self) -> int:
        return len([t for t in self._turns if t.role == TurnRole.USER])

    def begin_turn(self, role: TurnRole = TurnRole.USER) -> TurnRecord:
        turn_number = self.turn_count + (1 if role == TurnRole.USER else 0)
        turn = TurnRecord(
            session_id=self._session_id,
            turn_number=turn_number,
            role=role,
        )
        self._current_turn = turn
        self._turns.append(turn)

        if role == TurnRole.USER and self._max_turns and turn_number > self._max_turns:
            if self._on_limit:
                self._on_limit(self._session_id, turn_number, self._max_turns)

        return turn

    def end_turn(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        tool_calls_made: int = 0,
        cost_usd: float = 0.0,
    ) -> Optional[TurnRecord]:
        if self._current_turn is None:
            return None
        self._current_turn.ended_at = time.time()
        self._current_turn.input_tokens = input_tokens
        self._current_turn.output_tokens = output_tokens
        self._current_turn.tool_calls_made = tool_calls_made
        self._current_turn.cost_usd = cost_usd
        completed = self._current_turn
        self._current_turn = None
        return completed

    def is_limit_exceeded(self) -> bool:
        return self._max_turns is not None and self.turn_count > self._max_turns

    def summary(self) -> dict:
        user_turns = [t for t in self._turns if t.role == TurnRole.USER]
        latencies = [t.latency_ms for t in user_turns if t.latency_ms is not None]
        total_cost = sum(t.cost_usd for t in self._turns)
        total_tokens = sum(t.total_tokens for t in self._turns)
        return {
            "session_id": self._session_id,
            "turn_count": self.turn_count,
            "total_tokens": total_tokens,
            "total_cost_usd": round(total_cost, 6),
            "avg_turn_latency_ms": round(sum(latencies) / max(len(latencies), 1), 2) if latencies else None,
            "max_turn_latency_ms": round(max(latencies), 2) if latencies else None,
            "total_tool_calls": sum(t.tool_calls_made for t in self._turns),
            "session_duration_ms": round((time.time() - self._started_at) * 1000, 2),
            "limit_exceeded": self.is_limit_exceeded(),
        }
```

## Solution 3: Turn Depth Anomaly Detector

```python
from typing import List, Optional


class TurnDepthAnomalyDetector:
    """
    Detects sessions with abnormally high turn counts compared to
    a rolling baseline. Flags potential clarification loops or stuck agents.
    """

    def __init__(
        self,
        warning_threshold: int = 15,
        critical_threshold: int = 30,
        clarification_loop_pattern: int = 5,   # N consecutive turns with no tool calls
    ):
        self._warning = warning_threshold
        self._critical = critical_threshold
        self._loop_pattern = clarification_loop_pattern

    def analyze(self, tracker: TurnDepthTracker) -> dict:
        turn_count = tracker.turn_count
        user_turns = [t for t in tracker._turns if t.role == TurnRole.USER]

        # Detect clarification loop: many consecutive turns with 0 tool calls
        no_tool_streak = 0
        max_no_tool_streak = 0
        for turn in user_turns:
            if turn.tool_calls_made == 0:
                no_tool_streak += 1
                max_no_tool_streak = max(max_no_tool_streak, no_tool_streak)
            else:
                no_tool_streak = 0

        anomalies = []
        if turn_count >= self._critical:
            anomalies.append(f"critical_turn_depth: {turn_count} turns (threshold {self._critical})")
        elif turn_count >= self._warning:
            anomalies.append(f"high_turn_depth: {turn_count} turns (threshold {self._warning})")

        if max_no_tool_streak >= self._loop_pattern:
            anomalies.append(f"clarification_loop: {max_no_tool_streak} consecutive turns without tool calls")

        return {
            "session_id": tracker._session_id,
            "turn_count": turn_count,
            "anomalies": anomalies,
            "max_no_tool_streak": max_no_tool_streak,
            "severity": "critical" if any("critical" in a for a in anomalies)
                        else "warning" if anomalies else "ok",
        }
```

## Solution 4: Cross-Session Turn Depth Store

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, List, Optional, Tuple


class CrossSessionTurnDepthStore:
    """
    Accumulates per-session turn summaries for aggregate analysis:
    turn count distributions, cost-per-turn averages, and anomaly rates.
    """

    def __init__(self, max_sessions: int = 5000):
        self._max = max_sessions
        self._records: Deque[Tuple[float, dict]] = deque()
        self._lock = Lock()

    def record(self, summary: dict) -> None:
        with self._lock:
            self._records.append((time.time(), summary))
            if len(self._records) > self._max:
                self._records.popleft()

    def aggregate(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [s for ts, s in self._records if ts >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "sessions": 0}

        turn_counts = [s["turn_count"] for s in recent]
        costs = [s["total_cost_usd"] for s in recent]
        exceeded = sum(1 for s in recent if s.get("limit_exceeded"))

        return {
            "window_seconds": window_seconds,
            "sessions": len(recent),
            "turn_count_p50": sorted(turn_counts)[len(turn_counts) // 2],
            "turn_count_p95": sorted(turn_counts)[int(len(turn_counts) * 0.95)],
            "turn_count_max": max(turn_counts),
            "avg_cost_usd": round(sum(costs) / max(len(costs), 1), 6),
            "limit_exceeded_sessions": exceeded,
            "limit_exceeded_rate": round(exceeded / max(len(recent), 1), 4),
        }
```

## Solution 5: Turn Cost Attributor

```python
from dataclasses import dataclass
from typing import List


@dataclass
class TurnCostBreakdown:
    session_id: str
    turn_number: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    tool_calls: int
    cost_per_tool_call: float


class TurnCostAttributor:
    """
    Breaks down total session cost into per-turn attribution for
    identifying which turns drove the most spend.
    """

    def attribute(self, tracker: TurnDepthTracker) -> List[TurnCostBreakdown]:
        breakdowns = []
        for turn in tracker._turns:
            if turn.role != TurnRole.USER:
                continue
            breakdowns.append(TurnCostBreakdown(
                session_id=turn.session_id,
                turn_number=turn.turn_number,
                input_tokens=turn.input_tokens,
                output_tokens=turn.output_tokens,
                cost_usd=round(turn.cost_usd, 6),
                tool_calls=turn.tool_calls_made,
                cost_per_tool_call=round(
                    turn.cost_usd / max(turn.tool_calls_made, 1), 6
                ),
            ))
        return sorted(breakdowns, key=lambda b: b.cost_usd, reverse=True)
```

## Solution 6: Turn Depth Dashboard

```python
import time


class ConversationTurnDepthDashboard:
    """
    Combines aggregate turn depth metrics, anomaly detection results,
    and cost attribution into a single operational report.
    """

    def __init__(
        self,
        store: CrossSessionTurnDepthStore,
        detector: TurnDepthAnomalyDetector,
    ):
        self._store = store
        self._detector = detector

    def render(self, window_seconds: float = 3600.0) -> dict:
        aggregate = self._store.aggregate(window_seconds)
        return {
            "generated_at": time.time(),
            "aggregate": aggregate,
            "thresholds": {
                "warning_turns": self._detector._warning,
                "critical_turns": self._detector._critical,
                "loop_detection_streak": self._detector._loop_pattern,
            },
        }
```

## Comparison

| Approach | Per-Turn Recording | Max Turn Enforcement | Loop Detection | Cross-Session Aggregate | Cost Attribution |
|---|---|---|---|---|---|
| TurnDepthTracker | Yes | Yes (callback) | No | No | Partial (per-turn cost) |
| TurnDepthAnomalyDetector | No | No | Yes (streak) | No | No |
| CrossSessionTurnDepthStore | No | No | No | Yes (P50/P95) | No |
| TurnCostAttributor | No | No | No | No | Yes |
| ConversationTurnDepthDashboard | No | No | No | Via store | No |

**Best for production**: Set `max_turns=50` as the hard limit for general-purpose agents — legitimate complex tasks rarely exceed this; runaway loops always do. Alert when `limit_exceeded_rate` exceeds 0.05 (5% of sessions) — this indicates a systemic prompt or tool issue causing the agent to loop rather than resolve. Track `turn_count_p95` as a UX proxy: if it increases week-over-week without a corresponding increase in task complexity, the agent is becoming less efficient at resolving requests. Use `TurnCostAttributor` to identify which turn numbers are most expensive — turns 1-3 typically dominate due to large context setup, while later turns reveal whether tool calls are multiplying unnecessarily.
