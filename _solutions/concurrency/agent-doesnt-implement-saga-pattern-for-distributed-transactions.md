---
layout: solution
title: "Agent Doesn't Implement Saga Pattern for Distributed Transactions"
category: concurrency
description: "Agents that orchestrate multi-step operations across external services have no recovery plan when a step fails midway. The saga pattern breaks a distributed transaction into a sequence of local operations, each with a compensating action that undoes it — enabling rollback without distributed locks or two-phase commit."
tags: [saga, distributed-transactions, rollback, compensation, orchestration, reliability, multi-step]
---

# Agent Doesn't Implement Saga Pattern for Distributed Transactions

## Problem

An agent books a flight, then charges a credit card, then reserves a hotel. If the hotel reservation fails, the flight and charge need to be reversed — but without a saga, the agent just reports an error and leaves the user with a charged card and a booked flight they can't use. The saga pattern defines compensating transactions for every step: if step N fails, execute compensations for steps N-1 through 1 in reverse order, restoring the system to its original state.

**Symptoms:**
- Partial state left behind after multi-step tool call failures
- Users charged or resources reserved with no corresponding outcome
- No rollback when step 3 of 5 fails
- Agent retries the entire workflow instead of only the failed step
- Distributed operations leave systems inconsistent after crashes

---

## Option 1: Choreography Saga — Each Step Triggers the Next via Events

```python
import anthropic
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

class StepStatus(Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    COMPENSATED = "compensated"
    FAILED = "failed"

@dataclass
class SagaStep:
    step_id: str
    name: str
    action: Callable
    compensate: Callable
    status: StepStatus = StepStatus.PENDING
    result: dict = field(default_factory=dict)
    error: str = ""

@dataclass
class SagaExecution:
    saga_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    steps: list[SagaStep] = field(default_factory=list)
    completed_steps: list[str] = field(default_factory=list)  # step_ids in order
    status: str = "running"  # running | completed | compensating | rolled_back

class SagaOrchestrator:
    def __init__(self):
        self._executions: dict[str, SagaExecution] = {}

    def create(self, steps: list[tuple]) -> SagaExecution:
        """
        steps: list of (name, action_fn, compensate_fn)
        action_fn(context) -> dict  (raises on failure)
        compensate_fn(context, result) -> None
        """
        execution = SagaExecution(
            steps=[
                SagaStep(
                    step_id=str(uuid.uuid4()),
                    name=name,
                    action=action,
                    compensate=compensate
                )
                for name, action, compensate in steps
            ]
        )
        self._executions[execution.saga_id] = execution
        return execution

    def execute(self, execution: SagaExecution, context: dict) -> dict:
        print(f"\n[Saga {execution.saga_id[:8]}] Starting {len(execution.steps)} steps")

        for step in execution.steps:
            print(f"  -> {step.name}...", end=" ")
            try:
                result = step.action(context)
                step.result = result or {}
                step.status = StepStatus.COMPLETED
                execution.completed_steps.append(step.step_id)
                context.update(result or {})
                print(f"OK {result}")
            except Exception as e:
                step.status = StepStatus.FAILED
                step.error = str(e)
                print(f"FAILED: {e}")
                # Trigger compensations
                self._compensate(execution, context)
                execution.status = "rolled_back"
                return {"success": False, "error": str(e), "rolled_back": True}

        execution.status = "completed"
        print(f"  [Saga] Completed successfully")
        return {"success": True, "context": context}

    def _compensate(self, execution: SagaExecution, context: dict):
        print(f"  [Saga] Rolling back {len(execution.completed_steps)} completed steps...")
        # Compensate in reverse order
        completed_map = {s.step_id: s for s in execution.steps}
        for step_id in reversed(execution.completed_steps):
            step = completed_map[step_id]
            print(f"  <- Compensating {step.name}...", end=" ")
            try:
                step.compensate(context, step.result)
                step.status = StepStatus.COMPENSATED
                print("OK")
            except Exception as e:
                print(f"FAILED (manual intervention needed): {e}")

# Define travel booking saga
def make_booking_saga(orchestrator: SagaOrchestrator, fail_at: Optional[str] = None):
    def book_flight(ctx):
        if fail_at == "flight":
            raise ValueError("No seats available on this flight")
        flight_id = f"FL{uuid.uuid4().hex[:6].upper()}"
        return {"flight_id": flight_id, "flight_cost": 450.00}

    def cancel_flight(ctx, result):
        print(f"[Cancel] Flight {result.get('flight_id')} refunded $450")

    def charge_card(ctx):
        if fail_at == "payment":
            raise ValueError("Card declined: insufficient funds")
        charge_id = f"CH{uuid.uuid4().hex[:6].upper()}"
        total = ctx.get("flight_cost", 0) + ctx.get("hotel_cost", 0)
        return {"charge_id": charge_id, "charged": total}

    def refund_card(ctx, result):
        print(f"[Refund] Charge {result.get('charge_id')} reversed: ${result.get('charged', 0):.2f}")

    def book_hotel(ctx):
        if fail_at == "hotel":
            raise ValueError("Hotel fully booked for those dates")
        hotel_id = f"HT{uuid.uuid4().hex[:6].upper()}"
        return {"hotel_id": hotel_id, "hotel_cost": 200.00}

    def cancel_hotel(ctx, result):
        print(f"[Cancel] Hotel {result.get('hotel_id')} reservation cancelled")

    return orchestrator.create([
        ("Book Flight", book_flight, cancel_flight),
        ("Charge Credit Card", charge_card, refund_card),
        ("Book Hotel", book_hotel, cancel_hotel),
    ])

def run_saga_agent(query: str, fail_at: Optional[str] = None):
    client = anthropic.Anthropic()
    orchestrator = SagaOrchestrator()

    tools = [{
        "name": "book_travel",
        "description": "Book a complete travel package (flight + hotel)",
        "input_schema": {
            "type": "object",
            "properties": {
                "destination": {"type": "string"},
                "fail_at": {"type": "string", "description": "Simulate failure at step"}
            },
            "required": ["destination"]
        }
    }]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        tools=tools,
        messages=[{"role": "user", "content": query}]
    )

    for block in response.content:
        if block.type == "tool_use":
            execution = make_booking_saga(orchestrator, fail_at=fail_at)
            result = orchestrator.execute(execution, {"destination": block.input.get("destination", "Paris")})
            print(f"\nResult: {result}")

print("=== Successful booking ===")
run_saga_agent("Book me a trip to Paris", fail_at=None)

print("\n=== Hotel fails — full rollback ===")
run_saga_agent("Book me a trip to Tokyo", fail_at="hotel")

# Expected Token Savings: ~0% — saga logic is agent-side; prevents costly partial failures
# Environment: Any multi-step agent workflow with external services (payments, bookings, APIs)
```

---

## Option 2: Persistent Saga with Crash Recovery

```python
import anthropic
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

class SagaState(Enum):
    STARTED = "started"
    STEP_COMPLETED = "step_completed"
    STEP_FAILED = "step_failed"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    COMPLETED = "completed"

class PersistentSagaLog:
    """Durable saga log — survives process restarts."""

    def __init__(self, db_path: str = "/tmp/saga_log.db"):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS saga_log (
                log_id TEXT PRIMARY KEY,
                saga_id TEXT NOT NULL,
                step_name TEXT,
                state TEXT NOT NULL,
                payload TEXT,
                timestamp REAL DEFAULT (unixepoch('now', 'subsec'))
            )
        """)
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_saga ON saga_log(saga_id, timestamp)")
        self.db.commit()

    def append(self, saga_id: str, step_name: str, state: SagaState, payload: dict = None):
        self.db.execute(
            "INSERT INTO saga_log VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), saga_id, step_name, state.value,
             json.dumps(payload or {}), time.time())
        )
        self.db.commit()

    def get_saga_history(self, saga_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT step_name, state, payload FROM saga_log WHERE saga_id = ? ORDER BY timestamp",
            (saga_id,)
        ).fetchall()
        return [{"step": r[0], "state": r[1], "payload": json.loads(r[2])} for r in rows]

    def find_incomplete_sagas(self) -> list[str]:
        """Find saga IDs that started but never completed."""
        rows = self.db.execute("""
            SELECT DISTINCT saga_id FROM saga_log
            WHERE saga_id NOT IN (
                SELECT saga_id FROM saga_log WHERE state IN ('completed', 'compensated')
            )
        """).fetchall()
        return [r[0] for r in rows]

class PersistentSaga:
    def __init__(self, saga_id: str, log: PersistentSagaLog):
        self.saga_id = saga_id
        self.log = log
        self._steps: list[dict] = []  # {"name", "action", "compensate"}
        self._results: dict[str, dict] = {}

    def add_step(self, name: str, action: Callable, compensate: Callable):
        self._steps.append({"name": name, "action": action, "compensate": compensate})

    def execute(self, context: dict) -> dict:
        self.log.append(self.saga_id, None, SagaState.STARTED, {"context": str(context)[:200]})
        completed = []

        for step_def in self._steps:
            name = step_def["name"]
            print(f"  -> {name}...", end=" ")
            try:
                result = step_def["action"](context)
                self._results[name] = result or {}
                context.update(result or {})
                self.log.append(self.saga_id, name, SagaState.STEP_COMPLETED, result or {})
                completed.append(step_def)
                print(f"OK")
            except Exception as e:
                self.log.append(self.saga_id, name, SagaState.STEP_FAILED, {"error": str(e)})
                print(f"FAILED: {e}")
                self._rollback(completed, context)
                return {"success": False, "error": str(e)}

        self.log.append(self.saga_id, None, SagaState.COMPLETED, {})
        return {"success": True}

    def _rollback(self, completed_steps: list[dict], context: dict):
        self.log.append(self.saga_id, None, SagaState.COMPENSATING, {})
        for step_def in reversed(completed_steps):
            name = step_def["name"]
            print(f"  <- Compensating {name}...", end=" ")
            try:
                step_def["compensate"](context, self._results.get(name, {}))
                self.log.append(self.saga_id, name, SagaState.COMPENSATED, {})
                print("OK")
            except Exception as e:
                print(f"FAILED: {e} (requires manual fix)")
        self.log.append(self.saga_id, None, SagaState.COMPENSATED, {})

def run_persistent_saga_demo():
    log = PersistentSagaLog()
    saga_id = str(uuid.uuid4())
    saga = PersistentSaga(saga_id, log)
    fail_step = "Reserve Inventory"

    # Define steps
    reserved_items = {}

    def create_order(ctx):
        return {"order_id": f"ORD-{uuid.uuid4().hex[:6].upper()}"}

    def cancel_order(ctx, result):
        print(f"  [Compensate] Order {result.get('order_id')} cancelled")

    def charge_payment(ctx):
        return {"payment_id": f"PAY-{uuid.uuid4().hex[:6].upper()}", "amount": 99.99}

    def refund_payment(ctx, result):
        print(f"  [Compensate] Payment {result.get('payment_id')} refunded ${result.get('amount', 0)}")

    def reserve_inventory(ctx):
        if fail_step == "Reserve Inventory":
            raise ValueError("Item out of stock")
        reserved_items["item_123"] = True
        return {"reservation_id": f"RES-{uuid.uuid4().hex[:6].upper()}"}

    def release_inventory(ctx, result):
        reserved_items.pop("item_123", None)
        print(f"  [Compensate] Inventory {result.get('reservation_id')} released")

    def ship_order(ctx):
        return {"tracking_id": f"SHIP-{uuid.uuid4().hex[:6].upper()}"}

    def cancel_shipment(ctx, result):
        print(f"  [Compensate] Shipment {result.get('tracking_id')} cancelled")

    saga.add_step("Create Order", create_order, cancel_order)
    saga.add_step("Charge Payment", charge_payment, refund_payment)
    saga.add_step("Reserve Inventory", reserve_inventory, release_inventory)
    saga.add_step("Ship Order", ship_order, cancel_shipment)

    print(f"\nSaga {saga_id[:8]}: e-commerce order flow")
    result = saga.execute({"user_id": "user_42", "product": "Widget Pro"})
    print(f"\nResult: {result}")

    # Show audit trail
    history = log.get_saga_history(saga_id)
    print(f"\nAudit trail ({len(history)} events):")
    for entry in history:
        print(f"  [{entry['state']}] {entry['step'] or '(saga)'}")

    # Show incomplete sagas (for crash recovery)
    incomplete = log.find_incomplete_sagas()
    print(f"\nIncomplete sagas requiring recovery: {len(incomplete)}")

run_persistent_saga_demo()

# Expected Token Savings: ~0% — durable log adds I/O overhead; enables crash recovery
# Environment: Production: replace SQLite with PostgreSQL or DynamoDB for distributed deployments
```

---

## Option 3: Agent-Driven Saga — LLM Orchestrates the Compensations

```python
import anthropic
import json
import uuid
from dataclasses import dataclass, field
from typing import Callable

@dataclass
class AgentSagaContext:
    saga_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    completed_actions: list[dict] = field(default_factory=list)
    failed_at: str = ""
    compensation_log: list[str] = field(default_factory=list)

def run_agent_saga(goal: str, inject_failure: bool = True):
    client = anthropic.Anthropic()
    ctx = AgentSagaContext()

    # Saga action registry with compensations
    action_registry = {
        "create_user_account": {
            "fn": lambda params: {"user_id": f"USR-{uuid.uuid4().hex[:6].upper()}", **params},
            "compensate": "delete_user_account"
        },
        "allocate_storage": {
            "fn": lambda params: {"storage_id": f"STR-{uuid.uuid4().hex[:6].upper()}", "gb": params.get("gb", 10)},
            "compensate": "deallocate_storage"
        },
        "send_welcome_email": {
            "fn": lambda params: None if inject_failure else {"email_id": f"EMAIL-{uuid.uuid4().hex[:6].upper()}"},
            "should_fail": inject_failure,
            "compensate": "cancel_welcome_email"
        },
        # Compensating actions
        "delete_user_account": {"fn": lambda params: print(f"  [Compensate] Deleted user {params.get('user_id')}") or {}},
        "deallocate_storage": {"fn": lambda params: print(f"  [Compensate] Released storage {params.get('storage_id')}") or {}},
        "cancel_welcome_email": {"fn": lambda params: print(f"  [Compensate] Cancelled email {params.get('email_id', 'N/A')}") or {}},
    }

    tools = [
        {
            "name": "execute_saga_step",
            "description": "Execute a step in the saga workflow",
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "Action name from registry"},
                    "params": {"type": "object", "description": "Action parameters"}
                },
                "required": ["action"]
            }
        },
        {
            "name": "compensate_saga",
            "description": "Roll back completed saga steps due to failure",
            "input_schema": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Why compensation is needed"}
                },
                "required": ["reason"]
            }
        }
    ]

    def handle_tool_call(tool_name: str, tool_input: dict) -> str:
        if tool_name == "execute_saga_step":
            action_name = tool_input.get("action", "")
            params = tool_input.get("params", {})
            action = action_registry.get(action_name)
            if not action:
                return json.dumps({"error": f"Unknown action: {action_name}"})

            # Inject failure
            if action.get("should_fail"):
                ctx.failed_at = action_name
                return json.dumps({"error": f"Action failed: {action_name} - Service unavailable"})

            result = action["fn"]({**params, **{k: v for step in ctx.completed_actions for k, v in step.get("result", {}).items()}})
            ctx.completed_actions.append({
                "action": action_name,
                "params": params,
                "result": result or {},
                "compensate": action.get("compensate")
            })
            print(f"  -> {action_name}: OK {result}")
            return json.dumps({"success": True, "result": result or {}})

        elif tool_name == "compensate_saga":
            print(f"\n  [Agent] Compensating saga: {tool_input.get('reason')}")
            for step in reversed(ctx.completed_actions):
                if step.get("compensate"):
                    comp_action = action_registry.get(step["compensate"])
                    if comp_action:
                        comp_action["fn"](step["result"])
                        ctx.compensation_log.append(f"Compensated: {step['action']}")
            return json.dumps({"compensated": len(ctx.completed_actions)})

        return json.dumps({"error": "Unknown tool"})

    system = f"""You are a saga orchestrator. Execute this workflow:
Goal: {goal}

Steps to execute (in order):
1. create_user_account (params: username, email)
2. allocate_storage (params: gb=10)
3. send_welcome_email (params: template="welcome")

If ANY step fails, immediately call compensate_saga with the failure reason.
The compensate_saga tool will automatically roll back all completed steps.
Saga ID: {ctx.saga_id}"""

    messages = [{"role": "user", "content": f"Execute the saga for: {goal}"}]
    print(f"\n[Agent Saga {ctx.saga_id[:8]}] Goal: {goal}")
    print(f"Inject failure: {inject_failure}\n")

    for _ in range(10):  # Max turns
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            tools=tools,
            messages=messages
        )

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result_str = handle_tool_call(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_str
                })

        if response.stop_reason == "end_turn":
            final_text = next((b.text for b in response.content if b.type == "text"), "")
            print(f"\n[Agent] Final: {final_text[:150]}")
            break

        messages.append({"role": "assistant", "content": response.content})
        if tool_results:
            messages.append({"role": "user", "content": tool_results})

    print(f"\nCompleted steps: {[s['action'] for s in ctx.completed_actions]}")
    if ctx.compensation_log:
        print(f"Compensations: {ctx.compensation_log}")

print("=== Happy path ===")
run_agent_saga("Provision new user account for alice@example.com", inject_failure=False)
print("\n=== Failure path ===")
run_agent_saga("Provision new user account for bob@example.com", inject_failure=True)

# Expected Token Savings: ~0% — LLM orchestration adds overhead; value is automatic compensation
# Environment: Agentic workflows where the LLM decides which compensations to apply
```

---

## Option 4: Parallel Saga with Branch Compensation

```python
import anthropic
import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

@dataclass
class ParallelBranch:
    name: str
    action: Callable
    compensate: Callable
    result: dict = field(default_factory=dict)
    error: Optional[str] = None
    completed: bool = False

async def run_branch(branch: ParallelBranch, context: dict) -> bool:
    """Run a single saga branch. Returns True on success."""
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, branch.action, context
        )
        branch.result = result or {}
        branch.completed = True
        print(f"  [Parallel] {branch.name}: OK {result}")
        return True
    except Exception as e:
        branch.error = str(e)
        print(f"  [Parallel] {branch.name}: FAILED - {e}")
        return False

async def compensate_branch(branch: ParallelBranch, context: dict):
    """Compensate a completed branch."""
    if not branch.completed:
        return
    try:
        await asyncio.get_event_loop().run_in_executor(
            None, branch.compensate, context, branch.result
        )
        print(f"  [Compensate] {branch.name}: rolled back")
    except Exception as e:
        print(f"  [Compensate] {branch.name}: FAILED - {e} (manual intervention needed)")

async def execute_parallel_saga(
    parallel_branches: list[ParallelBranch],
    sequential_steps: list[tuple],  # (name, action, compensate)
    context: dict
) -> dict:
    """
    Run parallel branches first, then sequential steps.
    If any parallel branch fails, compensate all successful branches.
    If any sequential step fails, compensate sequential + all parallel branches.
    """
    saga_id = str(uuid.uuid4())
    print(f"\n[Parallel Saga {saga_id[:8]}]")

    # Phase 1: Run branches in parallel
    print(f"Phase 1: Running {len(parallel_branches)} branches in parallel...")
    tasks = [run_branch(b, context) for b in parallel_branches]
    results = await asyncio.gather(*tasks, return_exceptions=False)

    # Check if any branch failed
    failed_branches = [b for b, ok in zip(parallel_branches, results) if not ok]
    if failed_branches:
        print(f"\nPhase 1 failed: {[b.name for b in failed_branches]}")
        print("Compensating successful parallel branches...")
        successful = [b for b in parallel_branches if b.completed]
        comp_tasks = [compensate_branch(b, context) for b in reversed(successful)]
        await asyncio.gather(*comp_tasks)
        return {"success": False, "failed_at": "parallel_phase", "failed": [b.name for b in failed_branches]}

    # Merge parallel results into context
    for branch in parallel_branches:
        context.update(branch.result)

    # Phase 2: Sequential steps
    completed_sequential = []
    print(f"\nPhase 2: Running {len(sequential_steps)} sequential steps...")
    for name, action, compensate in sequential_steps:
        print(f"  -> {name}...", end=" ")
        try:
            result = action(context)
            context.update(result or {})
            completed_sequential.append((name, compensate, result or {}))
            print(f"OK {result}")
        except Exception as e:
            print(f"FAILED: {e}")
            print("\nCompensating sequential steps...")
            for comp_name, comp_fn, comp_result in reversed(completed_sequential):
                try:
                    comp_fn(context, comp_result)
                    print(f"  [Compensate] {comp_name}: OK")
                except Exception as ce:
                    print(f"  [Compensate] {comp_name}: FAILED - {ce}")

            print("\nCompensating parallel branches...")
            comp_tasks = [compensate_branch(b, context) for b in reversed(parallel_branches)]
            await asyncio.gather(*comp_tasks)
            return {"success": False, "failed_at": name}

    return {"success": True, "context": context}

async def main():
    client = anthropic.Anthropic()

    # Parallel: fetch user data + fetch payment methods simultaneously
    # Sequential: create order → ship → notify

    fail_ship = True  # Simulate shipping failure

    branches = [
        ParallelBranch(
            name="Fetch User Profile",
            action=lambda ctx: {"user_name": "Alice", "user_email": "alice@example.com"},
            compensate=lambda ctx, r: print(f"  [Compensate] Released user lock for {r.get('user_name')}")
        ),
        ParallelBranch(
            name="Validate Payment Method",
            action=lambda ctx: {"payment_token": f"tok_{uuid.uuid4().hex[:8]}", "payment_valid": True},
            compensate=lambda ctx, r: print(f"  [Compensate] Released payment token {r.get('payment_token')}")
        ),
        ParallelBranch(
            name="Check Inventory",
            action=lambda ctx: {"sku": "WIDGET-PRO", "inventory_reserved": True},
            compensate=lambda ctx, r: print(f"  [Compensate] Released inventory for {r.get('sku')}")
        ),
    ]

    def create_order(ctx):
        return {"order_id": f"ORD-{uuid.uuid4().hex[:6].upper()}"}

    def cancel_order(ctx, result):
        print(f"  [Compensate] Order {result.get('order_id')} cancelled")

    def ship_order(ctx):
        if fail_ship:
            raise ValueError("Shipping carrier API unavailable")
        return {"tracking_id": f"SHIP-{uuid.uuid4().hex[:6].upper()}"}

    def cancel_shipment(ctx, result):
        print(f"  [Compensate] Shipment {result.get('tracking_id', 'N/A')} cancelled")

    result = await execute_parallel_saga(
        parallel_branches=branches,
        sequential_steps=[
            ("Create Order", create_order, cancel_order),
            ("Ship Order", ship_order, cancel_shipment),
        ],
        context={"product_id": "WIDGET-PRO", "quantity": 1}
    )
    print(f"\nFinal result: {result}")

asyncio.run(main())

# Expected Token Savings: ~0% — asyncio parallelism reduces wall-clock time by N/1 for parallel phase
# Environment: asyncio required; parallel branches reduce latency for independent resource acquisition
```

---

## Option 5: Saga with Idempotency Keys — Safe Retries

```python
import anthropic
import json
import hashlib
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

class IdempotencyStore:
    """Track operation results by idempotency key — enables safe retries."""

    def __init__(self, db_path: str = "/tmp/idempotency.db"):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS idempotent_ops (
                key TEXT PRIMARY KEY,
                status TEXT,
                result TEXT,
                created_at REAL
            )
        """)
        self.db.commit()

    def get(self, key: str) -> Optional[dict]:
        row = self.db.execute(
            "SELECT status, result FROM idempotent_ops WHERE key = ?", (key,)
        ).fetchone()
        if row:
            return {"status": row[0], "result": json.loads(row[1])}
        return None

    def set(self, key: str, status: str, result: dict):
        self.db.execute(
            "INSERT OR REPLACE INTO idempotent_ops VALUES (?, ?, ?, ?)",
            (key, status, json.dumps(result), time.time())
        )
        self.db.commit()

def make_idempotency_key(saga_id: str, step_name: str, params: dict) -> str:
    content = f"{saga_id}:{step_name}:{json.dumps(params, sort_keys=True)}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]

@dataclass
class IdempotentSagaStep:
    name: str
    action: Callable
    compensate: Callable
    max_retries: int = 3
    retry_delay_s: float = 0.1

class IdempotentSaga:
    def __init__(self, saga_id: str, idempotency_store: IdempotencyStore):
        self.saga_id = saga_id
        self.store = idempotency_store
        self.steps: list[IdempotentSagaStep] = []
        self._completed: list[tuple] = []  # (step, result, idem_key)

    def add_step(self, name: str, action: Callable, compensate: Callable, max_retries: int = 3):
        self.steps.append(IdempotentSagaStep(name, action, compensate, max_retries))

    def execute(self, context: dict) -> dict:
        for step in self.steps:
            idem_key = make_idempotency_key(self.saga_id, step.name, context)

            # Check if already completed (idempotent replay)
            cached = self.store.get(idem_key)
            if cached and cached["status"] == "completed":
                print(f"  -> {step.name}: CACHED (idempotent replay)")
                context.update(cached["result"])
                self._completed.append((step, cached["result"], idem_key))
                continue

            # Execute with retry
            last_error = None
            for attempt in range(step.max_retries):
                print(f"  -> {step.name} (attempt {attempt+1}/{step.max_retries})...", end=" ")
                try:
                    result = step.action(context)
                    result = result or {}
                    self.store.set(idem_key, "completed", result)
                    context.update(result)
                    self._completed.append((step, result, idem_key))
                    print(f"OK")
                    last_error = None
                    break
                except Exception as e:
                    last_error = e
                    print(f"FAILED: {e}")
                    if attempt < step.max_retries - 1:
                        time.sleep(step.retry_delay_s)

            if last_error:
                self.store.set(idem_key, "failed", {"error": str(last_error)})
                self._compensate(context)
                return {"success": False, "error": str(last_error), "failed_step": step.name}

        return {"success": True, "context": context}

    def _compensate(self, context: dict):
        print(f"\n  [Rollback] Compensating {len(self._completed)} steps...")
        for step, result, idem_key in reversed(self._completed):
            print(f"  <- Compensating {step.name}...", end=" ")
            comp_key = idem_key + "_comp"
            if self.store.get(comp_key):
                print("ALREADY COMPENSATED (idempotent)")
                continue
            try:
                step.compensate(context, result)
                self.store.set(comp_key, "compensated", {})
                print("OK")
            except Exception as e:
                print(f"FAILED: {e}")

def run_idempotent_saga_demo():
    store = IdempotencyStore()
    attempt_count = [0]

    def flaky_action(ctx):
        attempt_count[0] += 1
        if attempt_count[0] < 3:  # Fails first 2 times
            raise ConnectionError(f"Transient network error (attempt {attempt_count[0]})")
        return {"reservation_id": f"RES-{uuid.uuid4().hex[:6].upper()}"}

    saga_id = str(uuid.uuid4())
    saga = IdempotentSaga(saga_id, store)

    saga.add_step(
        "Create Reservation", flaky_action,
        lambda ctx, r: print(f"  [Compensate] Reservation {r.get('reservation_id')} cancelled"),
        max_retries=5
    )
    saga.add_step(
        "Send Confirmation",
        lambda ctx: {"email_sent": True, "reservation_id": ctx.get("reservation_id")},
        lambda ctx, r: print(f"  [Compensate] Confirmation email revoked"),
        max_retries=3
    )

    print(f"\nSaga {saga_id[:8]}: testing idempotent retries")
    result = saga.execute({"user_id": "user_99"})
    print(f"\nResult: {result}")

    # Replay same saga_id — all steps hit cache (idempotent)
    print(f"\nReplaying saga (simulating process restart)...")
    attempt_count[0] = 99  # Would fail if not cached
    saga2 = IdempotentSaga(saga_id, store)
    saga2.add_step("Create Reservation", flaky_action, lambda ctx, r: None, max_retries=5)
    saga2.add_step("Send Confirmation", lambda ctx: None, lambda ctx, r: None, max_retries=3)
    result2 = saga2.execute({"user_id": "user_99"})
    print(f"Replay result: {result2}")

run_idempotent_saga_demo()

# Expected Token Savings: ~0% — idempotency prevents duplicate charges; retries handle transient errors
# Environment: Payment flows, reservation systems, any saga step that must not execute twice
```

---

## Option 6: Saga Monitor — Track All In-Flight Sagas and Detect Stuck Ones

```python
import anthropic
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

class SagaMonitor:
    """Production saga health monitor — detects stuck, failed, and completed sagas."""

    def __init__(self, db_path: str = "/tmp/saga_monitor.db", stuck_threshold_s: float = 30.0):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.stuck_threshold = stuck_threshold_s
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sagas (
                saga_id TEXT PRIMARY KEY,
                goal TEXT,
                status TEXT DEFAULT 'running',
                created_at REAL,
                updated_at REAL,
                total_steps INTEGER,
                current_step INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS saga_steps (
                step_id TEXT PRIMARY KEY,
                saga_id TEXT,
                step_name TEXT,
                status TEXT,
                started_at REAL,
                completed_at REAL,
                error TEXT
            );
        """)
        self.db.commit()

    def register_saga(self, saga_id: str, goal: str, total_steps: int):
        self.db.execute(
            "INSERT OR IGNORE INTO sagas VALUES (?, ?, 'running', ?, ?, ?, 0)",
            (saga_id, goal[:200], time.time(), time.time(), total_steps)
        )
        self.db.commit()

    def step_started(self, saga_id: str, step_name: str) -> str:
        step_id = str(uuid.uuid4())
        self.db.execute(
            "INSERT INTO saga_steps VALUES (?, ?, ?, 'running', ?, NULL, NULL)",
            (step_id, saga_id, step_name, time.time())
        )
        self.db.execute(
            "UPDATE sagas SET current_step = current_step + 1, updated_at = ? WHERE saga_id = ?",
            (time.time(), saga_id)
        )
        self.db.commit()
        return step_id

    def step_completed(self, step_id: str, success: bool, error: str = None):
        status = "completed" if success else "failed"
        self.db.execute(
            "UPDATE saga_steps SET status = ?, completed_at = ?, error = ? WHERE step_id = ?",
            (status, time.time(), error, step_id)
        )
        self.db.commit()

    def saga_finished(self, saga_id: str, status: str):
        self.db.execute(
            "UPDATE sagas SET status = ?, updated_at = ? WHERE saga_id = ?",
            (status, time.time(), saga_id)
        )
        self.db.commit()

    def get_stuck_sagas(self) -> list[dict]:
        threshold = time.time() - self.stuck_threshold
        rows = self.db.execute("""
            SELECT saga_id, goal, current_step, total_steps, updated_at
            FROM sagas
            WHERE status = 'running' AND updated_at < ?
        """, (threshold,)).fetchall()
        return [
            {"saga_id": r[0], "goal": r[1], "current_step": r[2],
             "total_steps": r[3], "stuck_for_s": time.time() - r[4]}
            for r in rows
        ]

    def dashboard(self):
        rows = self.db.execute("""
            SELECT status, COUNT(*) FROM sagas GROUP BY status
        """).fetchall()
        print("\n=== Saga Monitor Dashboard ===")
        for status, count in rows:
            print(f"  {status}: {count}")
        stuck = self.get_stuck_sagas()
        if stuck:
            print(f"  STUCK (>{self.stuck_threshold}s): {len(stuck)}")
            for s in stuck:
                print(f"    [{s['saga_id'][:8]}] {s['goal'][:40]} "
                      f"(step {s['current_step']}/{s['total_steps']}, stuck {s['stuck_for_s']:.0f}s)")

def run_monitored_saga(goal: str, fail_at_step: Optional[int] = None):
    client = anthropic.Anthropic()
    monitor = SagaMonitor(stuck_threshold_s=5.0)
    saga_id = str(uuid.uuid4())
    steps = ["Validate Input", "Reserve Resource", "Process Payment", "Notify User"]

    monitor.register_saga(saga_id, goal, len(steps))
    completed = []

    print(f"\n[Monitored Saga {saga_id[:8]}] {goal}")

    for i, step_name in enumerate(steps):
        step_id = monitor.step_started(saga_id, step_name)
        print(f"  -> {step_name}...", end=" ")
        try:
            if fail_at_step == i:
                raise ValueError(f"Simulated failure at step {i}")
            # Simulate step work
            time.sleep(0.05)
            monitor.step_completed(step_id, True)
            completed.append((step_name, step_id))
            print("OK")
        except Exception as e:
            monitor.step_completed(step_id, False, str(e))
            print(f"FAILED: {e}")
            # Rollback
            for comp_name, _ in reversed(completed):
                print(f"  <- Compensating {comp_name}... OK")
            monitor.saga_finished(saga_id, "rolled_back")
            monitor.dashboard()
            return {"success": False}

    monitor.saga_finished(saga_id, "completed")
    monitor.dashboard()
    return {"success": True}

run_monitored_saga("Provision cloud resources for customer X", fail_at_step=None)
run_monitored_saga("Provision cloud resources for customer Y", fail_at_step=2)

# Expected Token Savings: ~0% monitoring overhead; dashboard enables ops team to catch stuck sagas
# Environment: Production: ship saga_id in traces; alert on stuck_sagas > 0 via PagerDuty
```

---

## Comparison

| Option | Durability | Crash Recovery | Parallel Steps | Idempotent | Best For |
|--------|-----------|---------------|---------------|-----------|----------|
| Choreography | In-memory | No | No | No | Simple multi-step with clean rollback |
| Persistent Log | SQLite/DB | Yes | No | No | Production sagas surviving process restarts |
| Agent-Driven | In-memory | No | No | No | LLM decides which compensations to apply |
| Parallel Branches | In-memory | No | Yes | No | Independent parallel steps (fetch + validate) |
| Idempotency Keys | SQLite/DB | Yes | No | Yes | Payment flows where double-execution causes harm |
| Saga Monitor | SQLite/DB | Yes (detection) | No | No | Ops visibility into in-flight and stuck sagas |

**Recommendation:** Use **Option 2** (persistent saga log) as the production baseline — it survives crashes and provides a full audit trail. Add **Option 5** (idempotency keys) for any step that touches payments or sends messages. Run **Option 6** (saga monitor) continuously so your ops team can detect and recover stuck sagas before they cause user-visible impact.
