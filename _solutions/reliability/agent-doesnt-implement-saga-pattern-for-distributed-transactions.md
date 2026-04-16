---
title: "Agent Doesn't Implement Saga Pattern for Distributed Transactions"
description: "Multi-step agent workflows that span multiple services leave data in inconsistent states when intermediate steps fail. Implement the saga pattern with compensating transactions to ensure eventual consistency across distributed tool calls."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-saga-pattern-for-distributed-transactions
tags: [saga, distributed-transactions, compensating-transactions, eventual-consistency, reliability, orchestration]
symptoms:
  - "Partial writes after tool chain failure leave data in inconsistent state"
  - "No rollback logic when step 3 of a 5-step workflow fails"
  - "Manual cleanup required after agent crashes mid-workflow"
  - "Payment charged but inventory not decremented after downstream timeout"
  - "Duplicate side effects on retry because prior steps ran twice"
---

## Why This Happens

Agents often orchestrate multi-service workflows: create an order, charge a payment, reserve inventory, send a confirmation email. When step 3 fails, steps 1 and 2 have already committed. Unlike a database transaction, there is no 2-phase commit across microservices. Without compensating transactions, the system is left in a partially-applied state that requires manual intervention.

## Solution 1: Choreography-Based Saga with Compensating Transactions

```python
import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, List, Optional
from enum import Enum

class StepStatus(Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    COMPENSATED = "compensated"
    FAILED = "failed"

@dataclass
class SagaStep:
    name: str
    execute: Callable[..., Awaitable[Any]]
    compensate: Callable[..., Awaitable[None]]
    status: StepStatus = StepStatus.PENDING
    result: Any = None
    error: Optional[Exception] = None

class SagaExecutor:
    """
    Executes a list of steps in order. On failure, runs compensating
    transactions in reverse for all completed steps.
    """
    def __init__(self, saga_id: Optional[str] = None):
        self.saga_id = saga_id or str(uuid.uuid4())
        self.steps: List[SagaStep] = []
        self.completed: List[SagaStep] = []

    def add_step(self, name: str,
                 execute: Callable[..., Awaitable[Any]],
                 compensate: Callable[..., Awaitable[None]]) -> "SagaExecutor":
        self.steps.append(SagaStep(name=name, execute=execute, compensate=compensate))
        return self

    async def run(self, context: dict) -> dict:
        for step in self.steps:
            try:
                print(f"[saga:{self.saga_id}] executing {step.name}")
                step.result = await step.execute(context)
                step.status = StepStatus.COMPLETED
                self.completed.append(step)
                context[step.name] = step.result
            except Exception as exc:
                step.status = StepStatus.FAILED
                step.error = exc
                print(f"[saga:{self.saga_id}] {step.name} failed: {exc}. Rolling back.")
                await self._rollback(context)
                raise SagaFailedError(
                    saga_id=self.saga_id,
                    failed_step=step.name,
                    cause=exc,
                ) from exc
        return context

    async def _rollback(self, context: dict) -> None:
        for step in reversed(self.completed):
            try:
                print(f"[saga:{self.saga_id}] compensating {step.name}")
                await step.compensate(context)
                step.status = StepStatus.COMPENSATED
            except Exception as comp_exc:
                # Compensation failures are logged; human intervention required
                print(f"[saga:{self.saga_id}] COMPENSATION FAILED for {step.name}: {comp_exc}")

class SagaFailedError(Exception):
    def __init__(self, saga_id: str, failed_step: str, cause: Exception):
        super().__init__(f"Saga {saga_id} failed at step '{failed_step}': {cause}")
        self.saga_id = saga_id
        self.failed_step = failed_step
        self.cause = cause

# Usage: order fulfillment saga
async def run_order_saga(order_data: dict) -> dict:
    saga = SagaExecutor()

    saga.add_step(
        name="create_order",
        execute=lambda ctx: order_service.create(order_data),
        compensate=lambda ctx: order_service.cancel(ctx["create_order"]["order_id"]),
    ).add_step(
        name="charge_payment",
        execute=lambda ctx: payment_service.charge(
            ctx["create_order"]["order_id"], order_data["amount"]
        ),
        compensate=lambda ctx: payment_service.refund(
            ctx["charge_payment"]["charge_id"]
        ),
    ).add_step(
        name="reserve_inventory",
        execute=lambda ctx: inventory_service.reserve(order_data["items"]),
        compensate=lambda ctx: inventory_service.release(
            ctx["reserve_inventory"]["reservation_id"]
        ),
    ).add_step(
        name="send_confirmation",
        execute=lambda ctx: email_service.send_confirmation(ctx["create_order"]),
        compensate=lambda ctx: None,  # email is idempotent; no compensation needed
    )

    return await saga.run({})
```

## Solution 2: Persistent Saga State with Crash Recovery

```python
import json
import asyncio
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional
from datetime import datetime, timezone

@dataclass
class SagaRecord:
    saga_id: str
    workflow: str
    status: str           # running | completed | compensating | failed
    current_step: int
    steps_completed: List[str]
    context: dict
    started_at: str
    updated_at: str

class PersistentSagaStore:
    """Persists saga state so that a crashed agent can resume or compensate."""

    def __init__(self, db):
        self.db = db

    async def save(self, record: SagaRecord) -> None:
        record.updated_at = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            """
            INSERT INTO saga_records (saga_id, workflow, status, current_step,
                                      steps_completed, context, started_at, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT (saga_id) DO UPDATE
            SET status=$3, current_step=$4, steps_completed=$5,
                context=$6, updated_at=$8
            """,
            record.saga_id, record.workflow, record.status, record.current_step,
            json.dumps(record.steps_completed), json.dumps(record.context),
            record.started_at, record.updated_at,
        )

    async def load(self, saga_id: str) -> Optional[SagaRecord]:
        row = await self.db.fetchrow(
            "SELECT * FROM saga_records WHERE saga_id = $1", saga_id
        )
        if not row:
            return None
        return SagaRecord(
            saga_id=row["saga_id"],
            workflow=row["workflow"],
            status=row["status"],
            current_step=row["current_step"],
            steps_completed=json.loads(row["steps_completed"]),
            context=json.loads(row["context"]),
            started_at=row["started_at"],
            updated_at=row["updated_at"],
        )

    async def find_incomplete(self, workflow: str) -> List[SagaRecord]:
        rows = await self.db.fetch(
            "SELECT * FROM saga_records WHERE workflow=$1 AND status IN ('running','compensating')",
            workflow,
        )
        return [SagaRecord(**{**dict(r),
                              "steps_completed": json.loads(r["steps_completed"]),
                              "context": json.loads(r["context"])}) for r in rows]

class RecoverableSagaExecutor:
    def __init__(self, store: PersistentSagaStore, steps: List[SagaStep], workflow: str):
        self.store = store
        self.steps = steps
        self.workflow = workflow

    async def run(self, saga_id: str, initial_context: dict) -> dict:
        record = await self.store.load(saga_id)
        if record is None:
            record = SagaRecord(
                saga_id=saga_id, workflow=self.workflow, status="running",
                current_step=0, steps_completed=[], context=initial_context,
                started_at=datetime.now(timezone.utc).isoformat(), updated_at="",
            )

        if record.status == "compensating":
            await self._compensate(record)
            raise SagaFailedError(saga_id, "recovered_compensation", Exception("resumed compensation"))

        ctx = record.context
        for i, step in enumerate(self.steps):
            if i < record.current_step:
                continue  # skip already-completed steps
            try:
                record.current_step = i
                await self.store.save(record)
                ctx[step.name] = await step.execute(ctx)
                record.steps_completed.append(step.name)
                record.context = ctx
                await self.store.save(record)
            except Exception as exc:
                record.status = "compensating"
                await self.store.save(record)
                await self._compensate(record)
                record.status = "failed"
                await self.store.save(record)
                raise SagaFailedError(saga_id, step.name, exc) from exc

        record.status = "completed"
        await self.store.save(record)
        return ctx

    async def _compensate(self, record: SagaRecord) -> None:
        completed_names = set(record.steps_completed)
        for step in reversed(self.steps):
            if step.name in completed_names:
                try:
                    await step.compensate(record.context)
                except Exception as e:
                    print(f"Compensation error for {step.name}: {e}")
```

## Solution 3: Saga Orchestrator with Timeout and Retry Per Step

```python
import asyncio
from typing import Optional

@dataclass
class StepConfig:
    timeout_seconds: float = 30.0
    max_retries: int = 2
    retry_delay_seconds: float = 1.0

class RobustSagaOrchestrator:
    """Runs each saga step with per-step timeout and retry before giving up."""

    def __init__(self):
        self.steps: List[tuple[SagaStep, StepConfig]] = []

    def add_step(self, step: SagaStep, config: Optional[StepConfig] = None) -> "RobustSagaOrchestrator":
        self.steps.append((step, config or StepConfig()))
        return self

    async def run(self, context: dict) -> dict:
        completed: List[tuple[SagaStep, dict]] = []

        for step, cfg in self.steps:
            result = await self._execute_with_retry(step, context, cfg)
            if result is None:
                # Step exhausted retries — compensate all prior
                await self._compensate_all(completed, context)
                raise RuntimeError(f"Saga aborted at step '{step.name}' after {cfg.max_retries} retries")
            context[step.name] = result
            completed.append((step, dict(context)))

        return context

    async def _execute_with_retry(self, step: SagaStep, context: dict, cfg: StepConfig) -> Optional[Any]:
        for attempt in range(cfg.max_retries + 1):
            try:
                return await asyncio.wait_for(step.execute(context), timeout=cfg.timeout_seconds)
            except asyncio.TimeoutError:
                print(f"Step '{step.name}' timed out (attempt {attempt + 1})")
            except Exception as exc:
                print(f"Step '{step.name}' error (attempt {attempt + 1}): {exc}")
            if attempt < cfg.max_retries:
                await asyncio.sleep(cfg.retry_delay_seconds * (2 ** attempt))
        return None

    async def _compensate_all(self, completed: List[tuple[SagaStep, dict]], final_ctx: dict) -> None:
        for step, snapshot_ctx in reversed(completed):
            try:
                await step.compensate(snapshot_ctx)
            except Exception as e:
                print(f"Compensation failed for '{step.name}': {e}")
```

## Solution 4: Saga with Idempotency Keys to Prevent Double-Execution

```python
import hashlib
import json
from typing import Any, Dict, Optional

class IdempotentSagaStep:
    """
    Wraps each step with an idempotency key so retries are safe.
    Stores step outputs keyed by (saga_id, step_name) so re-running
    an already-completed step just returns the cached result.
    """

    def __init__(self, idempotency_store):
        self.store = idempotency_store  # Redis or DB

    def make_key(self, saga_id: str, step_name: str) -> str:
        return f"saga:{saga_id}:step:{step_name}"

    async def execute_idempotent(
        self,
        saga_id: str,
        step_name: str,
        fn: Callable[..., Awaitable[Any]],
        context: dict,
    ) -> Any:
        key = self.make_key(saga_id, step_name)
        cached = await self.store.get(key)
        if cached is not None:
            print(f"[idempotent] reusing result for {step_name}")
            return json.loads(cached)

        result = await fn(context)
        await self.store.set(key, json.dumps(result), ex=86400)  # 24h TTL
        return result

    async def mark_compensated(self, saga_id: str, step_name: str) -> None:
        key = self.make_key(saga_id, step_name) + ":compensated"
        await self.store.set(key, "1", ex=86400)

    async def is_compensated(self, saga_id: str, step_name: str) -> bool:
        key = self.make_key(saga_id, step_name) + ":compensated"
        return bool(await self.store.get(key))

class IdempotentSagaExecutor:
    def __init__(self, idempotency_store):
        self.idempotent = IdempotentSagaStep(idempotency_store)
        self.steps: List[SagaStep] = []

    def add_step(self, step: SagaStep) -> "IdempotentSagaExecutor":
        self.steps.append(step)
        return self

    async def run(self, saga_id: str, context: dict) -> dict:
        completed: List[SagaStep] = []
        for step in self.steps:
            try:
                context[step.name] = await self.idempotent.execute_idempotent(
                    saga_id, step.name, step.execute, context
                )
                completed.append(step)
            except Exception as exc:
                await self._compensate(saga_id, completed, context)
                raise SagaFailedError(saga_id, step.name, exc) from exc
        return context

    async def _compensate(self, saga_id: str, completed: List[SagaStep], context: dict) -> None:
        for step in reversed(completed):
            if await self.idempotent.is_compensated(saga_id, step.name):
                continue
            try:
                await step.compensate(context)
                await self.idempotent.mark_compensated(saga_id, step.name)
            except Exception as e:
                print(f"Compensation failed for {step.name}: {e}")
```

## Solution 5: Agent Tool Wrapper that Auto-Generates Compensating Calls

```python
from typing import Any, Callable, Optional
import inspect

class SagaToolRegistry:
    """
    Registry that pairs each tool call with its compensation.
    Agent adds tool calls via execute(); compensation is registered
    upfront so the saga knows how to undo each action.
    """

    def __init__(self):
        self._registry: Dict[str, tuple[Callable, Callable]] = {}
        self._saga_log: List[tuple[str, dict, Any]] = []

    def register(
        self,
        tool_name: str,
        execute_fn: Callable[..., Awaitable[Any]],
        compensate_fn: Callable[..., Awaitable[None]],
    ) -> None:
        self._registry[tool_name] = (execute_fn, compensate_fn)

    async def call(self, tool_name: str, **kwargs) -> Any:
        if tool_name not in self._registry:
            raise ValueError(f"Tool '{tool_name}' not registered in saga registry")
        execute_fn, _ = self._registry[tool_name]
        result = await execute_fn(**kwargs)
        self._saga_log.append((tool_name, kwargs, result))
        return result

    async def rollback_all(self) -> None:
        """Compensate all logged calls in reverse order."""
        for tool_name, kwargs, result in reversed(self._saga_log):
            _, compensate_fn = self._registry[tool_name]
            try:
                # Pass both original kwargs and result to compensation
                sig = inspect.signature(compensate_fn)
                if "result" in sig.parameters:
                    await compensate_fn(**kwargs, result=result)
                else:
                    await compensate_fn(**kwargs)
            except Exception as e:
                print(f"Failed to compensate {tool_name}({kwargs}): {e}")

    def clear_log(self) -> None:
        self._saga_log.clear()


# Agent integration
class SagaAwareAgent:
    def __init__(self, registry: SagaToolRegistry):
        self.registry = registry

    async def run_workflow(self, workflow_fn: Callable[["SagaAwareAgent"], Awaitable[Any]]) -> Any:
        self.registry.clear_log()
        try:
            result = await workflow_fn(self)
            return result
        except Exception as exc:
            print(f"Workflow failed, rolling back: {exc}")
            await self.registry.rollback_all()
            raise

    async def tool(self, name: str, **kwargs) -> Any:
        return await self.registry.call(name, **kwargs)
```

## Solution 6: Saga Metrics and Observability

```python
import time
from dataclasses import dataclass, field
from typing import Dict, List

@dataclass
class SagaMetrics:
    saga_id: str
    workflow: str
    step_durations: Dict[str, float] = field(default_factory=dict)
    compensated_steps: List[str] = field(default_factory=list)
    total_duration_ms: float = 0.0
    outcome: str = "unknown"  # completed | compensated | failed

class InstrumentedSagaExecutor:
    """Wraps SagaExecutor and emits structured metrics per saga execution."""

    def __init__(self, metrics_sink=None):
        self.metrics_sink = metrics_sink  # e.g. Prometheus, Datadog

    async def run(self, saga_id: str, steps: List[SagaStep], context: dict, workflow: str) -> dict:
        metrics = SagaMetrics(saga_id=saga_id, workflow=workflow)
        start = time.monotonic()
        completed: List[SagaStep] = []

        for step in steps:
            t0 = time.monotonic()
            try:
                context[step.name] = await step.execute(context)
                metrics.step_durations[step.name] = (time.monotonic() - t0) * 1000
                completed.append(step)
            except Exception as exc:
                metrics.step_durations[step.name] = (time.monotonic() - t0) * 1000
                metrics.outcome = "compensated"
                metrics.total_duration_ms = (time.monotonic() - start) * 1000
                for s in reversed(completed):
                    try:
                        await s.compensate(context)
                        metrics.compensated_steps.append(s.name)
                    except Exception as comp_err:
                        metrics.outcome = "failed"
                        print(f"Compensation error {s.name}: {comp_err}")
                self._emit(metrics)
                raise SagaFailedError(saga_id, step.name, exc) from exc

        metrics.outcome = "completed"
        metrics.total_duration_ms = (time.monotonic() - start) * 1000
        self._emit(metrics)
        return context

    def _emit(self, m: SagaMetrics) -> None:
        print(f"[saga_metrics] id={m.saga_id} workflow={m.workflow} outcome={m.outcome} "
              f"total_ms={m.total_duration_ms:.1f} steps={m.step_durations} "
              f"compensated={m.compensated_steps}")
        if self.metrics_sink:
            self.metrics_sink.record(m)
```

## Comparison

| Approach | Persistence | Crash Recovery | Retry Safety | Observability |
|---|---|---|---|---|
| Basic SagaExecutor | In-memory only | None | No (re-runs execute) | Print logs |
| PersistentSagaStore | DB-backed | Full resume/compensate | Partial (skips completed) | DB record |
| RobustSagaOrchestrator | In-memory | None | Yes (per-step retries) | Print logs |
| IdempotentSagaExecutor | Redis/DB + idempotency keys | Full | Yes (idempotent re-run) | Redis keys |
| SagaToolRegistry | In-memory log | None | No | Per-tool log |
| InstrumentedSagaExecutor | Metrics sink | None | No | Full metrics |

**Best choice for production**: Combine `PersistentSagaStore` + `IdempotentSagaExecutor` + `InstrumentedSagaExecutor`. Persist saga state to survive crashes, use idempotency keys to make retries safe, and emit structured metrics for every step and compensation.
