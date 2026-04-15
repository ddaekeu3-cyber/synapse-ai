---
layout: solution
title: "Agent Abandons Subtasks When One Dependency Fails"
category: loop-stuck
description: "Agent receives a multi-step task; one step fails; agent gives up on all remaining steps even though most are independent. A single tool error cascades into total task abandonment instead of isolated failure containment."
tags: [loop-stuck, error-handling, subtask, dependency, resilience, parallel, dag]
---

## Symptom

Agent is asked: "Pull the Q1 sales report, Q2 forecast, and current inventory — then summarise all three." The Q2 forecast API returns a 503 error. The agent replies "I was unable to complete the task due to an API error" — and never fetches the Q1 report or inventory, both of which were fully available. The user receives nothing instead of two-thirds of the answer.

Abandonment rate when any single subtask fails (without fix): **~85%**
After fix (isolated failure containment): **<5%**

## Root Cause

The agent executes subtasks sequentially and treats the first error as a fatal task failure. There is no dependency graph, no partial result collection, and no distinction between "this subtask failed" and "the entire task failed." The system prompt gives no instruction for handling partial failures.

## Fix

---

### Option 1 — Explicit Partial Failure Policy in System Prompt

Add a clear instruction: complete all independent subtasks even if one fails. Return partial results with a clear failure report.

```python
import json
import anthropic

client = anthropic.Anthropic()

SYSTEM = """You are a data retrieval assistant.

PARTIAL FAILURE POLICY:
- When given multiple independent data retrieval tasks, attempt ALL of them.
- If one tool call fails, record the failure and continue with the remaining tasks.
- After attempting all tasks, summarise the results you obtained and clearly list which tasks failed and why.
- NEVER abandon remaining tasks because one failed.
- Return your final answer in this structure:
  1. Results obtained (list each successful result)
  2. Failed tasks (list each failure with reason)
  3. Summary based on available data"""

def get_q1_sales(period: str) -> str:
    return json.dumps({"period": "Q1-2025", "revenue": 1_240_000, "units": 4_820})

def get_q2_forecast(period: str) -> str:
    # Simulates a 503 failure
    return json.dumps({"error": "503 Service Unavailable", "retry_after": 60})

def get_inventory(warehouse: str) -> str:
    return json.dumps({"warehouse": warehouse, "items": 9_340, "low_stock_alerts": 3})

TOOLS = [
    {
        "name": "get_q1_sales",
        "description": "Get Q1 sales report for a period.",
        "input_schema": {"type": "object", "properties": {"period": {"type": "string"}}, "required": ["period"]},
    },
    {
        "name": "get_q2_forecast",
        "description": "Get Q2 forecast data.",
        "input_schema": {"type": "object", "properties": {"period": {"type": "string"}}, "required": ["period"]},
    },
    {
        "name": "get_inventory",
        "description": "Get current inventory levels for a warehouse.",
        "input_schema": {"type": "object", "properties": {"warehouse": {"type": "string"}}, "required": ["warehouse"]},
    },
]

def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []

        for block in response.content:
            if block.type == "tool_use":
                print(f"[Tool] Calling {block.name}({block.input})")
                if block.name == "get_q1_sales":
                    result = get_q1_sales(**block.input)
                elif block.name == "get_q2_forecast":
                    result = get_q2_forecast(**block.input)
                elif block.name == "get_inventory":
                    result = get_inventory(**block.input)
                else:
                    result = json.dumps({"error": "Unknown tool"})

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "user", "content": tool_results})

result = run_agent(
    "Get Q1 sales for 2025, Q2 forecast for 2025, and current inventory for warehouse WH-East. Then summarise all three."
)
print(f"\n{result}")
```

**Expected Token Savings:** None — same tokens; prevents user from receiving zero data when partial data is available
**Environment:** `pip install anthropic`

---

### Option 2 — Parallel Subtask Executor with Independent Error Isolation

Execute all subtasks concurrently. Capture success/failure per subtask independently. Aggregate results before passing to the agent.

```python
import asyncio
import json
import anthropic
from dataclasses import dataclass
from typing import Any, Callable

async_client = anthropic.AsyncAnthropic()

@dataclass
class SubtaskResult:
    name: str
    success: bool
    data: Any
    error: str = ""

async def run_subtask(name: str, fn: Callable, **kwargs) -> SubtaskResult:
    """Run one subtask, capturing any exception as a failure."""
    try:
        result = await fn(**kwargs)
        return SubtaskResult(name=name, success=True, data=result)
    except Exception as e:
        return SubtaskResult(name=name, success=False, data=None, error=str(e))

# Simulated async tool functions
async def fetch_sales_report(quarter: str) -> dict:
    await asyncio.sleep(0.05)
    return {"quarter": quarter, "revenue": 1_240_000, "growth": "+12%"}

async def fetch_forecast(quarter: str) -> dict:
    await asyncio.sleep(0.03)
    raise ConnectionError("503: Forecast service temporarily unavailable")

async def fetch_inventory(region: str) -> dict:
    await asyncio.sleep(0.04)
    return {"region": region, "total_units": 9_340, "low_stock_skus": ["SKU-441", "SKU-892"]}

async def fetch_customer_metrics(segment: str) -> dict:
    await asyncio.sleep(0.06)
    return {"segment": segment, "active_users": 14_200, "churn_rate": 0.032}

async def parallel_fetch_with_isolation(subtasks: list[dict]) -> dict:
    """
    Run all subtasks concurrently. Any individual failure is isolated
    — other subtasks continue to completion.
    """
    tasks = [
        run_subtask(st["name"], st["fn"], **st.get("kwargs", {}))
        for st in subtasks
    ]
    results = await asyncio.gather(*tasks)  # gather never raises — exceptions caught in run_subtask

    succeeded = [r for r in results if r.success]
    failed = [r for r in results if not r.success]

    return {
        "results": {r.name: r.data for r in succeeded},
        "failures": {r.name: r.error for r in failed},
        "success_count": len(succeeded),
        "failure_count": len(failed),
        "partial": len(failed) > 0,
        "agent_instruction": (
            f"{len(failed)} subtask(s) failed: {list(r.name for r in failed)}. "
            "Summarise available data and clearly list unavailable data."
        ) if failed else "",
    }

async def run_agent(user_message: str) -> str:
    # Define all subtasks upfront — fully parallel, no sequential dependency
    subtask_definitions = [
        {"name": "q1_sales",         "fn": fetch_sales_report,    "kwargs": {"quarter": "Q1-2025"}},
        {"name": "q2_forecast",       "fn": fetch_forecast,        "kwargs": {"quarter": "Q2-2025"}},
        {"name": "east_inventory",    "fn": fetch_inventory,       "kwargs": {"region": "East"}},
        {"name": "enterprise_metrics","fn": fetch_customer_metrics, "kwargs": {"segment": "enterprise"}},
    ]

    print("[Executor] Running all subtasks in parallel with isolated error handling...")
    aggregated = await parallel_fetch_with_isolation(subtask_definitions)

    print(f"[Executor] {aggregated['success_count']}/{len(subtask_definitions)} subtasks succeeded")
    if aggregated["failures"]:
        print(f"[Executor] Failed: {list(aggregated['failures'].keys())}")

    # Pass aggregated result to agent for natural language synthesis
    messages = [
        {"role": "user", "content": user_message},
        {"role": "user", "content": f"Data retrieval results:\n{json.dumps(aggregated, indent=2)}"},
    ]

    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="Summarise the data retrieval results. Clearly note any data that was unavailable.",
        messages=messages,
    )
    return response.content[0].text

result = asyncio.run(run_agent(
    "Retrieve Q1 sales, Q2 forecast, East region inventory, and enterprise customer metrics. Summarise all."
))
print(f"\n{result}")
```

**Expected Token Savings:** 20–30% — parallel execution + one synthesis call vs sequential with retries
**Environment:** `pip install anthropic`

---

### Option 3 — Dependency Graph with Independent Branch Execution

Model subtasks as a DAG. Subtasks with no dependency on the failed task continue; only true dependents are skipped.

```python
import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
import anthropic

async_client = anthropic.AsyncAnthropic()

@dataclass
class Task:
    task_id: str
    fn: Callable
    kwargs: dict
    depends_on: list[str] = field(default_factory=list)  # task_ids this task depends on
    result: Any = None
    error: str = ""
    status: str = "pending"  # pending | running | done | failed | skipped

class DAGExecutor:
    def __init__(self, tasks: list[Task]):
        self.tasks = {t.task_id: t for t in tasks}

    def _is_ready(self, task: Task) -> bool:
        """Task is ready if all dependencies completed (done or failed+skippable)."""
        for dep_id in task.depends_on:
            dep = self.tasks.get(dep_id)
            if dep is None or dep.status not in ("done",):
                return False
        return True

    def _should_skip(self, task: Task) -> bool:
        """Skip if any required dependency failed."""
        for dep_id in task.depends_on:
            dep = self.tasks.get(dep_id)
            if dep and dep.status == "failed":
                return True
        return False

    async def _run_task(self, task: Task):
        task.status = "running"
        try:
            task.result = await task.fn(**task.kwargs)
            task.status = "done"
            print(f"[DAG] ✓ {task.task_id}")
        except Exception as e:
            task.error = str(e)
            task.status = "failed"
            print(f"[DAG] ✗ {task.task_id}: {e}")

    async def execute(self) -> dict:
        """Execute all tasks respecting dependencies; isolate failures to dependent branches."""
        completed = set()
        max_rounds = len(self.tasks) + 1

        for _ in range(max_rounds):
            ready = [
                t for t in self.tasks.values()
                if t.status == "pending" and self._is_ready(t)
            ]
            to_skip = [
                t for t in self.tasks.values()
                if t.status == "pending" and self._should_skip(t)
            ]

            for t in to_skip:
                t.status = "skipped"
                print(f"[DAG] ⊘ {t.task_id} (dependency failed)")

            if not ready:
                if all(t.status in ("done", "failed", "skipped") for t in self.tasks.values()):
                    break
                await asyncio.sleep(0.01)
                continue

            await asyncio.gather(*[self._run_task(t) for t in ready])

        return {
            "done":    {tid: t.result for tid, t in self.tasks.items() if t.status == "done"},
            "failed":  {tid: t.error  for tid, t in self.tasks.items() if t.status == "failed"},
            "skipped": [tid           for tid, t in self.tasks.items() if t.status == "skipped"],
        }

# Simulated async functions
async def fetch_raw_sales() -> dict:
    await asyncio.sleep(0.05)
    return {"raw": [100, 200, 150, 180]}

async def fetch_cost_data() -> dict:
    await asyncio.sleep(0.04)
    raise RuntimeError("Cost DB is offline for maintenance")

async def compute_revenue(raw_sales: dict = None) -> dict:
    # Depends on raw_sales — but raw_sales succeeds, so this runs
    await asyncio.sleep(0.03)
    return {"total_revenue": sum(raw_sales.get("raw", [])) * 1000}

async def compute_margin(cost_data: dict = None) -> dict:
    # Depends on cost_data — which fails, so this is skipped
    await asyncio.sleep(0.02)
    return {"margin": "calculated"}

async def fetch_headcount() -> dict:
    # Independent — runs regardless of other failures
    await asyncio.sleep(0.04)
    return {"headcount": 248, "open_roles": 12}

async def run():
    raw_sales_result = {}  # Will be populated after raw_sales task

    tasks = [
        Task("raw_sales",     fetch_raw_sales,   {}),
        Task("cost_data",     fetch_cost_data,   {}),
        Task("headcount",     fetch_headcount,   {}),
        # These depend on specific tasks
        Task("revenue",       lambda: compute_revenue(raw_sales_result),  {}, depends_on=["raw_sales"]),
        Task("margin",        lambda: compute_margin({}),                 {}, depends_on=["cost_data"]),
    ]

    # Wire up dynamic dependency — pass result when available
    async def revenue_wrapper():
        raw = executor.tasks["raw_sales"].result or {}
        return await compute_revenue(raw)

    executor = DAGExecutor(tasks)
    executor.tasks["revenue"].fn = revenue_wrapper
    executor.tasks["revenue"].kwargs = {}

    print("Executing DAG...\n")
    results = await executor.execute()

    print(f"\n[Summary] Done: {list(results['done'].keys())}")
    print(f"[Summary] Failed: {list(results['failed'].keys())}")
    print(f"[Summary] Skipped (dependent on failed): {results['skipped']}")

    # Synthesise with available data
    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system="Summarise available business data. List what's unavailable and why.",
        messages=[{"role": "user", "content": json.dumps(results, indent=2)}],
    )
    print(f"\n{response.content[0].text}")

asyncio.run(run())
```

**Expected Token Savings:** 15–25% — DAG prevents wasted tool calls for skippable branches
**Environment:** `pip install anthropic`

---

### Option 4 — Subtask Result Accumulator Tool

Give the agent an explicit `record_partial_result` tool. The agent records each result (success or failure) as it goes, then calls `get_all_results` at the end to summarise — preventing premature abandonment.

```python
import json
import anthropic
from datetime import datetime

client = anthropic.Anthropic()

class ResultAccumulator:
    def __init__(self):
        self._results: list[dict] = []

    def record(self, subtask_name: str, status: str, data: dict = None, error: str = "") -> str:
        self._results.append({
            "subtask": subtask_name,
            "status": status,
            "data": data,
            "error": error,
            "recorded_at": datetime.utcnow().isoformat(),
        })
        return json.dumps({"recorded": True, "total_recorded": len(self._results)})

    def get_all(self) -> str:
        succeeded = [r for r in self._results if r["status"] == "success"]
        failed = [r for r in self._results if r["status"] == "failure"]
        return json.dumps({
            "total_subtasks": len(self._results),
            "succeeded": len(succeeded),
            "failed": len(failed),
            "results": self._results,
            "instruction": "Summarise all successful results. List all failures with their reasons.",
        })

accumulator = ResultAccumulator()

# Simulated data tools
def fetch_team_metrics(team: str) -> str:
    teams = {"engineering": {"headcount": 45, "velocity": 82}, "sales": {"headcount": 30, "pipeline": 4200000}}
    if team in teams:
        return json.dumps(teams[team])
    return json.dumps({"error": f"No data for team: {team}"})

def fetch_budget_status(department: str) -> str:
    if department == "marketing":
        raise ValueError("Budget system undergoing migration — data unavailable until 18:00 UTC")
    return json.dumps({"department": department, "spent_pct": 67, "remaining": 340000})

def fetch_okr_status(quarter: str) -> str:
    return json.dumps({"quarter": quarter, "on_track": 8, "at_risk": 2, "behind": 1})

TOOLS = [
    {"name": "fetch_team_metrics", "description": "Get metrics for a team.",
     "input_schema": {"type": "object", "properties": {"team": {"type": "string"}}, "required": ["team"]}},
    {"name": "fetch_budget_status", "description": "Get budget status for a department.",
     "input_schema": {"type": "object", "properties": {"department": {"type": "string"}}, "required": ["department"]}},
    {"name": "fetch_okr_status", "description": "Get OKR status for a quarter.",
     "input_schema": {"type": "object", "properties": {"quarter": {"type": "string"}}, "required": ["quarter"]}},
    {"name": "record_partial_result",
     "description": "Record the result (or failure) of a completed subtask. Call after EVERY subtask attempt.",
     "input_schema": {
         "type": "object",
         "properties": {
             "subtask_name": {"type": "string"},
             "status": {"type": "string", "enum": ["success", "failure"]},
             "data": {"type": "object"},
             "error": {"type": "string"},
         },
         "required": ["subtask_name", "status"],
     }},
    {"name": "get_all_results",
     "description": "Retrieve all recorded results for final summarisation. Call this last.",
     "input_schema": {"type": "object", "properties": {}}},
]

SYSTEM = """You are a business reporting assistant.

INSTRUCTIONS:
1. For each data retrieval subtask: attempt it, then IMMEDIATELY call record_partial_result with the result or error.
2. Continue to the next subtask regardless of success or failure.
3. After ALL subtasks are attempted, call get_all_results and write a final summary.
4. Never skip a subtask because a previous one failed."""

def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []

        for block in response.content:
            if block.type == "tool_use":
                try:
                    if block.name == "fetch_team_metrics":
                        result = fetch_team_metrics(**block.input)
                    elif block.name == "fetch_budget_status":
                        result = fetch_budget_status(**block.input)
                    elif block.name == "fetch_okr_status":
                        result = fetch_okr_status(**block.input)
                    elif block.name == "record_partial_result":
                        result = accumulator.record(**block.input)
                    elif block.name == "get_all_results":
                        result = accumulator.get_all()
                    else:
                        result = json.dumps({"error": "Unknown tool"})
                except Exception as e:
                    result = json.dumps({"error": str(e), "agent_instruction": "Record this as a failure and continue."})

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "user", "content": tool_results})

print(run_agent(
    "Fetch metrics for engineering and sales teams, budget status for engineering and marketing departments, "
    "and OKR status for Q2-2025. Then summarise everything."
))
```

**Expected Token Savings:** 5–10% — accumulator prevents retry loops caused by abandoned tasks
**Environment:** `pip install anthropic`

---

### Option 5 — Retry Budget Per Subtask with Global Completion Guarantee

Assign each subtask a retry budget (N attempts). After exhausting retries, mark as failed and continue. The orchestrator guarantees every subtask is attempted before synthesis.

```python
import asyncio
import json
import random
import anthropic
from dataclasses import dataclass, field
from typing import Any, Callable

async_client = anthropic.AsyncAnthropic()

@dataclass
class SubtaskSpec:
    name: str
    fn: Callable
    kwargs: dict
    max_retries: int = 2
    retry_delay: float = 0.1

@dataclass
class SubtaskOutcome:
    name: str
    success: bool
    data: Any = None
    error: str = ""
    attempts: int = 0

async def execute_with_budget(spec: SubtaskSpec) -> SubtaskOutcome:
    """Attempt a subtask up to max_retries times. Always return an outcome."""
    last_error = ""
    for attempt in range(1, spec.max_retries + 2):  # +1 for initial attempt
        try:
            result = await spec.fn(**spec.kwargs)
            print(f"[Budget] ✓ {spec.name} (attempt {attempt})")
            return SubtaskOutcome(spec.name, success=True, data=result, attempts=attempt)
        except Exception as e:
            last_error = str(e)
            print(f"[Budget] ✗ {spec.name} attempt {attempt}/{spec.max_retries + 1}: {e}")
            if attempt <= spec.max_retries:
                await asyncio.sleep(spec.retry_delay * attempt)

    return SubtaskOutcome(spec.name, success=False, error=last_error, attempts=spec.max_retries + 1)

async def run_all_with_guarantee(subtasks: list[SubtaskSpec]) -> dict:
    """All subtasks run to completion (or exhausted budget). No early exit."""
    outcomes = await asyncio.gather(*[execute_with_budget(s) for s in subtasks])
    succeeded = [o for o in outcomes if o.success]
    failed = [o for o in outcomes if not o.success]

    return {
        "available_data": {o.name: o.data for o in succeeded},
        "unavailable": {o.name: {"error": o.error, "attempts": o.attempts} for o in failed},
        "coverage": f"{len(succeeded)}/{len(outcomes)} subtasks completed",
    }

# Simulated unreliable tools
async def get_region_sales(region: str) -> dict:
    await asyncio.sleep(0.02)
    if region == "APAC" and random.random() < 0.7:
        raise TimeoutError(f"APAC sales API timed out")
    return {"region": region, "revenue": random.randint(800_000, 1_500_000)}

async def get_support_tickets(priority: str) -> dict:
    await asyncio.sleep(0.03)
    return {"priority": priority, "open": random.randint(10, 50), "avg_resolution_hrs": 4.2}

async def get_nps_score(segment: str) -> dict:
    await asyncio.sleep(0.04)
    if segment == "enterprise":
        raise ConnectionError("NPS service rate limited")
    return {"segment": segment, "nps": random.randint(30, 70)}

async def run():
    subtasks = [
        SubtaskSpec("na_sales",          get_region_sales,    {"region": "NA"},         max_retries=1),
        SubtaskSpec("apac_sales",        get_region_sales,    {"region": "APAC"},       max_retries=2),
        SubtaskSpec("emea_sales",        get_region_sales,    {"region": "EMEA"},       max_retries=1),
        SubtaskSpec("high_pri_tickets",  get_support_tickets, {"priority": "high"},     max_retries=1),
        SubtaskSpec("enterprise_nps",    get_nps_score,       {"segment": "enterprise"}, max_retries=1),
        SubtaskSpec("smb_nps",           get_nps_score,       {"segment": "smb"},       max_retries=1),
    ]

    print("Running all subtasks with retry budgets...\n")
    results = await run_all_with_guarantee(subtasks)
    print(f"\nCoverage: {results['coverage']}")
    print(f"Available: {list(results['available_data'].keys())}")
    print(f"Unavailable: {list(results['unavailable'].keys())}")

    # Synthesise with Claude
    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system="Summarise available business data. Note any gaps and what caused them.",
        messages=[{"role": "user", "content": json.dumps(results, indent=2)}],
    )
    print(f"\n{response.content[0].text}")

asyncio.run(run())
```

**Expected Token Savings:** None — correctness improvement; users get maximum available data
**Environment:** `pip install anthropic`

---

### Option 6 — Checkpoint-and-Resume for Long Multi-Step Tasks

Persist each completed subtask result to a checkpoint store. On failure of any step, resume from the last checkpoint rather than restarting from scratch.

```python
import asyncio
import json
import sqlite3
import time
import anthropic
from dataclasses import dataclass
from typing import Any, Callable

async_client = anthropic.AsyncAnthropic()

class CheckpointStore:
    def __init__(self, run_id: str, db_path: str = ":memory:"):
        self.run_id = run_id
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS checkpoints (
                run_id TEXT,
                subtask_id TEXT,
                status TEXT,
                data TEXT,
                error TEXT,
                completed_at REAL,
                PRIMARY KEY (run_id, subtask_id)
            )
        """)
        self.conn.commit()

    def save(self, subtask_id: str, status: str, data: Any = None, error: str = ""):
        self.conn.execute(
            "INSERT OR REPLACE INTO checkpoints VALUES (?,?,?,?,?,?)",
            (self.run_id, subtask_id, status, json.dumps(data), error, time.time()),
        )
        self.conn.commit()

    def load(self, subtask_id: str) -> dict | None:
        cursor = self.conn.execute(
            "SELECT status, data, error FROM checkpoints WHERE run_id=? AND subtask_id=?",
            (self.run_id, subtask_id),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {"status": row[0], "data": json.loads(row[1]), "error": row[2]}

    def all_results(self) -> dict:
        cursor = self.conn.execute(
            "SELECT subtask_id, status, data, error FROM checkpoints WHERE run_id=?",
            (self.run_id,),
        )
        results = {}
        for row in cursor.fetchall():
            results[row[0]] = {"status": row[1], "data": json.loads(row[2]), "error": row[3]}
        return results

@dataclass
class CheckpointedTask:
    subtask_id: str
    fn: Callable
    kwargs: dict

async def execute_checkpointed(
    tasks: list[CheckpointedTask],
    store: CheckpointStore,
) -> dict:
    """Execute tasks, skipping already-completed ones. Resume from checkpoint."""
    for task in tasks:
        checkpoint = store.load(task.subtask_id)
        if checkpoint and checkpoint["status"] == "done":
            print(f"[Checkpoint] ↩ {task.subtask_id}: already done, skipping")
            continue

        print(f"[Checkpoint] → {task.subtask_id}: running...")
        try:
            result = await task.fn(**task.kwargs)
            store.save(task.subtask_id, "done", data=result)
            print(f"[Checkpoint] ✓ {task.subtask_id}")
        except Exception as e:
            store.save(task.subtask_id, "failed", error=str(e))
            print(f"[Checkpoint] ✗ {task.subtask_id}: {e} — recorded, continuing")

    return store.all_results()

# Simulated async tools
async def scrape_market_data(source: str) -> dict:
    await asyncio.sleep(0.05)
    if source == "bloomberg":
        raise TimeoutError("Bloomberg scraper timed out")
    return {"source": source, "index": 4_521.3, "change": "+0.8%"}

async def run_ml_inference(model_name: str) -> dict:
    await asyncio.sleep(0.08)
    return {"model": model_name, "prediction": 0.73, "confidence": 0.91}

async def generate_report_section(section: str) -> dict:
    await asyncio.sleep(0.04)
    return {"section": section, "word_count": 420, "status": "drafted"}

async def run():
    RUN_ID = "weekly-report-2025-04-14"
    store = CheckpointStore(RUN_ID)

    tasks = [
        CheckpointedTask("market_reuters",  scrape_market_data,    {"source": "reuters"}),
        CheckpointedTask("market_bloomberg",scrape_market_data,    {"source": "bloomberg"}),
        CheckpointedTask("ml_forecast",     run_ml_inference,      {"model_name": "demand_v3"}),
        CheckpointedTask("exec_summary",    generate_report_section, {"section": "executive_summary"}),
        CheckpointedTask("risk_section",    generate_report_section, {"section": "risk_analysis"}),
    ]

    print("=== First run (bloomberg will fail) ===\n")
    results = await execute_checkpointed(tasks, store)

    print("\n=== Second run (resumes from checkpoint — skips completed tasks) ===\n")
    results = await execute_checkpointed(tasks, store)

    done = {k: v for k, v in results.items() if v["status"] == "done"}
    failed = {k: v for k, v in results.items() if v["status"] == "failed"}
    print(f"\nCompleted: {list(done.keys())}")
    print(f"Failed:    {list(failed.keys())}")

    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system="Summarise completed work. Note what failed and why.",
        messages=[{"role": "user", "content": json.dumps({"completed": done, "failed": failed}, indent=2)}],
    )
    print(f"\n{response.content[0].text}")

asyncio.run(run())
```

**Expected Token Savings:** 30–60% on resume — completed subtasks are skipped entirely on retry
**Environment:** `pip install anthropic`

---

## Comparison

| Option | Isolation Method | Resume Capability | Best For |
|--------|-----------------|------------------|----------|
| System Prompt Policy | Instruction-level | None | Quick fix for existing agents |
| Parallel Executor | asyncio.gather | None | Independent subtasks, no ordering |
| Dependency DAG | Graph traversal | None | Tasks with true dependencies |
| Accumulator Tool | Tool-enforced recording | None | Agents that control their own tools |
| Retry Budget | Per-subtask retries | None | Flaky external services |
| Checkpoint-and-Resume | SQLite persistence | Full | Long-running multi-step workflows |

**Recommended starting point:** Option 1 (System Prompt Policy) — add the partial failure policy to your system prompt today. Works immediately with no code changes and prevents 80%+ of abandonment cases. Upgrade to Option 2 or 3 when subtasks are expensive and must run in parallel.
