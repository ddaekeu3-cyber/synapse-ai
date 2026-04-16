---
title: "Agent doesn't implement async timeout tree for nested calls"
description: "Timeout deadlines are not propagated through nested async subtasks, so a slow leaf call can consume the full outer deadline while parent coroutines wait indefinitely."
difficulty: intermediate
category: concurrency
tags: [asyncio, timeout, cancellation, structured-concurrency, deadline-propagation]
---

## Problem

When an agent fans out into nested async subtasks — tool calls spawning sub-calls, orchestrators spawning sub-agents — each level typically sets its own independent timeout. A slow leaf coroutine can exhaust the entire outer SLA while every parent waits patiently. When the outer timeout finally fires, inner tasks are left running, leaking resources and producing orphaned results that arrive after the caller has already given up.

The correct model is a **timeout tree**: each node receives the remaining budget from its parent, never exceeds it, and propagates cancellation downward the moment the root deadline expires.

```python
# BAD: every level sets its own independent timeout — no deadline inheritance
async def orchestrate():
    async with asyncio.timeout(30):          # outer: 30 s
        result_a = await call_tool_a()       # tool_a: its own 20 s timeout internally
        result_b = await call_tool_b()       # tool_b: its own 20 s timeout internally
        # total wall time can reach 40 s even though orchestrator wanted 30 s
```

## Solution 1: Deadline context with remaining-budget propagation

Track the absolute deadline in a `contextvars.ContextVar` so every nested coroutine can read the remaining budget and call `asyncio.wait_for` with it.

```python
import asyncio
import contextvars
import time
from typing import Optional

_deadline_var: contextvars.ContextVar[Optional[float]] = contextvars.ContextVar(
    "deadline", default=None
)


def set_deadline(seconds: float) -> float:
    """Set deadline to now + seconds, return absolute deadline."""
    deadline = time.monotonic() + seconds
    _deadline_var.set(deadline)
    return deadline


def remaining_budget(min_seconds: float = 0.1) -> float:
    """Return seconds remaining until deadline; raises TimeoutError if expired."""
    deadline = _deadline_var.get()
    if deadline is None:
        return 30.0  # unconstrained — use a safe default
    remaining = deadline - time.monotonic()
    if remaining < min_seconds:
        raise TimeoutError("Deadline already expired or too close")
    return remaining


async def deadline_guarded(coro, *, label: str = "task"):
    """Wrap a coroutine so it never runs past the inherited deadline."""
    budget = remaining_budget()
    try:
        return await asyncio.wait_for(coro, timeout=budget)
    except asyncio.TimeoutError:
        raise TimeoutError(f"{label} exceeded inherited deadline")


# --- Usage ---

async def leaf_tool_call(name: str) -> str:
    budget = remaining_budget()
    print(f"[{name}] running with {budget:.2f}s remaining")
    await asyncio.sleep(0.5)  # simulated work
    return f"{name}-result"


async def mid_level(items: list[str]) -> list[str]:
    results = []
    for item in items:
        result = await deadline_guarded(leaf_tool_call(item), label=item)
        results.append(result)
    return results


async def orchestrate(total_seconds: float = 10.0):
    set_deadline(total_seconds)
    return await deadline_guarded(mid_level(["a", "b", "c"]), label="mid_level")


asyncio.run(orchestrate())
```

## Solution 2: Timeout tree with asyncio.TaskGroup and shared deadline

Use Python 3.11+ `TaskGroup` so cancellation propagates to sibling tasks automatically when any one branch exceeds the shared deadline.

```python
import asyncio
import time
import contextvars
from dataclasses import dataclass
from typing import Any

_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar("dl", default=None)


@dataclass
class TimeoutNode:
    label: str
    budget_fraction: float = 1.0  # fraction of remaining parent budget

    def child_budget(self) -> float:
        parent_deadline = _deadline.get()
        if parent_deadline is None:
            return 30.0
        remaining = parent_deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"[{self.label}] no time remaining")
        return remaining * self.budget_fraction

    async def run(self, coro) -> Any:
        budget = self.child_budget()
        child_deadline = time.monotonic() + budget
        token = _deadline.set(child_deadline)
        try:
            return await asyncio.wait_for(coro, timeout=budget)
        except asyncio.TimeoutError:
            raise TimeoutError(f"[{self.label}] timed out after {budget:.2f}s")
        finally:
            _deadline.reset(token)


async def fetch_data(source: str, delay: float) -> dict:
    node = TimeoutNode(label=f"fetch:{source}", budget_fraction=0.8)

    async def _work():
        await asyncio.sleep(delay)
        return {"source": source, "data": f"payload-from-{source}"}

    return await node.run(_work())


async def aggregate(sources: list[tuple[str, float]]) -> list[dict]:
    node = TimeoutNode(label="aggregate", budget_fraction=0.9)

    async def _fanout():
        async with asyncio.TaskGroup() as tg:
            tasks = [
                tg.create_task(fetch_data(src, delay))
                for src, delay in sources
            ]
        return [t.result() for t in tasks]

    return await node.run(_fanout())


async def main():
    root = TimeoutNode(label="root")
    root_deadline = time.monotonic() + 5.0
    _deadline.set(root_deadline)

    try:
        results = await aggregate([("db", 0.3), ("cache", 0.1), ("api", 0.4)])
        print("Results:", results)
    except TimeoutError as e:
        print(f"Timeout: {e}")


asyncio.run(main())
```

## Solution 3: Recursive deadline tree with per-branch budget slicing

For tree-shaped agent orchestration, allocate a fraction of the remaining budget to each branch and enforce it recursively.

```python
import asyncio
import time
from typing import Callable, Any


class DeadlineTree:
    """
    Represents one node in a timeout tree.  Each child receives a slice of
    the parent's remaining budget.  Cancellation propagates automatically
    via asyncio.wait_for.
    """

    def __init__(self, label: str, deadline: float):
        self.label = label
        self.deadline = deadline

    @property
    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def child(self, label: str, fraction: float = 1.0) -> "DeadlineTree":
        """Create a child node whose deadline is a fraction of remaining budget."""
        child_deadline = time.monotonic() + self.remaining * fraction
        return DeadlineTree(label=f"{self.label}/{label}", deadline=child_deadline)

    async def run(self, coro, *, min_budget: float = 0.05) -> Any:
        budget = self.remaining
        if budget < min_budget:
            raise TimeoutError(f"[{self.label}] insufficient budget: {budget:.3f}s")
        try:
            return await asyncio.wait_for(coro, timeout=budget)
        except asyncio.TimeoutError:
            raise TimeoutError(f"[{self.label}] exceeded deadline")


# --- Agent orchestration using DeadlineTree ---

async def tool_call(tree: DeadlineTree, name: str, latency: float) -> str:
    node = tree.child(name, fraction=0.5)
    async def _work():
        await asyncio.sleep(latency)
        return f"{name}:ok"
    return await node.run(_work())


async def sub_agent(tree: DeadlineTree, task_id: str) -> dict:
    node = tree.child(f"sub:{task_id}", fraction=0.7)

    async def _work():
        r1 = await tool_call(node, "read", 0.1)
        r2 = await tool_call(node, "transform", 0.2)
        return {"task": task_id, "results": [r1, r2]}

    return await node.run(_work())


async def orchestrator(total_budget: float = 8.0):
    root = DeadlineTree("root", time.monotonic() + total_budget)

    async def _all():
        tasks = [sub_agent(root, str(i)) for i in range(3)]
        return await asyncio.gather(*tasks)

    return await root.run(_all())


results = asyncio.run(orchestrator())
print(results)
```

## Solution 4: Deadline-aware semaphore — refuses work when budget is critically low

Combine concurrency limiting with deadline awareness so that a semaphore will not grant a slot when the remaining budget is too low for the operation to succeed.

```python
import asyncio
import time
from typing import Optional


class DeadlineAwareSemaphore:
    """
    A semaphore that refuses to acquire if the deadline is too close,
    preventing wasted work that can't possibly complete in time.
    """

    def __init__(self, value: int, min_budget: float = 0.2):
        self._sem = asyncio.Semaphore(value)
        self.min_budget = min_budget
        self._deadline: Optional[float] = None

    def set_deadline(self, deadline: float):
        self._deadline = deadline

    def _check_budget(self):
        if self._deadline is not None:
            remaining = self._deadline - time.monotonic()
            if remaining < self.min_budget:
                raise TimeoutError(
                    f"Refusing slot: only {remaining:.3f}s left (min={self.min_budget}s)"
                )

    async def __aenter__(self):
        self._check_budget()
        # Also race the semaphore acquisition against the deadline
        if self._deadline is not None:
            budget = self._deadline - time.monotonic()
            try:
                await asyncio.wait_for(self._sem.acquire(), timeout=budget)
            except asyncio.TimeoutError:
                raise TimeoutError("Timed out waiting for semaphore slot")
        else:
            await self._sem.acquire()
        self._check_budget()  # re-check after potentially waiting
        return self

    async def __aexit__(self, *args):
        self._sem.release()


# --- Usage ---

sem = DeadlineAwareSemaphore(value=3, min_budget=0.3)


async def bounded_tool_call(name: str, latency: float) -> str:
    async with sem:
        await asyncio.sleep(latency)
        return f"{name}:done"


async def run_with_deadline(total: float = 5.0):
    deadline = time.monotonic() + total
    sem.set_deadline(deadline)

    names = [f"tool-{i}" for i in range(8)]
    latencies = [0.3] * 8

    tasks = [asyncio.create_task(bounded_tool_call(n, l))
             for n, l in zip(names, latencies)]

    results = []
    for t in asyncio.as_completed(tasks):
        try:
            results.append(await t)
        except TimeoutError as e:
            results.append(f"SKIPPED: {e}")

    return results


print(asyncio.run(run_with_deadline()))
```

## Solution 5: Distributed deadline propagation via request headers (for RPC/HTTP tool calls)

When tool calls cross process boundaries over HTTP, embed the absolute deadline as a header so the remote service can also enforce it.

```python
import asyncio
import time
import httpx
from typing import Any

DEADLINE_HEADER = "X-Request-Deadline"
DEADLINE_FMT = ".6f"  # Unix timestamp with microsecond precision


class DeadlineClient:
    """
    HTTP client that automatically injects the remaining deadline into
    outbound requests and respects it as the request timeout.
    """

    def __init__(self, base_url: str, default_timeout: float = 10.0):
        self.base_url = base_url
        self.default_timeout = default_timeout
        self._deadline: float | None = None

    def set_deadline(self, seconds_from_now: float):
        self._deadline = time.time() + seconds_from_now  # wall clock for HTTP

    def _build_timeout(self, min_budget: float = 0.1) -> float:
        if self._deadline is None:
            return self.default_timeout
        remaining = self._deadline - time.time()
        if remaining < min_budget:
            raise TimeoutError(f"No budget for HTTP call: {remaining:.3f}s")
        return remaining

    def _headers(self) -> dict:
        if self._deadline is None:
            return {}
        return {DEADLINE_HEADER: format(self._deadline, DEADLINE_FMT)}

    async def post(self, path: str, json: Any) -> Any:
        timeout = self._build_timeout()
        headers = self._headers()
        async with httpx.AsyncClient(base_url=self.base_url, timeout=timeout) as client:
            resp = await client.post(path, json=json, headers=headers)
            resp.raise_for_status()
            return resp.json()


# --- Server side: honor the deadline header ---

async def handle_with_deadline(request_headers: dict, handler_coro):
    """
    Extract deadline from headers and enforce it server-side.
    Use this in your FastAPI/aiohttp request middleware.
    """
    deadline_str = request_headers.get(DEADLINE_HEADER)
    if deadline_str:
        deadline_wall = float(deadline_str)
        remaining = deadline_wall - time.time()
        if remaining <= 0:
            raise TimeoutError("Request arrived after deadline")
        return await asyncio.wait_for(handler_coro, timeout=remaining)
    return await handler_coro


# --- Usage ---

async def agent_orchestrate():
    client = DeadlineClient("https://tools.internal")
    client.set_deadline(seconds_from_now=5.0)

    result_a = await client.post("/tool/search", json={"query": "revenue 2024"})
    result_b = await client.post("/tool/summarize", json={"text": result_a["text"]})
    return result_b
```

## Solution 6: Adaptive timeout tree with P99-based budget allocation

Record observed latencies per call type; when allocating child budgets, use P99 estimates to distribute the parent budget proportionally rather than using fixed fractions.

```python
import asyncio
import time
import statistics
from collections import defaultdict
from typing import Any, Callable

_latency_history: dict[str, list[float]] = defaultdict(list)
MAX_HISTORY = 100


def record_latency(label: str, elapsed: float):
    h = _latency_history[label]
    h.append(elapsed)
    if len(h) > MAX_HISTORY:
        h.pop(0)


def p99_estimate(label: str, fallback: float = 1.0) -> float:
    h = _latency_history[label]
    if len(h) < 5:
        return fallback
    sorted_h = sorted(h)
    idx = int(len(sorted_h) * 0.99)
    return sorted_h[min(idx, len(sorted_h) - 1)]


class AdaptiveDeadlineTree:
    def __init__(self, label: str, deadline: float, slack: float = 0.9):
        self.label = label
        self.deadline = deadline
        self.slack = slack  # fraction of budget actually used (reserve the rest)

    @property
    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def child_for(self, child_label: str, siblings: list[str]) -> "AdaptiveDeadlineTree":
        """
        Allocate budget to this child based on its P99 share among siblings.
        """
        estimates = {s: p99_estimate(s) for s in siblings}
        total_est = sum(estimates.values()) or 1.0
        fraction = estimates.get(child_label, 1.0) / total_est
        budget = self.remaining * self.slack * fraction
        child_deadline = time.monotonic() + budget
        return AdaptiveDeadlineTree(
            label=f"{self.label}/{child_label}",
            deadline=child_deadline,
            slack=self.slack,
        )

    async def run(self, coro: Any) -> Any:
        start = time.monotonic()
        budget = self.remaining
        if budget <= 0.05:
            raise TimeoutError(f"[{self.label}] no budget remaining")
        try:
            result = await asyncio.wait_for(coro, timeout=budget)
            record_latency(self.label, time.monotonic() - start)
            return result
        except asyncio.TimeoutError:
            record_latency(self.label, budget)  # record worst-case
            raise TimeoutError(f"[{self.label}] exceeded adaptive deadline")


# --- Example usage ---

async def call_step(name: str, latency: float) -> str:
    await asyncio.sleep(latency)
    return f"{name}:ok"


async def adaptive_pipeline(total: float = 6.0):
    root = AdaptiveDeadlineTree("root", time.monotonic() + total)
    steps = ["search", "rank", "generate", "validate"]

    results = []
    for step in steps:
        node = root.child_for(step, siblings=steps)
        result = await node.run(call_step(step, latency=0.4))
        results.append(result)

    return results


print(asyncio.run(adaptive_pipeline()))
```

## Comparison

| Approach | Propagation mechanism | Cancellation scope | Cross-process | Adaptive |
|---|---|---|---|---|
| Deadline context var | `contextvars.ContextVar` | Single process | No | No |
| TaskGroup + shared deadline | `TaskGroup` + context var | Sibling cancellation | No | No |
| Recursive DeadlineTree | Explicit tree objects | Per-branch | No | No |
| Deadline-aware semaphore | Semaphore + deadline check | Slot-level | No | No |
| HTTP header propagation | `X-Request-Deadline` header | Remote service | Yes | No |
| Adaptive P99 allocation | Latency history + P99 | Per-branch | No | Yes |

**Recommendation**: Use the **recursive DeadlineTree** (Solution 3) as the default for in-process orchestration — it is explicit and composable. Add **HTTP header propagation** (Solution 5) wherever tool calls cross process boundaries. Layer in **P99-based adaptive allocation** (Solution 6) once you have enough latency history to make estimates reliable.
