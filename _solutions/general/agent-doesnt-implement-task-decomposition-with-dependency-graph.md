---
layout: solution
title: "Agent Doesn't Implement Task Decomposition with Dependency Graph"
category: general
description: "Break complex goals into subtasks with explicit dependencies, then execute independent subtasks in parallel and dependent ones sequentially using a DAG scheduler."
tags: [task-decomposition, dependency-graph, dag, parallel, planning, orchestration]
---

# Agent Doesn't Implement Task Decomposition with Dependency Graph

Agents tackle complex goals as monolithic prompts, executing each step sequentially even when many steps are independent and could run in parallel. A dependency graph decomposition identifies which subtasks depend on others, executes the independent ones concurrently, and feeds their outputs into downstream tasks — cutting total runtime dramatically.

## Option 1: Static DAG with Sequential Execution

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()


@dataclass
class Task:
    id: str
    description: str
    depends_on: list[str] = field(default_factory=list)
    result: str = ""
    done: bool = False


def topo_sort(tasks: dict[str, Task]) -> list[str]:
    visited: set[str] = set()
    order: list[str] = []

    def visit(tid: str) -> None:
        if tid in visited:
            return
        visited.add(tid)
        for dep in tasks[tid].depends_on:
            visit(dep)
        order.append(tid)

    for tid in tasks:
        visit(tid)
    return order


def execute_task(task: Task, context: dict[str, str]) -> str:
    dep_context = "\n".join(f"[{dep}]: {context[dep]}" for dep in task.depends_on if dep in context)
    prompt = f"Task: {task.description}"
    if dep_context:
        prompt += f"\n\nContext from previous tasks:\n{dep_context}"

    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    return r.content[0].text


def run_dag(tasks: dict[str, Task]) -> dict[str, str]:
    order = topo_sort(tasks)
    results: dict[str, str] = {}

    for tid in order:
        task = tasks[tid]
        print(f"[DAG] Executing: {tid} (deps={task.depends_on})")
        result = execute_task(task, results)
        results[tid] = result
        task.done = True

    return results


if __name__ == "__main__":
    tasks = {
        "requirements": Task("requirements", "List 3 key requirements for a URL shortener service."),
        "schema": Task("schema", "Design a database schema for a URL shortener.", depends_on=["requirements"]),
        "api": Task("api", "Design the REST API endpoints for a URL shortener.", depends_on=["requirements"]),
        "summary": Task("summary", "Summarize the complete design.", depends_on=["schema", "api"]),
    }
    results = run_dag(tasks)
    print("\n=== Final Summary ===\n", results["summary"][:300])

# Expected Token Savings: DAG prevents re-deriving context; each task gets only relevant inputs
# Environment: Python 3.9+; static DAG suits well-defined multi-step workflows
```

## Option 2: LLM-Generated Decomposition with Auto-DAG

```python
import json
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

DECOMPOSE_PROMPT = """Break the following goal into 4-6 subtasks with dependencies.
Return JSON with this schema:
{
  "tasks": [
    {"id": "t1", "description": "<what to do>", "depends_on": []},
    {"id": "t2", "description": "<what to do>", "depends_on": ["t1"]},
    ...
  ]
}
Make dependencies minimal — only add a dependency if a task truly needs the previous result.

Goal: {goal}"""


async def decompose_goal(goal: str) -> list[dict]:
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": DECOMPOSE_PROMPT.format(goal=goal)}],
    )
    try:
        return json.loads(r.content[0].text)["tasks"]
    except (json.JSONDecodeError, KeyError):
        return [{"id": "t1", "description": goal, "depends_on": []}]


async def execute_task(task: dict, results: dict[str, str]) -> str:
    dep_ctx = "\n".join(f"[{d}] {results[d]}" for d in task["depends_on"] if d in results)
    prompt = f"Task: {task['description']}"
    if dep_ctx:
        prompt += f"\n\nPrevious results:\n{dep_ctx}"
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    return r.content[0].text


async def run_auto_dag(goal: str) -> dict[str, str]:
    tasks = await decompose_goal(goal)
    task_map = {t["id"]: t for t in tasks}
    results: dict[str, str] = {}
    completed: set[str] = set()

    while len(completed) < len(tasks):
        # Find tasks whose deps are all complete
        ready = [
            t for t in tasks
            if t["id"] not in completed
            and all(dep in completed for dep in t["depends_on"])
        ]
        if not ready:
            break  # deadlock or done

        print(f"[DAG] Parallel batch: {[t['id'] for t in ready]}")
        batch_results = await asyncio.gather(*[execute_task(t, results) for t in ready])

        for task, result in zip(ready, batch_results):
            results[task["id"]] = result
            completed.add(task["id"])

    return results


async def main() -> None:
    goal = "Build a design document for a multi-agent task orchestration system."
    results = await run_auto_dag(goal)
    for tid, result in results.items():
        print(f"\n[{tid}] {result[:150]}")


asyncio.run(main())

# Expected Token Savings: Parallel ready-tasks cut wall time; LLM decomposition avoids hardcoding
# Environment: Python 3.11+; add max_parallel limit if API rate limits are tight
```

## Option 3: DAG with Result Propagation and Partial Failure Handling

```python
import asyncio
import anthropic
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.AsyncAnthropic()


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class DAGTask:
    id: str
    prompt: str
    depends_on: list[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    result: str = ""
    error: str = ""
    optional: bool = False  # optional tasks don't block dependents on failure


class DAGRunner:
    def __init__(self, tasks: list[DAGTask]) -> None:
        self._tasks = {t.id: t for t in tasks}
        self._lock = asyncio.Lock()

    def _ready_tasks(self) -> list[DAGTask]:
        result = []
        for task in self._tasks.values():
            if task.status != TaskStatus.PENDING:
                continue
            deps_ok = all(
                self._tasks[dep].status in (TaskStatus.DONE, TaskStatus.SKIPPED)
                or (self._tasks[dep].status == TaskStatus.FAILED and self._tasks[dep].optional)
                for dep in task.depends_on
            )
            deps_failed = any(
                self._tasks[dep].status == TaskStatus.FAILED and not self._tasks[dep].optional
                for dep in task.depends_on
            )
            if deps_failed:
                task.status = TaskStatus.SKIPPED
                task.error = "Dependency failed"
            elif deps_ok:
                result.append(task)
        return result

    async def _execute(self, task: DAGTask) -> None:
        async with self._lock:
            task.status = TaskStatus.RUNNING

        dep_context = "\n".join(
            f"[{dep}]: {self._tasks[dep].result[:200]}"
            for dep in task.depends_on
            if self._tasks[dep].status == TaskStatus.DONE
        )
        prompt = task.prompt
        if dep_context:
            prompt += f"\n\nContext:\n{dep_context}"

        try:
            r = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            async with self._lock:
                task.result = r.content[0].text
                task.status = TaskStatus.DONE
            print(f"[DAG] ✓ {task.id}")
        except Exception as e:
            async with self._lock:
                task.error = str(e)
                task.status = TaskStatus.FAILED
            print(f"[DAG] ✗ {task.id}: {e}")

    async def run(self) -> dict[str, str]:
        while True:
            ready = self._ready_tasks()
            if not ready:
                pending = [t for t in self._tasks.values() if t.status == TaskStatus.PENDING]
                if pending:
                    print(f"[DAG] Deadlock with {len(pending)} pending tasks")
                break
            await asyncio.gather(*[self._execute(t) for t in ready])

        return {tid: t.result for tid, t in self._tasks.items() if t.result}


async def main() -> None:
    tasks = [
        DAGTask("goal_clarify", "State 3 key goals for a distributed caching system."),
        DAGTask("perf_reqs", "List performance requirements for a distributed cache.", depends_on=["goal_clarify"]),
        DAGTask("consistency", "Explain consistency trade-offs for distributed caching.", depends_on=["goal_clarify"]),
        DAGTask("tech_options", "List 3 technology options for distributed caching.", depends_on=["goal_clarify"]),
        DAGTask("recommendation", "Recommend the best approach.", depends_on=["perf_reqs", "consistency", "tech_options"]),
    ]
    runner = DAGRunner(tasks)
    results = await runner.run()
    print("\n=== Recommendation ===\n", results.get("recommendation", "N/A")[:300])


asyncio.run(main())

# Expected Token Savings: Optional tasks allow partial failure; parallel branches reduce wall time 3x
# Environment: Python 3.11+; set optional=True for enrichment tasks that aren't blockers
```

## Option 4: SQLite-Persisted DAG with Resume-on-Crash

```python
import asyncio
import json
import sqlite3
import time
import anthropic
from dataclasses import dataclass

DB_PATH = "dag_state.db"
client = anthropic.AsyncAnthropic()


@dataclass
class Task:
    id: str
    description: str
    depends_on: list[str]


def init_db(run_id: str, tasks: list[Task]) -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dag_tasks (
            run_id TEXT, task_id TEXT, description TEXT,
            depends_on TEXT, status TEXT, result TEXT, ts REAL,
            PRIMARY KEY (run_id, task_id)
        )
    """)
    conn.commit()
    for t in tasks:
        conn.execute(
            "INSERT OR IGNORE INTO dag_tasks VALUES (?,?,?,?,'pending',NULL,?)",
            (run_id, t.id, t.description, json.dumps(t.depends_on), time.time()),
        )
    conn.commit()
    return conn


def get_status(conn: sqlite3.Connection, run_id: str) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT task_id, status, result, depends_on FROM dag_tasks WHERE run_id=?",
        (run_id,),
    ).fetchall()
    return {r[0]: {"status": r[1], "result": r[2], "deps": json.loads(r[3])} for r in rows}


def mark_done(conn: sqlite3.Connection, run_id: str, task_id: str, result: str) -> None:
    conn.execute(
        "UPDATE dag_tasks SET status='done', result=?, ts=? WHERE run_id=? AND task_id=?",
        (result, time.time(), run_id, task_id),
    )
    conn.commit()


async def execute_task(task_id: str, description: str, dep_results: dict[str, str]) -> str:
    ctx = "\n".join(f"[{k}]: {v[:150]}" for k, v in dep_results.items())
    prompt = f"Task: {description}"
    if ctx:
        prompt += f"\n\nPrevious results:\n{ctx}"
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    return r.content[0].text


async def run_persistent_dag(run_id: str, tasks: list[Task]) -> dict[str, str]:
    conn = init_db(run_id, tasks)

    while True:
        state = get_status(conn, run_id)
        ready = [
            tid for tid, info in state.items()
            if info["status"] == "pending"
            and all(state[dep]["status"] == "done" for dep in info["deps"] if dep in state)
        ]
        if not ready:
            break

        print(f"[DAG] Batch: {ready}")

        async def run_one(tid: str) -> None:
            info = state[tid]
            dep_results = {dep: state[dep]["result"] or "" for dep in info["deps"]}
            result = await execute_task(tid, info.get("description") or tid, dep_results)
            mark_done(conn, run_id, tid, result)
            print(f"[DAG] ✓ {tid} (persisted)")

        await asyncio.gather(*[run_one(tid) for tid in ready])

    final_state = get_status(conn, run_id)
    conn.close()
    return {tid: info["result"] or "" for tid, info in final_state.items() if info["result"]}


async def main() -> None:
    run_id = "design_run_001"
    tasks = [
        Task("scope",   "Define the scope of a Python logging library.", []),
        Task("api",     "Design the public API for the logging library.", ["scope"]),
        Task("storage", "Design the storage backend for the logging library.", ["scope"]),
        Task("docs",    "Write the README introduction.", ["api", "storage"]),
    ]
    results = await run_persistent_dag(run_id, tasks)
    print("\n=== README Intro ===\n", results.get("docs", "N/A")[:300])

    # Second run demonstrates resume
    print("\n=== Resume run (all tasks already done) ===")
    results2 = await run_persistent_dag(run_id, tasks)
    print("Docs result reused:", bool(results2.get("docs")))


asyncio.run(main())

# Expected Token Savings: Persisted results survive crashes; re-runs skip completed tasks entirely
# Environment: Python 3.11+, SQLite3; use unique run_id per logical pipeline instance
```

## Option 5: Dynamic Fan-Out with Aggregation Step

```python
import asyncio
import anthropic
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()


@dataclass
class FanOutTask:
    topic: str
    angle: str


async def analyze_angle(topic: str, angle: str) -> tuple[str, str]:
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": f"Analyze '{topic}' from the perspective of: {angle}. Be concise (2-3 sentences)."}],
    )
    return angle, r.content[0].text


async def aggregate_insights(topic: str, insights: dict[str, str]) -> str:
    insight_block = "\n\n".join(f"[{angle}]:\n{text}" for angle, text in insights.items())
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": f"Synthesize these perspectives on '{topic}' into a balanced summary:\n\n{insight_block}"}],
    )
    return r.content[0].text


async def decompose_and_fanout(topic: str, angles: list[str]) -> str:
    print(f"[DAG] Fan-out: analyzing '{topic}' from {len(angles)} angles in parallel")

    # Phase 1: Parallel analysis (independent tasks)
    results = await asyncio.gather(*[analyze_angle(topic, a) for a in angles])
    insights = dict(results)

    print(f"[DAG] Fan-in: aggregating {len(insights)} insights")

    # Phase 2: Aggregation (depends on all fan-out results)
    summary = await aggregate_insights(topic, insights)
    return summary


async def main() -> None:
    topic = "Adopting async Python for AI agent systems"
    angles = [
        "performance and scalability",
        "developer experience and debugging",
        "risk and failure modes",
        "migration path from synchronous code",
    ]
    summary = await decompose_and_fanout(topic, angles)
    print("\n=== Synthesized Summary ===\n", summary)


asyncio.run(main())

# Expected Token Savings: Parallel fan-out reduces wall time 4x; aggregation uses all angles at once
# Environment: Python 3.11+; replace angles with LLM-generated sub-questions for dynamic decomposition
```

## Option 6: Priority-Ordered DAG with Critical Path Detection

```python
import asyncio
import anthropic
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()


@dataclass
class PTask:
    id: str
    prompt: str
    depends_on: list[str] = field(default_factory=list)
    priority: int = 1       # higher = runs first when multiple tasks are ready
    estimated_tokens: int = 200
    result: str = ""
    done: bool = False


def critical_path_length(task_id: str, tasks: dict[str, PTask], memo: dict[str, int]) -> int:
    if task_id in memo:
        return memo[task_id]
    task = tasks[task_id]
    if not task.depends_on:
        memo[task_id] = task.estimated_tokens
        return task.estimated_tokens
    path = task.estimated_tokens + max(
        critical_path_length(dep, tasks, memo) for dep in task.depends_on
    )
    memo[task_id] = path
    return path


async def execute(task: PTask, dep_results: dict[str, str]) -> str:
    ctx = "\n".join(f"[{k}]: {v[:150]}" for k, v in dep_results.items())
    prompt = task.prompt + (f"\n\nContext:\n{ctx}" if ctx else "")
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=task.estimated_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return r.content[0].text


async def run_priority_dag(tasks_list: list[PTask]) -> dict[str, str]:
    tasks = {t.id: t for t in tasks_list}
    memo: dict[str, int] = {}

    # Compute critical path for each task (longer path = higher urgency)
    for tid in tasks:
        critical_path_length(tid, tasks, memo)

    while True:
        ready = sorted(
            [t for t in tasks.values() if not t.done and all(tasks[d].done for d in t.depends_on)],
            key=lambda t: (memo.get(t.id, 0) + t.priority * 100),
            reverse=True,
        )
        if not ready:
            break

        print(f"[DAG] Ready (priority-ordered): {[(t.id, memo.get(t.id,0)) for t in ready]}")

        # Execute all ready tasks in parallel
        async def run_one(task: PTask) -> None:
            dep_results = {dep: tasks[dep].result for dep in task.depends_on}
            task.result = await execute(task, dep_results)
            task.done = True

        await asyncio.gather(*[run_one(t) for t in ready])

    return {tid: t.result for tid, t in tasks.items()}


async def main() -> None:
    tasks = [
        PTask("user_stories",  "Write 3 user stories for a code review tool.", priority=2),
        PTask("acceptance",    "Write acceptance criteria.", depends_on=["user_stories"], priority=2),
        PTask("tech_stack",    "Recommend a tech stack.", depends_on=["user_stories"], priority=1),
        PTask("architecture",  "Design the high-level architecture.", depends_on=["tech_stack", "acceptance"], priority=3, estimated_tokens=300),
        PTask("timeline",      "Estimate a 3-sprint timeline.", depends_on=["architecture"], priority=1),
    ]
    results = await run_priority_dag(tasks)
    print("\n=== Architecture ===\n", results.get("architecture", "")[:300])
    print("\n=== Timeline ===\n", results.get("timeline", "")[:200])


asyncio.run(main())

# Expected Token Savings: Critical path scheduling runs blockers first; total tokens unchanged but faster
# Environment: Python 3.11+; set estimated_tokens to actual expected output size for accurate scheduling
```

## Comparison

| Option | Decomposition | Parallel Exec | Persistence | Failure Handling | Best For |
|--------|--------------|--------------|------------|-----------------|----------|
| 1. Static DAG | Manual | No | No | No | Fixed pipelines |
| 2. LLM-Generated | Auto (haiku) | Yes | No | No | Dynamic goals |
| 3. Status Enum | Manual | Yes | No | Optional deps | Partial failures |
| 4. SQLite-Persisted | Manual | Yes | SQLite | Skip on failure | Long-running pipelines |
| 5. Fan-Out/Fan-In | Manual angles | Yes | No | No | Parallel analysis |
| 6. Priority + Critical Path | Manual | Yes | No | No | Deadline-aware scheduling |
