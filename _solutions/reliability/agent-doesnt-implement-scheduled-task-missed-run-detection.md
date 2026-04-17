---
title: "Agent Doesn't Implement Scheduled Task Missed Run Detection"
description: "Agents that run scheduled tasks (daily summaries, hourly syncs, periodic cleanups) with no missed-run detection silently skip executions when the process is down, restarting, or the task scheduler has a bug — and the omission is invisible until a downstream consumer notices stale data. Implement missed run detection that compares expected execution times against actual execution records, alerts on gaps, and supports catch-up execution for idempotent tasks."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-scheduled-task-missed-run-detection
tags: [scheduled-tasks, missed-run, cron, catch-up-execution, task-scheduling, reliability]
symptoms:
  - "Daily report was not generated but no alert fired — noticed only by the recipient"
  - "Hourly sync skipped during agent restart window — downstream data is 3 hours stale"
  - "No record of whether a scheduled task ran or was skipped for any given interval"
  - "Scheduler bug silently drops tasks during high-load periods with no visibility"
  - "After a crash recovery, no mechanism to determine which scheduled intervals were missed"
---

## Why This Happens

Scheduled task frameworks fire a task at a given time and forget. If the agent process is not running, the trigger is simply dropped. If the scheduler is running but the task executor raises an unhandled exception, the run is lost. Without a persistent log of expected run times versus actual run times, there is no way to detect the gap. Missed run detection requires three components: a schedule model that can enumerate expected run times for any interval, a persistent run record that captures every actual execution, and a comparator that identifies expected-but-missing entries and raises an alert or triggers a catch-up.

## Solution 1: Schedule Model

```python
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional


@dataclass
class ScheduleDefinition:
    task_name: str
    interval_seconds: float         # e.g. 3600 for hourly
    first_run_at: float             # unix timestamp of the first scheduled run
    max_catch_up_runs: int = 5      # cap on catch-up executions
    is_idempotent: bool = True      # only idempotent tasks support catch-up

    def expected_runs_in_window(
        self,
        window_start: float,
        window_end: float,
    ) -> List[float]:
        """Return list of expected run timestamps in [window_start, window_end)."""
        if window_start >= window_end or self.interval_seconds <= 0:
            return []
        first = self.first_run_at
        # find first run >= window_start
        if first < window_start:
            steps = math.ceil((window_start - first) / self.interval_seconds)
            first = first + steps * self.interval_seconds
        runs = []
        t = first
        while t < window_end:
            runs.append(t)
            t += self.interval_seconds
        return runs
```

## Solution 2: Task Run Record Store

```python
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional


class RunOutcome(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    SKIPPED = "skipped"


@dataclass
class TaskRunRecord:
    task_name: str
    scheduled_at: float
    started_at: float
    finished_at: Optional[float]
    outcome: RunOutcome
    error: Optional[str] = None
    run_id: str = ""

    def __post_init__(self) -> None:
        if not self.run_id:
            import hashlib
            key = f"{self.task_name}:{self.scheduled_at}"
            self.run_id = hashlib.sha256(key.encode()).hexdigest()[:12]


class TaskRunRecordStore:
    """Persists task run records to a local JSON file."""

    def __init__(self, path: str = "/tmp/task_run_records.json"):
        self._path = Path(path)
        self._lock = Lock()

    def save(self, record: TaskRunRecord) -> None:
        with self._lock:
            all_records = self._load_all()
            key = f"{record.task_name}:{record.scheduled_at}"
            all_records[key] = {
                "task_name": record.task_name,
                "scheduled_at": record.scheduled_at,
                "started_at": record.started_at,
                "finished_at": record.finished_at,
                "outcome": record.outcome.value,
                "error": record.error,
                "run_id": record.run_id,
            }
            self._path.write_text(json.dumps(all_records, indent=2))

    def records_for_task(
        self,
        task_name: str,
        since: float = 0.0,
    ) -> List[TaskRunRecord]:
        with self._lock:
            all_records = self._load_all()
        result = []
        for data in all_records.values():
            if data["task_name"] != task_name:
                continue
            if data["scheduled_at"] < since:
                continue
            result.append(TaskRunRecord(
                task_name=data["task_name"],
                scheduled_at=data["scheduled_at"],
                started_at=data["started_at"],
                finished_at=data.get("finished_at"),
                outcome=RunOutcome(data["outcome"]),
                error=data.get("error"),
                run_id=data.get("run_id", ""),
            ))
        return result

    def _load_all(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
```

## Solution 3: Missed Run Detector

```python
import time
from typing import List


@dataclass
class MissedRun:
    task_name: str
    scheduled_at: float
    age_seconds: float
    is_catch_up_eligible: bool


class MissedRunDetector:
    """
    Compares expected run times from a schedule model against
    actual run records to identify missed executions.
    """

    def __init__(
        self,
        schedule: ScheduleDefinition,
        store: TaskRunRecordStore,
        look_back_seconds: float = 86400.0,
    ):
        self._schedule = schedule
        self._store = store
        self._look_back = look_back_seconds

    def detect(self) -> List[MissedRun]:
        now = time.time()
        window_start = now - self._look_back
        expected = self._schedule.expected_runs_in_window(window_start, now)

        records = self._store.records_for_task(
            self._schedule.task_name, since=window_start
        )
        executed_at = {
            r.scheduled_at
            for r in records
            if r.outcome in (RunOutcome.SUCCESS, RunOutcome.SKIPPED)
        }

        missed = []
        for ts in expected:
            if ts not in executed_at:
                age = now - ts
                missed.append(MissedRun(
                    task_name=self._schedule.task_name,
                    scheduled_at=ts,
                    age_seconds=round(age, 1),
                    is_catch_up_eligible=(
                        self._schedule.is_idempotent
                        and len(missed) < self._schedule.max_catch_up_runs
                    ),
                ))
        return missed
```

## Solution 4: Catch-Up Executor

```python
import asyncio
import time
from typing import Any, Callable, List


class CatchUpExecutor:
    """
    Executes missed runs in chronological order for idempotent tasks.
    Records each execution outcome in the run store.
    """

    def __init__(self, store: TaskRunRecordStore):
        self._store = store

    async def execute_missed(
        self,
        missed_runs: List[MissedRun],
        task_fn: Callable[[float], Any],
    ) -> List[TaskRunRecord]:
        eligible = [m for m in missed_runs if m.is_catch_up_eligible]
        eligible.sort(key=lambda m: m.scheduled_at)
        results = []
        for missed in eligible:
            start = time.time()
            error = None
            outcome = RunOutcome.SUCCESS
            try:
                await task_fn(missed.scheduled_at)
            except Exception as exc:
                error = str(exc)
                outcome = RunOutcome.FAILURE
            record = TaskRunRecord(
                task_name=missed.task_name,
                scheduled_at=missed.scheduled_at,
                started_at=start,
                finished_at=time.time(),
                outcome=outcome,
                error=error,
            )
            self._store.save(record)
            results.append(record)
        return results
```

## Solution 5: Scheduled Task Runner with Miss Tracking

```python
import asyncio
import time
from typing import Any, Callable


class MissTrackingScheduledTaskRunner:
    """
    Wraps a task function with schedule adherence tracking.
    Records every execution with its scheduled time so the missed-run
    detector can compare against expected intervals.
    """

    def __init__(
        self,
        schedule: ScheduleDefinition,
        store: TaskRunRecordStore,
    ):
        self._schedule = schedule
        self._store = store

    async def run(
        self,
        task_fn: Callable[[], Any],
        scheduled_at: float,
    ) -> TaskRunRecord:
        start = time.time()
        error = None
        outcome = RunOutcome.SUCCESS
        try:
            await task_fn()
        except Exception as exc:
            error = str(exc)
            outcome = RunOutcome.FAILURE
        record = TaskRunRecord(
            task_name=self._schedule.task_name,
            scheduled_at=scheduled_at,
            started_at=start,
            finished_at=time.time(),
            outcome=outcome,
            error=error,
        )
        self._store.save(record)
        return record

    async def run_continuously(self, task_fn: Callable[[], Any]) -> None:
        """Drive the task at the configured interval, recording every run."""
        schedule = self._schedule
        interval = schedule.interval_seconds
        next_run = schedule.first_run_at
        while True:
            now = time.time()
            wait = next_run - now
            if wait > 0:
                await asyncio.sleep(wait)
            scheduled_at = next_run
            next_run += interval
            await self.run(task_fn, scheduled_at)
```

## Solution 6: Missed Run Alert Dashboard

```python
import time
from typing import List


class MissedRunAlertDashboard:
    """
    Combines miss detection with a summary report suitable for
    structured logging or an on-call alert payload.
    """

    def __init__(
        self,
        detectors: List[MissedRunDetector],
        alert_threshold_missed: int = 1,
    ):
        self._detectors = detectors
        self._threshold = alert_threshold_missed

    def render(self) -> dict:
        report = {
            "generated_at": time.time(),
            "tasks": [],
            "total_missed": 0,
            "alert": False,
        }
        for detector in self._detectors:
            missed = detector.detect()
            task_report = {
                "task_name": detector._schedule.task_name,
                "missed_count": len(missed),
                "missed_runs": [
                    {
                        "scheduled_at": m.scheduled_at,
                        "age_seconds": m.age_seconds,
                        "catch_up_eligible": m.is_catch_up_eligible,
                    }
                    for m in missed
                ],
            }
            report["tasks"].append(task_report)
            report["total_missed"] += len(missed)

        report["alert"] = report["total_missed"] >= self._threshold
        return report
```

## Comparison

| Approach | Schedule Modeling | Run Recording | Miss Detection | Catch-Up Execution | Dashboard |
|---|---|---|---|---|---|
| ScheduleDefinition | Yes (interval math) | No | No | No | No |
| TaskRunRecordStore | No | Yes (persistent) | No | No | No |
| MissedRunDetector | Via schedule | Via store | Yes | No | No |
| CatchUpExecutor | No | Via store | No | Yes (idempotent) | No |
| MissTrackingScheduledTaskRunner | Via schedule | Yes | No | No | No |
| MissedRunAlertDashboard | No | No | Via detectors | No | Yes |

**Best for production**: Record every task execution against its `scheduled_at` timestamp — not `started_at` — so the detector can correlate against the expected schedule even when tasks run slightly late. Set `look_back_seconds` to at least three interval lengths: a task that runs every hour should look back six hours to catch cascading misses. Only enable catch-up execution for tasks marked `is_idempotent=True`; for non-idempotent tasks (e.g., payment sends), alert and let a human decide. Cap `max_catch_up_runs` at a small number (3–5) to prevent a long outage from triggering a thundering-herd of catch-up work on recovery.
