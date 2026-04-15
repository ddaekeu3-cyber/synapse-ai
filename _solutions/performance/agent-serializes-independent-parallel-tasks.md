---
layout: solution
title: "Agent Runs Independent Tasks Sequentially Instead of in Parallel"
category: performance
description: "Agent needs to fetch data from 5 APIs. It calls them one at a time: 2s + 2s + 2s + 2s + 2s = 10 seconds total. All 5 calls are independent. Running them in parallel would take 2 seconds. The agent serializes work that could be parallelized."
tags: [performance, parallel, asyncio, concurrency, gather, throughput, latency, sequential]
---

# Agent Runs Independent Tasks Sequentially Instead of in Parallel

## Symptom
- Agent takes 30 seconds to complete a task that should take 5 seconds
- Profiling shows: 5 API calls × 6 seconds each, run sequentially
- Logs: `fetch_user → fetch_orders → fetch_inventory → fetch_prices → fetch_reviews` (one at a time)
- Tool calls in agent loop are dispatched one at a time even when independent
- User sees the agent "thinking" for a long time with no output
- Scaling to 100 items takes 100× longer instead of near-constant time

## Root Cause
Sequential execution is the default. `await call_a()` completes before `await call_b()` starts. Without explicit parallelization, every step blocks the next. This is correct when steps depend on each other (step B needs step A's output) but wasteful when steps are independent. Agents often request multiple pieces of information that could be gathered simultaneously — fetching user data, order history, and product catalog don't depend on each other.

## Fix

### Option 1: asyncio.gather — run independent coroutines in parallel

```python
import asyncio
import httpx
import time

# WRONG — sequential: 5 × 2s = 10s total
async def fetch_all_sequential(user_id: str) -> dict:
    start = time.monotonic()
    user = await fetch_user(user_id)           # 2s
    orders = await fetch_orders(user_id)       # 2s
    inventory = await fetch_inventory()        # 2s
    prices = await fetch_prices()              # 2s
    reviews = await fetch_reviews(user_id)    # 2s
    elapsed = time.monotonic() - start
    print(f"Sequential: {elapsed:.1f}s")      # → ~10s
    return {"user": user, "orders": orders, "inventory": inventory, ...}

# RIGHT — parallel: max(2s, 2s, 2s, 2s, 2s) = 2s total
async def fetch_all_parallel(user_id: str) -> dict:
    start = time.monotonic()

    user, orders, inventory, prices, reviews = await asyncio.gather(
        fetch_user(user_id),
        fetch_orders(user_id),
        fetch_inventory(),
        fetch_prices(),
        fetch_reviews(user_id)
    )

    elapsed = time.monotonic() - start
    print(f"Parallel: {elapsed:.1f}s")        # → ~2s
    return {"user": user, "orders": orders, "inventory": inventory,
            "prices": prices, "reviews": reviews}

# Handle partial failures — one error doesn't cancel others:
async def fetch_all_resilient(user_id: str) -> dict:
    results = await asyncio.gather(
        fetch_user(user_id),
        fetch_orders(user_id),
        fetch_inventory(),
        fetch_prices(),
        fetch_reviews(user_id),
        return_exceptions=True  # Don't cancel on first error
    )

    keys = ["user", "orders", "inventory", "prices", "reviews"]
    return {
        key: None if isinstance(r, Exception) else r
        for key, r in zip(keys, results)
    }
```

### Option 2: Parallel tool execution in agent loop

```python
import anthropic
import asyncio

client = anthropic.AsyncAnthropic()

async def execute_tool(tool_call: dict) -> dict:
    """Execute a single tool call"""
    name = tool_call["name"]
    params = tool_call["input"]
    match name:
        case "fetch_user":
            result = await fetch_user(params["user_id"])
        case "fetch_orders":
            result = await fetch_orders(params["user_id"])
        case "search_web":
            result = await search_web(params["query"])
        case _:
            raise ValueError(f"Unknown tool: {name}")
    return {"tool_use_id": tool_call["id"], "content": str(result)}

async def run_agent_with_parallel_tools(messages: list[dict]) -> str:
    """
    Agent loop that executes all tool calls in a single round in parallel.
    Model often requests multiple tools at once — run them concurrently.
    """
    while True:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            messages=messages,
            tools=TOOL_DEFINITIONS,
            max_tokens=4096
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        # Extract all tool calls from this response
        tool_calls = [b for b in response.content if b.type == "tool_use"]

        if not tool_calls:
            break

        print(f"Executing {len(tool_calls)} tool calls in parallel...")
        start = time.monotonic()

        # Run ALL tool calls in parallel — not one at a time
        tool_results = await asyncio.gather(
            *[execute_tool({"id": tc.id, "name": tc.name, "input": tc.input})
              for tc in tool_calls],
            return_exceptions=True
        )

        elapsed = time.monotonic() - start
        print(f"All {len(tool_calls)} tools completed in {elapsed:.1f}s")

        # Add results to conversation
        messages.append({"role": "assistant", "content": response.content})
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": r["tool_use_id"] if not isinstance(r, Exception) else tool_calls[i].id,
                    "content": str(r) if not isinstance(r, Exception) else f"Error: {r}",
                    "is_error": isinstance(r, Exception)
                }
                for i, r in enumerate(tool_results)
            ]
        })
```

### Option 3: Batch processing with controlled concurrency

```python
import asyncio
from typing import TypeVar, Callable, Awaitable

T = TypeVar("T")
R = TypeVar("R")

async def parallel_map(
    items: list[T],
    fn: Callable[[T], Awaitable[R]],
    max_concurrent: int = 10,
    return_exceptions: bool = False
) -> list[R]:
    """
    Apply async function to all items in parallel with concurrency limit.
    Equivalent to: [await fn(item) for item in items] but parallel.
    """
    semaphore = asyncio.Semaphore(max_concurrent)

    async def bounded(item: T) -> R:
        async with semaphore:
            return await fn(item)

    return await asyncio.gather(
        *[bounded(item) for item in items],
        return_exceptions=return_exceptions
    )

# Process 1000 items, 10 at a time:
async def process_all_users(user_ids: list[str]) -> list[dict]:
    start = time.monotonic()
    results = await parallel_map(
        user_ids,
        fn=process_single_user,
        max_concurrent=10  # Respect API rate limits
    )
    elapsed = time.monotonic() - start

    # Sequential: 1000 × 2s = 2000s
    # Parallel(10): ~200s (10× speedup)
    print(f"Processed {len(user_ids)} users in {elapsed:.1f}s "
          f"({len(user_ids)/elapsed:.1f} users/sec)")
    return results

# Chunk large batches to avoid memory pressure:
async def process_in_chunks(items: list, chunk_size: int = 100) -> list:
    all_results = []
    for i in range(0, len(items), chunk_size):
        chunk = items[i:i + chunk_size]
        chunk_results = await parallel_map(chunk, process_item, max_concurrent=10)
        all_results.extend(chunk_results)
        print(f"Progress: {min(i + chunk_size, len(items))}/{len(items)}")
    return all_results
```

### Option 4: Identify parallelizable steps with dependency analysis

```python
from dataclasses import dataclass, field

@dataclass
class TaskStep:
    name: str
    depends_on: list[str] = field(default_factory=list)
    fn: callable = None

class ParallelTaskPlanner:
    """
    Given a set of tasks with dependencies, run independent ones in parallel.
    Tasks with no unfulfilled dependencies run concurrently.
    """

    def __init__(self, steps: list[TaskStep]):
        self.steps = {s.name: s for s in steps}
        self.results: dict[str, any] = {}

    async def run(self) -> dict[str, any]:
        completed = set()
        in_progress = set()

        while len(completed) < len(self.steps):
            # Find tasks whose dependencies are all satisfied
            ready = [
                name for name, step in self.steps.items()
                if name not in completed
                and name not in in_progress
                and all(dep in completed for dep in step.depends_on)
            ]

            if not ready:
                if not in_progress:
                    raise RuntimeError(f"Deadlock: no progress possible. Completed: {completed}")
                await asyncio.sleep(0.01)
                continue

            print(f"Running in parallel: {ready}")
            in_progress.update(ready)

            # Run all ready tasks in parallel
            task_coros = {name: self.steps[name].fn(self.results) for name in ready}
            done = await asyncio.gather(*task_coros.values(), return_exceptions=True)

            for name, result in zip(task_coros.keys(), done):
                self.results[name] = result
                completed.add(name)
                in_progress.discard(name)

        return self.results

# Define task graph:
planner = ParallelTaskPlanner([
    # These 3 run in parallel (no dependencies):
    TaskStep("fetch_user", fn=lambda r: fetch_user("user_123")),
    TaskStep("fetch_catalog", fn=lambda r: fetch_catalog()),
    TaskStep("fetch_promotions", fn=lambda r: fetch_promotions()),

    # This runs after fetch_user AND fetch_catalog:
    TaskStep("build_recommendations",
             depends_on=["fetch_user", "fetch_catalog"],
             fn=lambda r: build_recs(r["fetch_user"], r["fetch_catalog"])),

    # This runs after everything:
    TaskStep("generate_report",
             depends_on=["build_recommendations", "fetch_promotions"],
             fn=lambda r: generate_report(r["build_recommendations"], r["fetch_promotions"]))
])

results = await planner.run()
```

### Option 5: System prompt — instruct model to request tools in parallel

```
System prompt:
"Tool use efficiency rules:

When you need multiple pieces of information that are independent of each other,
request ALL of them in a SINGLE response using multiple tool calls.

CORRECT — request all at once:
[tool: fetch_user(user_id="123")]
[tool: fetch_orders(user_id="123")]
[tool: fetch_catalog()]
→ These run in parallel. Task completes 3× faster.

WRONG — request one at a time:
[tool: fetch_user(user_id="123")]
(wait for result)
[tool: fetch_orders(user_id="123")]
(wait for result)
→ Sequential. Each request waits for the previous one.

Rule: If step B does not use step A's output, request A and B simultaneously.
Identify all information you need upfront and request it in one batch."
```

### Option 6: Performance profiling — detect sequential bottlenecks

```python
import asyncio
import time
import functools
from collections import defaultdict

class ExecutionProfiler:
    """Track tool call timing to identify sequential bottlenecks"""

    def __init__(self):
        self._calls: list[dict] = []
        self._active: dict[str, float] = {}

    def start(self, name: str):
        self._active[name] = time.monotonic()

    def end(self, name: str, result_size: int = 0):
        if name in self._active:
            elapsed = time.monotonic() - self._active.pop(name)
            self._calls.append({
                "name": name,
                "duration": elapsed,
                "result_size": result_size,
                "ended_at": time.monotonic()
            })

    def report(self) -> dict:
        if not self._calls:
            return {}

        total_wall_time = max(c["ended_at"] for c in self._calls) - min(
            c["ended_at"] - c["duration"] for c in self._calls
        )
        total_work_time = sum(c["duration"] for c in self._calls)
        parallelization_ratio = total_work_time / total_wall_time if total_wall_time > 0 else 1

        return {
            "total_calls": len(self._calls),
            "wall_time_seconds": round(total_wall_time, 2),
            "total_work_seconds": round(total_work_time, 2),
            "parallelization_ratio": round(parallelization_ratio, 2),
            "efficiency": f"{(total_work_time/total_wall_time*100):.0f}% parallel" if parallelization_ratio > 1 else "100% sequential",
            "slowest_calls": sorted(self._calls, key=lambda x: -x["duration"])[:3],
            "recommendation": (
                "Good parallelization" if parallelization_ratio > 1.5
                else "Consider parallelizing independent calls — significant sequential overhead detected"
            )
        }

profiler = ExecutionProfiler()

def profiled(fn):
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        profiler.start(fn.__name__)
        try:
            result = await fn(*args, **kwargs)
            profiler.end(fn.__name__, result_size=len(str(result)))
            return result
        except Exception:
            profiler.end(fn.__name__)
            raise
    return wrapper

@profiled
async def fetch_user(user_id: str): ...

@profiled
async def fetch_orders(user_id: str): ...

# After task:
report = profiler.report()
print(f"Efficiency: {report['efficiency']}")
print(f"Wall time: {report['wall_time_seconds']}s vs {report['total_work_seconds']}s total work")
# If parallelization_ratio ≈ 1.0 → fully sequential, needs optimization
# If parallelization_ratio ≈ N → N calls ran in parallel (good)
```

## Sequential vs Parallel Timing

| Scenario | Sequential | Parallel (10) | Speedup |
|---|---|---|---|
| 5 API calls × 2s | 10s | 2s | 5× |
| 100 items × 500ms | 50s | 5s | 10× |
| 1000 DB queries × 10ms | 10s | ~1s | 10× |
| 3 LLM calls × 8s | 24s | 8s | 3× |
| Mixed (dep chain of 3) | 15s | ~7s | 2× |

## Expected Token Savings
10s sequential task → user timeout → retry with "be faster" prompt → re-explanation: ~8,000 tokens
2s parallel task → user sees result immediately: 0 impatience overhead

## Environment
- Any agent making multiple independent I/O calls; critical for agents doing data aggregation, batch processing, report generation, or multi-source research
- Source: direct experience; sequential execution of parallelizable work is the most common avoidable performance bottleneck in agent pipelines
