---
title: "Agent Doesn't Implement State Machine for Multi-Step Workflows"
description: "Agents that execute multi-step workflows (data collection → validation → processing → delivery) without a formal state machine have no recovery path when a step fails mid-way — they either restart from scratch, skip failed steps silently, or enter undefined intermediate states that are impossible to debug. Implement an explicit state machine that models each workflow step as a named state, persists the current state durably, and supports resumption from the last successful state after failure."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-state-machine-for-multi-step-workflows
tags: [state-machine, workflow, multi-step, resumable, fault-tolerance, workflow-recovery]
symptoms:
  - "When step 3 of a 5-step workflow fails, the agent restarts from step 1"
  - "No record of which steps completed before a crash — must re-run everything"
  - "Multi-step workflow silently skips a failed intermediate step and produces partial output"
  - "Cannot pause a long-running workflow and resume it after a system restart"
  - "Workflow state is held only in memory — a process kill loses all progress"
---

## Why This Happens

Multi-step workflows implemented as sequential function calls have no durable state between steps. If the process crashes or a step fails, there is no record of what completed and what did not. Re-running the workflow from the beginning risks double-executing side-effectful steps (sending an email that was already sent) or wasting time redoing expensive steps (re-embedding 10,000 documents). A state machine gives each step a name, persists transitions durably, and provides a clear recovery protocol: on restart, read the persisted state, skip completed steps, and resume from the first incomplete step.

## Solution 1: Workflow State Model

```python
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
class WorkflowStep:
    name: str
    status: StepStatus = StepStatus.PENDING
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    result: Any = None
    error: Optional[str] = None
    attempt_count: int = 0
    max_attempts: int = 3


@dataclass
class WorkflowState:
    workflow_id: str
    workflow_name: str
    steps: List[WorkflowStep]
    current_step_index: int = 0
    started_at: float = field(default_factory=__import__("time").time)
    finished_at: Optional[float] = None
    final_status: Optional[str] = None
    context: Dict[str, Any] = field(default_factory=dict)

    def current_step(self) -> Optional[WorkflowStep]:
        if 0 <= self.current_step_index < len(self.steps):
            return self.steps[self.current_step_index]
        return None

    def is_complete(self) -> bool:
        return self.current_step_index >= len(self.steps)

    def completed_steps(self) -> List[WorkflowStep]:
        return [s for s in self.steps if s.status == StepStatus.COMPLETED]
```

## Solution 2: Workflow State Persistence

```python
import json
import time
from pathlib import Path
from threading import Lock
from typing import Optional


class WorkflowStatePersistence:
    """
    Persists workflow state to a local JSON store.
    Each workflow_id maps to its full state snapshot.
    """

    def __init__(self, store_path: str = "/tmp/workflow_states.json"):
        self._path = Path(store_path)
        self._lock = Lock()

    def save(self, state: WorkflowState) -> None:
        with self._lock:
            all_states = self._load_all()
            all_states[state.workflow_id] = self._serialize(state)
            self._path.write_text(json.dumps(all_states, indent=2))

    def load(self, workflow_id: str) -> Optional[WorkflowState]:
        with self._lock:
            all_states = self._load_all()
            data = all_states.get(workflow_id)
            if not data:
                return None
            return self._deserialize(data)

    def _load_all(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    @staticmethod
    def _serialize(state: WorkflowState) -> dict:
        return {
            "workflow_id": state.workflow_id,
            "workflow_name": state.workflow_name,
            "current_step_index": state.current_step_index,
            "started_at": state.started_at,
            "finished_at": state.finished_at,
            "final_status": state.final_status,
            "context": state.context,
            "steps": [
                {
                    "name": s.name,
                    "status": s.status.value,
                    "started_at": s.started_at,
                    "finished_at": s.finished_at,
                    "result": s.result,
                    "error": s.error,
                    "attempt_count": s.attempt_count,
                    "max_attempts": s.max_attempts,
                }
                for s in state.steps
            ],
        }

    @staticmethod
    def _deserialize(data: dict) -> WorkflowState:
        steps = [
            WorkflowStep(
                name=s["name"],
                status=StepStatus(s["status"]),
                started_at=s.get("started_at"),
                finished_at=s.get("finished_at"),
                result=s.get("result"),
                error=s.get("error"),
                attempt_count=s.get("attempt_count", 0),
                max_attempts=s.get("max_attempts", 3),
            )
            for s in data.get("steps", [])
        ]
        return WorkflowState(
            workflow_id=data["workflow_id"],
            workflow_name=data["workflow_name"],
            steps=steps,
            current_step_index=data.get("current_step_index", 0),
            started_at=data.get("started_at", time.time()),
            finished_at=data.get("finished_at"),
            final_status=data.get("final_status"),
            context=data.get("context", {}),
        )
```

## Solution 3: State Machine Executor

```python
import asyncio
import time
from typing import Any, Callable, Dict


class WorkflowStateMachineExecutor:
    """
    Executes a workflow by advancing through steps in order.
    On each step: check if already completed (resume path), execute,
    persist, advance to next. Handles per-step retry with max_attempts.
    """

    def __init__(
        self,
        persistence: WorkflowStatePersistence,
        step_fns: Dict[str, Callable],
    ):
        self._persistence = persistence
        self._step_fns = step_fns

    async def run(self, state: WorkflowState) -> WorkflowState:
        self._persistence.save(state)

        while not state.is_complete():
            step = state.current_step()
            if step is None:
                break

            # Resume: skip already-completed steps
            if step.status == StepStatus.COMPLETED:
                state.current_step_index += 1
                continue

            # Skip steps that exceeded max attempts
            if step.attempt_count >= step.max_attempts and step.status == StepStatus.FAILED:
                step.status = StepStatus.SKIPPED
                state.current_step_index += 1
                self._persistence.save(state)
                continue

            step_fn = self._step_fns.get(step.name)
            if step_fn is None:
                step.status = StepStatus.FAILED
                step.error = f"no handler registered for step '{step.name}'"
                state.current_step_index += 1
                self._persistence.save(state)
                continue

            step.status = StepStatus.RUNNING
            step.started_at = time.time()
            step.attempt_count += 1
            self._persistence.save(state)

            try:
                result = await step_fn(state.context)
                step.result = result
                step.status = StepStatus.COMPLETED
                step.finished_at = time.time()
                if isinstance(result, dict):
                    state.context.update(result)
                state.current_step_index += 1
            except Exception as exc:
                step.error = str(exc)
                step.finished_at = time.time()
                if step.attempt_count < step.max_attempts:
                    step.status = StepStatus.PENDING  # will retry
                else:
                    step.status = StepStatus.FAILED
                    state.current_step_index += 1

            self._persistence.save(state)

        state.finished_at = time.time()
        failed = sum(1 for s in state.steps if s.status == StepStatus.FAILED)
        state.final_status = "failed" if failed > 0 else "completed"
        self._persistence.save(state)
        return state
```

## Solution 4: Workflow Factory

```python
import uuid


class WorkflowFactory:
    """
    Creates WorkflowState instances from named step definitions.
    Supports resuming an existing workflow from persisted state.
    """

    def __init__(self, persistence: WorkflowStatePersistence):
        self._persistence = persistence

    def create(
        self,
        workflow_name: str,
        step_names: list,
        context: dict = None,
        max_attempts_per_step: int = 3,
    ) -> WorkflowState:
        workflow_id = str(uuid.uuid4())
        steps = [
            WorkflowStep(name=name, max_attempts=max_attempts_per_step)
            for name in step_names
        ]
        return WorkflowState(
            workflow_id=workflow_id,
            workflow_name=workflow_name,
            steps=steps,
            context=context or {},
        )

    def resume(self, workflow_id: str) -> WorkflowState:
        state = self._persistence.load(workflow_id)
        if state is None:
            raise ValueError(f"Workflow {workflow_id} not found in persistence store")
        return state
```

## Solution 5: Workflow Progress Reporter

```python
import time
from typing import List


class WorkflowProgressReporter:
    """
    Generates a human-readable progress report for a workflow state.
    """

    def report(self, state: WorkflowState) -> dict:
        total = len(state.steps)
        completed = sum(1 for s in state.steps if s.status == StepStatus.COMPLETED)
        failed = sum(1 for s in state.steps if s.status == StepStatus.FAILED)
        running = sum(1 for s in state.steps if s.status == StepStatus.RUNNING)

        elapsed = time.time() - state.started_at
        pct = round(completed / max(total, 1) * 100, 1)

        return {
            "workflow_id": state.workflow_id,
            "workflow_name": state.workflow_name,
            "progress_pct": pct,
            "steps_completed": completed,
            "steps_failed": failed,
            "steps_running": running,
            "steps_total": total,
            "elapsed_seconds": round(elapsed, 1),
            "final_status": state.final_status,
            "current_step": state.current_step().name if state.current_step() else None,
            "step_details": [
                {
                    "name": s.name,
                    "status": s.status.value,
                    "attempt_count": s.attempt_count,
                    "error": s.error,
                }
                for s in state.steps
            ],
        }
```

## Solution 6: Workflow Registry Dashboard

```python
import time


class WorkflowRegistryDashboard:
    """
    Scans the persistence store to report on all known workflow
    states — active, completed, and failed.
    """

    def __init__(self, persistence: WorkflowStatePersistence):
        self._persistence = persistence

    def render(self) -> dict:
        all_data = self._persistence._load_all()
        total = len(all_data)
        completed = sum(1 for d in all_data.values() if d.get("final_status") == "completed")
        failed = sum(1 for d in all_data.values() if d.get("final_status") == "failed")
        active = sum(1 for d in all_data.values() if d.get("final_status") is None)

        return {
            "generated_at": time.time(),
            "total_workflows": total,
            "completed": completed,
            "failed": failed,
            "active": active,
            "success_rate": round(completed / max(total, 1), 4),
        }
```

## Comparison

| Approach | Durable State | Step Retry | Resume on Restart | Progress Reporting | Registry View |
|---|---|---|---|---|---|
| WorkflowStatePersistence | Yes (JSON/file) | No | Yes (via load) | No | No |
| WorkflowStateMachineExecutor | Via persistence | Yes (max_attempts) | Yes (skip completed) | No | No |
| WorkflowFactory | No | No | Yes (resume()) | No | No |
| WorkflowProgressReporter | No | No | No | Yes | No |
| WorkflowRegistryDashboard | No | No | No | No | Yes |

**Best for production**: Use Redis or a database as the persistence backend for multi-instance deployments — file-based state is not safe for concurrent access from multiple agent replicas running the same workflow. Persist state after every step transition, not just at the end — this ensures recovery from a crash between any two steps. Pass results between steps via `state.context` rather than function arguments so that the context is always available after a resume. Set `max_attempts_per_step=3` for external API steps and 1 for idempotency-sensitive steps (email sends, payment triggers) where retrying after an unclear failure is dangerous.
