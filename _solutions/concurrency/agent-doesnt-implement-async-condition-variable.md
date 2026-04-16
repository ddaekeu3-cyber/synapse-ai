---
title: "Agent Doesn't Implement Async Condition Variable"
description: "Agent coroutines that need to wait for shared state to reach a specific condition resort to polling loops with sleep — wasting CPU and introducing latency instead of waiting on an async condition variable."
category: concurrency
difficulty: advanced
tags: [asyncio, condition-variable, synchronization, wait, notify, concurrency, coordination]
---

# Agent Doesn't Implement Async Condition Variable

## Problem

Agents often need to coordinate coroutines around shared state: "wait until the tool result is ready", "wait until the rate limit window resets", "wait until all workers have checked in". The anti-pattern is a polling loop — `while not ready: await asyncio.sleep(0.1)` — which wastes CPU, introduces 0–100ms latency jitter, and doesn't scale when many coroutines are waiting. `asyncio.Condition` (the async equivalent of POSIX `pthread_cond_t`) solves this: waiters sleep with zero CPU overhead and are woken exactly when the condition changes.

## Solution 1: Basic asyncio.Condition — Wait Until State Changes

Use `asyncio.Condition` to block coroutines until a predicate becomes true, then notify all waiters atomically.

```python
import asyncio
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

class ToolResultStore:
    """
    Shared store for tool results.
    Workers deposit results; agents wait for specific results.
    No polling — condition variable wakes agents precisely when their result arrives.
    """

    def __init__(self):
        self._results: dict[str, any] = {}
        self._cond = asyncio.Condition()

    async def deposit(self, tool_call_id: str, result: any) -> None:
        async with self._cond:
            self._results[tool_call_id] = result
            self._cond.notify_all()  # wake all waiters

    async def wait_for(self, tool_call_id: str, timeout: float = 30.0) -> any:
        """Block until result for tool_call_id is available. Raises TimeoutError."""
        async with self._cond:
            try:
                await asyncio.wait_for(
                    self._cond.wait_for(lambda: tool_call_id in self._results),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                raise TimeoutError(f"Tool call {tool_call_id!r} did not complete within {timeout}s")
            return self._results.pop(tool_call_id)

store = ToolResultStore()

async def tool_worker(tool_call_id: str, coro):
    """Execute a tool and deposit result into the store."""
    try:
        result = await coro
        await store.deposit(tool_call_id, {"status": "ok", "data": result})
    except Exception as exc:
        await store.deposit(tool_call_id, {"status": "error", "error": str(exc)})

async def fake_search(query: str) -> dict:
    await asyncio.sleep(0.3)
    return {"results": [f"Result for {query}"]}

async def fake_db(table: str) -> dict:
    await asyncio.sleep(0.5)
    return {"rows": [{"id": 1, "table": table}]}

async def agent_with_condition_variable(question: str) -> str:
    # Launch tools in background
    asyncio.create_task(tool_worker("search-1", fake_search(question)))
    asyncio.create_task(tool_worker("db-1", fake_db("users")))

    # Wait for results — zero CPU polling
    search_result, db_result = await asyncio.gather(
        store.wait_for("search-1"),
        store.wait_for("db-1"),
    )

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Search: {search_result}\nDB: {db_result}\n\n{question}",
        }],
    )
    return resp.content[0].text
```

**When to use**: Any pattern where one coroutine produces a value that another is waiting for. Replace every `while not ready: await asyncio.sleep(N)` with a condition variable.

---

## Solution 2: Broadcast Condition — Notify Multiple Waiters on State Transition

When shared state transitions (e.g., "model is ready", "rate limit lifted"), notify all waiting coroutines simultaneously.

```python
import asyncio
import time
from enum import Enum
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

class AgentState(Enum):
    INITIALIZING  = "initializing"
    READY         = "ready"
    RATE_LIMITED  = "rate_limited"
    SHUTTING_DOWN = "shutting_down"

class AgentStateMachine:
    """
    Agent with condition-variable-based state transitions.
    All coroutines waiting on a state change are notified at once.
    """

    def __init__(self):
        self._state = AgentState.INITIALIZING
        self._cond = asyncio.Condition()
        self._rate_limit_until: float = 0.0

    @property
    def state(self) -> AgentState:
        return self._state

    async def set_state(self, new_state: AgentState, rate_limit_until: float = 0.0) -> None:
        async with self._cond:
            self._state = new_state
            self._rate_limit_until = rate_limit_until
            self._cond.notify_all()

    async def wait_until_ready(self, timeout: float = 60.0) -> bool:
        """Block until state is READY. Returns False if timed out or shutdown."""
        async with self._cond:
            try:
                await asyncio.wait_for(
                    self._cond.wait_for(
                        lambda: self._state in (AgentState.READY, AgentState.SHUTTING_DOWN)
                    ),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                return False
            return self._state == AgentState.READY

    async def wait_for_rate_limit(self) -> bool:
        """Block until rate limit lifts. Returns False on shutdown."""
        async with self._cond:
            await self._cond.wait_for(
                lambda: self._state != AgentState.RATE_LIMITED or
                        time.monotonic() >= self._rate_limit_until
            )
            return self._state != AgentState.SHUTTING_DOWN

    async def acquire_for_request(self) -> bool:
        """
        Called before each LLM request.
        Waits through INITIALIZING and RATE_LIMITED states.
        Returns False if shutting down.
        """
        async with self._cond:
            while True:
                if self._state == AgentState.SHUTTING_DOWN:
                    return False
                if self._state == AgentState.READY:
                    return True
                if self._state == AgentState.RATE_LIMITED:
                    remaining = self._rate_limit_until - time.monotonic()
                    if remaining <= 0:
                        self._state = AgentState.READY
                        self._cond.notify_all()
                        return True
                await self._cond.wait()

agent_sm = AgentStateMachine()

async def rate_limited_agent(user_message: str) -> dict:
    ready = await agent_sm.acquire_for_request()
    if not ready:
        return {"error": "agent_shutting_down"}

    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": user_message}],
        )
        return {"response": resp.content[0].text}
    except Exception as exc:
        if "429" in str(exc):
            await agent_sm.set_state(AgentState.RATE_LIMITED, time.monotonic() + 5.0)
            return {"error": "rate_limited", "retry_in": 5}
        raise

async def demo():
    # Startup sequence
    await asyncio.sleep(0.1)  # simulate init
    await agent_sm.set_state(AgentState.READY)

    # Multiple concurrent requests all wait for READY
    results = await asyncio.gather(*[
        rate_limited_agent(f"Question {i}")
        for i in range(5)
    ])
    return results
```

**When to use**: Agents with lifecycle states (init → ready → rate-limited → ready). A condition variable broadcasts state changes to all waiting coroutines without polling.

---

## Solution 3: Predicate Condition — Wait for Arbitrary State Predicates

Generalize: let callers specify arbitrary predicates to wait on, not just a fixed set of states.

```python
import asyncio
from typing import Any, Callable
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

class PredicateCondition:
    """
    A condition variable that lets callers supply arbitrary predicates.
    Every notify() evaluates all waiting predicates and wakes matching waiters.
    """

    def __init__(self):
        self._state: dict[str, Any] = {}
        self._cond = asyncio.Condition()

    async def update(self, **kwargs) -> None:
        """Update state and notify all waiters to re-evaluate their predicates."""
        async with self._cond:
            self._state.update(kwargs)
            self._cond.notify_all()

    async def wait_until(
        self,
        predicate: Callable[[dict], bool],
        timeout: float | None = None,
    ) -> dict:
        """
        Block until predicate(state) is True.
        Returns a snapshot of the state when the predicate first holds.
        """
        async with self._cond:
            coro = self._cond.wait_for(lambda: predicate(self._state))
            if timeout is not None:
                await asyncio.wait_for(coro, timeout=timeout)
            else:
                await coro
            return dict(self._state)

    async def get(self) -> dict:
        async with self._cond:
            return dict(self._state)

# Global agent state
agent_state = PredicateCondition()

async def context_loader():
    """Loads context into shared state; agents wait on specific fields."""
    await asyncio.sleep(0.2)
    await agent_state.update(system_prompt_loaded=True, tools_loaded=False)
    await asyncio.sleep(0.1)
    await agent_state.update(tools_loaded=True, user_profile={"id": "u1", "name": "Alice"})
    await asyncio.sleep(0.05)
    await agent_state.update(context_ready=True)

async def agent_worker(worker_id: int, question: str) -> str:
    # Wait for exactly the conditions this worker needs
    state = await agent_state.wait_until(
        predicate=lambda s: s.get("system_prompt_loaded") and s.get("user_profile") is not None,
        timeout=10.0,
    )

    user_profile = state.get("user_profile", {})
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system="You are a helpful assistant.",
        messages=[{
            "role": "user",
            "content": f"User: {user_profile.get('name')}. Question: {question}",
        }],
    )
    return resp.content[0].text

async def demo():
    loader = asyncio.create_task(context_loader())
    workers = [
        asyncio.create_task(agent_worker(i, f"Question {i}"))
        for i in range(5)
    ]
    await loader
    results = await asyncio.gather(*workers)
    return results
```

**When to use**: Agents with complex initialization dependencies where different components need different subsets of shared state to be ready.

---

## Solution 4: Counted Rendezvous — Wait Until N Workers Have Checked In

Block until a fixed number of subagents/workers have completed their phase before proceeding.

```python
import asyncio
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

class Rendezvous:
    """
    Counted barrier: blocks until N parties have all called arrive().
    After that, all are released simultaneously.
    Can be reused for multiple phases.
    """

    def __init__(self, n_parties: int):
        self._n = n_parties
        self._count = 0
        self._phase = 0
        self._cond = asyncio.Condition()

    async def arrive_and_wait(self) -> int:
        """
        Signal arrival and block until all N parties have arrived.
        Returns the completed phase number.
        """
        async with self._cond:
            self._count += 1
            arrived_count = self._count
            current_phase = self._phase

            if arrived_count == self._n:
                # Last to arrive: release everyone
                self._count = 0
                self._phase += 1
                self._cond.notify_all()
                return current_phase

            # Wait until phase advances (all parties arrived)
            await self._cond.wait_for(lambda: self._phase > current_phase)
            return current_phase

async def subagent_phase(
    agent_id: int,
    barrier: Rendezvous,
    phase_work_coro,
) -> str:
    """
    Subagent that completes work then waits for all peers before proceeding.
    Useful for multi-phase agent pipelines (research → synthesis → review).
    """
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic()

    # Phase 1: independent research
    result = await phase_work_coro
    print(f"[agent-{agent_id}] Phase 1 complete: {str(result)[:50]}")

    # Wait for all agents to finish phase 1 before starting phase 2
    await barrier.arrive_and_wait()

    # Phase 2: synthesis (all agents have their phase 1 results)
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": f"Summarize: {result}"}],
    )
    return resp.content[0].text

async def multi_phase_agent_pipeline(questions: list[str]) -> list[str]:
    n = len(questions)
    barrier = Rendezvous(n_parties=n)

    async def research(q: str) -> dict:
        await asyncio.sleep(0.1)
        return {"query": q, "findings": f"Findings for: {q}"}

    results = await asyncio.gather(*[
        subagent_phase(i, barrier, research(q))
        for i, q in enumerate(questions)
    ])
    return list(results)
```

**When to use**: Multi-phase agent pipelines (research, synthesis, review) where all agents must complete one phase before any begin the next. The rendezvous replaces complex inter-agent signaling.

---

## Solution 5: Producer–Consumer with Condition — Back-Pressure via State

A producer that generates tool calls and a consumer (LLM loop) that processes them, with a condition variable signaling "work available" and "space available" for back-pressure.

```python
import asyncio
from collections import deque
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

class BoundedWorkQueue:
    """
    Bounded queue with condition variable signaling.
    Producer waits when queue is full (back-pressure).
    Consumer waits when queue is empty.
    No polling in either direction.
    """

    def __init__(self, maxsize: int):
        self._items: deque = deque()
        self._maxsize = maxsize
        self._cond = asyncio.Condition()
        self._done = False

    async def put(self, item) -> None:
        async with self._cond:
            # Wait until there is space (back-pressure)
            await self._cond.wait_for(lambda: len(self._items) < self._maxsize or self._done)
            if self._done:
                return
            self._items.append(item)
            self._cond.notify_all()  # wake consumers

    async def get(self) -> tuple[any, bool]:
        """Returns (item, has_item). Returns (None, False) when done and empty."""
        async with self._cond:
            await self._cond.wait_for(lambda: self._items or self._done)
            if self._items:
                item = self._items.popleft()
                self._cond.notify_all()  # wake producers (space freed)
                return item, True
            return None, False  # done + empty

    async def close(self) -> None:
        async with self._cond:
            self._done = True
            self._cond.notify_all()

    def __len__(self) -> int:
        return len(self._items)

async def tool_producer(queue: BoundedWorkQueue, n_tasks: int) -> None:
    """Generates tool call tasks and enqueues them."""
    for i in range(n_tasks):
        await queue.put({"task_id": i, "query": f"search query {i}"})
        await asyncio.sleep(0.02)  # simulate tool call overhead
    await queue.close()

async def llm_consumer(queue: BoundedWorkQueue) -> list[str]:
    """Processes tool results through LLM."""
    results = []
    while True:
        item, ok = await queue.get()
        if not ok:
            break
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": f"Process: {item}"}],
        )
        results.append(resp.content[0].text)
    return results

async def demo():
    queue = BoundedWorkQueue(maxsize=5)  # back-pressure kicks in after 5 queued items
    producer = asyncio.create_task(tool_producer(queue, n_tasks=20))
    results = await llm_consumer(queue)
    await producer
    print(f"Processed {len(results)} items")
    return results
```

**When to use**: Agent pipelines where tool call generation and LLM processing run at different rates. The condition variable implements zero-latency back-pressure without polling.

---

## Solution 6: Timeout-Aware Condition with Fallback — Graceful Degradation on Slow Waiters

When a condition doesn't become true within a deadline, fall back to a best-effort response rather than hanging indefinitely.

```python
import asyncio
import time
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

class TimeoutCondition:
    """
    Condition variable with deadline propagation.
    If the condition doesn't become true by the deadline, callers receive
    a fallback value instead of raising TimeoutError.
    """

    def __init__(self):
        self._store: dict[str, any] = {}
        self._cond = asyncio.Condition()

    async def set(self, key: str, value: any) -> None:
        async with self._cond:
            self._store[key] = value
            self._cond.notify_all()

    async def wait_or_fallback(
        self,
        key: str,
        fallback: any,
        timeout: float,
    ) -> tuple[any, bool]:
        """
        Wait for key to be set.
        Returns (value, True) on success or (fallback, False) on timeout.
        """
        deadline = time.monotonic() + timeout
        async with self._cond:
            while key not in self._store:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return fallback, False
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    return fallback, False
            return self._store[key], True

    async def wait_any_or_fallback(
        self,
        keys: list[str],
        fallbacks: dict[str, any],
        timeout: float,
    ) -> dict[str, tuple[any, bool]]:
        """Wait for multiple keys simultaneously; return whatever is ready by deadline."""
        deadline = time.monotonic() + timeout
        results: dict[str, tuple[any, bool]] = {}

        tasks = {
            key: asyncio.create_task(
                self.wait_or_fallback(key, fallbacks.get(key), timeout)
            )
            for key in keys
        }
        done_map = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for key, outcome in zip(tasks.keys(), done_map):
            if isinstance(outcome, Exception):
                results[key] = (fallbacks.get(key), False)
            else:
                results[key] = outcome
        return results

cond = TimeoutCondition()

async def slow_tool(key: str, delay: float, value: any):
    await asyncio.sleep(delay)
    await cond.set(key, value)

async def agent_with_timeout_condition(question: str) -> dict:
    # Launch tools with varying latencies
    asyncio.create_task(slow_tool("fast_db",    0.1, {"rows": 5}))
    asyncio.create_task(slow_tool("slow_api",   2.0, {"data": "api_result"}))
    asyncio.create_task(slow_tool("medium_svc", 0.5, {"status": "ok"}))

    # Wait for all, but fall back to None if any exceed 0.8s
    results = await cond.wait_any_or_fallback(
        keys=["fast_db", "slow_api", "medium_svc"],
        fallbacks={
            "fast_db":   None,
            "slow_api":  {"data": "unavailable"},   # stale fallback
            "medium_svc": None,
        },
        timeout=0.8,
    )

    context_parts = []
    degraded = []
    for key, (value, fresh) in results.items():
        if value is not None:
            context_parts.append(f"[{key}{'*' if not fresh else ''}]: {value}")
        if not fresh:
            degraded.append(key)

    context = "\n".join(context_parts)
    if degraded:
        context += f"\n\nNote: {degraded} returned fallback data due to timeout."

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"{context}\n\n{question}"}],
    )
    return {
        "response": resp.content[0].text,
        "degraded_tools": degraded,
        "results_metadata": {k: {"fresh": v[1]} for k, v in results.items()},
    }
```

**When to use**: Agents with tail-latency-sensitive SLAs. Timeout-aware conditions let you define per-dependency deadlines and degrade gracefully when individual tools are slow.

---

## Comparison

| Solution | Use Case | Waiters | CPU During Wait | Timeout Support | Fallback | Best For |
|---|---|---|---|---|---|---|
| Basic condition | Single result ready | Many | Zero | Yes | No | Tool result coordination |
| Broadcast condition | State machine transitions | Many | Zero | Yes | No | Agent lifecycle states |
| Predicate condition | Arbitrary state predicates | Many | Zero | Yes | No | Complex init dependencies |
| Rendezvous | N-way synchronization | Exactly N | Zero | No | No | Multi-phase pipelines |
| Producer-consumer | Back-pressure | 1–N | Zero | No | No | Uneven producer/consumer rates |
| Timeout with fallback | Deadline-bounded wait | Many | Zero | Yes | Yes | SLA-sensitive agents |

**Rule of thumb**: Replace every `while not ready: await asyncio.sleep(N)` loop with `asyncio.Condition`. The condition variable uses zero CPU while waiting and wakes waiters with microsecond latency rather than the polling interval.
