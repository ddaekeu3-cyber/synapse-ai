---
title: "Agent Doesn't Implement Long-Running Task Heartbeat Monitoring"
description: "Agents executing long-running tasks — multi-step research, batch document processing, extended code generation — have no mechanism to signal that work is still in progress. Without heartbeats, orchestrators and users cannot distinguish a stuck agent from a slow one, leading to premature timeouts, duplicate task submissions, and lost work. Implement heartbeat emission during long tasks and heartbeat monitoring that detects stalled agents and triggers recovery."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-long-running-task-heartbeat-monitoring
tags: [heartbeat, long-running-tasks, stall-detection, task-monitoring, timeout, recovery]
symptoms:
  - "Orchestrator kills agent after fixed timeout even when it is making progress"
  - "No way to distinguish a stuck agent from a slow one during long tasks"
  - "Duplicate task submissions occur because callers assume silence means failure"
  - "Long batch jobs silently die mid-way with no record of how far they progressed"
  - "Recovery logic cannot resume from the last checkpoint because no progress was recorded"
---

## Why This Happens

Long-running tasks are designed around a request-response model that assumes completion within seconds. When a task takes minutes, the absence of a response is indistinguishable from failure. Heartbeats solve this by sending periodic progress signals — not the final result, but evidence of life and progress. The monitoring layer watches for heartbeat gaps and triggers recovery only when silence exceeds the expected heartbeat interval by a configurable factor, distinguishing genuine stalls from normal slow progress.

## Solution 1: Task Heartbeat Record

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class TaskPhase(str, Enum):
    INITIALIZING = "initializing"
    PLANNING = "planning"
    EXECUTING = "executing"
    TOOL_CALL = "tool_call"
    SYNTHESIZING = "synthesizing"
    FINALIZING = "finalizing"


@dataclass
class TaskHeartbeat:
    task_id: str
    session_id: str
    timestamp: float
    phase: TaskPhase
    progress_pct: Optional[float]      # 0.0 – 100.0 if measurable
    steps_completed: int
    steps_total: Optional[int]
    current_step_description: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def age_seconds(self) -> float:
        return time.time() - self.timestamp
```

## Solution 2: Heartbeat Emitter

```python
import asyncio
import time
from typing import Any, Callable, Dict, Optional


class TaskHeartbeatEmitter:
    """
    Emits heartbeats on a fixed interval during long-running task execution.
    Caller provides a sink_fn that persists or forwards heartbeats.
    """

    def __init__(
        self,
        task_id: str,
        session_id: str,
        sink_fn: Callable[[TaskHeartbeat], None],
        interval_seconds: float = 15.0,
    ):
        self._task_id = task_id
        self._session_id = session_id
        self._sink = sink_fn
        self._interval = interval_seconds
        self._steps_completed = 0
        self._steps_total: Optional[int] = None
        self._phase = TaskPhase.INITIALIZING
        self._current_step = "starting"
        self._running = False
        self._task: Optional[asyncio.Task] = None

    def update_progress(
        self,
        phase: TaskPhase,
        current_step: str,
        steps_completed: int = 0,
        steps_total: Optional[int] = None,
    ) -> None:
        self._phase = phase
        self._current_step = current_step
        self._steps_completed = steps_completed
        if steps_total is not None:
            self._steps_total = steps_total

    def _make_heartbeat(self) -> TaskHeartbeat:
        progress = None
        if self._steps_total and self._steps_total > 0:
            progress = round(self._steps_completed / self._steps_total * 100, 1)
        return TaskHeartbeat(
            task_id=self._task_id,
            session_id=self._session_id,
            timestamp=time.time(),
            phase=self._phase,
            progress_pct=progress,
            steps_completed=self._steps_completed,
            steps_total=self._steps_total,
            current_step_description=self._current_step,
        )

    async def _loop(self) -> None:
        while self._running:
            try:
                self._sink(self._make_heartbeat())
            except Exception:
                pass
            await asyncio.sleep(self._interval)

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # Emit a final heartbeat marking completion
        self._sink(self._make_heartbeat())
```

## Solution 3: Heartbeat Store

```python
import time
from threading import Lock
from typing import Dict, List, Optional


class HeartbeatStore:
    """
    Persists the most recent heartbeat per task_id for monitoring.
    Evicts stale entries automatically.
    """

    def __init__(self, max_age_seconds: float = 3600.0):
        self._lock = Lock()
        self._heartbeats: Dict[str, TaskHeartbeat] = {}
        self._max_age = max_age_seconds

    def record(self, heartbeat: TaskHeartbeat) -> None:
        with self._lock:
            self._heartbeats[heartbeat.task_id] = heartbeat
            self._evict()

    def latest(self, task_id: str) -> Optional[TaskHeartbeat]:
        with self._lock:
            return self._heartbeats.get(task_id)

    def active_tasks(self) -> List[TaskHeartbeat]:
        cutoff = time.time() - self._max_age
        with self._lock:
            return [h for h in self._heartbeats.values() if h.timestamp >= cutoff]

    def _evict(self) -> None:
        cutoff = time.time() - self._max_age
        stale = [tid for tid, h in self._heartbeats.items() if h.timestamp < cutoff]
        for tid in stale:
            del self._heartbeats[tid]
```

## Solution 4: Stall Detector

```python
import time
from typing import List, Optional


@dataclass
class StallEvent:
    task_id: str
    session_id: str
    last_heartbeat_at: float
    silence_seconds: float
    last_phase: TaskPhase
    last_step: str
    stall_threshold_seconds: float


class HeartbeatStallDetector:
    """
    Checks all active tasks against their expected heartbeat interval.
    Tasks that have not emitted a heartbeat within (interval * miss_factor)
    seconds are reported as stalled.
    """

    def __init__(
        self,
        store: HeartbeatStore,
        expected_interval_seconds: float = 15.0,
        miss_factor: float = 3.0,    # stalled after 3 missed heartbeats
    ):
        self._store = store
        self._threshold = expected_interval_seconds * miss_factor

    def detect_stalls(self) -> List[StallEvent]:
        stalls = []
        now = time.time()
        for heartbeat in self._store.active_tasks():
            silence = now - heartbeat.timestamp
            if silence >= self._threshold:
                stalls.append(StallEvent(
                    task_id=heartbeat.task_id,
                    session_id=heartbeat.session_id,
                    last_heartbeat_at=heartbeat.timestamp,
                    silence_seconds=round(silence, 1),
                    last_phase=heartbeat.phase,
                    last_step=heartbeat.current_step_description,
                    stall_threshold_seconds=self._threshold,
                ))
        return stalls
```

## Solution 5: Stall Recovery Manager

```python
import time
from typing import Callable, Dict, List, Optional


class StallRecoveryManager:
    """
    Responds to detected stalls by calling a registered recovery function
    for each task. Tracks recovery attempts to avoid repeated triggers.
    """

    def __init__(
        self,
        detector: HeartbeatStallDetector,
        max_recovery_attempts: int = 2,
        recovery_cooldown_seconds: float = 120.0,
    ):
        self._detector = detector
        self._max_attempts = max_recovery_attempts
        self._cooldown = recovery_cooldown_seconds
        self._attempts: Dict[str, int] = {}
        self._last_recovery: Dict[str, float] = {}
        self._handlers: Dict[str, Callable[[StallEvent], None]] = {}

    def register_handler(
        self, task_id: str, handler: Callable[[StallEvent], None]
    ) -> None:
        self._handlers[task_id] = handler

    def run_recovery_pass(self) -> List[dict]:
        stalls = self._detector.detect_stalls()
        results = []
        now = time.time()

        for stall in stalls:
            tid = stall.task_id
            attempts = self._attempts.get(tid, 0)
            last = self._last_recovery.get(tid, 0)

            if attempts >= self._max_attempts:
                results.append({"task_id": tid, "action": "abandoned", "attempts": attempts})
                continue

            if now - last < self._cooldown:
                results.append({"task_id": tid, "action": "cooldown", "attempts": attempts})
                continue

            handler = self._handlers.get(tid)
            if handler:
                try:
                    handler(stall)
                    self._attempts[tid] = attempts + 1
                    self._last_recovery[tid] = now
                    results.append({"task_id": tid, "action": "recovered", "attempts": attempts + 1})
                except Exception as exc:
                    results.append({"task_id": tid, "action": "recovery_failed", "error": str(exc)})
            else:
                results.append({"task_id": tid, "action": "no_handler"})

        return results
```

## Solution 6: Heartbeat Monitoring Dashboard

```python
import time
from typing import List


class HeartbeatMonitoringDashboard:
    """
    Combines active task status, stall detection, and recovery history
    into a single operational view for on-call visibility.
    """

    def __init__(
        self,
        store: HeartbeatStore,
        detector: HeartbeatStallDetector,
        recovery_manager: StallRecoveryManager,
    ):
        self._store = store
        self._detector = detector
        self._recovery = recovery_manager

    def render(self) -> dict:
        active = self._store.active_tasks()
        stalls = self._detector.detect_stalls()

        return {
            "generated_at": time.time(),
            "active_tasks": len(active),
            "stalled_tasks": len(stalls),
            "tasks": [
                {
                    "task_id": h.task_id,
                    "phase": h.phase.value,
                    "progress_pct": h.progress_pct,
                    "steps": f"{h.steps_completed}/{h.steps_total or '?'}",
                    "last_heartbeat_seconds_ago": round(h.age_seconds(), 1),
                    "stalled": any(s.task_id == h.task_id for s in stalls),
                }
                for h in sorted(active, key=lambda h: h.timestamp, reverse=True)
            ],
            "stall_details": [
                {
                    "task_id": s.task_id,
                    "silence_seconds": s.silence_seconds,
                    "last_phase": s.last_phase.value,
                    "last_step": s.last_step,
                }
                for s in stalls
            ],
        }
```

## Comparison

| Approach | Heartbeat Emission | Heartbeat Storage | Stall Detection | Recovery Triggering | Dashboard |
|---|---|---|---|---|---|
| TaskHeartbeatEmitter | Yes (periodic loop) | No | No | No | No |
| HeartbeatStore | No | Yes (latest per task) | No | No | No |
| HeartbeatStallDetector | No | Via store | Yes (interval × factor) | No | No |
| StallRecoveryManager | No | No | Via detector | Yes (with cooldown) | No |
| HeartbeatMonitoringDashboard | No | Via store | Via detector | No | Yes |

**Best for production**: Set `interval_seconds=15` and `miss_factor=3` to trigger stall detection after 45 seconds of silence — long enough to avoid false positives on slow tool calls but short enough to catch genuine hangs before upstream timeouts fire. Emit heartbeats with `progress_pct` populated whenever step counts are measurable: this lets operators know whether a stall happened at 2% or 98% completion, which changes the urgency of recovery. Store heartbeats in Redis with a TTL of `interval_seconds * miss_factor * 2` to avoid unbounded memory growth on long-running fleet deployments.
