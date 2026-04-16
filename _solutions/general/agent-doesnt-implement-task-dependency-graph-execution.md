---
layout: solution
title: "Agent Doesn't Implement Task Dependency Graph Execution"
description: "How to build DAG-based task orchestration so dependent subtasks execute in the correct order while independent tasks run in parallel."
tags: [general, orchestration, concurrency, asyncio, dag, topological-sort]
difficulty: intermediate
solution_count: 6
---

## Problem

Agents decompose complex goals into subtasks but execute them sequentially or naively in parallel without respecting dependencies. This causes failures when a subtask runs before its inputs are ready, produces incorrect results from stale data, or wastes time waiting when parallel execution was safe.

```python
# Bad: sequential regardless of dependencies
async def run_subtasks(subtasks):
    results = {}
    for task in subtasks:  # ignores which tasks could run in parallel
        results[task.name] = await execute(task)
    return results
```

---

## Solution 1 — Simple Topological Sort with asyncio

Sort tasks by dependency depth, then execute each level concurrently.

```python
import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

@dataclass
class Task:
    name: str
    func: Callable[..., Awaitable[Any]]
    deps: list[str] = field(default_factory=list)
    args: dict = field(default_factory=dict)

def topological_levels(tasks: list[Task]) -> list[list[Task]]:
    """Group tasks into levels where all tasks in a level can run concurrently."""
    task_map = {t.name: t for t in tasks}
    in_degree = {t.name: 0 for t in tasks}
    dependents = defaultdict(list)

    for task in tasks:
        for dep in task.deps:
            in_degree[task.name] += 1
            dependents[dep].append(task.name)

    levels = []
    queue = deque(name for name, deg in in_degree.items() if deg == 0)

    while queue:
        level = list(queue)
        levels.append([task_map[name] for name in level])
        queue.clear()
        for name in level:
            for dependent in dependents[name]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

    if sum(len(lvl) for lvl in levels) != len(tasks):
        raise ValueError("Cycle detected in task dependency graph")

    return levels

async def execute_dag(tasks: list[Task]) -> dict[str, Any]:
    results: dict[str, Any] = {}

    for level in topological_levels(tasks):
        # All tasks in this level can run in parallel
        coros = [t.func(**{k: results.get(v, v) for k, v in t.args.items()}) for t in level]
        level_results = await asyncio.gather(*coros)
        for task, result in zip(level, level_results):
            results[task.name] = result

    return results

# Usage
async def fetch_user(user_id: str) -> dict:
    return {"id": user_id, "name": "Alice"}

async def fetch_orders(user_id: str) -> list:
    return [{"id": "o1", "user": user_id}]

async def compute_summary(user: dict, orders: list) -> dict:
    return {"user": user["name"], "order_count": len(orders)}

tasks = [
    Task("user", fetch_user, deps=[], args={"user_id": "u123"}),
    Task("orders", fetch_orders, deps=[], args={"user_id": "u123"}),
    Task("summary", compute_summary, deps=["user", "orders"],
         args={"user": "user", "orders": "orders"}),
]

results = asyncio.run(execute_dag(tasks))
```

---

## Solution 2 — DAG Executor with asyncio.Event per Node

Use `asyncio.Event` per task to allow downstream tasks to await their specific dependencies without polling.

```python
import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

@dataclass
class DAGNode:
    name: str
    func: Callable[..., Awaitable[Any]]
    deps: list[str] = field(default_factory=list)

class DAGExecutor:
    def __init__(self, nodes: list[DAGNode]):
        self._nodes = {n.name: n for n in nodes}
        self._results: dict[str, Any] = {}
        self._events: dict[str, asyncio.Event] = {n.name: asyncio.Event() for n in nodes}
        self._errors: dict[str, Exception] = {}

    async def _run_node(self, node: DAGNode) -> None:
        # Wait for all dependencies
        for dep in node.deps:
            await self._events[dep].wait()
            if dep in self._errors:
                self._errors[node.name] = RuntimeError(
                    f"Dependency '{dep}' failed: {self._errors[dep]}"
                )
                self._events[node.name].set()
                return

        try:
            dep_results = {dep: self._results[dep] for dep in node.deps}
            self._results[node.name] = await node.func(**dep_results)
        except Exception as e:
            self._errors[node.name] = e
        finally:
            self._events[node.name].set()

    async def run(self) -> dict[str, Any]:
        async with asyncio.TaskGroup() as tg:
            for node in self._nodes.values():
                tg.create_task(self._run_node(node))

        if self._errors:
            failed = list(self._errors.keys())
            raise RuntimeError(f"DAG execution failed for nodes: {failed}")

        return self._results

# Usage
async def step_a() -> str:
    await asyncio.sleep(0.1)
    return "result_a"

async def step_b() -> str:
    await asyncio.sleep(0.05)
    return "result_b"

async def step_c(step_a: str, step_b: str) -> str:
    return f"combined: {step_a} + {step_b}"

nodes = [
    DAGNode("step_a", step_a),
    DAGNode("step_b", step_b),
    DAGNode("step_c", step_c, deps=["step_a", "step_b"]),
]

executor = DAGExecutor(nodes)
results = asyncio.run(executor.run())
print(results["step_c"])  # "combined: result_a + result_b"
```

---

## Solution 3 — LLM-Driven DAG Builder + Executor

Let the LLM decompose a goal into a dependency graph, then execute it automatically.

```python
import asyncio
import json
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

DECOMPOSE_PROMPT = """Decompose this goal into subtasks with dependencies.
Return JSON: {"tasks": [{"name": str, "description": str, "deps": [str], "tool": str}]}
Dependencies must be valid task names. No circular dependencies.

Goal: {goal}"""

async def build_dag_from_goal(goal: str) -> list[dict]:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": DECOMPOSE_PROMPT.format(goal=goal)}],
    )
    text = response.content[0].text.strip()
    # Extract JSON
    start = text.find("{")
    end = text.rfind("}") + 1
    data = json.loads(text[start:end])
    return data["tasks"]

TOOL_REGISTRY = {
    "web_search": lambda description, **deps: asyncio.sleep(0.1, result=f"search results for: {description}"),
    "summarize": lambda description, **deps: asyncio.sleep(0.05, result=f"summary: {list(deps.values())}"),
    "format_report": lambda description, **deps: asyncio.sleep(0.02, result=f"report: {list(deps.values())}"),
}

async def execute_llm_dag(goal: str) -> dict[str, Any]:
    task_specs = await build_dag_from_goal(goal)

    results: dict[str, Any] = {}
    events: dict[str, asyncio.Event] = {t["name"]: asyncio.Event() for t in task_specs}

    async def run_task(spec: dict) -> None:
        for dep in spec.get("deps", []):
            await events[dep].wait()

        tool = TOOL_REGISTRY.get(spec["tool"],
            lambda description, **kw: asyncio.sleep(0, result=f"no-op: {description}"))
        dep_results = {dep: results[dep] for dep in spec.get("deps", [])}
        results[spec["name"]] = await tool(description=spec["description"], **dep_results)
        events[spec["name"]].set()

    async with asyncio.TaskGroup() as tg:
        for spec in task_specs:
            tg.create_task(run_task(spec))

    return results
```

---

## Solution 4 — Priority-Aware DAG with Critical Path Scheduling

Compute the critical path and prioritize tasks that lie on it to minimize total makespan.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

@dataclass
class PrioritizedTask:
    name: str
    func: Callable[..., Awaitable[Any]]
    deps: list[str] = field(default_factory=list)
    estimated_duration: float = 1.0  # seconds

def compute_critical_path(tasks: dict[str, PrioritizedTask]) -> dict[str, float]:
    """Return earliest-start time for each task (longest path to root)."""
    memo: dict[str, float] = {}

    def earliest_finish(name: str) -> float:
        if name in memo:
            return memo[name]
        task = tasks[name]
        if not task.deps:
            memo[name] = task.estimated_duration
        else:
            memo[name] = max(earliest_finish(d) for d in task.deps) + task.estimated_duration
        return memo[name]

    for name in tasks:
        earliest_finish(name)
    return memo

class CriticalPathDAGExecutor:
    def __init__(self, tasks: list[PrioritizedTask]):
        self._tasks = {t.name: t for t in tasks}
        self._results: dict[str, Any] = {}
        self._events = {t.name: asyncio.Event() for t in tasks}
        self._critical_finish = compute_critical_path(self._tasks)

    async def _run_task(self, task: PrioritizedTask) -> None:
        for dep in task.deps:
            await self._events[dep].wait()

        # Higher critical-path finish = higher priority (schedule first)
        priority = self._critical_finish[task.name]
        await asyncio.sleep(0)  # yield to allow priority-based scheduling hint

        dep_results = {dep: self._results[dep] for dep in task.deps}
        self._results[task.name] = await task.func(**dep_results)
        self._events[task.name].set()

    async def run(self) -> dict[str, Any]:
        async with asyncio.TaskGroup() as tg:
            # Submit highest-critical-path tasks first
            for task in sorted(self._tasks.values(),
                                key=lambda t: -self._critical_finish[t.name]):
                tg.create_task(self._run_task(task))
        return self._results

# Usage
async def slow_data_fetch() -> list:
    await asyncio.sleep(2.0)
    return [1, 2, 3]

async def fast_config_load() -> dict:
    await asyncio.sleep(0.1)
    return {"key": "value"}

async def process(slow_data_fetch: list, fast_config_load: dict) -> str:
    return f"processed {len(slow_data_fetch)} items with {fast_config_load}"

executor = CriticalPathDAGExecutor([
    PrioritizedTask("slow_data_fetch", slow_data_fetch, estimated_duration=2.0),
    PrioritizedTask("fast_config_load", fast_config_load, estimated_duration=0.1),
    PrioritizedTask("process", process, deps=["slow_data_fetch", "fast_config_load"],
                    estimated_duration=0.5),
])
```

---

## Solution 5 — Persistent DAG with Checkpoint and Resume

Persist task results so a crashed DAG can resume from where it left off without re-running completed tasks.

```python
import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable
from pathlib import Path

@dataclass
class CheckpointedTask:
    name: str
    func: Callable[..., Awaitable[Any]]
    deps: list[str] = field(default_factory=list)

class CheckpointedDAGExecutor:
    def __init__(self, tasks: list[CheckpointedTask], checkpoint_path: str):
        self._tasks = {t.name: t for t in tasks}
        self._checkpoint = Path(checkpoint_path)
        self._results: dict[str, Any] = self._load_checkpoint()
        self._events = {t.name: asyncio.Event() for t in tasks}

        # Pre-set events for already-completed tasks
        for name in self._results:
            if name in self._events:
                self._events[name].set()

    def _load_checkpoint(self) -> dict[str, Any]:
        if self._checkpoint.exists():
            with open(self._checkpoint) as f:
                data = json.load(f)
            print(f"Resuming from checkpoint: {list(data.keys())} already done")
            return data
        return {}

    def _save_checkpoint(self) -> None:
        with open(self._checkpoint, "w") as f:
            json.dump(self._results, f, indent=2, default=str)

    async def _run_task(self, task: CheckpointedTask) -> None:
        # Skip already-completed tasks
        if task.name in self._results:
            return

        for dep in task.deps:
            await self._events[dep].wait()

        dep_results = {dep: self._results[dep] for dep in task.deps}
        result = await task.func(**dep_results)
        self._results[task.name] = result
        self._save_checkpoint()
        self._events[task.name].set()
        print(f"Completed and checkpointed: {task.name}")

    async def run(self) -> dict[str, Any]:
        async with asyncio.TaskGroup() as tg:
            for task in self._tasks.values():
                tg.create_task(self._run_task(task))

        # Clean up checkpoint on successful completion
        if self._checkpoint.exists():
            self._checkpoint.unlink()

        return self._results

# Usage — survives process restart
executor = CheckpointedDAGExecutor(
    tasks=[
        CheckpointedTask("fetch_raw", lambda: asyncio.sleep(1, result={"rows": 1000})),
        CheckpointedTask("transform", lambda fetch_raw: asyncio.sleep(0.5, result={"rows": 900}),
                         deps=["fetch_raw"]),
        CheckpointedTask("load", lambda transform: asyncio.sleep(0.2, result="loaded"),
                         deps=["transform"]),
    ],
    checkpoint_path="/tmp/dag_checkpoint.json"
)
```

---

## Solution 6 — Multi-Agent DAG with Subagent Tool Calls

Distribute DAG nodes across Anthropic API calls, passing results through tool use.

```python
import asyncio
import json
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from typing import Any

client = AsyncAnthropic()

@dataclass
class AgentTask:
    name: str
    prompt_template: str  # uses {dep_name} placeholders
    deps: list[str] = field(default_factory=list)

async def run_agent_task(task: AgentTask, dep_results: dict[str, Any]) -> str:
    prompt = task.prompt_template.format(**{k: str(v) for k, v in dep_results.items()})
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()

class MultiAgentDAG:
    def __init__(self, tasks: list[AgentTask]):
        self._tasks = {t.name: t for t in tasks}
        self._results: dict[str, Any] = {}
        self._events = {t.name: asyncio.Event() for t in tasks}

    async def _run_node(self, task: AgentTask) -> None:
        for dep in task.deps:
            await self._events[dep].wait()

        dep_results = {dep: self._results[dep] for dep in task.deps}
        self._results[task.name] = await run_agent_task(task, dep_results)
        self._events[task.name].set()

    async def run(self, max_concurrency: int = 5) -> dict[str, Any]:
        semaphore = asyncio.Semaphore(max_concurrency)

        async def bounded_run(task: AgentTask) -> None:
            async with semaphore:
                await self._run_node(task)

        async with asyncio.TaskGroup() as tg:
            for task in self._tasks.values():
                tg.create_task(bounded_run(task))

        return self._results

# Example multi-agent research pipeline
dag = MultiAgentDAG([
    AgentTask(
        "market_research",
        "Summarize the EV market in 3 bullet points.",
    ),
    AgentTask(
        "competitor_analysis",
        "List top 3 EV competitors and their strengths.",
    ),
    AgentTask(
        "strategy_recommendation",
        "Given market: {market_research}\nCompetitors: {competitor_analysis}\n"
        "Recommend a product strategy in 2 sentences.",
        deps=["market_research", "competitor_analysis"],
    ),
])

results = asyncio.run(dag.run())
print(results["strategy_recommendation"])
```

---

## Comparison

| Approach | Parallelism | Failure Handling | Resumable | Best For |
|---|---|---|---|---|
| Topological levels | Level-parallel | Raises on any failure | No | Simple pipelines |
| Event-per-node | Full parallel | Per-node error isolation | No | Complex dependency graphs |
| LLM-driven builder | Full parallel | Per-node | No | Dynamic goal decomposition |
| Critical path | Full parallel | Per-node | No | Minimizing makespan |
| Checkpoint + resume | Full parallel | Per-node | **Yes** | Long-running / crash-safe |
| Multi-agent | Bounded parallel | Per-node | No | LLM-heavy orchestration |
