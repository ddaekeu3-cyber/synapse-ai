---
title: "Agent Doesn't Implement Checkpoint Recovery for Long-Running Workflows"
description: "Agents executing multi-step workflows lose all progress when a failure occurs mid-execution: a network error on step 8 of 12 restarts the entire sequence from step 1. Implement checkpoint recovery that persists workflow state after each completed step, detects prior partial runs on startup, and resumes from the last successful checkpoint rather than restarting from scratch."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-checkpoint-recovery-for-long-running-workflows
tags: [checkpoint-recovery, workflow-resumption, fault-tolerance, persistent-state, partial-failure, idempotency]
symptoms:
  - "A 20-minute workflow restarts from step 1 after a transient network error on step 15"
  - "Expensive LLM calls in early steps are re-executed on every retry"
  - "No record of which steps completed — impossible to know what was already done"
  - "Parallel sub-workflows lose their results when one branch fails"
  - "Users waiting for long-running jobs get no partial results on failure"
---

## Why This Happens

Without checkpointing, a workflow is a stateless sequence: each run is independent and produces no durable artifacts until it completes. A failure at any point discards all work. Checkpointing inserts a persistence call after each successful step, writing the step output and metadata to a store. On restart, the workflow engine reads the checkpoint, identifies the last completed step, and resumes from the next one. This requires each step to be idempotent (safe to skip if already done) and each step output to be serializable.

## Solution 1: Checkpoint Record

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class StepCheckpoint:
    step_id: str
    step_name: str
    status: StepStatus
    result: Any = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    attempt_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def duration_seconds(self) -> Optional[float]:
        if self.started_at and self.completed_at:
            return round(self.completed_at - self.started_at, 3)
        return None


@dataclass
class WorkflowCheckpoint:
    workflow_id: str
    workflow_name: str
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    steps: Dict[str, StepCheckpoint] = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)   # shared workflow context
    completed: bool = False
    failed: bool = False
    resume_from_step: Optional[str] = None

    def last_completed_step(self) -> Optional[str]:
        completed = [
            s for s in self.steps.values()
            if s.status == StepStatus.COMPLETED
        ]
        if not completed:
            return None
        return max(completed, key=lambda s: s.completed_at or 0).step_id

    def is_step_done(self, step_id: str) -> bool:
        step = self.steps.get(step_id)
        return step is not None and step.status == StepStatus.COMPLETED
```

## Solution 2: Checkpoint Store

```python
import json
import os
import threading
from typing import Dict, Optional


class CheckpointStore:
    """
    Persists workflow checkpoints to a JSON file per workflow ID.
    In production, replace with a database or distributed store.
    """

    def __init__(self, storage_dir: str = "/tmp/agent_checkpoints"):
        self._dir = storage_dir
        os.makedirs(storage_dir, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, workflow_id: str) -> str:
        safe_id = workflow_id.replace("/", "_")
        return os.path.join(self._dir, f"{safe_id}.json")

    def save(self, checkpoint: WorkflowCheckpoint) -> None:
        checkpoint.updated_at = time.time()
        data = self._serialize(checkpoint)
        path = self._path(checkpoint.workflow_id)
        with self._lock:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2, default=str)
            os.replace(tmp, path)   # atomic rename

    def load(self, workflow_id: str) -> Optional[WorkflowCheckpoint]:
        path = self._path(workflow_id)
        if not os.path.exists(path):
            return None
        with open(path) as f:
            data = json.load(f)
        return self._deserialize(data)

    def delete(self, workflow_id: str) -> None:
        path = self._path(workflow_id)
        if os.path.exists(path):
            os.remove(path)

    def list_active(self) -> list:
        results = []
        for fname in os.listdir(self._dir):
            if fname.endswith(".json"):
                wf_id = fname[:-5]
                cp = self.load(wf_id)
                if cp and not cp.completed and not cp.failed:
                    results.append({"workflow_id": wf_id, "updated_at": cp.updated_at})
        return results

    def _serialize(self, cp: WorkflowCheckpoint) -> dict:
        steps = {
            sid: {
                "step_id": s.step_id,
                "step_name": s.step_name,
                "status": s.status,
                "result": s.result,
                "error": s.error,
                "started_at": s.started_at,
                "completed_at": s.completed_at,
                "attempt_count": s.attempt_count,
                "metadata": s.metadata,
            }
            for sid, s in cp.steps.items()
        }
        return {
            "workflow_id": cp.workflow_id,
            "workflow_name": cp.workflow_name,
            "created_at": cp.created_at,
            "updated_at": cp.updated_at,
            "steps": steps,
            "context": cp.context,
            "completed": cp.completed,
            "failed": cp.failed,
            "resume_from_step": cp.resume_from_step,
        }

    def _deserialize(self, data: dict) -> WorkflowCheckpoint:
        steps = {
            sid: StepCheckpoint(**sv)
            for sid, sv in data.get("steps", {}).items()
        }
        return WorkflowCheckpoint(
            workflow_id=data["workflow_id"],
            workflow_name=data["workflow_name"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            steps=steps,
            context=data.get("context", {}),
            completed=data.get("completed", False),
            failed=data.get("failed", False),
            resume_from_step=data.get("resume_from_step"),
        )
```

## Solution 3: Checkpointing Workflow Step

```python
import asyncio
import time
from typing import Any, Callable, Optional


class CheckpointingWorkflowStep:
    """
    Wraps a workflow step function with checkpoint read/write logic.
    If the step is already completed in the checkpoint, returns the
    cached result immediately (skip). Otherwise executes and saves.
    """

    def __init__(self, store: CheckpointStore):
        self._store = store

    async def execute(
        self,
        checkpoint: WorkflowCheckpoint,
        step_id: str,
        step_name: str,
        step_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        # Skip if already completed
        if checkpoint.is_step_done(step_id):
            cached = checkpoint.steps[step_id].result
            return cached

        step = StepCheckpoint(
            step_id=step_id,
            step_name=step_name,
            status=StepStatus.RUNNING,
            started_at=time.time(),
        )
        checkpoint.steps[step_id] = step
        step.attempt_count += 1
        self._store.save(checkpoint)

        try:
            result = await step_fn(*args, **kwargs)
            step.status = StepStatus.COMPLETED
            step.result = result
            step.completed_at = time.time()
            self._store.save(checkpoint)
            return result
        except Exception as exc:
            step.status = StepStatus.FAILED
            step.error = str(exc)[:500]
            step.completed_at = time.time()
            checkpoint.failed = True
            self._store.save(checkpoint)
            raise
```

## Solution 4: Recoverable Workflow Runner

```python
import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass
class WorkflowStepSpec:
    step_id: str
    step_name: str
    fn: Callable
    args: tuple = ()
    kwargs: dict = None

    def __post_init__(self):
        if self.kwargs is None:
            self.kwargs = {}


class RecoverableWorkflowRunner:
    """
    Executes a sequence of WorkflowStepSpecs with automatic checkpointing.
    On restart with an existing workflow_id, resumes from the last completed step.
    Passes the shared checkpoint context to each step so steps can share state.
    """

    def __init__(self, store: CheckpointStore):
        self._store = store
        self._step_executor = CheckpointingWorkflowStep(store)

    async def run(
        self,
        workflow_id: str,
        workflow_name: str,
        steps: List[WorkflowStepSpec],
        initial_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        checkpoint = self._store.load(workflow_id)
        is_resume = checkpoint is not None

        if checkpoint is None:
            checkpoint = WorkflowCheckpoint(
                workflow_id=workflow_id,
                workflow_name=workflow_name,
                context=initial_context or {},
            )
            self._store.save(checkpoint)

        results: Dict[str, Any] = {}
        for spec in steps:
            if checkpoint.failed:
                break

            result = await self._step_executor.execute(
                checkpoint=checkpoint,
                step_id=spec.step_id,
                step_name=spec.step_name,
                step_fn=spec.fn,
                *spec.args,
                **{**spec.kwargs, "_context": checkpoint.context},
            )
            results[spec.step_id] = result

        if not checkpoint.failed:
            checkpoint.completed = True
            self._store.save(checkpoint)

        return {
            "workflow_id": workflow_id,
            "resumed": is_resume,
            "completed": checkpoint.completed,
            "failed": checkpoint.failed,
            "results": results,
            "step_count": len(steps),
            "steps_executed": sum(
                1 for s in checkpoint.steps.values()
                if s.status == StepStatus.COMPLETED
            ),
        }
```

## Solution 5: Checkpoint Inspector

```python
import time


class CheckpointInspector:
    """
    Provides human-readable views of checkpoint state for debugging
    and operational visibility.
    """

    def __init__(self, store: CheckpointStore):
        self._store = store

    def status(self, workflow_id: str) -> Optional[dict]:
        cp = self._store.load(workflow_id)
        if cp is None:
            return None

        steps_summary = []
        for step in cp.steps.values():
            steps_summary.append({
                "step_id": step.step_id,
                "step_name": step.step_name,
                "status": step.status,
                "duration_s": step.duration_seconds(),
                "attempt_count": step.attempt_count,
                "error": step.error,
            })

        completed_steps = [s for s in cp.steps.values() if s.status == StepStatus.COMPLETED]
        total_duration = sum(
            (s.duration_seconds() or 0) for s in completed_steps
        )

        return {
            "workflow_id": cp.workflow_id,
            "workflow_name": cp.workflow_name,
            "completed": cp.completed,
            "failed": cp.failed,
            "age_seconds": round(time.time() - cp.created_at, 1),
            "steps_completed": len(completed_steps),
            "total_steps": len(cp.steps),
            "total_duration_s": round(total_duration, 3),
            "last_completed_step": cp.last_completed_step(),
            "steps": steps_summary,
        }

    def stale_workflows(self, older_than_seconds: float = 3600.0) -> list:
        active = self._store.list_active()
        cutoff = time.time() - older_than_seconds
        return [w for w in active if w["updated_at"] < cutoff]
```

## Solution 6: Checkpoint Dashboard

```python
import time


class CheckpointDashboard:
    """Aggregates checkpoint health across all active workflows."""

    def __init__(self, store: CheckpointStore, inspector: CheckpointInspector):
        self._store = store
        self._inspector = inspector

    def render(self) -> dict:
        active = self._store.list_active()
        stale = self._inspector.stale_workflows(older_than_seconds=3600.0)
        alerts = []
        if stale:
            alerts.append({
                "type": "stale_workflows",
                "severity": "warning",
                "count": len(stale),
                "message": f"{len(stale)} workflows have not progressed in over 1 hour.",
            })
        return {
            "generated_at": time.time(),
            "active_workflows": len(active),
            "stale_workflows": len(stale),
            "alerts": alerts,
            "healthy": len(alerts) == 0,
        }
```

## Comparison

| Approach | Step Skip on Resume | Persistent State | Atomic Writes | Progress Visibility | Stale Detection |
|---|---|---|---|---|---|
| CheckpointStore | No | Yes (JSON file) | Yes (atomic rename) | No | No |
| CheckpointingWorkflowStep | Yes | Via store | Via store | No | No |
| RecoverableWorkflowRunner | Yes | Via store | Via store | Yes | No |
| CheckpointInspector | No | No | No | Yes | Yes |
| CheckpointDashboard | No | No | No | No | Yes |

**Best for production**: Use `workflow_id` derived deterministically from the input (e.g., SHA-256 of the user request + timestamp) so restarts naturally find the existing checkpoint. Mark each step function idempotent — if a step creates an external resource (sends an email, creates a DB row), check for existence before creating. Use atomic file rename in `CheckpointStore` to prevent checkpoint corruption from mid-write crashes. Monitor `CheckpointInspector.stale_workflows()` — a workflow stuck without progress for more than 2× its expected duration indicates either a deadlock or a failed step that did not update the checkpoint.
