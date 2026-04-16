---
title: "Agent Doesn't Implement Conversation Turn Depth Tracking"
description: "Agents that do not track how many turns a conversation has consumed before reaching a conclusion have no visibility into efficiency: a task that requires 20 turns to complete is indistinguishable from one that requires 3, and both are counted equally in request metrics. Implement conversation turn depth tracking that counts user and agent turns, identifies high-depth sessions, correlates turn depth with goal completion and token cost, and alerts when sessions are trending toward excessive depth."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-conversation-turn-depth-tracking
tags: [turn-depth, conversation-efficiency, session-analytics, turn-count, multi-turn, depth-tracking]
symptoms:
  - "No metric exists for how many turns a task requires — only total sessions are counted"
  - "Some sessions consume 10× the tokens of others with no indicator of depth difference"
  - "Cannot identify which task types require the most back-and-forth before completion"
  - "No alert when a session is trending toward excessive turn depth without progress"
  - "Turn count data is unavailable for correlating with user satisfaction or cost"
---

## Why This Happens

Request-level metrics capture individual turns in isolation. Session-level metrics capture overall success or failure. Neither captures depth: how many exchanges were required between a user request and a satisfying conclusion. Turn depth is the primary driver of cost efficiency — a task that requires 15 turns costs 5× more than one that requires 3, and if both end in success the difference is invisible to request metrics. Tracking turn depth makes inefficiency observable: high-depth sessions identify poorly scoped prompts, tool loops, or task categories where the agent lacks sufficient context to answer in fewer exchanges.

## Solution 1: Turn Record

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class TurnRole(str, Enum):
    USER = "user"
    AGENT = "agent"
    TOOL = "tool"
    SYSTEM = "system"


@dataclass
class TurnRecord:
    turn_index: int
    role: TurnRole
    recorded_at: float = field(default_factory=time.time)
    token_count: Optional[int] = None
    tool_calls_made: int = 0
    latency_ms: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
```

## Solution 2: Turn Depth Tracker

```python
import threading
import time
from typing import List, Optional


class TurnDepthTracker:
    """
    Tracks the sequence of turns within a single session.
    Provides turn counts by role and depth statistics.
    """

    def __init__(self, session_id: str):
        self._session_id = session_id
        self._turns: List[TurnRecord] = []
        self._lock = threading.Lock()
        self._started_at = time.time()

    def record_turn(
        self,
        role: TurnRole,
        token_count: Optional[int] = None,
        tool_calls_made: int = 0,
        latency_ms: Optional[float] = None,
        metadata: Optional[dict] = None,
    ) -> TurnRecord:
        with self._lock:
            turn = TurnRecord(
                turn_index=len(self._turns),
                role=role,
                token_count=token_count,
                tool_calls_made=tool_calls_made,
                latency_ms=latency_ms,
                metadata=metadata or {},
            )
            self._turns.append(turn)
            return turn

    def total_turns(self) -> int:
        with self._lock:
            return len(self._turns)

    def user_turns(self) -> int:
        with self._lock:
            return sum(1 for t in self._turns if t.role == TurnRole.USER)

    def agent_turns(self) -> int:
        with self._lock:
            return sum(1 for t in self._turns if t.role == TurnRole.AGENT)

    def total_tokens(self) -> int:
        with self._lock:
            return sum(t.token_count or 0 for t in self._turns)

    def total_tool_calls(self) -> int:
        with self._lock:
            return sum(t.tool_calls_made for t in self._turns)

    def session_duration_seconds(self) -> float:
        return round(time.time() - self._started_at, 2)

    def snapshot(self) -> dict:
        return {
            "session_id": self._session_id,
            "total_turns": self.total_turns(),
            "user_turns": self.user_turns(),
            "agent_turns": self.agent_turns(),
            "total_tokens": self.total_tokens(),
            "total_tool_calls": self.total_tool_calls(),
            "session_duration_seconds": self.session_duration_seconds(),
        }
```

## Solution 3: Excessive Depth Alert

```python
from dataclasses import dataclass
from typing import Optional


@dataclass
class DepthAlertConfig:
    warn_threshold: int = 10      # turns before warning
    critical_threshold: int = 20  # turns before critical alert
    stall_detection_turns: int = 5  # no progress in N turns = stall


class ExcessiveDepthAlerter:
    """
    Evaluates current session depth against thresholds and detects
    stalled sessions (many turns without goal progress).
    """

    def __init__(self, config: DepthAlertConfig = None):
        self._config = config or DepthAlertConfig()

    def check(
        self,
        tracker: TurnDepthTracker,
        goal_completed: bool = False,
    ) -> dict:
        depth = tracker.total_turns()
        user_turns = tracker.user_turns()

        if goal_completed:
            severity = "none"
            message = "goal completed"
        elif depth >= self._config.critical_threshold:
            severity = "critical"
            message = f"session depth {depth} exceeds critical threshold {self._config.critical_threshold}"
        elif depth >= self._config.warn_threshold:
            severity = "warning"
            message = f"session depth {depth} exceeds warn threshold {self._config.warn_threshold}"
        else:
            severity = "none"
            message = "depth within bounds"

        return {
            "session_id": tracker._session_id,
            "current_depth": depth,
            "user_turns": user_turns,
            "severity": severity,
            "message": message,
            "goal_completed": goal_completed,
        }
```

## Solution 4: Turn Depth Metrics Recorder

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, List, Optional, Tuple


class TurnDepthMetricsRecorder:
    """
    Accumulates completed session depth observations.
    Supports percentile queries and category-level breakdowns.
    """

    def __init__(self, max_records: int = 20000):
        self._max = max_records
        self._records: Deque[Tuple[float, dict]] = deque()
        self._lock = Lock()

    def record(self, snapshot: dict, category: str = "general") -> None:
        entry = {**snapshot, "category": category, "recorded_at": time.time()}
        with self._lock:
            self._records.append((time.time(), entry))
            if len(self._records) > self._max:
                self._records.popleft()

    def percentile(
        self,
        field: str,
        pct: float,
        window_seconds: float = 3600.0,
        category: Optional[str] = None,
    ) -> Optional[float]:
        cutoff = time.time() - window_seconds
        with self._lock:
            values = sorted(
                r[field] for _, r in self._records
                if r["recorded_at"] >= cutoff
                and (category is None or r.get("category") == category)
                and field in r and r[field] is not None
            )
        if not values:
            return None
        idx = min(int(len(values) * pct / 100.0), len(values) - 1)
        return round(values[idx], 2)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for _, r in self._records if r["recorded_at"] >= cutoff]

        if not recent:
            return {"window_seconds": window_seconds, "sessions": 0}

        by_cat: Dict[str, List[int]] = {}
        for r in recent:
            cat = r.get("category", "general")
            by_cat.setdefault(cat, []).append(r.get("total_turns", 0))

        return {
            "window_seconds": window_seconds,
            "sessions": len(recent),
            "p50_turns": self.percentile("total_turns", 50, window_seconds),
            "p95_turns": self.percentile("total_turns", 95, window_seconds),
            "p99_turns": self.percentile("total_turns", 99, window_seconds),
            "avg_tokens": round(
                sum(r.get("total_tokens", 0) for r in recent) / len(recent), 1
            ),
            "by_category": {
                cat: {
                    "sessions": len(depths),
                    "avg_turns": round(sum(depths) / len(depths), 2),
                    "max_turns": max(depths),
                }
                for cat, depths in by_cat.items()
            },
        }
```

## Solution 5: Turn Efficiency Analyzer

```python
from typing import List, Optional


class TurnEfficiencyAnalyzer:
    """
    Correlates turn depth with goal completion and token cost to
    identify which session characteristics predict low efficiency.
    """

    def __init__(self, recorder: TurnDepthMetricsRecorder):
        self._recorder = recorder

    def efficiency_report(self, window_seconds: float = 86400.0) -> dict:
        import time
        cutoff = time.time() - window_seconds
        with self._recorder._lock:
            recent = [r for _, r in self._recorder._records if r["recorded_at"] >= cutoff]

        if not recent:
            return {"window_seconds": window_seconds, "sessions": 0}

        completed = [r for r in recent if r.get("goal_completed", False)]
        not_completed = [r for r in recent if not r.get("goal_completed", False)]

        def avg_turns(records):
            if not records:
                return None
            return round(sum(r.get("total_turns", 0) for r in records) / len(records), 2)

        high_depth = [r for r in recent if r.get("total_turns", 0) >= 15]

        return {
            "window_seconds": window_seconds,
            "total_sessions": len(recent),
            "avg_turns_completed": avg_turns(completed),
            "avg_turns_not_completed": avg_turns(not_completed),
            "high_depth_sessions": len(high_depth),
            "high_depth_completion_rate": round(
                sum(1 for r in high_depth if r.get("goal_completed", False)) / max(len(high_depth), 1),
                4,
            ),
        }
```

## Solution 6: Turn Depth Dashboard

```python
import time


class TurnDepthDashboard:
    """
    Combines live session depth, historical percentiles, and efficiency
    analysis into a single observability report.
    """

    def __init__(
        self,
        recorder: TurnDepthMetricsRecorder,
        alerter: ExcessiveDepthAlerter,
        analyzer: TurnEfficiencyAnalyzer,
    ):
        self._recorder = recorder
        self._alerter = alerter
        self._analyzer = analyzer

    def render(self, active_trackers: list = None) -> dict:
        active_alerts = []
        if active_trackers:
            for tracker in active_trackers:
                alert = self._alerter.check(tracker)
                if alert["severity"] != "none":
                    active_alerts.append(alert)

        return {
            "generated_at": time.time(),
            "historical": self._recorder.summary(window_seconds=3600.0),
            "efficiency": self._analyzer.efficiency_report(window_seconds=86400.0),
            "active_depth_alerts": active_alerts,
        }
```

## Comparison

| Approach | Per-Turn Recording | Depth Thresholds | Historical Percentiles | Category Breakdown | Efficiency Correlation |
|---|---|---|---|---|---|
| TurnDepthTracker | Yes (per role) | No | No | No | No |
| ExcessiveDepthAlerter | No | Yes (warn/critical) | No | No | No |
| TurnDepthMetricsRecorder | No | No | Yes (P50/P95/P99) | Yes | No |
| TurnEfficiencyAnalyzer | No | No | Via recorder | No | Yes |
| TurnDepthDashboard | No | Via alerter | Via recorder | Via recorder | Via analyzer |

**Best for production**: Record turn depth per session and segment by goal category — this surfaces which task types require the most back-and-forth and are candidates for prompt or context improvements. Alert when a live session crosses `warn_threshold=10` user turns without a goal completion signal: at this depth, the agent is likely in a loop or the user's request is underdefined. Track `avg_turns_completed` vs `avg_turns_not_completed` in `TurnEfficiencyAnalyzer` — a large gap means high-depth sessions tend to fail, which indicates the agent should escalate or ask clarifying questions earlier rather than making additional attempts.
