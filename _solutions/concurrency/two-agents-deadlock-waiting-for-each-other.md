---
layout: solution
title: "Two Agents Deadlock Waiting for Each Other — Circular Dependency"
category: concurrency
description: "Agent A waits for Agent B to complete a sub-task. Agent B is waiting for a result from Agent A. Neither proceeds. Classic circular wait deadlock in multi-agent systems."
tags: [deadlock, circular-dependency, multi-agent, concurrency, orchestration]
---

# Two Agents Deadlock Waiting for Each Other — Circular Dependency

## Symptom
- Two agents both show as "waiting" indefinitely
- System hangs with no errors — just silence
- Agent A is blocked on `await agent_b.get_result()`
- Agent B is blocked on `await agent_a.get_result()`
- Logs show both agents waiting, neither making progress
- Restarting fixes it temporarily but deadlock recurs

## Root Cause
Circular resource dependency: A needs B's output, B needs A's output, both wait indefinitely. Deadlock requires four conditions (Coffman): mutual exclusion, hold-and-wait, no preemption, circular wait. Eliminating any one prevents deadlock.

## Fix

### Option 1: Break circular dependency by reordering

```python
# DEADLOCK: A waits for B, B waits for A
async def agent_a():
    b_result = await agent_b.get_result()  # Waits for B
    return process(b_result)

async def agent_b():
    a_result = await agent_a.get_result()  # Waits for A — DEADLOCK
    return process(a_result)

# FIX: identify what B actually needs from A
# Often, B doesn't need A's full result — just an initial value
async def fixed_workflow():
    # Step 1: A produces initial data (no B dependency)
    initial_data = await agent_a.phase_one()

    # Step 2: B uses initial data (no A dependency now)
    b_result = await agent_b.run(initial_data)

    # Step 3: A completes using B's output
    final_result = await agent_a.phase_two(b_result)
    return final_result
```

### Option 2: Timeout to detect and break deadlock

```python
import asyncio

async def agent_with_timeout(agent_fn, timeout_seconds: float = 30.0, name: str = "agent"):
    try:
        return await asyncio.wait_for(agent_fn(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        raise RuntimeError(
            f"Agent '{name}' timed out after {timeout_seconds}s. "
            f"Possible deadlock — check for circular dependencies."
        )

async def safe_multi_agent_run():
    try:
        results = await asyncio.gather(
            agent_with_timeout(agent_a, timeout=30, name="agent_a"),
            agent_with_timeout(agent_b, timeout=30, name="agent_b"),
        )
        return results
    except RuntimeError as e:
        print(f"Deadlock detected: {e}")
        # Cancel all pending tasks
        for task in asyncio.all_tasks():
            if not task.done():
                task.cancel()
        raise
```

### Option 3: Resource ordering to prevent circular wait

```python
import asyncio

# Assign numeric IDs to resources; always acquire in order
RESOURCE_ORDER = {
    "database": 1,
    "cache": 2,
    "file_system": 3,
    "external_api": 4,
}

class OrderedResourceManager:
    def __init__(self):
        self._locks = {name: asyncio.Lock() for name in RESOURCE_ORDER}

    async def acquire_resources(self, resource_names: list[str]):
        """Always acquire locks in canonical order — prevents circular wait"""
        ordered = sorted(resource_names, key=lambda r: RESOURCE_ORDER[r])
        acquired = []
        try:
            for resource in ordered:
                await self._locks[resource].acquire()
                acquired.append(resource)
        except:
            # Release all acquired locks on failure
            for resource in acquired:
                self._locks[resource].release()
            raise
        return acquired

    def release_resources(self, resource_names: list[str]):
        for resource in resource_names:
            self._locks[resource].release()

manager = OrderedResourceManager()

async def agent_a():
    # Acquires database then cache (order: 1, 2)
    resources = await manager.acquire_resources(["database", "cache"])
    try:
        return await do_work()
    finally:
        manager.release_resources(resources)

async def agent_b():
    # Also acquires database then cache (same order — no deadlock)
    resources = await manager.acquire_resources(["cache", "database"])  # Reordered to 1, 2
    try:
        return await do_work()
    finally:
        manager.release_resources(resources)
```

### Option 4: Message-passing instead of direct calls

```python
import asyncio

class AgentMessageBus:
    """Agents communicate via messages — no direct blocking calls"""
    def __init__(self):
        self._queues = {}

    def register(self, agent_id: str) -> asyncio.Queue:
        self._queues[agent_id] = asyncio.Queue()
        return self._queues[agent_id]

    async def send(self, to: str, message: dict):
        await self._queues[to].put(message)

    async def receive(self, agent_id: str, timeout: float = 30.0) -> dict:
        return await asyncio.wait_for(
            self._queues[agent_id].get(),
            timeout=timeout
        )

bus = AgentMessageBus()

async def agent_a(bus):
    queue = bus.register("agent_a")
    # Send request to B (non-blocking)
    await bus.send("agent_b", {"from": "agent_a", "request": "process_data"})
    # Wait for response with timeout
    response = await bus.receive("agent_a", timeout=30)
    return response["result"]

async def agent_b(bus):
    queue = bus.register("agent_b")
    # Process requests as they arrive — no calls to A
    while True:
        message = await bus.receive("agent_b", timeout=60)
        result = await process(message["request"])
        await bus.send(message["from"], {"result": result})
```

### Option 5: Deadlock detection via dependency graph

```python
from collections import defaultdict

class DependencyGraph:
    """Track what each agent is waiting for, detect cycles"""
    def __init__(self):
        self.waiting_for = {}  # agent_id -> agent_id it's waiting on

    def add_wait(self, waiter: str, waiting_for: str):
        self.waiting_for[waiter] = waiting_for
        if self.has_cycle():
            self.waiting_for.pop(waiter)
            raise RuntimeError(
                f"Deadlock detected: {waiter} → {waiting_for} creates a cycle. "
                f"Current dependencies: {self.waiting_for}"
            )

    def remove_wait(self, waiter: str):
        self.waiting_for.pop(waiter, None)

    def has_cycle(self) -> bool:
        visited = set()
        for start in self.waiting_for:
            current = start
            path = set()
            while current in self.waiting_for:
                if current in path:
                    return True  # Cycle detected
                path.add(current)
                current = self.waiting_for[current]
        return False

dep_graph = DependencyGraph()

async def wait_for_agent(my_id: str, target_id: str, target_fn):
    dep_graph.add_wait(my_id, target_id)
    try:
        return await target_fn()
    finally:
        dep_graph.remove_wait(my_id)
```

## Deadlock Prevention Checklist

| Coffman Condition | How to break it |
|---|---|
| Mutual exclusion | Use lock-free data structures where possible |
| Hold-and-wait | Acquire all locks at once or release before requesting more |
| No preemption | Use timeouts — allow lock acquisition to fail |
| Circular wait | Always acquire resources in canonical order |

## Expected Token Savings
Debugging silent deadlock without detection: ~15,000 tokens (hours of investigation)
Timeout + dependency tracking: detects within seconds

## Environment
- Multi-agent orchestration systems; most common with shared resources
- Source: direct experience, computer science concurrency fundamentals
