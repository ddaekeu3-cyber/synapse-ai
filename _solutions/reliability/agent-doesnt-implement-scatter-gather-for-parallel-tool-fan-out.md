---
title: "Agent Doesn't Implement Scatter-Gather for Parallel Tool Fan-Out"
description: "AI agents that execute tool calls sequentially when they could run in parallel waste wall-clock time proportional to the number of calls. The scatter-gather pattern fans out independent calls concurrently and collects results when all (or a quorum) complete, cutting latency from O(n) to O(max)."
date: 2025-02-05
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-scatter-gather-for-parallel-tool-fan-out
tags:
  - scatter-gather
  - parallel-execution
  - fan-out
  - asyncio
  - latency
  - tool-calls
  - performance
symptoms:
  - "Agent makes 5 API calls sequentially taking 5× the time of one call"
  - "LLM generates multiple tool calls in one response but agent executes them one at a time"
  - "Tool call latency budget is exceeded because calls are chained instead of parallelised"
  - "Agent does not use asyncio.gather even though tool calls have no dependencies"
  - "Adding more tool calls to a plan increases total latency linearly"
---

## Problem

When an LLM generates several independent tool calls — "search X", "look up Y", "fetch Z" — a naive executor processes them sequentially. If each call takes 500 ms, three calls take 1500 ms. With scatter-gather, all three run concurrently and the total is ~500 ms.

The scatter-gather pattern has two components:
1. **Scatter**: dispatch all independent work items concurrently.
2. **Gather**: collect results when all (or a minimum quorum) complete; handle partial failures gracefully.

---

## Solution 1: Basic asyncio.gather Scatter-Gather

```python
import asyncio
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple


@dataclass
class GatherResult:
    index: int
    value: Optional[Any]
    error: Optional[Exception]

    @property
    def ok(self) -> bool:
        return self.error is None


async def scatter_gather(
    tasks: List[Callable[[], Any]],
    return_exceptions: bool = True,
) -> List[GatherResult]:
    """
    Fan out all callables concurrently and collect results.

    Usage:
        results = await scatter_gather([
            lambda: web_search("topic A"),
            lambda: db_query("SELECT ..."),
            lambda: fetch_document("url"),
        ])
        for r in results:
            if r.ok:
                process(r.value)
            else:
                handle_error(r.error)
    """
    coros = [fn() for fn in tasks]
    raw = await asyncio.gather(*coros, return_exceptions=return_exceptions)
    return [
        GatherResult(
            index=i,
            value=v if not isinstance(v, Exception) else None,
            error=v if isinstance(v, Exception) else None,
        )
        for i, v in enumerate(raw)
    ]


async def scatter_gather_dict(
    tasks: dict[str, Callable[[], Any]],
) -> dict[str, GatherResult]:
    """
    Named scatter-gather: maps task names to results.

    Usage:
        results = await scatter_gather_dict({
            "search":   lambda: web_search(query),
            "profile":  lambda: fetch_user_profile(uid),
            "history":  lambda: load_session_history(sid),
        })
        search_result = results["search"].value
    """
    names = list(tasks.keys())
    raw_results = await scatter_gather([tasks[n] for n in names])
    return {name: raw_results[i] for i, name in enumerate(names)}
```

---

## Solution 2: Bounded Scatter-Gather (Concurrency-Limited)

Fan out with a maximum concurrency cap to avoid overwhelming downstream APIs.

```python
import asyncio
from typing import Any, Callable, List, Optional


class BoundedScatterGather:
    """
    Scatter-gather with a semaphore to cap concurrent executions.

    Usage:
        sg = BoundedScatterGather(max_concurrent=5)
        results = await sg.run([
            lambda: call_api(item) for item in large_list
        ])
    """

    def __init__(self, max_concurrent: int = 10):
        self._sem = asyncio.Semaphore(max_concurrent)

    async def _bounded(self, fn: Callable) -> Any:
        async with self._sem:
            return await fn()

    async def run(self, tasks: List[Callable],
                  return_exceptions: bool = True) -> List[GatherResult]:
        wrapped = [lambda f=fn: self._bounded(f) for fn in tasks]
        return await scatter_gather(wrapped, return_exceptions)

    async def run_dict(self, tasks: dict[str, Callable]) -> dict[str, GatherResult]:
        names = list(tasks.keys())
        results = await self.run([tasks[n] for n in names])
        return {name: results[i] for i, name in enumerate(names)}
```

---

## Solution 3: Quorum Scatter-Gather

Proceed as soon as a minimum number of results arrive (e.g. 3-of-5). Remaining tasks are cancelled. Useful for redundant data sources where the fastest response wins.

```python
import asyncio
from dataclasses import dataclass
from typing import Any, Callable, List, Optional


@dataclass
class QuorumResult:
    completed: List[GatherResult]
    cancelled: int
    total: int


async def quorum_scatter_gather(
    tasks: List[Callable[[], Any]],
    min_results: int,
    timeout: Optional[float] = None,
) -> QuorumResult:
    """
    Return as soon as `min_results` tasks complete successfully.
    Cancel remaining tasks.

    Usage:
        # Query 5 replicas; return when 3 respond
        result = await quorum_scatter_gather(
            [lambda: replica.query(q) for replica in replicas],
            min_results=3,
            timeout=2.0,
        )
        values = [r.value for r in result.completed if r.ok]
    """
    collected: List[GatherResult] = []
    done_event = asyncio.Event()
    pending_tasks: List[asyncio.Task] = []
    lock = asyncio.Lock()

    async def run_one(i: int, fn: Callable):
        try:
            value = await fn()
            async with lock:
                collected.append(GatherResult(i, value, None))
                if len(collected) >= min_results:
                    done_event.set()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            async with lock:
                collected.append(GatherResult(i, None, exc))

    pending_tasks = [
        asyncio.create_task(run_one(i, fn))
        for i, fn in enumerate(tasks)
    ]

    try:
        if timeout:
            await asyncio.wait_for(done_event.wait(), timeout)
        else:
            await done_event.wait()
    except asyncio.TimeoutError:
        pass

    cancelled = 0
    for task in pending_tasks:
        if not task.done():
            task.cancel()
            cancelled += 1
    await asyncio.gather(*pending_tasks, return_exceptions=True)

    return QuorumResult(
        completed=collected,
        cancelled=cancelled,
        total=len(tasks),
    )
```

---

## Solution 4: Streaming Scatter-Gather (Yield as Results Arrive)

Process results as they come in instead of waiting for all tasks. Useful when downstream processing can start before all sources respond.

```python
import asyncio
from typing import Any, AsyncGenerator, Callable, List


async def streaming_scatter_gather(
    tasks: List[Callable[[], Any]],
    timeout_per_task: Optional[float] = None,
) -> AsyncGenerator[GatherResult, None]:
    """
    Async generator that yields results as they complete (fastest-first).

    Usage:
        async for result in streaming_scatter_gather(tool_calls):
            if result.ok:
                await pipeline.feed(result.value)
    """
    queue: asyncio.Queue = asyncio.Queue()
    n = len(tasks)

    async def run_one(i: int, fn: Callable):
        try:
            if timeout_per_task:
                value = await asyncio.wait_for(fn(), timeout_per_task)
            else:
                value = await fn()
            await queue.put(GatherResult(i, value, None))
        except Exception as exc:
            await queue.put(GatherResult(i, None, exc))

    worker_tasks = [
        asyncio.create_task(run_one(i, fn))
        for i, fn in enumerate(tasks)
    ]

    for _ in range(n):
        result = await queue.get()
        yield result

    await asyncio.gather(*worker_tasks, return_exceptions=True)
```

---

## Solution 5: Dependency-Aware Task Graph Executor

When tool calls have dependencies (B must run after A; C can run in parallel with B), model them as a DAG and execute at maximum parallelism respecting the dependency order.

```python
import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set


@dataclass
class TaskNode:
    name: str
    fn: Callable[[], Any]
    depends_on: List[str] = field(default_factory=list)
    result: Optional[Any] = None
    error: Optional[Exception] = None
    done: bool = False


class DAGTaskExecutor:
    """
    Executes tasks at maximum parallelism respecting declared dependencies.

    Usage:
        executor = DAGTaskExecutor()
        executor.add("search",   web_search, depends_on=[])
        executor.add("profile",  get_profile, depends_on=[])
        executor.add("summary",  summarise,  depends_on=["search", "profile"])

        results = await executor.run()
        print(results["summary"])
    """

    def __init__(self):
        self._nodes: Dict[str, TaskNode] = {}

    def add(self, name: str, fn: Callable,
            depends_on: Optional[List[str]] = None):
        self._nodes[name] = TaskNode(name=name, fn=fn,
                                      depends_on=depends_on or [])

    def _ready(self) -> List[str]:
        return [
            name for name, node in self._nodes.items()
            if not node.done
            and node.error is None
            and all(self._nodes[dep].done for dep in node.depends_on)
        ]

    async def run(self) -> Dict[str, Any]:
        running: Dict[str, asyncio.Task] = {}
        while True:
            ready = [n for n in self._ready() if n not in running]
            for name in ready:
                node = self._nodes[name]
                t = asyncio.create_task(node.fn(), name=name)
                running[name] = t

            if not running:
                break  # all done

            done, _ = await asyncio.wait(
                running.values(), return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                name = task.get_name()
                node = self._nodes[name]
                del running[name]
                if task.exception():
                    node.error = task.exception()
                else:
                    node.result = task.result()
                    node.done = True

            # Stop if any error
            errors = {n: nd.error for n, nd in self._nodes.items() if nd.error}
            if errors:
                for t in running.values():
                    t.cancel()
                raise RuntimeError(f"Task errors: {errors}")

        return {name: node.result for name, node in self._nodes.items()}
```

---

## Solution 6: Scatter-Gather Agent Tool Executor

Drop-in executor for AI agent tool call responses. Parses a list of tool calls from the LLM response and executes them using scatter-gather.

```python
import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional


@dataclass
class ToolCallRequest:
    call_id: str
    tool_name: str
    arguments: dict


@dataclass
class ToolCallResult:
    call_id: str
    tool_name: str
    result: Optional[Any]
    error: Optional[str]
    duration_ms: float


class ScatterGatherToolExecutor:
    """
    Executes LLM tool call lists with maximum parallelism.

    Usage:
        executor = ScatterGatherToolExecutor(max_concurrent=10)
        executor.register("web_search",  web_search_fn)
        executor.register("db_query",    db_query_fn)
        executor.register("send_email",  send_email_fn)

        tool_calls = [
            ToolCallRequest("c1", "web_search", {"query": "AI news"}),
            ToolCallRequest("c2", "db_query",   {"sql": "SELECT ..."}),
            ToolCallRequest("c3", "web_search",  {"query": "Python tips"}),
        ]
        results = await executor.execute(tool_calls)
        for r in results:
            print(r.call_id, r.tool_name, r.result)
    """

    def __init__(self, max_concurrent: int = 10):
        self._tools: Dict[str, Callable] = {}
        self._sg = BoundedScatterGather(max_concurrent)

    def register(self, name: str, fn: Callable):
        self._tools[name] = fn

    async def _run_one(self, req: ToolCallRequest) -> ToolCallResult:
        fn = self._tools.get(req.tool_name)
        if fn is None:
            return ToolCallResult(req.call_id, req.tool_name,
                                   None, f"Unknown tool: {req.tool_name}", 0.0)
        t0 = time.monotonic()
        try:
            result = await fn(**req.arguments)
            return ToolCallResult(
                req.call_id, req.tool_name, result, None,
                (time.monotonic() - t0) * 1000,
            )
        except Exception as exc:
            return ToolCallResult(
                req.call_id, req.tool_name, None, str(exc),
                (time.monotonic() - t0) * 1000,
            )

    async def execute(self, requests: List[ToolCallRequest],
                       timeout: Optional[float] = None) -> List[ToolCallResult]:
        tasks = [lambda r=req: self._run_one(r) for req in requests]
        if timeout:
            raw = await asyncio.wait_for(
                self._sg.run(tasks, return_exceptions=True), timeout
            )
        else:
            raw = await self._sg.run(tasks, return_exceptions=True)
        return [r.value for r in raw if r.ok]

    def stats_from(self, results: List[ToolCallResult]) -> dict:
        durations = [r.duration_ms for r in results]
        errors = [r for r in results if r.error]
        return {
            "total": len(results),
            "errors": len(errors),
            "max_ms": round(max(durations, default=0), 1),
            "total_sequential_ms": round(sum(durations), 1),
        }
```

---

## Comparison

| Approach | Handles Failures | Concurrency Cap | Dependency-Aware |
|---|---|---|---|
| **Basic asyncio.gather** | Via return_exceptions | No | No |
| **Bounded Scatter-Gather** | Via return_exceptions | Yes | No |
| **Quorum Scatter-Gather** | Partial (min_results) | No | No |
| **Streaming Scatter-Gather** | Per-result | No | No |
| **DAG Task Executor** | Stops on error | No | Yes |
| **Tool Call Executor** | Per-call error field | Yes | No |

**Key insight**: use the Bounded Scatter-Gather as the default for all multi-tool LLM responses. Use the DAG executor only when some tools genuinely depend on others (e.g., "fetch user → then fetch their orders"). Streaming scatter-gather pays off when downstream processing is slow and results can be pipelined.
