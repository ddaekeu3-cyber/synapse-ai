---
title: "Agent Doesn't Implement Multi-Turn Conversation Depth Tracking"
description: "Agents that count requests without tracking conversation depth cannot detect sessions that are unusually long, indicate user frustration through repeated clarification loops, or signal a stuck agent that keeps asking instead of answering. Implement multi-turn conversation depth tracking that records turn counts, identifies depth anomalies, and surfaces sessions where the agent is failing to converge."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-multi-turn-conversation-depth-tracking
tags: [conversation-depth, turn-tracking, user-frustration, convergence, multi-turn, session-health]
symptoms:
  - "Agent enters a clarification loop — 15 turns asking for more information without answering"
  - "No alert when a session reaches 30 turns, which almost always indicates a stuck agent"
  - "Cannot distinguish a legitimate 20-turn research session from a frustrated clarification loop"
  - "Turn count not tracked — only request count, which conflates tool calls with user turns"
  - "User abandons a session after 25 turns with no answer — invisible in any metric"
---

## Why This Happens

Request counts include tool calls, system retries, and internal agent steps — they are not the same as user-facing conversation turns. Conversation depth is the number of user-message/agent-response pairs in a session. Without tracking it separately, there is no way to detect sessions that are unusually deep, identify whether depth correlates with resolution or abandonment, or alert when the agent is in a loop. Turn-count tracking requires incrementing a per-session counter on each user message, recording the distribution across sessions, and detecting depth anomalies against a baseline.

## Solution 1: Conversation Turn Record

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class TurnOutcome(str, Enum):
    ANSWERED = "answered"          # agent provided a final answer
    CLARIFICATION = "clarification"  # agent asked for more info
    TOOL_CALL = "tool_call"        # agent made a tool call
    ERROR = "error"                # agent returned an error
    UNKNOWN = "unknown"


@dataclass
class ConversationTurnRecord:
    session_id: str
    turn_number: int
    user_message_length: int
    agent_response_length: int
    outcome: TurnOutcome
    timestamp: float = field(default_factory=time.time)
    latency_ms: float = 0.0
    tool_calls_made: int = 0
```

## Solution 2: Per-Session Turn Depth Tracker

```python
import time
from collections import defaultdict
from threading import Lock
from typing import Dict, List, Optional


class PerSessionTurnDepthTracker:
    """
    Tracks conversation turn depth per session.
    Provides per-session depth queries and fleet-wide distribution.
    """

    def __init__(self, max_sessions: int = 100000):
        self._sessions: Dict[str, List[ConversationTurnRecord]] = {}
        self._lock = Lock()
        self._max_sessions = max_sessions

    def record_turn(self, record: ConversationTurnRecord) -> None:
        with self._lock:
            if record.session_id not in self._sessions:
                if len(self._sessions) >= self._max_sessions:
                    oldest = next(iter(self._sessions))
                    del self._sessions[oldest]
                self._sessions[record.session_id] = []
            self._sessions[record.session_id].append(record)

    def depth(self, session_id: str) -> int:
        with self._lock:
            return len(self._sessions.get(session_id, []))

    def session_summary(self, session_id: str) -> dict:
        with self._lock:
            turns = self._sessions.get(session_id, [])
        if not turns:
            return {"session_id": session_id, "depth": 0}
        outcomes = [t.outcome.value for t in turns]
        clarifications = sum(1 for o in outcomes if o == "clarification")
        return {
            "session_id": session_id,
            "depth": len(turns),
            "clarification_turns": clarifications,
            "clarification_rate": round(clarifications / len(turns), 3),
            "started_at": turns[0].timestamp,
            "last_turn_at": turns[-1].timestamp,
            "duration_seconds": round(turns[-1].timestamp - turns[0].timestamp, 1),
            "outcome_distribution": {o: outcomes.count(o) for o in set(outcomes)},
        }

    def all_depths(self) -> List[int]:
        with self._lock:
            return [len(turns) for turns in self._sessions.values()]
```

## Solution 3: Conversation Depth Anomaly Detector

```python
import math
import time
from threading import Lock
from typing import List, Optional


class ConversationDepthAnomalyDetector:
    """
    Flags sessions with unusually high turn counts or high clarification rates.
    Uses a rolling fleet baseline for anomaly detection.
    """

    def __init__(
        self,
        tracker: PerSessionTurnDepthTracker,
        max_turns_absolute: int = 30,         # hard cap: always flag above this
        clarification_rate_threshold: float = 0.5,  # 50% clarification turns = stuck
        depth_zscore_threshold: float = 2.5,
        min_fleet_sessions: int = 20,
    ):
        self._tracker = tracker
        self._max_turns = max_turns_absolute
        self._clarification_threshold = clarification_rate_threshold
        self._zscore_threshold = depth_zscore_threshold
        self._min_fleet = min_fleet_sessions

    def _fleet_stats(self) -> tuple:
        depths = self._tracker.all_depths()
        if len(depths) < self._min_fleet:
            return None, None
        mean = sum(depths) / len(depths)
        variance = sum((d - mean) ** 2 for d in depths) / len(depths)
        std = math.sqrt(variance)
        return mean, std

    def check(self, session_id: str) -> dict:
        depth = self._tracker.depth(session_id)
        summary = self._tracker.session_summary(session_id)
        anomalies = []

        # Hard cap
        if depth >= self._max_turns:
            anomalies.append({
                "type": "max_turns_exceeded",
                "depth": depth,
                "threshold": self._max_turns,
            })

        # Clarification loop
        clar_rate = summary.get("clarification_rate", 0.0)
        if clar_rate >= self._clarification_threshold and depth >= 4:
            anomalies.append({
                "type": "clarification_loop",
                "clarification_rate": clar_rate,
                "threshold": self._clarification_threshold,
            })

        # Fleet z-score
        mean, std = self._fleet_stats()
        if mean is not None and std and std > 0:
            zscore = (depth - mean) / std
            if zscore >= self._zscore_threshold:
                anomalies.append({
                    "type": "depth_outlier",
                    "zscore": round(zscore, 2),
                    "depth": depth,
                    "fleet_mean": round(mean, 1),
                    "threshold_zscore": self._zscore_threshold,
                })

        return {
            "session_id": session_id,
            "depth": depth,
            "anomalous": len(anomalies) > 0,
            "anomalies": anomalies,
        }
```

## Solution 4: Depth Distribution Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, List, Optional, Tuple


class ConversationDepthDistributionTracker:
    """
    Records final conversation depths when sessions end.
    Supports fleet-wide percentile queries.
    """

    def __init__(self, max_records: int = 50000):
        self._records: Deque[Tuple[float, int, str]] = deque()
        # (ended_at, final_depth, outcome_type)
        self._max = max_records
        self._lock = Lock()

    def record_session_end(self, session_id: str, final_depth: int, outcome: str = "") -> None:
        with self._lock:
            self._records.append((time.time(), final_depth, outcome))
            if len(self._records) > self._max:
                self._records.popleft()

    def percentile(self, pct: float, window_seconds: float = 86400.0) -> Optional[float]:
        cutoff = time.time() - window_seconds
        with self._lock:
            values = sorted(d for ts, d, _ in self._records if ts >= cutoff)
        if not values:
            return None
        idx = min(int(len(values) * pct / 100.0), len(values) - 1)
        return float(values[idx])

    def summary(self, window_seconds: float = 86400.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [(ts, d, o) for ts, d, o in self._records if ts >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "sessions": 0}
        depths = [d for _, d, _ in recent]
        return {
            "window_seconds": window_seconds,
            "sessions": len(recent),
            "mean_depth": round(sum(depths) / len(depths), 2),
            "p50_depth": self.percentile(50, window_seconds),
            "p90_depth": self.percentile(90, window_seconds),
            "p99_depth": self.percentile(99, window_seconds),
            "max_depth": max(depths),
        }
```

## Solution 5: Stuck Session Alerter

```python
import time
from typing import Callable, Dict, List, Optional


class StuckSessionAlerter:
    """
    Periodically checks active sessions for depth anomalies and fires alerts
    for sessions that appear stuck in a clarification loop.
    """

    def __init__(
        self,
        anomaly_detector: ConversationDepthAnomalyDetector,
        alert_fn: Optional[Callable[[dict], None]] = None,
        cooldown_seconds: float = 300.0,
    ):
        self._detector = anomaly_detector
        self._alert_fn = alert_fn
        self._cooldown = cooldown_seconds
        self._last_alerted: Dict[str, float] = {}
        self._alerted: List[dict] = []

    def check_sessions(self, session_ids: List[str]) -> List[dict]:
        now = time.time()
        alerts = []
        for sid in session_ids:
            result = self._detector.check(sid)
            if not result.get("anomalous"):
                continue
            last = self._last_alerted.get(sid, 0.0)
            if now - last < self._cooldown:
                continue
            self._last_alerted[sid] = now
            self._alerted.append(result)
            alerts.append(result)
            if self._alert_fn:
                self._alert_fn(result)
        return alerts
```

## Solution 6: Conversation Depth Dashboard

```python
import time


class ConversationDepthDashboard:
    """
    Renders fleet-wide conversation depth distribution and active anomalies.
    """

    def __init__(
        self,
        tracker: PerSessionTurnDepthTracker,
        distribution: ConversationDepthDistributionTracker,
        alerter: StuckSessionAlerter,
    ):
        self._tracker = tracker
        self._distribution = distribution
        self._alerter = alerter

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "active_sessions": len(self._tracker._sessions),
            "depth_distribution_24h": self._distribution.summary(86400.0),
            "recent_stuck_alerts": self._alerter._alerted[-10:],
        }
```

## Comparison

| Approach | Per-Session Depth | Clarification Detection | Fleet Baseline | Session-End Distribution | Alert on Stuck |
|---|---|---|---|---|---|
| PerSessionTurnDepthTracker | Yes | Via outcome | No | No | No |
| ConversationDepthAnomalyDetector | Via tracker | Yes (rate threshold) | Yes (z-score) | No | No |
| ConversationDepthDistributionTracker | No | No | No | Yes (percentiles) | No |
| StuckSessionAlerter | Via detector | Via detector | Via detector | No | Yes |
| ConversationDepthDashboard | No | No | No | No | Yes |

**Best for production**: Set `max_turns_absolute=30` as the hard alert threshold — genuine research sessions rarely exceed 20 turns, and sessions above 30 almost always indicate a stuck agent or an automated script. Track `clarification_rate` separately from turn count: a 6-turn session with 5 clarifications is more concerning than a 20-turn research session with 0. Alert the on-call on sessions with `clarification_rate > 0.5` and `depth >= 6` — these are active user-frustration signals. Record final depth in `ConversationDepthDistributionTracker` when sessions end (via timeout or user close) to build the fleet baseline for z-score anomaly detection.
