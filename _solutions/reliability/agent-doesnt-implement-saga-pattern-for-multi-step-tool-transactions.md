---
title: "Agent Doesn't Implement Saga Pattern for Multi-Step Tool Transactions"
description: "Agents that execute multi-step tool sequences without compensation logic leave systems in partial states when a step fails: an order is created but payment is never charged, a file is uploaded but its database record is never written. Implement the saga pattern with per-step compensating actions that undo completed steps when a later step fails, ensuring all-or-nothing semantics across tool chains."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-saga-pattern-for-multi-step-tool-transactions
tags: [saga-pattern, compensation, distributed-transactions, rollback, multi-step-tools, eventual-consistency]
symptoms:
  - "Failed tool sequence leaves orphaned records in the database"
  - "Payment charged but order not confirmed — no rollback on downstream failure"
  - "No mechanism to undo step 1 when step 3 fails"
  - "Error handling only logs the failure — no attempt to restore prior state"
  - "Multi-step agent workflows have no atomicity guarantee"
---

## Why This Happens

Traditional database transactions span a single system. Multi-step agent tool calls span multiple services — each with its own state store and no shared transaction coordinator. The saga pattern addresses this by pairing each step with a compensating action: if step N fails, the saga executor runs compensating actions for steps N-1, N-2, ... 1 in reverse order, restoring each service to its pre-saga state. Compensation is not always a perfect undo (a sent email cannot be unsent), but for stateful resources like database records, payments, and file storage, compensation actions restore consistency.

## Solution 1: Saga Step Definition

```python
import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    FAILED = "failed"
    COMPENSATION_FAILED = "compensation_failed"


@dataclass
class SagaStep:
    step_id: str
    name: str
    action: Callable           # async callable: (context) -> result
    compensate: Optional[Callable] = None  # async callable: (context, result) -> None
    timeout_seconds: float = 30.0
    max_retries: int = 0
    status: StepStatus = StepStatus.PENDING
    result: Any = None
    error: Optional[str] = None
```

## Solution 2: Saga Context

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class SagaContext:
    """
    Shared mutable context passed to every step action and compensating action.
    Steps write results here; compensating actions read them.
    """
    saga_id: str
    input: Dict[str, Any] = field(default_factory=dict)
    step_results: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)

    def set(self, key: str, value: Any) -> None:
        self.step_results[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.step_results.get(key, default)
```

## Solution 3: Saga Executor

```python
import asyncio
import time
from typing import List


class SagaExecutionError(Exception):
    def __init__(self, failed_step: str, original_error: str, compensation_errors: List[str]):
        self.failed_step = failed_step
        self.original_error = original_error
        self.compensation_errors = compensation_errors
        super().__init__(
            f"saga failed at step '{failed_step}': {original_error}. "
            f"Compensation errors: {compensation_errors}"
        )


class SagaExecutor:
    """
    Executes a sequence of SagaSteps. On failure at any step, runs
    compensating actions in reverse order for all completed steps.
    """

    def __init__(self, steps: List[SagaStep], context: SagaContext):
        self._steps = steps
        self._ctx = context

    async def run(self) -> SagaContext:
        completed: List[SagaStep] = []

        for step in self._steps:
            step.status = StepStatus.RUNNING
            try:
                for attempt in range(step.max_retries + 1):
                    try:
                        result = await asyncio.wait_for(
                            step.action(self._ctx),
                            timeout=step.timeout_seconds,
                        )
                        step.result = result
                        step.status = StepStatus.COMPLETED
                        self._ctx.set(step.step_id, result)
                        completed.append(step)
                        break
                    except (asyncio.TimeoutError, Exception) as exc:
                        if attempt < step.max_retries:
                            await asyncio.sleep(0.5 * (2 ** attempt))
                            continue
                        raise
            except Exception as exc:
                step.status = StepStatus.FAILED
                step.error = str(exc)[:300]
                compensation_errors = await self._compensate(completed)
                raise SagaExecutionError(
                    failed_step=step.name,
                    original_error=step.error,
                    compensation_errors=compensation_errors,
                )

        return self._ctx

    async def _compensate(self, completed: List[SagaStep]) -> List[str]:
        errors = []
        for step in reversed(completed):
            if step.compensate is None:
                continue
            step.status = StepStatus.COMPENSATING
            try:
                await asyncio.wait_for(
                    step.compensate(self._ctx, step.result),
                    timeout=step.timeout_seconds,
                )
                step.status = StepStatus.COMPENSATED
            except Exception as exc:
                step.status = StepStatus.COMPENSATION_FAILED
                errors.append(f"{step.name}: {str(exc)[:200]}")
        return errors
```

## Solution 4: Saga Registry

```python
import uuid
from typing import Callable, Dict, List, Optional


class SagaDefinition:
    """
    Declarative builder for a saga — register steps and compensations
    before creating an executor instance.
    """

    def __init__(self, saga_name: str):
        self.saga_name = saga_name
        self._step_specs: List[dict] = []

    def step(
        self,
        step_id: str,
        name: str,
        action: Callable,
        compensate: Optional[Callable] = None,
        timeout_seconds: float = 30.0,
        max_retries: int = 0,
    ) -> "SagaDefinition":
        self._step_specs.append({
            "step_id": step_id,
            "name": name,
            "action": action,
            "compensate": compensate,
            "timeout_seconds": timeout_seconds,
            "max_retries": max_retries,
        })
        return self

    def build_executor(self, input_data: dict = None) -> SagaExecutor:
        saga_id = uuid.uuid4().hex
        ctx = SagaContext(saga_id=saga_id, input=input_data or {})
        steps = [
            SagaStep(
                step_id=spec["step_id"],
                name=spec["name"],
                action=spec["action"],
                compensate=spec["compensate"],
                timeout_seconds=spec["timeout_seconds"],
                max_retries=spec["max_retries"],
            )
            for spec in self._step_specs
        ]
        return SagaExecutor(steps=steps, context=ctx)
```

## Solution 5: Saga Outcome Recorder

```python
import time
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class SagaOutcomeRecord:
    saga_id: str
    saga_name: str
    success: bool
    failed_step: Optional[str]
    compensation_triggered: bool
    compensation_errors: List[str]
    duration_ms: float
    recorded_at: float = field(default_factory=time.time)


class SagaOutcomeRecorder:
    """
    Persists saga execution outcomes for audit and reliability analysis.
    High compensation rates on a specific step indicate a systemic issue.
    """

    def __init__(self, max_records: int = 10000):
        self._records: List[SagaOutcomeRecord] = []
        self._max = max_records

    def record(self, record: SagaOutcomeRecord) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append(record)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r.recorded_at >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "sagas": 0}

        success_count = sum(1 for r in recent if r.success)
        compensation_count = sum(1 for r in recent if r.compensation_triggered)
        failed_steps: Dict[str, int] = {}
        for r in recent:
            if r.failed_step:
                failed_steps[r.failed_step] = failed_steps.get(r.failed_step, 0) + 1

        return {
            "window_seconds": window_seconds,
            "sagas": len(recent),
            "success_rate": round(success_count / len(recent), 4),
            "compensation_rate": round(compensation_count / len(recent), 4),
            "most_failed_step": max(failed_steps, key=failed_steps.get) if failed_steps else None,
            "failed_steps": dict(sorted(failed_steps.items(), key=lambda x: -x[1])),
        }
```

## Solution 6: Saga Audit Dashboard

```python
import time


class SagaAuditDashboard:
    def __init__(self, recorder: SagaOutcomeRecorder):
        self._recorder = recorder

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "last_hour": self._recorder.summary(3600.0),
            "last_day": self._recorder.summary(86400.0),
        }
```

## Comparison

| Approach | Forward Execution | Compensation | Retry Support | Outcome Tracking | Declarative API |
|---|---|---|---|---|---|
| SagaStep | Yes (action) | Yes (compensate) | Yes (max_retries) | No | No |
| SagaExecutor | Yes (sequential) | Yes (reverse order) | Via step | No | No |
| SagaDefinition | Via executor | Via executor | Via executor | No | Yes |
| SagaOutcomeRecorder | No | No | No | Yes | No |
| SagaAuditDashboard | No | No | No | Via recorder | No |

**Best for production**: Write compensating actions for every step that mutates external state (database writes, payments, file uploads). Mark steps without a meaningful undo (email sends, webhook notifications) with `compensate=None` and accept that they are best-effort. Set `max_retries=2` for network-bound steps and `max_retries=0` for steps with side effects that must not be duplicated. Monitor `compensation_rate` from `SagaOutcomeRecorder.summary()`: above 5% on a specific step indicates that step is fragile and its dependencies need hardening rather than more compensation logic.
