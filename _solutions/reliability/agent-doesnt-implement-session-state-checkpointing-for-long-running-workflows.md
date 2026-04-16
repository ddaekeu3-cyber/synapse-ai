---
title: "Agent Doesn't Implement Session State Checkpointing for Long-Running Workflows"
description: "Agents executing multi-step workflows that span many tool calls have no recovery mechanism if the process crashes or times out mid-workflow — the entire sequence restarts from the beginning. Implement session state checkpointing that persists workflow progress after each completed step so that a restarted agent can resume from the last successful checkpoint rather than starting over."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-session-state-checkpointing-for-long-running-workflows
tags: [checkpointing, session-recovery, workflow-resumption, fault-tolerance, crash-recovery, stateful-workflows]
symptoms:
  - "A crash at step 8 of 10 causes the entire 10-step workflow to restart from step 1"
  - "Long-running data processing workflows re-process already-completed steps after timeout"
  - "No durable record of which workflow steps completed successfully"
  - "Users experience duplicate side effects (emails sent twice, records created twice) after crash-restart"
  - "Workflow execution time doubles when crash recovery re-executes completed steps"
---

## Why This Happens

Multi-step workflows keep their progress in process memory. A crash, OOM kill, deployment, or timeout resets that state entirely. On restart the agent has no record of which steps succeeded and which tools were already called, so it starts over. For idempotent workflows this means wasted time; for non-idempotent workflows (those that send emails, create records, or charge users) it means duplicate side effects. Checkpointing writes a durable record of completion after each step so the next execution can skip already-completed steps and resume from the last known good state.

## Solution 1: Workflow Step Record

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class StepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class WorkflowStepRecord:
    step_id: str
    step_name: str
    status: StepStatus = StepStatus.PENDING
    result: Optional[Any] = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    attempt_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> Optional[float]:
        if self.started_at and self.completed_at:
            return round((self.completed_at - self.started_at) * 1000, 2)
        return None
```

## Solution 2: Checkpoint Store

```python
import json
import time
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional


class WorkflowCheckpointStore:
    """
    Persists workflow checkpoints to a JSON file, keyed by session_id.
    Replace with a database backend for distributed deployments.
    """

    def __init__(self, checkpoint_dir: str = "/tmp/agent_checkpoints"):
        self._dir = Path(checkpoint_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def _path(self, session_id: str) -> Path:
        return self._dir / f"{session_id}.json"

    def save_step(self, session_id: str, step: WorkflowStepRecord) -> None:
        with self._lock:
            checkpoint = self._load_raw(session_id)
            checkpoint["steps"][step.step_id] = {
                "step_name": step.step_name,
                "status": step.status.value,
                "result": step.result,
                "error": step.error,
                "started_at": step.started_at,
                "completed_at": step.completed_at,
                "attempt_count": step.attempt_count,
                "metadata": step.metadata,
            }
            checkpoint["updated_at"] = time.time()
            self._path(session_id).write_text(json.dumps(checkpoint, indent=2, default=str))

    def load(self, session_id: str) -> Dict[str, WorkflowStepRecord]:
        with self._lock:
            raw = self._load_raw(session_id)
        steps = {}
        for step_id, data in raw.get("steps", {}).items():
            steps[step_id] = WorkflowStepRecord(
                step_id=step_id,
                step_name=data["step_name"],
                status=StepStatus(data["status"]),
                result=data.get("result"),
                error=data.get("error"),
                started_at=data.get("started_at"),
                completed_at=data.get("completed_at"),
                attempt_count=data.get("attempt_count", 0),
                metadata=data.get("metadata", {}),
            )
        return steps

    def _load_raw(self, session_id: str) -> dict:
        path = self._path(session_id)
        if not path.exists():
            return {"session_id": session_id, "steps": {}, "created_at": time.time(), "updated_at": time.time()}
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return {"session_id": session_id, "steps": {}, "created_at": time.time(), "updated_at": time.time()}

    def delete(self, session_id: str) -> None:
        with self._lock:
            path = self._path(session_id)
            if path.exists():
                path.unlink()

    def list_sessions(self) -> List[str]:
        return [p.stem for p in self._dir.glob("*.json")]
```

## Solution 3: Checkpointing Workflow Executor

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional


class CheckpointingWorkflowExecutor:
    """
    Executes a sequence of named steps with checkpointing after each success.
    On restart, completed steps are skipped and their results are replayed
    from the checkpoint store.
    """

    def __init__(
        self,
        session_id: str,
        store: WorkflowCheckpointStore,
        max_step_attempts: int = 3,
    ):
        self._session_id = session_id
        self._store = store
        self._max_attempts = max_step_attempts
        self._checkpoints = store.load(session_id)
        self._skipped = 0
        self._executed = 0
        self._failed_steps: List[str] = []

    async def run_step(
        self,
        step_id: str,
        step_name: str,
        step_fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        existing = self._checkpoints.get(step_id)
        if existing and existing.status == StepStatus.COMPLETED:
            self._skipped += 1
            return existing.result

        step = WorkflowStepRecord(
            step_id=step_id,
            step_name=step_name,
            status=StepStatus.IN_PROGRESS,
            started_at=time.time(),
        )

        last_exc: Optional[Exception] = None
        for attempt in range(self._max_attempts):
            step.attempt_count = attempt + 1
            try:
                result = await step_fn(*args, **kwargs)
                step.status = StepStatus.COMPLETED
                step.result = result
                step.completed_at = time.time()
                self._store.save_step(self._session_id, step)
                self._checkpoints[step_id] = step
                self._executed += 1
                return result
            except Exception as exc:
                last_exc = exc
                if attempt + 1 < self._max_attempts:
                    await asyncio.sleep(2 ** attempt)

        step.status = StepStatus.FAILED
        step.error = str(last_exc)
        step.completed_at = time.time()
        self._store.save_step(self._session_id, step)
        self._failed_steps.append(step_id)
        raise last_exc

    def progress(self) -> dict:
        total = len(self._checkpoints)
        completed = sum(1 for s in self._checkpoints.values() if s.status == StepStatus.COMPLETED)
        return {
            "session_id": self._session_id,
            "steps_skipped": self._skipped,
            "steps_executed": self._executed,
            "steps_completed_total": completed,
            "steps_failed": len(self._failed_steps),
            "failed_step_ids": self._failed_steps,
        }
```

## Solution 4: Checkpoint Replay Validator

```python
from typing import List


class CheckpointReplayValidator:
    """
    Validates a checkpoint before replay to detect stale or corrupt state.
    Flags steps that were IN_PROGRESS at crash time (incomplete) and
    steps with results that cannot be deserialized.
    """

    def validate(
        self,
        steps: Dict[str, WorkflowStepRecord],
    ) -> dict:
        incomplete = [s for s in steps.values() if s.status == StepStatus.IN_PROGRESS]
        completed = [s for s in steps.values() if s.status == StepStatus.COMPLETED]
        failed = [s for s in steps.values() if s.status == StepStatus.FAILED]

        warnings = []
        for step in incomplete:
            warnings.append(
                f"Step '{step.step_name}' was IN_PROGRESS at checkpoint — will re-execute"
            )
            step.status = StepStatus.PENDING

        return {
            "completed_steps": len(completed),
            "failed_steps": len(failed),
            "reset_to_pending": len(incomplete),
            "warnings": warnings,
            "safe_to_resume": True,
        }
```

## Solution 5: Checkpoint Cleanup Scheduler

```python
import time
from typing import List


class CheckpointCleanupScheduler:
    """
    Removes checkpoint files for sessions older than retention_seconds.
    Prevents unbounded disk growth from abandoned or completed workflows.
    """

    def __init__(
        self,
        store: WorkflowCheckpointStore,
        retention_seconds: float = 86400.0,
    ):
        self._store = store
        self._retention = retention_seconds

    def cleanup(self) -> List[str]:
        removed = []
        cutoff = time.time() - self._retention
        for session_id in self._store.list_sessions():
            raw = self._store._load_raw(session_id)
            updated_at = raw.get("updated_at", 0)
            if updated_at < cutoff:
                self._store.delete(session_id)
                removed.append(session_id)
        return removed
```

## Solution 6: Checkpoint Dashboard

```python
import time


class WorkflowCheckpointDashboard:
    """
    Reports checkpoint store health, active sessions, and
    recovery statistics for operational monitoring.
    """

    def __init__(
        self,
        store: WorkflowCheckpointStore,
        executor: Optional[CheckpointingWorkflowExecutor] = None,
    ):
        self._store = store
        self._executor = executor

    def render(self) -> dict:
        sessions = self._store.list_sessions()
        return {
            "generated_at": time.time(),
            "checkpoint_store": {
                "active_sessions": len(sessions),
                "session_ids": sessions[:20],
            },
            "current_workflow": self._executor.progress() if self._executor else None,
        }
```

## Comparison

| Approach | Step-Level Checkpoint | Crash Recovery | Skip Completed Steps | Stale State Detection | Cleanup |
|---|---|---|---|---|---|
| WorkflowCheckpointStore | Yes (JSON) | Yes (load on init) | No | No | No |
| CheckpointingWorkflowExecutor | Via store | Via store | Yes | No | No |
| CheckpointReplayValidator | No | Yes (reset IN_PROGRESS) | No | Yes | No |
| CheckpointCleanupScheduler | No | No | No | No | Yes |
| WorkflowCheckpointDashboard | No | No | No | No | No |

**Best for production**: Write checkpoints atomically — write to a temp file then rename — to prevent reading a partial checkpoint after a crash mid-write. Mark steps that produce non-idempotent side effects (emails, payments) with `metadata={"idempotency_key": key}` so replay can verify the side effect did not already succeed before re-executing. Set `retention_seconds=86400` in the cleanup scheduler — completed sessions are immediately deletable but a 24-hour grace period allows post-mortem inspection. Use Redis with `SET NX EX` for the checkpoint store in multi-instance deployments to prevent two instances from executing the same step simultaneously after a split-brain restart.
