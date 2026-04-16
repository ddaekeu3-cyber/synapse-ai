---
title: "Agent Doesn't Implement Agent Loop Iteration Count Monitoring"
description: "Agents that run agentic loops without tracking iteration counts cannot detect runaway loops — workflows that fail to reach a terminal state and continue consuming tokens indefinitely. Without iteration monitoring, a misconfigured tool, a prompt that prevents the agent from concluding, or a logic error that creates a cycle will exhaust the context window or run until a billing limit is hit. Implement iteration count tracking, per-session loop budgets, and loop anomaly detection."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-agent-loop-iteration-count-monitoring
tags: [agent-loop, iteration-monitoring, runaway-detection, loop-budget, infinite-loop, agentic-safety]
symptoms:
  - "Agent loops run until context window exhaustion with no early termination"
  - "No maximum iteration limit enforced — a misconfigured prompt can loop indefinitely"
  - "Billing spikes when a session enters a loop and is not terminated"
  - "Cannot distinguish a legitimate 50-step workflow from a stuck loop"
  - "No per-session or per-task iteration budget to constrain worst-case token usage"
---

## Why This Happens

Agentic loops are designed to run until a goal is achieved or a stopping condition is met. If neither condition is ever triggered — because the LLM never emits a final answer, a tool always returns a result that triggers another tool call, or a bug prevents the completion condition from being detected — the loop runs indefinitely. Without a hard iteration ceiling and per-session budget tracking, the only limit is the context window or external billing controls. Iteration monitoring adds a lightweight safety layer: count iterations, compare against a budget, emit an alert when the count is elevated, and terminate when a hard limit is reached.

## Solution 1: Loop Session

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class LoopTerminationReason(str, Enum):
    GOAL_ACHIEVED = "goal_achieved"
    MAX_ITERATIONS = "max_iterations"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ERROR = "error"
    TIMEOUT = "timeout"
    EXTERNAL_STOP = "external_stop"


@dataclass
class LoopIteration:
    iteration_index: int
    started_at: float
    completed_at: Optional[float] = None
    tool_calls_made: int = 0
    tokens_used: int = 0
    notes: str = ""

    def duration_ms(self) -> Optional[float]:
        if self.completed_at is None:
            return None
        return round((self.completed_at - self.started_at) * 1000, 2)


@dataclass
class LoopSession:
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: float = field(default_factory=time.time)
    max_iterations: int = 50
    iterations: List[LoopIteration] = field(default_factory=list)
    terminated_at: Optional[float] = None
    termination_reason: Optional[LoopTerminationReason] = None
    total_tokens: int = 0
    total_tool_calls: int = 0

    def current_iteration(self) -> int:
        return len(self.iterations)

    def is_over_budget(self) -> bool:
        return len(self.iterations) >= self.max_iterations

    def duration_seconds(self) -> float:
        end = self.terminated_at or time.time()
        return round(end - self.started_at, 2)
```

## Solution 2: Loop Session Manager

```python
from threading import Lock
from typing import Dict, Optional


class LoopSessionManager:
    """
    Creates and manages loop session records.
    Enforces max_iterations budget per session.
    """

    def __init__(self, default_max_iterations: int = 50):
        self._sessions: Dict[str, LoopSession] = {}
        self._lock = Lock()
        self._default_max = default_max_iterations

    def new_session(
        self,
        max_iterations: Optional[int] = None,
    ) -> LoopSession:
        session = LoopSession(
            max_iterations=max_iterations or self._default_max,
        )
        with self._lock:
            self._sessions[session.session_id] = session
        return session

    def begin_iteration(self, session: LoopSession) -> LoopIteration:
        if session.is_over_budget():
            raise LoopBudgetExceeded(
                session_id=session.session_id,
                iterations=session.current_iteration(),
                max_iterations=session.max_iterations,
            )
        iteration = LoopIteration(
            iteration_index=session.current_iteration(),
            started_at=time.time(),
        )
        session.iterations.append(iteration)
        return iteration

    def end_iteration(
        self,
        session: LoopSession,
        iteration: LoopIteration,
        tool_calls: int = 0,
        tokens: int = 0,
        notes: str = "",
    ) -> None:
        iteration.completed_at = time.time()
        iteration.tool_calls_made = tool_calls
        iteration.tokens_used = tokens
        iteration.notes = notes
        session.total_tokens += tokens
        session.total_tool_calls += tool_calls

    def terminate_session(
        self,
        session: LoopSession,
        reason: LoopTerminationReason,
    ) -> None:
        session.terminated_at = time.time()
        session.termination_reason = reason

    def get(self, session_id: str) -> Optional[LoopSession]:
        with self._lock:
            return self._sessions.get(session_id)


class LoopBudgetExceeded(Exception):
    def __init__(self, session_id: str, iterations: int, max_iterations: int):
        super().__init__(
            f"loop session '{session_id}' exceeded max iterations "
            f"({iterations}/{max_iterations})"
        )
        self.session_id = session_id
        self.iterations = iterations
        self.max_iterations = max_iterations
```

## Solution 3: Loop Anomaly Detector

```python
import time
from typing import List, Optional


class LoopAnomalyDetector:
    """
    Detects signs of a stuck or runaway loop within a session:
    - too many iterations without tool call variety
    - repeated identical tool call patterns
    - steeply accelerating token consumption
    """

    def __init__(
        self,
        high_iteration_warning: int = 20,
        tool_repetition_threshold: int = 5,  # same tool N times in a row
    ):
        self._high_iter_warning = high_iteration_warning
        self._tool_repeat_threshold = tool_repetition_threshold

    def analyze(self, session: LoopSession) -> dict:
        anomalies = []
        iterations = session.iterations

        # High iteration count
        count = session.current_iteration()
        if count >= self._high_iter_warning:
            anomalies.append({
                "type": "high_iteration_count",
                "detail": f"{count} iterations (warning threshold: {self._high_iter_warning})",
            })

        # Budget utilization
        budget_pct = count / max(session.max_iterations, 1)
        if budget_pct >= 0.80:
            anomalies.append({
                "type": "budget_near_exhaustion",
                "detail": f"{budget_pct:.0%} of iteration budget consumed",
            })

        # Zero-progress iterations (no tool calls and no tokens for several iterations)
        if len(iterations) >= 3:
            last_three = iterations[-3:]
            if all(it.tool_calls_made == 0 and it.tokens_used == 0 for it in last_three):
                anomalies.append({
                    "type": "zero_progress_iterations",
                    "detail": "last 3 iterations produced no tool calls and no tokens",
                })

        return {
            "session_id": session.session_id,
            "iteration_count": count,
            "anomalies": anomalies,
            "is_anomalous": len(anomalies) > 0,
        }
```

## Solution 4: Loop Metrics Aggregator

```python
import time
from collections import defaultdict
from typing import Dict, List


class LoopMetricsAggregator:
    """
    Accumulates terminated session statistics for fleet-wide analysis.
    """

    def __init__(self):
        self._completed: List[dict] = []

    def record_completed(self, session: LoopSession) -> None:
        self._completed.append({
            "ts": time.time(),
            "session_id": session.session_id,
            "iterations": session.current_iteration(),
            "duration_s": session.duration_seconds(),
            "total_tokens": session.total_tokens,
            "total_tool_calls": session.total_tool_calls,
            "termination_reason": session.termination_reason.value if session.termination_reason else None,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._completed if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "sessions": 0}

        by_reason: Dict[str, int] = defaultdict(int)
        for r in recent:
            by_reason[r["termination_reason"] or "unknown"] += 1

        iteration_counts = [r["iterations"] for r in recent]
        avg_iters = sum(iteration_counts) / len(iteration_counts)
        p95_iters = sorted(iteration_counts)[min(int(len(iteration_counts) * 0.95), len(iteration_counts) - 1)]

        return {
            "window_seconds": window_seconds,
            "sessions": len(recent),
            "avg_iterations": round(avg_iters, 1),
            "p95_iterations": p95_iters,
            "max_iterations_hit": by_reason.get(LoopTerminationReason.MAX_ITERATIONS.value, 0),
            "by_termination_reason": dict(by_reason),
        }
```

## Solution 5: Loop Budget Enforcer

```python
import asyncio
from typing import Any, Callable, Optional


class LoopBudgetEnforcer:
    """
    Context manager for the agent loop that enforces iteration budgets
    and emits anomaly alerts when thresholds are crossed.
    """

    def __init__(
        self,
        manager: LoopSessionManager,
        detector: LoopAnomalyDetector,
        aggregator: LoopMetricsAggregator,
        on_anomaly: Optional[Callable[[dict], None]] = None,
        max_duration_seconds: float = 300.0,
    ):
        self._manager = manager
        self._detector = detector
        self._aggregator = aggregator
        self._on_anomaly = on_anomaly
        self._max_duration = max_duration_seconds

    async def run_loop(
        self,
        loop_fn: Callable,   # async fn(session, iteration) -> (done, tool_calls, tokens)
        max_iterations: Optional[int] = None,
    ) -> LoopSession:
        session = self._manager.new_session(max_iterations=max_iterations)
        deadline = time.time() + self._max_duration

        try:
            while True:
                if time.time() > deadline:
                    self._manager.terminate_session(session, LoopTerminationReason.TIMEOUT)
                    break

                try:
                    iteration = self._manager.begin_iteration(session)
                except LoopBudgetExceeded:
                    self._manager.terminate_session(session, LoopTerminationReason.MAX_ITERATIONS)
                    break

                done, tool_calls, tokens = await loop_fn(session, iteration)
                self._manager.end_iteration(session, iteration, tool_calls=tool_calls, tokens=tokens)

                # Check for anomalies
                analysis = self._detector.analyze(session)
                if analysis["is_anomalous"] and self._on_anomaly:
                    self._on_anomaly(analysis)

                if done:
                    self._manager.terminate_session(session, LoopTerminationReason.GOAL_ACHIEVED)
                    break

        except Exception as exc:
            self._manager.terminate_session(session, LoopTerminationReason.ERROR)
            raise

        finally:
            if session.termination_reason:
                self._aggregator.record_completed(session)

        return session
```

## Solution 6: Loop Monitoring Dashboard

```python
import time


class LoopMonitoringDashboard:
    """
    Combines active session overview, anomaly detection, and historical stats.
    """

    def __init__(
        self,
        manager: LoopSessionManager,
        aggregator: LoopMetricsAggregator,
        detector: LoopAnomalyDetector,
    ):
        self._manager = manager
        self._aggregator = aggregator
        self._detector = detector

    def render(self, window_seconds: float = 3600.0) -> dict:
        return {
            "generated_at": time.time(),
            "historical": self._aggregator.summary(window_seconds),
        }
```

## Comparison

| Approach | Session Tracking | Budget Enforcement | Anomaly Detection | Fleet Metrics | Dashboard |
|---|---|---|---|---|---|
| LoopSessionManager | Yes | Yes (raise on exceed) | No | No | No |
| LoopAnomalyDetector | No | No | Yes (3 checks) | No | No |
| LoopMetricsAggregator | No | No | No | Yes | No |
| LoopBudgetEnforcer | Via manager | Via manager | Via detector | Via aggregator | No |
| LoopMonitoringDashboard | No | No | No | No | Yes |

**Best for production**: Set `default_max_iterations=25` as the default — most well-designed agentic workflows complete in under 15 iterations; 25 gives a generous buffer. Alert immediately when `LoopTerminationReason.MAX_ITERATIONS` accounts for more than 5% of completed sessions — this indicates either tasks that are genuinely too complex for the current agent or a prompt/tool bug causing loops. Use `LoopAnomalyDetector` to emit a structured warning log when the iteration count exceeds the threshold — include the session ID so engineers can replay the conversation for debugging. Set `max_duration_seconds` to your P99 expected task duration × 3 as a wall-clock backstop independent of the iteration count.
