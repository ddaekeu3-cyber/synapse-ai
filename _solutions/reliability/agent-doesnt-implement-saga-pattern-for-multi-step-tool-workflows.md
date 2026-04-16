---
title: "Agent Doesn't Implement Saga Pattern for Multi-Step Tool Workflows"
description: "Agents that execute multi-step tool workflows without compensation logic leave systems in inconsistent states when a later step fails: a booking is created, a payment is charged, but the confirmation email fails — and there is no rollback. Implement the saga pattern that defines compensating transactions for each step, executes them in reverse order on failure, and records saga state to survive process restarts mid-execution."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-saga-pattern-for-multi-step-tool-workflows
tags: [saga-pattern, compensation, rollback, distributed-transactions, multi-step-workflow, consistency]
symptoms:
  - "Payment charged but booking not confirmed — user pays but gets no reservation"
  - "Record created in database but downstream service call failed — orphaned record accumulates"
  - "No rollback path when step 4 of a 6-step workflow fails — state is permanently inconsistent"
  - "Agent retries the entire workflow from step 1 after a mid-workflow failure, causing duplicate side effects"
  - "No record of which workflow steps completed before a process crash"
---

## Why This Happens

Multi-step workflows that call external services are distributed transactions without a coordinator. When step N fails, steps 1 through N-1 have already executed and produced side effects (charges, records, messages). Without compensation logic, these side effects persist even though the overall workflow failed. The saga pattern makes compensation explicit: each step declares a compensating action (e.g., `charge` is compensated by `refund`), and on failure, the saga executor calls compensations in reverse order for all completed steps.

## Solution 1: Saga Step Definition

```python
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


@dataclass
class SagaStep:
    step_id: str
    name: str
    action: Callable          # async fn(**kwargs) -> result
    compensation: Optional[Callable] = None   # async fn(**kwargs) -> None
    timeout_seconds: float = 30.0
    max_retries: int = 2
    critical: bool = True     # if False, failure continues saga (best-effort step)

    def has_compensation(self) -> bool:
        return self.compensation is not None
```

## Solution 2: Saga Execution State

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class SagaStepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    FAILED = "failed"
    SKIPPED = "skipped"


class SagaStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    FAILED = "failed"


@dataclass
class SagaStepRecord:
    step_id: str
    name: str
    status: SagaStepStatus = SagaStepStatus.PENDING
    result: Any = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    compensation_result: Any = None


@dataclass
class SagaExecutionState:
    saga_id: str
    saga_name: str
    status: SagaStatus = SagaStatus.RUNNING
    steps: List[SagaStepRecord] = field(default_factory=list)
    context: Dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    failure_step_id: Optional[str] = None
    failure_error: Optional[str] = None

    def step_record(self, step_id: str) -> Optional[SagaStepRecord]:
        return next((s for s in self.steps if s.step_id == step_id), None)

    def completed_steps(self) -> List[SagaStepRecord]:
        return [s for s in self.steps if s.status == SagaStepStatus.COMPLETED]
```

## Solution 3: Saga Executor

```python
import asyncio
import time
import uuid
from typing import Any, Dict, List, Optional


class SagaExecutor:
    """
    Executes a sequence of SagaSteps with automatic compensation on failure.
    Each step result is stored in the execution context so later steps can
    reference earlier results (e.g., use the booking_id from step 1 in step 3).
    On failure, compensations run in reverse order for all completed steps.
    """

    def __init__(self, saga_name: str, steps: List[SagaStep]):
        self._name = saga_name
        self._steps = steps

    async def execute(
        self,
        initial_context: Optional[Dict[str, Any]] = None,
    ) -> SagaExecutionState:
        saga_id = str(uuid.uuid4())[:12]
        state = SagaExecutionState(
            saga_id=saga_id,
            saga_name=self._name,
            context=dict(initial_context or {}),
        )
        state.steps = [
            SagaStepRecord(step_id=step.step_id, name=step.name)
            for step in self._steps
        ]

        for step in self._steps:
            record = state.step_record(step.step_id)
            record.status = SagaStepStatus.RUNNING
            record.started_at = time.time()

            try:
                for attempt in range(step.max_retries + 1):
                    try:
                        result = await asyncio.wait_for(
                            step.action(**state.context),
                            timeout=step.timeout_seconds,
                        )
                        break
                    except Exception as exc:
                        if attempt == step.max_retries:
                            raise
                        await asyncio.sleep(0.5 * (2 ** attempt))

                record.result = result
                record.status = SagaStepStatus.COMPLETED
                record.completed_at = time.time()
                # Merge step result into context
                if isinstance(result, dict):
                    state.context.update(result)
                else:
                    state.context[step.step_id + "_result"] = result

            except Exception as exc:
                record.status = SagaStepStatus.FAILED
                record.error = str(exc)[:300]
                record.completed_at = time.time()

                if not step.critical:
                    record.status = SagaStepStatus.SKIPPED
                    continue

                state.status = SagaStatus.COMPENSATING
                state.failure_step_id = step.step_id
                state.failure_error = str(exc)[:300]
                await self._compensate(state)
                state.status = SagaStatus.COMPENSATED
                state.finished_at = time.time()
                return state

        state.status = SagaStatus.COMPLETED
        state.finished_at = time.time()
        return state

    async def _compensate(self, state: SagaExecutionState) -> None:
        completed = list(reversed(state.completed_steps()))
        for record in completed:
            step = next((s for s in self._steps if s.step_id == record.step_id), None)
            if step is None or not step.has_compensation():
                continue
            record.status = SagaStepStatus.COMPENSATING
            try:
                result = await asyncio.wait_for(
                    step.compensation(**state.context),
                    timeout=step.timeout_seconds,
                )
                record.compensation_result = result
                record.status = SagaStepStatus.COMPENSATED
            except Exception as exc:
                record.status = SagaStepStatus.FAILED
                record.error = f"compensation failed: {str(exc)[:200]}"
```

## Solution 4: Saga State Persister

```python
import json
import time
from typing import Dict, Optional


class SagaStatePersister:
    """
    Persists saga execution state so that a process restart can detect
    and resume (or alert on) in-flight sagas.
    In-memory implementation — replace with Redis or database for production.
    """

    def __init__(self):
        self._store: Dict[str, dict] = {}

    def save(self, state: SagaExecutionState) -> None:
        self._store[state.saga_id] = {
            "saga_id": state.saga_id,
            "saga_name": state.saga_name,
            "status": state.status.value,
            "failure_step_id": state.failure_step_id,
            "failure_error": state.failure_error,
            "started_at": state.started_at,
            "finished_at": state.finished_at,
            "steps": [
                {
                    "step_id": s.step_id,
                    "name": s.name,
                    "status": s.status.value,
                    "error": s.error,
                }
                for s in state.steps
            ],
        }

    def load(self, saga_id: str) -> Optional[dict]:
        return self._store.get(saga_id)

    def incomplete_sagas(self) -> list:
        return [
            v for v in self._store.values()
            if v["status"] in (SagaStatus.RUNNING.value, SagaStatus.COMPENSATING.value)
        ]
```

## Solution 5: Saga Registry

```python
from typing import Any, Callable, Dict, List, Optional


class SagaRegistry:
    """
    Stores saga definitions (name -> list of steps) for reuse across sessions.
    Builds SagaExecutor instances on demand.
    """

    def __init__(self, persister: Optional[SagaStatePersister] = None):
        self._definitions: Dict[str, List[SagaStep]] = {}
        self._persister = persister

    def register(self, name: str, steps: List[SagaStep]) -> None:
        self._definitions[name] = steps

    async def run(
        self,
        saga_name: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> SagaExecutionState:
        steps = self._definitions.get(saga_name)
        if steps is None:
            raise KeyError(f"No saga registered under '{saga_name}'")
        executor = SagaExecutor(saga_name, steps)
        state = await executor.execute(context)
        if self._persister:
            self._persister.save(state)
        return state
```

## Solution 6: Saga Outcome Reporter

```python
import time
from typing import List


class SagaOutcomeReporter:
    """
    Summarizes saga execution outcomes: success rates, compensation rates,
    and which steps fail or require compensation most often.
    """

    def __init__(self, persister: SagaStatePersister, window_seconds: float = 86400.0):
        self._persister = persister
        self._window = window_seconds

    def report(self) -> dict:
        cutoff = time.time() - self._window
        all_sagas = [
            v for v in self._persister._store.values()
            if (v.get("started_at") or 0) >= cutoff
        ]

        total = len(all_sagas)
        completed = sum(1 for s in all_sagas if s["status"] == "completed")
        compensated = sum(1 for s in all_sagas if s["status"] == "compensated")
        failed = sum(1 for s in all_sagas if s["status"] == "failed")

        step_failures: Dict[str, int] = {}
        for saga in all_sagas:
            if saga.get("failure_step_id"):
                step_failures[saga["failure_step_id"]] = (
                    step_failures.get(saga["failure_step_id"], 0) + 1
                )

        return {
            "generated_at": time.time(),
            "window_seconds": self._window,
            "total_sagas": total,
            "completed": completed,
            "compensated": compensated,
            "failed": failed,
            "success_rate": round(completed / max(total, 1), 4),
            "compensation_rate": round(compensated / max(total, 1), 4),
            "most_failed_steps": dict(sorted(step_failures.items(), key=lambda x: -x[1])[:5]),
        }


from typing import Dict
```

## Comparison

| Approach | Forward Execution | Compensation | State Persistence | Registry | Reporting |
|---|---|---|---|---|---|
| SagaExecutor | Yes (with retry) | Yes (reverse order) | No | No | No |
| SagaStatePersister | No | No | Yes (in-memory/pluggable) | No | No |
| SagaRegistry | Via executor | Via executor | Via persister | Yes | No |
| SagaOutcomeReporter | No | No | Via persister | No | Yes |

**Best for production**: Define compensation for every step that produces an external side effect — charges, record creation, message delivery. Steps with no compensation (idempotent reads) can omit it. Set `critical=False` on best-effort steps like sending a welcome email: saga completion should not be blocked by non-critical side effects. Persist `SagaExecutionState` to Redis or a database before each step so a process crash does not leave orphaned side effects invisible — the incomplete saga list from `SagaStatePersister.incomplete_sagas()` should be checked at process startup and alerted on.
