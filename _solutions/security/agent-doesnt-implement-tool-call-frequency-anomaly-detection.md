---
title: "Agent Doesn't Implement Tool Call Frequency Anomaly Detection"
description: "Agents that place no bounds on tool call frequency within a session are vulnerable to runaway loops and adversarial amplification: a prompt injection that causes the agent to repeatedly call an external API, a bug that triggers a tool in a tight loop, or an attacker attempting to exhaust API quotas through a single session. Implement tool call frequency anomaly detection that tracks per-tool and per-session call rates and blocks execution when rates exceed configurable baselines."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-tool-call-frequency-anomaly-detection
tags: [tool-call-frequency, anomaly-detection, runaway-loop, rate-abuse, session-limits, call-amplification]
symptoms:
  - "Prompt injection causes a tool to be called 200 times in a single session"
  - "Bug in agent loop triggers the same tool repeatedly with no termination condition"
  - "Single session exhausts third-party API quota for the entire application"
  - "No per-tool call count limit within a session or time window"
  - "Tool call storm invisible until external API returns 429 or quota alert fires"
---

## Why This Happens

Tool call frequency limits are rarely designed into agents because the normal case is well-behaved: a task calls each tool a handful of times. Edge cases — prompt injections, reasoning loops, adversarial inputs — can cause unbounded repetition. The agent has no natural termination signal for a loop, so it continues until an external limit (token budget, API quota, timeout) stops it — usually after significant damage. Frequency anomaly detection requires a per-session, per-tool counter with a sliding window and a configurable ceiling that blocks further calls once exceeded.

## Solution 1: Tool Call Frequency Counter

```python
import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Deque, Dict, Tuple


class ToolCallFrequencyCounter:
    """
    Tracks tool call counts per session and per tool within a sliding window.
    Provides rate and count queries for anomaly evaluation.
    """

    def __init__(self, window_seconds: float = 60.0):
        self._window = window_seconds
        # key: (session_id, tool_name) -> deque of timestamps
        self._calls: Dict[Tuple[str, str], Deque[float]] = {}
        self._lock = Lock()

    def record(self, session_id: str, tool_name: str) -> None:
        key = (session_id, tool_name)
        with self._lock:
            if key not in self._calls:
                self._calls[key] = deque()
            self._calls[key].append(time.time())

    def count_in_window(self, session_id: str, tool_name: str) -> int:
        key = (session_id, tool_name)
        cutoff = time.time() - self._window
        with self._lock:
            ts_list = self._calls.get(key, deque())
            return sum(1 for ts in ts_list if ts >= cutoff)

    def session_total(self, session_id: str) -> int:
        cutoff = time.time() - self._window
        with self._lock:
            return sum(
                sum(1 for ts in ts_list if ts >= cutoff)
                for (sid, _), ts_list in self._calls.items()
                if sid == session_id
            )

    def rate_per_minute(self, session_id: str, tool_name: str) -> float:
        count = self.count_in_window(session_id, tool_name)
        return round(count / (self._window / 60.0), 2)
```

## Solution 2: Frequency Anomaly Policy

```python
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class FrequencyAnomalyPolicy:
    # Per-tool limits within the sliding window
    default_tool_max_calls: int = 20
    per_tool_max_calls: Dict[str, int] = field(default_factory=dict)

    # Session-level aggregate limit
    session_max_calls_per_window: int = 100

    # Burst detection: calls within a very short interval
    burst_window_seconds: float = 5.0
    burst_max_calls: int = 10

    def tool_limit(self, tool_name: str) -> int:
        return self.per_tool_max_calls.get(tool_name, self.default_tool_max_calls)
```

## Solution 3: Burst Detector

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, Tuple


class BurstDetector:
    """
    Detects short-interval call bursts that indicate a runaway loop
    rather than legitimate parallel tool usage.
    """

    def __init__(self, window_seconds: float = 5.0, max_calls: int = 10):
        self._window = window_seconds
        self._max = max_calls
        self._calls: Dict[Tuple[str, str], Deque[float]] = {}
        self._lock = Lock()

    def record_and_check(self, session_id: str, tool_name: str) -> bool:
        """Returns True if a burst is detected."""
        key = (session_id, tool_name)
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            if key not in self._calls:
                self._calls[key] = deque()
            dq = self._calls[key]
            dq.append(now)
            # Prune old entries
            while dq and dq[0] < cutoff:
                dq.popleft()
            return len(dq) > self._max
```

## Solution 4: Frequency Anomaly Detector

```python
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class AnomalyType(str, Enum):
    TOOL_RATE_EXCEEDED = "tool_rate_exceeded"
    SESSION_RATE_EXCEEDED = "session_rate_exceeded"
    BURST_DETECTED = "burst_detected"


@dataclass
class FrequencyAnomalyVerdict:
    allowed: bool
    anomaly_type: Optional[AnomalyType]
    tool_name: str
    session_id: str
    current_count: int
    limit: int
    message: str


class ToolCallFrequencyAnomalyDetector:
    """
    Combines per-tool rate counting, session aggregate counting, and
    burst detection into a single allow/block decision.
    """

    def __init__(
        self,
        counter: ToolCallFrequencyCounter,
        burst_detector: BurstDetector,
        policy: FrequencyAnomalyPolicy,
    ):
        self._counter = counter
        self._burst = burst_detector
        self._policy = policy

    def evaluate(self, session_id: str, tool_name: str) -> FrequencyAnomalyVerdict:
        # Burst check first (fastest signal)
        if self._burst.record_and_check(session_id, tool_name):
            return FrequencyAnomalyVerdict(
                allowed=False,
                anomaly_type=AnomalyType.BURST_DETECTED,
                tool_name=tool_name,
                session_id=session_id,
                current_count=self._policy.burst_max_calls,
                limit=self._policy.burst_max_calls,
                message=f"burst: >{self._policy.burst_max_calls} calls in {self._policy.burst_window_seconds}s",
            )

        self._counter.record(session_id, tool_name)

        # Per-tool rate
        tool_count = self._counter.count_in_window(session_id, tool_name)
        tool_limit = self._policy.tool_limit(tool_name)
        if tool_count > tool_limit:
            return FrequencyAnomalyVerdict(
                allowed=False,
                anomaly_type=AnomalyType.TOOL_RATE_EXCEEDED,
                tool_name=tool_name,
                session_id=session_id,
                current_count=tool_count,
                limit=tool_limit,
                message=f"{tool_name} called {tool_count}× (limit {tool_limit}) in window",
            )

        # Session aggregate
        session_total = self._counter.session_total(session_id)
        session_limit = self._policy.session_max_calls_per_window
        if session_total > session_limit:
            return FrequencyAnomalyVerdict(
                allowed=False,
                anomaly_type=AnomalyType.SESSION_RATE_EXCEEDED,
                tool_name=tool_name,
                session_id=session_id,
                current_count=session_total,
                limit=session_limit,
                message=f"session total {session_total} exceeds {session_limit}",
            )

        return FrequencyAnomalyVerdict(
            allowed=True,
            anomaly_type=None,
            tool_name=tool_name,
            session_id=session_id,
            current_count=tool_count,
            limit=tool_limit,
            message="ok",
        )
```

## Solution 5: Frequency-Gated Tool Dispatcher

```python
import time
from typing import Any, Callable, Optional


class ToolCallFrequencyExceededError(Exception):
    def __init__(self, verdict: FrequencyAnomalyVerdict):
        super().__init__(verdict.message)
        self.verdict = verdict


class FrequencyGatedToolDispatcher:
    """
    Wraps tool dispatch with frequency anomaly detection.
    Raises ToolCallFrequencyExceededError when limits are breached.
    """

    def __init__(
        self,
        detector: ToolCallFrequencyAnomalyDetector,
        audit_fn: Optional[Callable[[dict], None]] = None,
    ):
        self._detector = detector
        self._audit = audit_fn or (lambda _: None)
        self._blocked_count = 0

    async def dispatch(
        self,
        session_id: str,
        tool_name: str,
        fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        verdict = self._detector.evaluate(session_id, tool_name)

        if not verdict.allowed:
            self._blocked_count += 1
            self._audit({
                "ts": time.time(),
                "session_id": session_id,
                "tool_name": tool_name,
                "anomaly_type": verdict.anomaly_type.value if verdict.anomaly_type else None,
                "message": verdict.message,
            })
            raise ToolCallFrequencyExceededError(verdict)

        return await fn(*args, **kwargs)

    def stats(self) -> dict:
        return {"blocked_calls": self._blocked_count}
```

## Solution 6: Frequency Anomaly Audit Log

```python
import time
from collections import deque
from threading import Lock
from typing import Deque


class FrequencyAnomalyAuditLog:
    """
    Records blocked tool call frequency events and surfaces
    sessions with systematic anomaly patterns.
    """

    def __init__(self, max_records: int = 5_000):
        self._max = max_records
        self._records: Deque[dict] = deque()
        self._lock = Lock()

    def record(self, audit_event: dict) -> None:
        with self._lock:
            self._records.append({**audit_event, "recorded_at": time.time()})
            if len(self._records) > self._max:
                self._records.popleft()

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r.get("recorded_at", 0) >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "blocked_calls": 0}

        by_type: dict = {}
        for r in recent:
            t = r.get("anomaly_type", "unknown")
            by_type[t] = by_type.get(t, 0) + 1

        return {
            "window_seconds": window_seconds,
            "blocked_calls": len(recent),
            "unique_sessions": len({r.get("session_id") for r in recent}),
            "by_anomaly_type": by_type,
            "most_blocked_tools": self._top_tools(recent),
        }

    @staticmethod
    def _top_tools(records: list) -> list:
        counts: dict = {}
        for r in records:
            t = r.get("tool_name", "unknown")
            counts[t] = counts.get(t, 0) + 1
        return sorted(
            [{"tool_name": k, "blocked_count": v} for k, v in counts.items()],
            key=lambda x: -x["blocked_count"],
        )[:5]
```

## Comparison

| Approach | Per-Tool Rate | Session Aggregate | Burst Detection | Call Blocking | Anomaly Audit |
|---|---|---|---|---|---|
| ToolCallFrequencyCounter | Yes (sliding) | Yes | No | No | No |
| BurstDetector | No | No | Yes (short window) | No | No |
| ToolCallFrequencyAnomalyDetector | Via counter | Via counter | Via detector | No | No |
| FrequencyGatedToolDispatcher | Via detector | Via detector | Via detector | Yes | Via audit_fn |
| FrequencyAnomalyAuditLog | No | No | No | No | Yes |

**Best for production**: Set per-tool limits based on observed normal usage — if a search tool is called at most 5 times in a well-behaved session, set `per_tool_max_calls["search"] = 15` (3× normal) as the anomaly threshold. Use `burst_window_seconds=5, burst_max_calls=8` to catch tight loops before the sliding window limit is reached. Raise `ToolCallFrequencyExceededError` in a way that terminates the agent loop — not just the single tool call — because a loop that is blocked on one call will immediately retry. Alert via `FrequencyAnomalyAuditLog` when any session triggers more than 3 blocked calls within a minute.
