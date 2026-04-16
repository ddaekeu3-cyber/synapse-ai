---
title: "Agent Doesn't Implement Partial Result Recovery on Multi-Step Task Failure"
description: "Agents that execute multi-step tasks atomically — treating the entire task as failed if any step fails — discard all progress made before the failure point: a 10-step research task that fails on step 7 restarts from scratch on retry, re-executing 6 successful steps at full cost and latency. Implement partial result recovery that checkpoints successful step outputs, detects which step failed, and resumes from the last successful checkpoint rather than restarting from the beginning."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-partial-result-recovery-on-multi-step-task-failure
tags: [partial-recovery, checkpointing, task-resumption, multi-step-reliability, step-recovery, task-persistence]
symptoms:
  - "A 10-step task that fails on step 8 restarts all 10 steps on retry"
  - "No intermediate results are saved — every failure requires starting over"
  - "Long-running tasks have no recovery path after transient failures"
  - "Re-execution of already-completed steps wastes tokens and increases latency"
  - "Cannot inspect which step caused a failure without replaying the entire task"
---

## Why This Happens

Multi-step task orchestration is typically written as a sequential loop: execute step 1, execute step 2, ..., execute step N. If step 7 raises an exception, the entire function unwinds and all outputs from steps 1–6 are lost in local variables. A recovery mechanism requires externalizing these outputs: writing each step's result to a persistent store immediately after success, so that a retry can read back the completed steps and skip them. This is checkpoint-and-resume: the task definition lists all steps, the executor checks for an existing checkpoint before executing each step, and resumes from the first un-checkpointed step.

## Solution 1: Task Step Definition

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class StepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class TaskStep:
    step_id: str
    name: str
    execute_fn: Callable          # async fn(**step_inputs) -> Any
    depends_on: List[str] = field(default_factory=list)  # step_ids this step needs
    retryable: bool = True
    timeout_seconds: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MultiStepTask:
    task_id: str
    name: str
    steps: List[TaskStep]
    initial_inputs: Dict[str, Any] = field(default_factory=dict)

    def step_by_id(self, step_id: str) -> Optional[TaskStep]:
        return next((s for s in self.steps if s.step_id == step_id), None)
```

## Solution 2: Task Checkpoint Store

```python
import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


class TaskCheckpointStore:
    """
    Persists step results and task state to a JSON file.
    Supports resumption by reading back completed step outputs.
    """

    def __init__(self, path: str = "/tmp/task_checkpoints.json"):
        self._path = Path(path)
        self._data: Dict[str, dict] = {}
        self._lock = threading.Lock()
        self._load()

    def save_step_result(
        self,
        task_id: str,
        step_id: str,
        result: Any,
        status: StepStatus = StepStatus.COMPLETED,
    ) -> None:
        with self._lock:
            if task_id not in self._data:
                self._data[task_id] = {"steps": {}, "created_at": time.time()}
            self._data[task_id]["steps"][step_id] = {
                "result": result,
                "status": status.value,
                "completed_at": time.time(),
            }
            self._data[task_id]["updated_at"] = time.time()
            self._persist()

    def get_step_result(self, task_id: str, step_id: str) -> Optional[dict]:
        with self._lock:
            return self._data.get(task_id, {}).get("steps", {}).get(step_id)

    def is_step_completed(self, task_id: str, step_id: str) -> bool:
        entry = self.get_step_result(task_id, step_id)
        return entry is not None and entry.get("status") == StepStatus.COMPLETED.value

    def completed_steps(self, task_id: str) -> List[str]:
        with self._lock:
            steps = self._data.get(task_id, {}).get("steps", {})
            return [sid for sid, s in steps.items() if s.get("status") == StepStatus.COMPLETED.value]

    def clear_task(self, task_id: str) -> None:
        with self._lock:
            self._data.pop(task_id, None)
            self._persist()

    def _persist(self) -> None:
        try:
            self._path.write_text(json.dumps(self._data, indent=2, default=str))
        except OSError:
            pass

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            self._data = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
```

## Solution 3: Checkpoint-Aware Task Executor

```python
import asyncio
import time
from typing import Any, Dict, Optional


class CheckpointAwareTaskExecutor:
    """
    Executes a MultiStepTask with checkpoint-and-resume support.
    Skips steps whose results are already checkpointed.
    Saves results after each successful step.
    """

    def __init__(self, store: TaskCheckpointStore):
        self._store = store
        self._tasks_completed = 0
        self._tasks_resumed = 0
        self._steps_skipped = 0
        self._steps_executed = 0

    async def execute(self, task: MultiStepTask) -> dict:
        completed_steps = self._store.completed_steps(task.task_id)
        is_resume = len(completed_steps) > 0
        if is_resume:
            self._tasks_resumed += 1

        step_outputs: Dict[str, Any] = {}
        # Load existing checkpoint results into working memory
        for step_id in completed_steps:
            entry = self._store.get_step_result(task.task_id, step_id)
            if entry:
                step_outputs[step_id] = entry["result"]

        execution_log = []
        last_error = None

        for step in task.steps:
            if self._store.is_step_completed(task.task_id, step.step_id):
                self._steps_skipped += 1
                execution_log.append({
                    "step_id": step.step_id,
                    "status": "skipped_checkpointed",
                })
                continue

            # Gather inputs from dependency outputs
            step_inputs = {**task.initial_inputs}
            for dep_id in step.depends_on:
                dep_result = step_outputs.get(dep_id)
                if dep_result is not None:
                    step_inputs[f"{dep_id}_result"] = dep_result

            start = time.time()
            try:
                if step.timeout_seconds:
                    result = await asyncio.wait_for(
                        step.execute_fn(**step_inputs),
                        timeout=step.timeout_seconds,
                    )
                else:
                    result = await step.execute_fn(**step_inputs)

                latency_ms = round((time.time() - start) * 1000, 2)
                step_outputs[step.step_id] = result
                self._store.save_step_result(task.task_id, step.step_id, result)
                self._steps_executed += 1
                execution_log.append({
                    "step_id": step.step_id,
                    "status": "completed",
                    "latency_ms": latency_ms,
                })

            except Exception as exc:
                last_error = exc
                self._store.save_step_result(
                    task.task_id, step.step_id, None, StepStatus.FAILED
                )
                execution_log.append({
                    "step_id": step.step_id,
                    "status": "failed",
                    "error": str(exc),
                })
                raise TaskStepFailedError(task.task_id, step.step_id, str(exc)) from exc

        self._tasks_completed += 1
        self._store.clear_task(task.task_id)

        return {
            "task_id": task.task_id,
            "status": "completed",
            "resumed": is_resume,
            "steps_skipped": self._steps_skipped,
            "steps_executed": self._steps_executed,
            "execution_log": execution_log,
            "final_outputs": step_outputs,
        }

    def stats(self) -> dict:
        return {
            "tasks_completed": self._tasks_completed,
            "tasks_resumed": self._tasks_resumed,
            "steps_skipped": self._steps_skipped,
            "steps_executed": self._steps_executed,
        }


class TaskStepFailedError(Exception):
    def __init__(self, task_id: str, step_id: str, reason: str):
        super().__init__(
            f"task '{task_id}' failed at step '{step_id}': {reason}"
        )
        self.task_id = task_id
        self.step_id = step_id
        self.reason = reason
```

## Solution 4: Task Recovery Manager

```python
import time
from typing import Dict, List, Optional


class TaskRecoveryManager:
    """
    Manages recovery policies for failed tasks.
    Decides whether to resume from checkpoint, restart fully, or abandon.
    """

    def __init__(
        self,
        store: TaskCheckpointStore,
        max_recovery_attempts: int = 3,
        checkpoint_ttl_seconds: float = 3600.0,
    ):
        self._store = store
        self._max_attempts = max_recovery_attempts
        self._ttl = checkpoint_ttl_seconds
        self._recovery_attempts: Dict[str, int] = {}

    def can_recover(self, task_id: str) -> bool:
        attempts = self._recovery_attempts.get(task_id, 0)
        if attempts >= self._max_attempts:
            return False
        completed = self._store.completed_steps(task_id)
        return len(completed) > 0

    def recovery_plan(self, task: MultiStepTask) -> dict:
        completed = self._store.completed_steps(task.task_id)
        remaining = [s.step_id for s in task.steps if s.step_id not in completed]
        attempts = self._recovery_attempts.get(task.task_id, 0)

        return {
            "task_id": task.task_id,
            "completed_steps": completed,
            "remaining_steps": remaining,
            "recovery_attempts": attempts,
            "can_recover": self.can_recover(task.task_id),
            "resume_from": remaining[0] if remaining else None,
        }

    def record_attempt(self, task_id: str) -> None:
        self._recovery_attempts[task_id] = self._recovery_attempts.get(task_id, 0) + 1

    def abandon(self, task_id: str) -> None:
        self._store.clear_task(task_id)
        self._recovery_attempts.pop(task_id, None)
```

## Solution 5: Task Progress Reporter

```python
import time
from typing import List, Optional


class TaskProgressReporter:
    """
    Produces a human-readable progress report for a running or
    partially-completed task based on checkpoint state.
    """

    def __init__(self, store: TaskCheckpointStore):
        self._store = store

    def report(self, task: MultiStepTask) -> dict:
        completed = set(self._store.completed_steps(task.task_id))
        total = len(task.steps)
        n_completed = len(completed)
        pct = round(n_completed / max(total, 1) * 100, 1)

        step_states = []
        for step in task.steps:
            entry = self._store.get_step_result(task.task_id, step.step_id)
            status = entry["status"] if entry else "pending"
            step_states.append({
                "step_id": step.step_id,
                "name": step.name,
                "status": status,
            })

        return {
            "task_id": task.task_id,
            "task_name": task.name,
            "progress_pct": pct,
            "steps_completed": n_completed,
            "steps_total": total,
            "step_states": step_states,
        }
```

## Solution 6: Task Recovery Dashboard

```python
import time


class TaskRecoveryDashboard:
    """
    Combines executor stats, recovery manager state, and task progress
    into a single operational view.
    """

    def __init__(
        self,
        executor: CheckpointAwareTaskExecutor,
        recovery_manager: TaskRecoveryManager,
        reporter: TaskProgressReporter,
    ):
        self._executor = executor
        self._manager = recovery_manager
        self._reporter = reporter

    def render(self, active_tasks: list = None) -> dict:
        active_reports = []
        if active_tasks:
            for task in active_tasks:
                active_reports.append(self._reporter.report(task))

        return {
            "generated_at": time.time(),
            "executor_stats": self._executor.stats(),
            "active_tasks": active_reports,
        }
```

## Comparison

| Approach | Step Checkpointing | Checkpoint Persistence | Resume on Retry | Recovery Policy | Progress Reporting |
|---|---|---|---|---|---|
| TaskCheckpointStore | Yes (per step) | Yes (JSON file) | Via load | No | No |
| CheckpointAwareTaskExecutor | Yes | Via store | Yes (auto skip) | No | No |
| TaskRecoveryManager | No | Via store | Via executor | Yes | No |
| TaskProgressReporter | No | Via store | No | No | Yes |
| TaskRecoveryDashboard | No | No | No | No | Yes (combined) |

**Best for production**: Checkpoint after every step, not after every N steps — the overhead of writing a small JSON object is negligible compared to the cost of re-executing a step that calls an LLM or a slow tool. Use a Redis-backed checkpoint store in multi-instance deployments so that any instance can resume a task started by a different instance. Set `max_recovery_attempts=3` in `TaskRecoveryManager`: after 3 recovery failures the task is systematically broken, not transiently failing, and abandonment with user notification is more appropriate than continued retry.
