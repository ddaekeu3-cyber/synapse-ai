---
title: "Agent Doesn't Implement Goal Decomposition with Dependency Tracking"
description: "Break high-level goals into a dependency graph of subtasks, execute independent subtasks in parallel, and sequence dependent ones correctly."
category: general
difficulty: advanced
tags: [planning, decomposition, dependency-graph, asyncio, orchestration, parallelism]
---

# Agent Doesn't Implement Goal Decomposition with Dependency Tracking

## Problem

Agents given complex goals like "Write a research report on X" execute steps sequentially without understanding which steps could run in parallel. Worse, they often miss dependencies — running analysis before data gathering is complete. Goal decomposition with dependency tracking enables correct ordering, maximum parallelism, and clear progress visibility.

---

## Option 1: Simple Topological Sort Execution

```python
import asyncio
import anthropic
import json
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class Task:
    id: str
    description: str
    depends_on: list[str] = field(default_factory=list)
    result: str | None = None
    done: bool = False

async def decompose_goal(goal: str) -> list[Task]:
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system='''Decompose the goal into subtasks with dependencies.
Return JSON array: [{"id": "t1", "description": "...", "depends_on": []}]
Use depends_on to list IDs of tasks that must complete first.
Keep to 4-7 tasks. IDs must be strings like "t1", "t2", etc.''',
        messages=[{"role": "user", "content": f"Goal: {goal}"}]
    )
    try:
        tasks_data = json.loads(resp.content[0].text)
        return [Task(**t) for t in tasks_data]
    except Exception as e:
        raise ValueError(f"Failed to parse task decomposition: {e}\nRaw: {resp.content[0].text}")

async def execute_task(task: Task, completed: dict[str, str]) -> str:
    context = "\n".join([f"[{tid}]: {result[:200]}" for tid, result in completed.items() if tid in task.depends_on])
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=f"Prior context:\n{context}" if context else "Complete this task concisely.",
        messages=[{"role": "user", "content": task.description}]
    )
    return resp.content[0].text

async def run_dependency_graph(tasks: list[Task]) -> dict[str, str]:
    task_map = {t.id: t for t in tasks}
    completed: dict[str, str] = {}
    in_flight: dict[str, asyncio.Task] = {}

    while len(completed) < len(tasks):
        # Find tasks ready to run (all deps satisfied, not yet started)
        ready = [
            t for t in tasks
            if not t.done
            and t.id not in in_flight
            and all(dep in completed for dep in t.depends_on)
        ]

        # Launch all ready tasks in parallel
        for task in ready:
            print(f"[PLAN] Starting: {task.id} — {task.description[:50]}")
            in_flight[task.id] = asyncio.create_task(execute_task(task, completed))

        if not in_flight:
            break  # deadlock guard

        # Wait for any task to complete
        done_tasks, _ = await asyncio.wait(in_flight.values(), return_when=asyncio.FIRST_COMPLETED)
        for done in done_tasks:
            # Find which task_id this corresponds to
            for tid, t in list(in_flight.items()):
                if t == done:
                    result = await t
                    completed[tid] = result
                    task_map[tid].done = True
                    task_map[tid].result = result
                    del in_flight[tid]
                    print(f"[PLAN] Completed: {tid}")
                    break

    return completed

async def main():
    goal = "Write a comprehensive summary of the benefits and risks of quantum computing for enterprise IT."
    print(f"Decomposing: {goal}\n")
    tasks = await decompose_goal(goal)
    print(f"Tasks: {[t.id for t in tasks]}\n")
    results = await run_dependency_graph(tasks)
    print(f"\nAll results:\n" + "\n---\n".join([f"{k}: {v[:100]}" for k, v in results.items()]))

asyncio.run(main())
```

---

## Option 2: LLM-Planned DAG with Critical Path Analysis

```python
import asyncio
import anthropic
import json
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class PlanNode:
    id: str
    description: str
    estimated_minutes: float
    depends_on: list[str]
    result: str | None = None

def compute_critical_path(nodes: list[PlanNode]) -> list[str]:
    """Longest dependency chain (critical path)."""
    node_map = {n.id: n for n in nodes}
    memo: dict[str, float] = {}

    def longest_path(nid: str) -> float:
        if nid in memo:
            return memo[nid]
        node = node_map[nid]
        if not node.depends_on:
            memo[nid] = node.estimated_minutes
            return memo[nid]
        max_dep = max(longest_path(dep) for dep in node.depends_on)
        memo[nid] = max_dep + node.estimated_minutes
        return memo[nid]

    for n in nodes:
        longest_path(n.id)

    # Critical path: trace back through highest-cost nodes
    critical: list[str] = []
    current = max(nodes, key=lambda n: memo[n.id])
    while True:
        critical.append(current.id)
        if not current.depends_on:
            break
        current = max(current.depends_on, key=lambda d: memo[d])
        current = node_map[current]
    return list(reversed(critical))

async def plan_and_execute(goal: str) -> dict[str, str]:
    # Step 1: Plan
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        system='''Create a detailed execution plan. Return JSON array:
[{"id": "t1", "description": "...", "estimated_minutes": 2.0, "depends_on": []}]
Be realistic about estimates. 4-8 tasks.''',
        messages=[{"role": "user", "content": f"Goal: {goal}"}]
    )
    try:
        plan_data = json.loads(resp.content[0].text)
        nodes = [PlanNode(**n) for n in plan_data]
    except Exception:
        raise ValueError("Failed to parse plan")

    critical = compute_critical_path(nodes)
    total_sequential = sum(n.estimated_minutes for n in nodes)
    critical_time = sum(n.estimated_minutes for n in nodes if n.id in critical)
    print(f"[PLAN] Critical path: {' → '.join(critical)}")
    print(f"[PLAN] Sequential time: {total_sequential:.1f}min, Parallel critical: {critical_time:.1f}min")

    # Step 2: Execute with topological ordering
    node_map = {n.id: n for n in nodes}
    completed: dict[str, str] = {}
    in_flight: dict[str, asyncio.Task] = {}

    async def execute(node: PlanNode) -> str:
        ctx = "\n".join([f"[{d}]: {completed[d][:150]}" for d in node.depends_on if d in completed])
        r = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=f"Context:\n{ctx}" if ctx else "Complete concisely.",
            messages=[{"role": "user", "content": node.description}]
        )
        return r.content[0].text

    while len(completed) < len(nodes):
        ready = [n for n in nodes if n.id not in completed and n.id not in in_flight
                 and all(d in completed for d in n.depends_on)]
        for node in ready:
            in_flight[node.id] = asyncio.create_task(execute(node))
        if not in_flight:
            break
        done_set, _ = await asyncio.wait(in_flight.values(), return_when=asyncio.FIRST_COMPLETED)
        for done in done_set:
            for nid, task in list(in_flight.items()):
                if task == done:
                    completed[nid] = await task
                    del in_flight[nid]
                    print(f"[PLAN] ✓ {nid}")
                    break

    return completed

async def main():
    results = await plan_and_execute("Build a competitive analysis report on cloud database services.")
    print("\nFinal results:")
    for k, v in results.items():
        print(f"[{k}] {v[:80]}...")

asyncio.run(main())
```

---

## Option 3: Dynamic Replanning on Subtask Failure

```python
import asyncio
import anthropic
import json
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.AsyncAnthropic()

class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"

@dataclass
class DynTask:
    id: str
    description: str
    depends_on: list[str]
    status: TaskStatus = TaskStatus.PENDING
    result: str | None = None
    error: str | None = None
    retries: int = 0

async def replan_around_failure(goal: str, failed_task: DynTask, completed: dict[str, str]) -> list[DynTask]:
    """Ask the LLM to create alternative tasks for the failed one."""
    ctx_summary = "\n".join([f"[{k}]: {v[:100]}" for k, v in list(completed.items())[:5]])
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        system='Return JSON array of 1-2 alternative subtasks to replace the failed one. Same format: [{"id": "alt1", "description": "...", "depends_on": [...]}]',
        messages=[{"role": "user", "content": f"Goal: {goal}\nFailed task: {failed_task.description}\nError: {failed_task.error}\nCompleted so far:\n{ctx_summary}"}]
    )
    try:
        alt_data = json.loads(resp.content[0].text)
        return [DynTask(**t) for t in alt_data]
    except Exception:
        return []

async def execute_with_replanning(goal: str, tasks: list[DynTask], max_failures: int = 2) -> dict[str, str]:
    completed: dict[str, str] = {}
    total_failures = 0

    while True:
        pending = [t for t in tasks if t.status == TaskStatus.PENDING]
        if not pending and not any(t.status == TaskStatus.RUNNING for t in tasks):
            break

        # Find ready tasks
        ready = [t for t in pending if all(d in completed for d in t.depends_on)]
        if not ready:
            await asyncio.sleep(0.1)
            continue

        # Execute ready tasks in parallel
        async def run_task(task: DynTask):
            task.status = TaskStatus.RUNNING
            ctx = "\n".join([f"[{d}]: {completed.get(d, '')[:150]}" for d in task.depends_on])
            try:
                resp = await asyncio.wait_for(
                    client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=400,
                        system=f"Context:\n{ctx}" if ctx else "Be concise.",
                        messages=[{"role": "user", "content": task.description}]
                    ),
                    timeout=15.0
                )
                task.result = resp.content[0].text
                task.status = TaskStatus.DONE
                completed[task.id] = task.result
                print(f"[DYNPLAN] ✓ {task.id}")
            except Exception as e:
                task.error = str(e)
                task.status = TaskStatus.FAILED
                print(f"[DYNPLAN] ✗ {task.id}: {e}")

        await asyncio.gather(*[run_task(t) for t in ready])

        # Handle failures with replanning
        failed = [t for t in tasks if t.status == TaskStatus.FAILED and t.retries == 0]
        for f_task in failed:
            f_task.retries += 1
            total_failures += 1
            if total_failures <= max_failures:
                print(f"[DYNPLAN] Replanning around failed task: {f_task.id}")
                alternatives = await replan_around_failure(goal, f_task, completed)
                if alternatives:
                    tasks.extend(alternatives)
                    print(f"[DYNPLAN] Added {len(alternatives)} alternative tasks")

    return completed

async def main():
    goal = "Analyze the pros and cons of microservices vs monolithic architecture."
    init_resp = await client.messages.create(
        model="claude-sonnet-4-6", max_tokens=500,
        system='Return JSON: [{"id": "t1", "description": "...", "depends_on": []}] for 4-5 tasks.',
        messages=[{"role": "user", "content": f"Goal: {goal}"}]
    )
    tasks = [DynTask(**t) for t in json.loads(init_resp.content[0].text)]
    results = await execute_with_replanning(goal, tasks)
    print(f"\nCompleted {len(results)}/{len(tasks)} tasks")

asyncio.run(main())
```

---

## Option 4: Hierarchical Goal Decomposition (Goals Within Goals)

```python
import asyncio
import anthropic
import json
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class HierarchicalGoal:
    id: str
    description: str
    level: int  # 0=top, 1=sub, 2=leaf
    depends_on: list[str]
    children: list["HierarchicalGoal"] = field(default_factory=list)
    result: str | None = None

async def decompose(goal_desc: str, level: int, max_depth: int = 2) -> list[HierarchicalGoal]:
    if level >= max_depth:
        return []
    n_subtasks = 3 if level == 0 else 2
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=f'Break into {n_subtasks} subtasks. JSON: [{{"id": "l{level}_t1", "description": "...", "depends_on": []}}]',
        messages=[{"role": "user", "content": goal_desc}]
    )
    try:
        data = json.loads(resp.content[0].text)
        nodes = [HierarchicalGoal(id=d["id"], description=d["description"], level=level+1,
                                   depends_on=d.get("depends_on", [])) for d in data]
        # Recursively decompose each node
        if level + 1 < max_depth:
            child_decomps = await asyncio.gather(*[decompose(n.description, level+1, max_depth) for n in nodes])
            for node, children in zip(nodes, child_decomps):
                node.children = children
        return nodes
    except Exception:
        return []

async def execute_hierarchical(goals: list[HierarchicalGoal], completed: dict) -> str:
    """Execute leaves first, then aggregate upward."""
    async def exec_goal(goal: HierarchicalGoal) -> str:
        if goal.children:
            # Execute children first
            child_results = await asyncio.gather(*[exec_goal(c) for c in goal.children])
            # Aggregate children results
            child_summary = "\n".join([f"[{g.id}]: {r[:100]}" for g, r in zip(goal.children, child_results)])
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                system="Synthesize these sub-results into one coherent answer.",
                messages=[{"role": "user", "content": f"Task: {goal.description}\n\nSub-results:\n{child_summary}"}]
            )
        else:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=250,
                messages=[{"role": "user", "content": goal.description}]
            )
        goal.result = resp.content[0].text
        completed[goal.id] = goal.result
        return goal.result

    results = await asyncio.gather(*[exec_goal(g) for g in goals])
    return "\n\n".join(results)

async def main():
    top_goal = "Create a strategic analysis of adopting LLMs in enterprise software development."
    print("Building decomposition tree...")
    subtasks = await decompose(top_goal, level=0, max_depth=2)
    print(f"Tree: {len(subtasks)} top-level, {sum(len(s.children) for s in subtasks)} leaf tasks")

    completed: dict = {}
    result = await execute_hierarchical(subtasks, completed)
    print(f"\nFinal synthesis:\n{result[:300]}")

asyncio.run(main())
```

---

## Option 5: Goal Tracking with Progress Streaming

```python
import asyncio
import anthropic
import json
import time
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.AsyncAnthropic()

class Phase(Enum):
    PLANNING = "planning"
    EXECUTING = "executing"
    SYNTHESIZING = "synthesizing"
    COMPLETE = "complete"

@dataclass
class TrackedPlan:
    goal: str
    tasks: list[dict] = field(default_factory=list)
    results: dict[str, str] = field(default_factory=dict)
    phase: Phase = Phase.PLANNING
    started_at: float = field(default_factory=time.time)

    def progress(self) -> float:
        if not self.tasks:
            return 0.0
        return len(self.results) / len(self.tasks)

    def elapsed_s(self) -> float:
        return time.time() - self.started_at

async def execute_tracked_plan(goal: str) -> TrackedPlan:
    plan = TrackedPlan(goal=goal)

    # Phase 1: Plan
    plan.phase = Phase.PLANNING
    print(f"[{plan.phase.value.upper()}] Decomposing goal...")
    resp = await client.messages.create(
        model="claude-sonnet-4-6", max_tokens=500,
        system='JSON: [{"id":"t1","description":"...","depends_on":[]}] 4-6 tasks.',
        messages=[{"role": "user", "content": goal}]
    )
    plan.tasks = json.loads(resp.content[0].text)
    task_map = {t["id"]: t for t in plan.tasks}
    print(f"[PLANNING] {len(plan.tasks)} tasks: {[t['id'] for t in plan.tasks]}")

    # Phase 2: Execute
    plan.phase = Phase.EXECUTING
    in_flight: dict[str, asyncio.Task] = {}

    async def run(task: dict) -> str:
        ctx = "\n".join([f"[{d}]: {plan.results[d][:150]}" for d in task["depends_on"] if d in plan.results])
        r = await client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=350,
            system=f"Context:\n{ctx}" if ctx else "",
            messages=[{"role": "user", "content": task["description"]}]
        )
        return r.content[0].text

    while len(plan.results) < len(plan.tasks):
        ready = [t for t in plan.tasks
                 if t["id"] not in plan.results and t["id"] not in in_flight
                 and all(d in plan.results for d in t["depends_on"])]

        for t in ready:
            in_flight[t["id"]] = asyncio.create_task(run(t))

        if not in_flight:
            break

        done_set, _ = await asyncio.wait(in_flight.values(), return_when=asyncio.FIRST_COMPLETED)
        for done in done_set:
            for tid, task_obj in list(in_flight.items()):
                if task_obj == done:
                    plan.results[tid] = await task_obj
                    del in_flight[tid]
                    print(f"[EXECUTING] {tid} done ({plan.progress():.0%}) t={plan.elapsed_s():.1f}s")

    # Phase 3: Synthesize
    plan.phase = Phase.SYNTHESIZING
    summary = "\n\n".join([f"[{tid}] {res[:200]}" for tid, res in plan.results.items()])
    synth = await client.messages.create(
        model="claude-sonnet-4-6", max_tokens=800,
        system="Synthesize all subtask results into a cohesive final answer.",
        messages=[{"role": "user", "content": f"Goal: {goal}\n\nSubtask results:\n{summary}"}]
    )
    plan.results["_final"] = synth.content[0].text
    plan.phase = Phase.COMPLETE
    print(f"[COMPLETE] Total time: {plan.elapsed_s():.1f}s")
    return plan

async def main():
    plan = await execute_tracked_plan("Evaluate the trade-offs between REST and GraphQL APIs for a mobile application.")
    print(f"\nFinal answer:\n{plan.results['_final'][:400]}")

asyncio.run(main())
```

---

## Option 6: Constraint-Aware Decomposition with Resource Budgets

```python
import asyncio
import anthropic
import json
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class ResourceBudget:
    max_parallel_tasks: int = 3
    max_total_tokens: int = 50000
    tokens_used: int = 0
    _sem: asyncio.Semaphore = field(init=False)

    def __post_init__(self):
        self._sem = asyncio.Semaphore(self.max_parallel_tasks)

    def can_proceed(self, estimated_tokens: int = 500) -> bool:
        return self.tokens_used + estimated_tokens <= self.max_total_tokens

    def consume(self, tokens: int):
        self.tokens_used += tokens

@dataclass
class ConstrainedTask:
    id: str
    description: str
    depends_on: list[str]
    estimated_tokens: int = 400
    priority: int = 1  # higher = run first among ready tasks

async def constrained_execute(tasks: list[ConstrainedTask], budget: ResourceBudget) -> dict[str, str]:
    completed: dict[str, str] = {}
    skipped: list[str] = []
    in_flight: dict[str, asyncio.Task] = {}

    async def run(task: ConstrainedTask) -> str:
        async with budget._sem:
            if not budget.can_proceed(task.estimated_tokens):
                skipped.append(task.id)
                return f"[SKIPPED: budget exhausted]"
            ctx = "\n".join([f"[{d}]: {completed.get(d, '')[:100]}" for d in task.depends_on])
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=task.estimated_tokens,
                system=f"Context:\n{ctx}" if ctx else "",
                messages=[{"role": "user", "content": task.description}]
            )
            tokens = resp.usage.input_tokens + resp.usage.output_tokens
            budget.consume(tokens)
            print(f"[CONSTRAINED] {task.id} done (tokens={tokens}, total={budget.tokens_used})")
            return resp.content[0].text

    # Sort by priority descending for ready queue
    task_map = {t.id: t for t in tasks}

    while len(completed) + len(skipped) < len(tasks):
        ready = sorted(
            [t for t in tasks if t.id not in completed and t.id not in skipped and t.id not in in_flight
             and all(d in completed for d in t.depends_on)],
            key=lambda t: -t.priority
        )

        if not ready and not in_flight:
            break  # no progress possible

        for task in ready[:budget.max_parallel_tasks]:
            in_flight[task.id] = asyncio.create_task(run(task))

        if in_flight:
            done_set, _ = await asyncio.wait(in_flight.values(), return_when=asyncio.FIRST_COMPLETED)
            for done in done_set:
                for tid, t_obj in list(in_flight.items()):
                    if t_obj == done:
                        result = await t_obj
                        if "[SKIPPED" not in result:
                            completed[tid] = result
                        else:
                            skipped.append(tid)
                        del in_flight[tid]
                        break

    if skipped:
        print(f"[CONSTRAINED] Skipped {len(skipped)} tasks due to budget: {skipped}")
    return completed

async def main():
    goal = "Compare containerization strategies: Docker vs Podman vs containerd."
    resp = await client.messages.create(
        model="claude-sonnet-4-6", max_tokens=500,
        system='JSON: [{"id":"t1","description":"...","depends_on":[],"priority":1,"estimated_tokens":400}] 5 tasks.',
        messages=[{"role": "user", "content": goal}]
    )
    tasks = [ConstrainedTask(**t) for t in json.loads(resp.content[0].text)]
    budget = ResourceBudget(max_parallel_tasks=2, max_total_tokens=8000)
    results = await constrained_execute(tasks, budget)
    print(f"Completed {len(results)}/{len(tasks)} tasks, {budget.tokens_used} tokens used")

asyncio.run(main())
```

---

## Comparison

| Option | DAG Construction | Parallelism | Failure Handling | Best For |
|--------|----------------|-------------|-----------------|----------|
| 1 – Topological Sort | LLM-generated | Max parallel by deps | None | Simple multi-step tasks |
| 2 – Critical Path | LLM + math | Max parallel | None | Time-optimized execution |
| 3 – Dynamic Replanning | LLM-generated | Max parallel | Replan on failure | Unreliable tool environments |
| 4 – Hierarchical | Recursive LLM | Per-level parallel | None | Nested complex goals |
| 5 – Progress Tracking | LLM-generated | Max parallel | None | User-facing long tasks |
| 6 – Resource-Constrained | LLM-generated | Semaphore-capped | Budget skip | Cost-sensitive execution |

**Recommendation:** Use Option 1 for most cases — it's simple and effective. Add Option 2's critical path analysis when users need time estimates. Layer Option 3's dynamic replanning in production environments where individual steps may fail. Use Option 6 when token cost budgets are strict.
