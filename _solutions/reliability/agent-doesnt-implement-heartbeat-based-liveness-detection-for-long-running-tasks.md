---
title: "Agent Doesn't Implement Heartbeat-Based Liveness Detection for Long-Running Tasks"
description: "Agents that run multi-minute tasks — document processing, multi-step research, batch tool calls — have no way to distinguish a healthy slow task from a silent hang. Implement heartbeat-based liveness detection: tasks emit regular heartbeats to a monitor, the monitor declares tasks dead after a missed-heartbeat threshold, and the runtime can then cancel, restart, or escalate the affected task."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-heartbeat-based-liveness-detection-for-long-running-tasks
tags: [heartbeat, liveness-detection, stuck-task-detection, watchdog, long-running-tasks, health-monitoring]
symptoms:
  - "Task appears to be running but produces no output for 10+ minutes — no way to tell if it's alive"
  - "Worker process hangs silently after a network partition; no alert fires until a human notices"
  - "Timeout fires too late — task is already consuming resources for 30 minutes before detection"
  - "No distinction between 'slow but making progress' and 'completely stuck'"
  - "Restart storms because stuck tasks are never detected and accumulate until OOM"
---

## Why This Happens

Long-running tasks are started and then awaited — the runtime trusts that `await` will eventually return. When a downstream API hangs indefinitely, or a subprocess deadlocks on a lock, or a network socket blocks forever, the awaiting coroutine never wakes. A simple timeout is too blunt: legitimate slow tasks get killed. Heartbeats solve this: the task actively signals progress at intervals, and a watchdog declares the task dead only if signals stop. Progress heartbeats (with a payload indicating completion percentage) further distinguish "alive and working" from "alive but spinning on the same step."

## Solution 1: Heartbeat Token

```python
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class HeartbeatStatus(str, Enum):
    ALIVE = "alive"
    PROGRESS = "progress"
    STALLED = "stalled"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Heartbeat:
    task_id: str
    sequence: int
    timestamp: float = field(default_factory=time.time)
    status: HeartbeatStatus = HeartbeatStatus.ALIVE
    progress_pct: Optional[float] = None    # 0.0–100.0 if known
    current_step: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def age_seconds(self) -> float:
        return time.time() - self.timestamp

    def is_progress(self) -> bool:
        return self.progress_pct is not None
```

## Solution 2: Task Heartbeat Emitter

```python
import asyncio
import time
from typing import Callable, Optional


class TaskHeartbeatEmitter:
    """
    Emits heartbeats from within a long-running task.
    The task calls beat() at natural checkpoints — between tool calls,
    after each document chunk, at loop iterations.
    Integrates with asyncio: beat() is non-blocking.
    """

    def __init__(
        self,
        task_id: str,
        emit_fn: Callable[[Heartbeat], None],
        interval_seconds: float = 10.0,
    ):
        self._task_id = task_id
        self._emit = emit_fn
        self._interval = interval_seconds
        self._sequence = 0
        self._last_beat_at = time.time()
        self._auto_beat_task: Optional[asyncio.Task] = None

    def beat(
        self,
        status: HeartbeatStatus = HeartbeatStatus.ALIVE,
        progress_pct: Optional[float] = None,
        current_step: str = "",
    ) -> None:
        self._sequence += 1
        hb = Heartbeat(
            task_id=self._task_id,
            sequence=self._sequence,
            status=status,
            progress_pct=progress_pct,
            current_step=current_step,
        )
        self._last_beat_at = time.time()
        self._emit(hb)

    def start_auto_beat(self) -> None:
        """Start a background task that emits ALIVE heartbeats automatically."""
        self._auto_beat_task = asyncio.create_task(self._auto_loop())

    async def _auto_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            self.beat(status=HeartbeatStatus.ALIVE)

    def stop_auto_beat(self) -> None:
        if self._auto_beat_task and not self._auto_beat_task.done():
            self._auto_beat_task.cancel()

    def time_since_last_beat(self) -> float:
        return time.time() - self._last_beat_at
```

## Solution 3: Liveness Monitor

```python
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional


class LivenessState(str, Enum):
    HEALTHY = "healthy"
    SUSPECT = "suspect"
    DEAD = "dead"
    COMPLETED = "completed"


@dataclass
class TaskLivenessRecord:
    task_id: str
    registered_at: float = field(default_factory=time.time)
    last_heartbeat: Optional[Heartbeat] = None
    last_heartbeat_at: float = field(default_factory=time.time)
    state: LivenessState = LivenessState.HEALTHY
    missed_beats: int = 0
    total_beats_received: int = 0


class HeartbeatLivenessMonitor:
    """
    Receives heartbeats and tracks liveness state for all registered tasks.
    Transitions: HEALTHY → SUSPECT (after suspect_threshold missed beats)
                 SUSPECT → DEAD (after dead_threshold missed beats)
    Fires on_dead callbacks when a task is declared dead.
    """

    def __init__(
        self,
        heartbeat_interval_seconds: float = 10.0,
        suspect_threshold: int = 2,
        dead_threshold: int = 5,
        check_interval_seconds: float = 5.0,
    ):
        self._beat_interval = heartbeat_interval_seconds
        self._suspect_at = suspect_threshold
        self._dead_at = dead_threshold
        self._check_interval = check_interval_seconds
        self._tasks: Dict[str, TaskLivenessRecord] = {}
        self._on_suspect: List[Callable[[TaskLivenessRecord], None]] = []
        self._on_dead: List[Callable[[TaskLivenessRecord], None]] = []
        self._monitor_task: Optional[asyncio.Task] = None

    def register_task(self, task_id: str) -> None:
        self._tasks[task_id] = TaskLivenessRecord(task_id=task_id)

    def receive_heartbeat(self, hb: Heartbeat) -> None:
        record = self._tasks.get(hb.task_id)
        if record is None:
            return
        record.last_heartbeat = hb
        record.last_heartbeat_at = time.time()
        record.total_beats_received += 1
        record.missed_beats = 0

        if hb.status in (HeartbeatStatus.COMPLETED, HeartbeatStatus.FAILED):
            record.state = LivenessState.COMPLETED
        else:
            record.state = LivenessState.HEALTHY

    def on_suspect(self, fn: Callable) -> None:
        self._on_suspect.append(fn)

    def on_dead(self, fn: Callable) -> None:
        self._on_dead.append(fn)

    def start(self) -> None:
        self._monitor_task = asyncio.create_task(self._check_loop())

    def stop(self) -> None:
        if self._monitor_task:
            self._monitor_task.cancel()

    async def _check_loop(self) -> None:
        while True:
            await asyncio.sleep(self._check_interval)
            self._check_all()

    def _check_all(self) -> None:
        now = time.time()
        for record in list(self._tasks.values()):
            if record.state == LivenessState.COMPLETED:
                continue
            age = now - record.last_heartbeat_at
            missed = int(age / self._beat_interval)
            record.missed_beats = missed

            if missed >= self._dead_at and record.state != LivenessState.DEAD:
                record.state = LivenessState.DEAD
                for fn in self._on_dead:
                    try:
                        fn(record)
                    except Exception:
                        pass
            elif missed >= self._suspect_at and record.state == LivenessState.HEALTHY:
                record.state = LivenessState.SUSPECT
                for fn in self._on_suspect:
                    try:
                        fn(record)
                    except Exception:
                        pass

    def unregister_task(self, task_id: str) -> None:
        self._tasks.pop(task_id, None)

    def states(self) -> Dict[str, str]:
        return {tid: r.state.value for tid, r in self._tasks.items()}
```

## Solution 4: Progress Stall Detector

```python
import time
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class ProgressStall:
    task_id: str
    stall_duration_seconds: float
    last_progress_pct: Optional[float]
    last_step: str


class ProgressStallDetector:
    """
    Detects tasks that are sending heartbeats (so are 'alive') but have
    not advanced their progress percentage for too long.
    Differentiates a genuinely stuck task from one that correctly
    reports no progress because it can't estimate completion.
    """

    def __init__(self, max_stall_seconds: float = 120.0):
        self._max_stall = max_stall_seconds
        self._last_progress: Dict[str, tuple] = {}   # task_id -> (pct, timestamp, step)

    def observe(self, hb: Heartbeat) -> Optional[ProgressStall]:
        if hb.progress_pct is None:
            return None   # task doesn't report progress — can't detect stall

        prev = self._last_progress.get(hb.task_id)
        now = time.time()

        if prev is None:
            self._last_progress[hb.task_id] = (hb.progress_pct, now, hb.current_step)
            return None

        prev_pct, prev_time, prev_step = prev
        if hb.progress_pct > prev_pct:
            # Progress made — reset
            self._last_progress[hb.task_id] = (hb.progress_pct, now, hb.current_step)
            return None

        stall_duration = now - prev_time
        if stall_duration > self._max_stall:
            return ProgressStall(
                task_id=hb.task_id,
                stall_duration_seconds=round(stall_duration, 1),
                last_progress_pct=prev_pct,
                last_step=prev_step,
            )
        return None
```

## Solution 5: Liveness-Aware Task Runner

```python
import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable, Optional


class LivenessAwareTaskRunner:
    """
    Wraps a coroutine with automatic heartbeat emission and liveness tracking.
    The wrapped coroutine receives an emitter it calls at progress checkpoints.
    On dead-detection, the runner cancels the coroutine and triggers restart.
    """

    def __init__(
        self,
        monitor: HeartbeatLivenessMonitor,
        heartbeat_interval_seconds: float = 10.0,
        max_restarts: int = 3,
    ):
        self._monitor = monitor
        self._hb_interval = heartbeat_interval_seconds
        self._max_restarts = max_restarts

    @asynccontextmanager
    async def managed_task(
        self, task_id: str
    ) -> AsyncIterator[TaskHeartbeatEmitter]:
        self._monitor.register_task(task_id)
        emitter = TaskHeartbeatEmitter(
            task_id=task_id,
            emit_fn=self._monitor.receive_heartbeat,
            interval_seconds=self._hb_interval,
        )
        emitter.start_auto_beat()
        try:
            emitter.beat(HeartbeatStatus.ALIVE, current_step="started")
            yield emitter
            emitter.beat(HeartbeatStatus.COMPLETED, progress_pct=100.0)
        except asyncio.CancelledError:
            emitter.beat(HeartbeatStatus.FAILED, current_step="cancelled")
            raise
        except Exception:
            emitter.beat(HeartbeatStatus.FAILED, current_step="error")
            raise
        finally:
            emitter.stop_auto_beat()
            self._monitor.unregister_task(task_id)
```

## Solution 6: Liveness Dashboard

```python
import time
from typing import Dict, List


class LivenessDashboard:
    """
    Fleet-level liveness summary across all registered tasks.
    Surfaces dead and suspect tasks with their last known state.
    """

    def __init__(
        self,
        monitor: HeartbeatLivenessMonitor,
        stall_detector: ProgressStallDetector,
    ):
        self._monitor = monitor
        self._stall = stall_detector

    def render(self) -> dict:
        records = list(self._monitor._tasks.values())
        by_state: Dict[str, List[dict]] = {
            "healthy": [], "suspect": [], "dead": [], "completed": []
        }

        for r in records:
            entry = {
                "task_id": r.task_id,
                "state": r.state.value,
                "missed_beats": r.missed_beats,
                "total_beats": r.total_beats_received,
                "last_step": r.last_heartbeat.current_step if r.last_heartbeat else "",
                "last_progress_pct": r.last_heartbeat.progress_pct if r.last_heartbeat else None,
            }
            by_state.get(r.state.value, by_state["healthy"]).append(entry)

        return {
            "generated_at": time.time(),
            "summary": {
                state: len(tasks) for state, tasks in by_state.items()
            },
            "dead_tasks": by_state["dead"],
            "suspect_tasks": by_state["suspect"],
            "healthy_count": len(by_state["healthy"]),
            "alerts": [
                f"dead task: {t['task_id']} last_step={t['last_step']}"
                for t in by_state["dead"]
            ],
        }
```

## Comparison

| Approach | Heartbeat Emission | Liveness Tracking | Progress Stall | Fleet Dashboard |
|---|---|---|---|---|
| TaskHeartbeatEmitter | Yes (manual + auto) | No | No | No |
| HeartbeatLivenessMonitor | No | Yes (suspect/dead) | No | No |
| ProgressStallDetector | No | No | Yes | No |
| LivenessAwareTaskRunner | Via emitter | Via monitor | No | No |
| LivenessDashboard | No | Via monitor | Via detector | Yes |

**Best for production**: Call `emitter.beat()` at every natural task checkpoint — after each tool call, at each loop iteration, when starting a new sub-step. Set heartbeat interval to 10 seconds and `dead_threshold` to 5 missed beats (50 seconds total). Use `ProgressStallDetector` for tasks that can estimate completion percentage — a task stuck at 40% for 2 minutes needs different handling than a task that never reported progress at all. Register `on_dead` callbacks that cancel the asyncio task, log the failure, and enqueue a restart — but bound retries to prevent restart storms.
