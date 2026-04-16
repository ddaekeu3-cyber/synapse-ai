---
title: "Agent Doesn't Implement Compensating Transactions for Failed Workflows"
description: "Multi-step agent workflows that fail mid-way leave the system in a partially applied state. Without compensation logic, rolled-back business logic is never undone, leaving orphaned records, double-charges, and inconsistent state."
difficulty: advanced
category: reliability
tags: [compensation, saga, rollback, transactions, workflow, reliability, distributed]
---

## Problem

An agent executes a multi-step workflow: create a record, charge a payment, send an email, update inventory. If step 3 fails, steps 1 and 2 have already mutated external systems. A simple retry or error return doesn't undo those mutations. Without compensation logic, the system is left in a state that requires manual intervention.

```python
# Broken: no compensation on failure
async def provision_user(user_id: str, plan: str):
    await db.create_user(user_id, plan)           # Step 1
    await billing.charge(user_id, plan)            # Step 2
    await email.send_welcome(user_id)              # Step 3 — crashes
    await inventory.decrement(plan)                # Step 4 — never reached
# After crash: user record exists, payment taken, no email, inventory wrong
```

---

## Solution 1: Manual Compensation Stack (Try/Compensate Pattern)

```python
import asyncio
from dataclasses import dataclass, field
from typing import Callable, Awaitable

@dataclass
class CompensationStep:
    name: str
    compensate: Callable[[], Awaitable[None]]

class CompensationStack:
    """
    LIFO stack of compensation actions.
    Push a compensator after each successful step.
    On failure, unwind the stack in reverse order.
    """

    def __init__(self):
        self._stack: list[CompensationStep] = []
        self._compensated = False

    def push(self, name: str, compensate: Callable[[], Awaitable[None]]):
        self._stack.append(CompensationStep(name, compensate))

    async def compensate(self):
        if self._compensated:
            return
        self._compensated = True
        errors: list[tuple[str, Exception]] = []
        for step in reversed(self._stack):
            try:
                await step.compensate()
                print(f"[Compensate] Undone: {step.name}")
            except Exception as e:
                print(f"[Compensate] Failed to undo {step.name}: {e}")
                errors.append((step.name, e))
        if errors:
            names = [n for n, _ in errors]
            raise CompensationError(f"Partial compensation failure: {names}")

class CompensationError(RuntimeError):
    pass

# Usage
async def provision_user(user_id: str, plan: str):
    stack = CompensationStack()
    try:
        # Step 1
        await db.create_user(user_id, plan)
        stack.push("delete_user",
                   lambda: db.delete_user(user_id))

        # Step 2
        charge_id = await billing.charge(user_id, plan)
        stack.push("refund_charge",
                   lambda cid=charge_id: billing.refund(cid))

        # Step 3 (may fail)
        await email.send_welcome(user_id)
        stack.push("send_failure_notice",
                   lambda: email.send_provision_failed(user_id))

        # Step 4
        await inventory.decrement(plan)
        stack.push("restore_inventory",
                   lambda: inventory.increment(plan))

    except Exception as e:
        print(f"[Workflow] Step failed: {e}. Running compensation...")
        await stack.compensate()
        raise

# Stubs
class db:
    @staticmethod async def create_user(uid, plan): pass
    @staticmethod async def delete_user(uid): pass

class billing:
    @staticmethod async def charge(uid, plan): return "ch_123"
    @staticmethod async def refund(cid): pass

class email:
    @staticmethod async def send_welcome(uid): raise RuntimeError("SMTP down")
    @staticmethod async def send_provision_failed(uid): pass

class inventory:
    @staticmethod async def decrement(plan): pass
    @staticmethod async def increment(plan): pass
```

---

## Solution 2: Saga Orchestrator with Persistent State

```python
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Awaitable

class StepStatus(Enum):
    PENDING     = auto()
    RUNNING     = auto()
    DONE        = auto()
    FAILED      = auto()
    COMPENSATED = auto()

@dataclass
class SagaStep:
    name: str
    action: Callable[[dict], Awaitable[Any]]
    compensator: Callable[[dict], Awaitable[None]]
    status: StepStatus = StepStatus.PENDING
    result: Any = None
    error: str | None = None

@dataclass
class SagaState:
    saga_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    steps: list[dict] = field(default_factory=list)  # serializable
    current_step: int = 0
    status: str = "running"  # running | done | compensating | failed
    context: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

class SagaOrchestrator:
    """
    Persistent saga orchestrator: state is saved after each step,
    allowing crash recovery and resume.
    """

    def __init__(self, persist_fn: Callable[[SagaState], Awaitable[None]],
                 load_fn: Callable[[str], Awaitable[SagaState | None]]):
        self._persist = persist_fn
        self._load = load_fn

    async def run(self, saga_id: str,
                  steps: list[SagaStep],
                  initial_context: dict) -> dict:
        # Resume if state exists (crash recovery)
        state = await self._load(saga_id)
        if state is None:
            state = SagaState(saga_id=saga_id, context=initial_context)

        completed_steps: list[SagaStep] = []

        for i, step in enumerate(steps):
            if i < state.current_step:
                completed_steps.append(step)
                continue  # already done before crash

            state.current_step = i
            state.status = "running"
            await self._persist(state)

            try:
                print(f"[Saga {saga_id[:8]}] Step {i+1}/{len(steps)}: {step.name}")
                step.result = await step.action(state.context)
                step.status = StepStatus.DONE
                if step.result is not None:
                    state.context[step.name] = step.result
                completed_steps.append(step)

                state.current_step = i + 1
                await self._persist(state)

            except Exception as e:
                step.status = StepStatus.FAILED
                step.error = str(e)
                state.status = "compensating"
                await self._persist(state)

                print(f"[Saga] Step '{step.name}' failed: {e}. Compensating...")
                await self._compensate(saga_id, completed_steps, state)
                state.status = "failed"
                await self._persist(state)
                raise SagaFailed(saga_id, step.name, e) from e

        state.status = "done"
        await self._persist(state)
        return state.context

    async def _compensate(self, saga_id: str, done_steps: list[SagaStep],
                           state: SagaState):
        for step in reversed(done_steps):
            try:
                await step.compensator(state.context)
                step.status = StepStatus.COMPENSATED
                print(f"[Saga] Compensated: {step.name}")
            except Exception as e:
                print(f"[Saga] WARNING: compensation failed for {step.name}: {e}")
                # Log for manual intervention but continue compensating others

class SagaFailed(RuntimeError):
    def __init__(self, saga_id: str, step: str, cause: Exception):
        super().__init__(f"Saga {saga_id} failed at step '{step}': {cause}")
        self.saga_id = saga_id
        self.failed_step = step
        self.cause = cause
```

---

## Solution 3: Idempotent Steps with Compensation Receipts

```python
import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

@dataclass
class CompensationReceipt:
    """Proof that a compensation was applied. Enables idempotent re-compensation."""
    receipt_id: str
    step_name: str
    compensated_at: float
    details: dict

class IdempotentCompensator:
    """
    Ensures each compensation action runs exactly once even under retries.
    Uses a receipt store (Redis/DB) to track which compensations have fired.
    """

    def __init__(self,
                 issue_receipt: Callable[[CompensationReceipt], Awaitable[None]],
                 has_receipt: Callable[[str], Awaitable[bool]]):
        self._issue_receipt = issue_receipt
        self._has_receipt = has_receipt

    def _receipt_id(self, saga_id: str, step_name: str) -> str:
        return hashlib.sha256(f"{saga_id}:{step_name}:compensate".encode()).hexdigest()[:16]

    async def compensate(self, saga_id: str, step_name: str,
                          action: Callable[[], Awaitable[dict]]) -> bool:
        """
        Run compensation exactly once.
        Returns True if compensation was applied, False if already done.
        """
        receipt_id = self._receipt_id(saga_id, step_name)
        if await self._has_receipt(receipt_id):
            print(f"[Idempotent] Skipping already-compensated step: {step_name}")
            return False

        details = await action()

        receipt = CompensationReceipt(
            receipt_id=receipt_id,
            step_name=step_name,
            compensated_at=time.time(),
            details=details or {},
        )
        await self._issue_receipt(receipt)
        print(f"[Idempotent] Compensated: {step_name} (receipt: {receipt_id})")
        return True

# In-memory receipt store (use Redis/DB in production)
class InMemoryReceiptStore:
    def __init__(self):
        self._receipts: dict[str, CompensationReceipt] = {}

    async def issue(self, receipt: CompensationReceipt):
        self._receipts[receipt.receipt_id] = receipt

    async def has(self, receipt_id: str) -> bool:
        return receipt_id in self._receipts

async def idempotent_compensate_demo():
    store = InMemoryReceiptStore()
    compensator = IdempotentCompensator(store.issue, store.has)
    saga_id = "saga-xyz"

    # First call: runs compensation
    applied = await compensator.compensate(
        saga_id, "charge_user",
        action=lambda: asyncio.coroutine(lambda: {"refund_id": "rf_123"})()
    )

    # Second call: skipped (idempotent)
    applied2 = await compensator.compensate(
        saga_id, "charge_user",
        action=lambda: asyncio.coroutine(lambda: {"refund_id": "rf_456"})()
    )
    print(f"First: {applied}, Second (should be False): {applied2}")
```

---

## Solution 4: Workflow with Conditional Compensation

```python
import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

@dataclass
class ConditionalStep:
    """
    A workflow step where compensation behavior depends on outcome.
    For example: only refund if charge actually succeeded.
    """
    name: str
    action: Callable[[dict], Awaitable[Any]]
    compensator: Callable[[dict, Any], Awaitable[None]] | None = None
    # If True, failure of this step is tolerated (no compensation triggered)
    optional: bool = False
    # If True, failure skips compensation of preceding steps too
    abort_on_failure: bool = False

class ConditionalWorkflow:
    def __init__(self):
        self._steps: list[ConditionalStep] = []

    def add_step(self, step: ConditionalStep) -> "ConditionalWorkflow":
        self._steps.append(step)
        return self

    async def execute(self, context: dict) -> dict:
        done: list[tuple[ConditionalStep, Any]] = []

        for step in self._steps:
            try:
                result = await step.action(context)
                context[step.name] = result
                done.append((step, result))
                print(f"[Workflow] ✓ {step.name}")
            except Exception as e:
                print(f"[Workflow] ✗ {step.name}: {e}")
                if step.optional:
                    print(f"[Workflow] Step '{step.name}' is optional, continuing")
                    done.append((step, None))
                    continue
                # Compensate in reverse
                if step.abort_on_failure:
                    print(f"[Workflow] Abort-on-failure: skipping compensation")
                else:
                    await self._compensate(done, context)
                raise WorkflowError(step.name, e) from e

        return context

    async def _compensate(self, done: list[tuple["ConditionalStep", Any]],
                           context: dict):
        for step, result in reversed(done):
            if step.compensator is None or result is None:
                continue
            try:
                await step.compensator(context, result)
                print(f"[Workflow] Compensated: {step.name}")
            except Exception as ce:
                print(f"[Workflow] Compensation failed for {step.name}: {ce}")

class WorkflowError(RuntimeError):
    def __init__(self, step: str, cause: Exception):
        super().__init__(f"Workflow failed at '{step}': {cause}")

# Example: user provisioning workflow
async def build_provision_workflow() -> ConditionalWorkflow:
    wf = ConditionalWorkflow()

    wf.add_step(ConditionalStep(
        name="create_user",
        action=lambda ctx: create_user_record(ctx["user_id"], ctx["plan"]),
        compensator=lambda ctx, r: delete_user_record(ctx["user_id"]),
    ))
    wf.add_step(ConditionalStep(
        name="charge_payment",
        action=lambda ctx: charge_card(ctx["user_id"], ctx["plan"]),
        compensator=lambda ctx, r: refund_charge(r["charge_id"]),
    ))
    wf.add_step(ConditionalStep(
        name="send_welcome_email",
        action=lambda ctx: send_email(ctx["user_id"]),
        compensator=None,  # can't un-send an email
        optional=True,     # don't roll back if email fails
    ))
    wf.add_step(ConditionalStep(
        name="update_inventory",
        action=lambda ctx: decrement_inventory(ctx["plan"]),
        compensator=lambda ctx, r: increment_inventory(ctx["plan"]),
    ))

    return wf

async def create_user_record(uid, plan): return {"user_id": uid}
async def delete_user_record(uid): pass
async def charge_card(uid, plan): return {"charge_id": "ch_123"}
async def refund_charge(cid): pass
async def send_email(uid): pass
async def decrement_inventory(plan): pass
async def increment_inventory(plan): pass
```

---

## Solution 5: Durable Compensation with Dead-Letter Retry

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Awaitable

@dataclass
class PendingCompensation:
    compensation_id: str
    saga_id: str
    step_name: str
    payload: dict
    max_retries: int = 5
    attempt_count: int = 0
    next_retry_at: float = field(default_factory=time.time)
    dead_lettered: bool = False

class DurableCompensationQueue:
    """
    Compensations that fail are queued for retry with exponential backoff.
    Failed compensations after max_retries are dead-lettered for manual review.
    """

    def __init__(self):
        self._pending: dict[str, PendingCompensation] = {}
        self._dead_letter: list[PendingCompensation] = []
        self._lock = asyncio.Lock()

    async def enqueue(self, comp: PendingCompensation):
        async with self._lock:
            self._pending[comp.compensation_id] = comp

    async def run_retry_loop(
        self,
        handlers: dict[str, Callable[[dict], Awaitable[None]]],
        interval: float = 5.0
    ):
        while True:
            await asyncio.sleep(interval)
            now = time.time()
            async with self._lock:
                ready = [
                    c for c in self._pending.values()
                    if c.next_retry_at <= now and not c.dead_lettered
                ]

            for comp in ready:
                handler = handlers.get(comp.step_name)
                if not handler:
                    print(f"[DurableComp] No handler for '{comp.step_name}'")
                    continue
                try:
                    await handler(comp.payload)
                    async with self._lock:
                        del self._pending[comp.compensation_id]
                    print(f"[DurableComp] Compensation succeeded: {comp.step_name}")
                except Exception as e:
                    async with self._lock:
                        comp.attempt_count += 1
                        if comp.attempt_count >= comp.max_retries:
                            comp.dead_lettered = True
                            self._dead_letter.append(comp)
                            del self._pending[comp.compensation_id]
                            print(f"[DurableComp] DEAD LETTER: {comp.step_name} "
                                  f"saga={comp.saga_id} — manual intervention required")
                        else:
                            backoff = min(300, 2 ** comp.attempt_count)
                            comp.next_retry_at = time.time() + backoff
                            print(f"[DurableComp] Retry {comp.attempt_count}/"
                                  f"{comp.max_retries} for {comp.step_name} in {backoff}s")

    def dead_letter_count(self) -> int:
        return len(self._dead_letter)

    def pending_count(self) -> int:
        return len(self._pending)
```

---

## Solution 6: Compensation Audit Log and Recovery Report

```python
import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

@dataclass
class CompensationEvent:
    event_id: str
    saga_id: str
    step_name: str
    event_type: str  # "started", "succeeded", "failed", "dead_lettered"
    timestamp: float = field(default_factory=time.time)
    details: dict = field(default_factory=dict)

class CompensationAuditLog:
    """
    Immutable append-only log of all compensation events.
    Enables post-incident analysis and automated recovery verification.
    """

    def __init__(self):
        self._events: list[CompensationEvent] = []
        self._lock = asyncio.Lock()

    async def record(self, event: CompensationEvent):
        async with self._lock:
            self._events.append(event)
        print(f"[AuditLog] {event.event_type.upper()}: "
              f"saga={event.saga_id[:8]} step={event.step_name}")

    def events_for_saga(self, saga_id: str) -> list[CompensationEvent]:
        return [e for e in self._events if e.saga_id == saga_id]

    def recovery_report(self, saga_id: str) -> dict:
        events = self.events_for_saga(saga_id)
        steps_started = {e.step_name for e in events if e.event_type == "started"}
        steps_succeeded = {e.step_name for e in events if e.event_type == "succeeded"}
        steps_failed = {e.step_name for e in events if e.event_type == "failed"}
        steps_dead = {e.step_name for e in events if e.event_type == "dead_lettered"}

        pending = steps_started - steps_succeeded - steps_dead
        return {
            "saga_id": saga_id,
            "total_compensations_attempted": len(steps_started),
            "succeeded": sorted(steps_succeeded),
            "failed_but_retrying": sorted(pending),
            "dead_lettered": sorted(steps_dead),
            "fully_compensated": len(steps_dead) == 0 and len(pending) == 0,
            "requires_manual_intervention": sorted(steps_dead),
        }

    def export_jsonl(self) -> str:
        return "\n".join(
            json.dumps({
                "event_id": e.event_id,
                "saga_id": e.saga_id,
                "step": e.step_name,
                "type": e.event_type,
                "ts": e.timestamp,
                **e.details,
            })
            for e in self._events
        )
```

---

## Comparison

| Solution | Persistence | Retry Failed Compensations | Idempotent | Audit | Best For |
|---|---|---|---|---|---|
| 1. Compensation stack | None (in-memory) | No | No | No | Simple in-process workflows |
| 2. Saga orchestrator | Yes (pluggable) | No | No | No | Multi-step crash-recoverable sagas |
| 3. Idempotent receipts | Yes (receipt store) | No | Yes | No | Retry-safe compensation |
| 4. Conditional workflow | None | No | No | No | Optional steps, abort policies |
| 5. Durable DLQ | Yes (in-memory DLQ) | Yes (exponential) | No | No | Unreliable external compensations |
| 6. Audit log | Yes (append-only) | No | No | Yes | Post-incident analysis |

**Key principle**: every step that mutates external state must register its compensator *before* the mutation proceeds (or at minimum atomically with it). Compensators must be idempotent — they may be called more than once under retry. Steps that cannot be compensated (sent emails, logged audit events) should be placed last in the workflow so they only execute after all compensatable steps succeed.
