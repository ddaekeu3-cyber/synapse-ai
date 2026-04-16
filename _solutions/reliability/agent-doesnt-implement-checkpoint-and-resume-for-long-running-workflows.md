---
title: "Agent Doesn't Implement Checkpoint and Resume for Long-Running Workflows"
description: "Agents that execute multi-step workflows without checkpointing lose all progress if the process crashes, times out, or is killed mid-execution. A 30-step research workflow that fails at step 27 must restart from scratch. Implement checkpointing that persists workflow state after each successful step and resume logic that restores the checkpoint and continues from the last completed step on restart."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-checkpoint-and-resume-for-long-running-workflows
tags: [checkpoint, resume, workflow-persistence, crash-recovery, idempotency, long-running-tasks]
symptoms:
  - "Long workflows restart from the beginning after any process interruption"
  - "No persisted state between workflow steps — all progress is in memory only"
  - "A deployment or OOM kill loses hours of multi-step agent work"
  - "Cannot resume a workflow after a transient API failure at step N"
  - "Duplicate side effects when a retried workflow re-executes already-completed steps"
---

## Why This Happens

Long-running agent workflows are stateful sequences of tool calls, LLM calls, and decisions. When the process dies, all in-memory state is lost. Without checkpoints, the only recovery option is a full restart. Checkpointing requires serializing enough state after each step to reconstruct the workflow at that point: completed step results, accumulated context, and the position in the execution plan. Resume logic reads the checkpoint, skips completed steps (preventing duplicate side effects), and continues from the first incomplete step.

## Solution 1: Workflow Checkpoint

```python
import json
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
class StepRecord:
    step_id: str
    step_name: str
    status: StepStatus = StepStatus.PENDING
    result: Optional[Any] = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    attempt_count: int = 0

    def duration_ms(self) -> Optional[float]:
        if self.started_at and self.completed_at:
            return round((self.completed_at - self.started_at) * 1000, 2)
        return None


@dataclass
class WorkflowCheckpoint:
    workflow_id: str
    workflow_name: str
    created_at: float
    updated_at: float
    steps: Dict[str, StepRecord] = field(default_factory=dict)
    step_order: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    completed: bool = False
    final_result: Optional[Any] = None

    def next_pending_step(self) -> Optional[StepRecord]:
        for step_id in self.step_order:
            record = self.steps.get(step_id)
            if record and record.status == StepStatus.PENDING:
                return record
        return None

    def all_completed(self) -> bool:
        return all(
            s.status in (StepStatus.COMPLETED, StepStatus.SKIPPED)
            for s in self.steps.values()
        )

    def completed_results(self) -> Dict[str, Any]:
        return {
            step_id: record.result
            for step_id, record in self.steps.items()
            if record.status == StepStatus.COMPLETED
        }
```

## Solution 2: Checkpoint Store

```python
import json
import os
from pathlib import Path
from threading import Lock
from typing import Optional


class CheckpointStore:
    """
    Persists workflow checkpoints to a local directory.
    Replace with Redis or a database for distributed deployments.
    """

    def __init__(self, directory: str = "/tmp/agent_checkpoints"):
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def _path(self, workflow_id: str) -> Path:
        return self._dir / f"{workflow_id}.json"

    def save(self, checkpoint: WorkflowCheckpoint) -> None:
        checkpoint.updated_at = time.time()
        data = {
            "workflow_id": checkpoint.workflow_id,
            "workflow_name": checkpoint.workflow_name,
            "created_at": checkpoint.created_at,
            "updated_at": checkpoint.updated_at,
            "step_order": checkpoint.step_order,
            "completed": checkpoint.completed,
            "final_result": checkpoint.final_result,
            "metadata": checkpoint.metadata,
            "steps": {
                sid: {
                    "step_id": s.step_id,
                    "step_name": s.step_name,
                    "status": s.status.value,
                    "result": s.result,
                    "error": s.error,
                    "started_at": s.started_at,
                    "completed_at": s.completed_at,
                    "attempt_count": s.attempt_count,
                }
                for sid, s in checkpoint.steps.items()
            },
        }
        with self._lock:
            self._path(checkpoint.workflow_id).write_text(json.dumps(data, indent=2, default=str))

    def load(self, workflow_id: str) -> Optional[WorkflowCheckpoint]:
        path = self._path(workflow_id)
        if not path.exists():
            return None
        with self._lock:
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                return None

        checkpoint = WorkflowCheckpoint(
            workflow_id=data["workflow_id"],
            workflow_name=data["workflow_name"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            step_order=data.get("step_order", []),
            completed=data.get("completed", False),
            final_result=data.get("final_result"),
            metadata=data.get("metadata", {}),
        )
        for sid, s in data.get("steps", {}).items():
            checkpoint.steps[sid] = StepRecord(
                step_id=s["step_id"],
                step_name=s["step_name"],
                status=StepStatus(s["status"]),
                result=s.get("result"),
                error=s.get("error"),
                started_at=s.get("started_at"),
                completed_at=s.get("completed_at"),
                attempt_count=s.get("attempt_count", 0),
            )
        return checkpoint

    def delete(self, workflow_id: str) -> None:
        path = self._path(workflow_id)
        with self._lock:
            if path.exists():
                path.unlink()
```

## Solution 3: Checkpointed Workflow Executor

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional


WorkflowStep = tuple  # (step_id, step_name, async_fn)


class CheckpointedWorkflowExecutor:
    """
    Executes a list of workflow steps with checkpointing after each step.
    On resume, completed steps are skipped using their persisted results.
    """

    def __init__(
        self,
        store: CheckpointStore,
        max_step_retries: int = 2,
    ):
        self._store = store
        self._max_retries = max_step_retries

    async def execute(
        self,
        workflow_id: str,
        workflow_name: str,
        steps: List[WorkflowStep],
        resume: bool = True,
    ) -> WorkflowCheckpoint:
        # Load or create checkpoint
        checkpoint = None
        if resume:
            checkpoint = self._store.load(workflow_id)

        if checkpoint is None:
            checkpoint = WorkflowCheckpoint(
                workflow_id=workflow_id,
                workflow_name=workflow_name,
                created_at=time.time(),
                updated_at=time.time(),
            )
            for step_id, step_name, _ in steps:
                checkpoint.step_order.append(step_id)
                checkpoint.steps[step_id] = StepRecord(
                    step_id=step_id,
                    step_name=step_name,
                )
            self._store.save(checkpoint)

        # Execute each step, skipping completed ones
        accumulated_results = checkpoint.completed_results()

        for step_id, step_name, step_fn in steps:
            record = checkpoint.steps.get(step_id)
            if record is None:
                continue
            if record.status == StepStatus.COMPLETED:
                continue  # skip — already done

            record.status = StepStatus.RUNNING
            record.started_at = time.time()
            record.attempt_count += 1
            self._store.save(checkpoint)

            for attempt in range(self._max_retries + 1):
                try:
                    result = await step_fn(accumulated_results)
                    record.result = result
                    record.status = StepStatus.COMPLETED
                    record.completed_at = time.time()
                    accumulated_results[step_id] = result
                    self._store.save(checkpoint)
                    break
                except Exception as exc:
                    if attempt < self._max_retries:
                        await asyncio.sleep(1.0 * (attempt + 1))
                        record.attempt_count += 1
                    else:
                        record.status = StepStatus.FAILED
                        record.error = str(exc)
                        record.completed_at = time.time()
                        self._store.save(checkpoint)
                        raise WorkflowStepFailed(step_id=step_id, cause=exc) from exc

        checkpoint.completed = True
        checkpoint.final_result = accumulated_results
        self._store.save(checkpoint)
        return checkpoint


class WorkflowStepFailed(Exception):
    def __init__(self, step_id: str, cause: Exception):
        super().__init__(f"workflow step '{step_id}' failed: {cause}")
        self.step_id = step_id
        self.cause = cause
```

## Solution 4: Checkpoint Recovery Advisor

```python
import time
from typing import List


class CheckpointRecoveryAdvisor:
    """
    Analyzes a checkpoint to recommend recovery actions:
    resume, restart, or manual intervention.
    """

    def advise(self, checkpoint: WorkflowCheckpoint) -> dict:
        if checkpoint.completed:
            return {"action": "none", "reason": "workflow already completed"}

        failed_steps = [
            s for s in checkpoint.steps.values()
            if s.status == StepStatus.FAILED
        ]
        pending_steps = [
            s for s in checkpoint.steps.values()
            if s.status == StepStatus.PENDING
        ]
        completed_count = sum(
            1 for s in checkpoint.steps.values()
            if s.status == StepStatus.COMPLETED
        )

        if not failed_steps and pending_steps:
            return {
                "action": "resume",
                "reason": f"{completed_count} steps completed, {len(pending_steps)} pending",
                "next_step": pending_steps[0].step_name if pending_steps else None,
            }

        if failed_steps:
            high_attempt = [s for s in failed_steps if s.attempt_count >= 3]
            if high_attempt:
                return {
                    "action": "manual_intervention",
                    "reason": f"{len(high_attempt)} step(s) failed 3+ times",
                    "failed_steps": [s.step_name for s in high_attempt],
                }
            return {
                "action": "resume_with_retry",
                "reason": f"{len(failed_steps)} step(s) failed, retry recommended",
                "failed_steps": [s.step_name for s in failed_steps],
            }

        return {"action": "restart", "reason": "no recoverable state found"}
```

## Solution 5: Workflow Progress Reporter

```python
import time


class WorkflowProgressReporter:
    """
    Produces a human-readable progress report for a workflow checkpoint.
    """

    def report(self, checkpoint: WorkflowCheckpoint) -> dict:
        steps = [checkpoint.steps[sid] for sid in checkpoint.step_order if sid in checkpoint.steps]
        completed = [s for s in steps if s.status == StepStatus.COMPLETED]
        failed = [s for s in steps if s.status == StepStatus.FAILED]
        pending = [s for s in steps if s.status == StepStatus.PENDING]
        running = [s for s in steps if s.status == StepStatus.RUNNING]

        return {
            "workflow_id": checkpoint.workflow_id,
            "workflow_name": checkpoint.workflow_name,
            "progress_pct": round(len(completed) / max(len(steps), 1) * 100, 1),
            "completed": len(completed),
            "failed": len(failed),
            "pending": len(pending),
            "running": len(running),
            "total": len(steps),
            "is_done": checkpoint.completed,
            "age_seconds": round(time.time() - checkpoint.created_at, 1),
            "last_updated_seconds_ago": round(time.time() - checkpoint.updated_at, 1),
        }
```

## Solution 6: Checkpoint Dashboard

```python
import time


class CheckpointDashboard:
    """
    Combines progress reporting and recovery advice for a live workflow.
    """

    def __init__(
        self,
        store: CheckpointStore,
        reporter: WorkflowProgressReporter,
        advisor: CheckpointRecoveryAdvisor,
    ):
        self._store = store
        self._reporter = reporter
        self._advisor = advisor

    def render(self, workflow_id: str) -> dict:
        checkpoint = self._store.load(workflow_id)
        if checkpoint is None:
            return {"workflow_id": workflow_id, "status": "not_found"}
        return {
            "generated_at": time.time(),
            "progress": self._reporter.report(checkpoint),
            "recovery_advice": self._advisor.advise(checkpoint),
        }
```

## Comparison

| Approach | Step Tracking | Persistence | Skip Completed | Recovery Advice | Dashboard |
|---|---|---|---|---|---|
| WorkflowCheckpoint | Yes | No | Via status | No | No |
| CheckpointStore | Via checkpoint | Yes (JSON file) | No | No | No |
| CheckpointedWorkflowExecutor | Via checkpoint | Via store | Yes | No | No |
| CheckpointRecoveryAdvisor | Via checkpoint | No | No | Yes | No |
| WorkflowProgressReporter | Via checkpoint | No | No | No | No |
| CheckpointDashboard | No | No | No | No | Yes |

**Best for production**: Store checkpoints in Redis with a TTL of 7 days — this covers all realistic retry windows while automatically cleaning up abandoned workflows. Make every step function idempotent — the executor retries failed steps, so a step that partially succeeded must be safe to re-run. Use `CheckpointRecoveryAdvisor` in your alerting pipeline: a `manual_intervention` recommendation should page the on-call engineer with the workflow ID and failed step names. Include the workflow ID in every log event emitted during execution so that post-incident analysis can reconstruct the full execution timeline from logs.
