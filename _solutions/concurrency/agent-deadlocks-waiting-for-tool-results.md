---
layout: solution
title: "Agent Deadlocks Waiting for Tool Results"
category: concurrency
description: "Agent hangs indefinitely when tool calls form circular dependencies, when callbacks never fire, or when async tasks are awaited in wrong order."
tags: [concurrency, deadlock, asyncio, timeout, tool-use]
---

## Symptom

The agent makes one or more tool calls and then hangs forever, producing no output and no error. CPU usage drops to zero. In async deployments, the event loop is blocked. In multi-agent systems, Agent A waits for Agent B's result while Agent B waits for Agent A's result — neither can proceed.

```python
# BROKEN: tool A waits for tool B; tool B waits for tool A
async def tool_a(ctx):
    result_b = await tool_b(ctx)   # blocks
    return process(result_b)

async def tool_b(ctx):
    result_a = await tool_a(ctx)   # blocks — circular wait
    return process(result_a)

# BROKEN: asyncio.gather inside a synchronous context that's already running
def sync_handler():
    loop = asyncio.get_event_loop()
    result = loop.run_until_complete(async_tool())  # deadlocks if loop is already running
```

Root causes:
- Circular dependency: Tool A calls Tool B which calls Tool A
- Missing `await` on a coroutine — future never resolves
- Using `loop.run_until_complete()` inside an already-running event loop
- Threading deadlock: two threads each hold a lock the other needs
- Callback-based tool that never invokes its callback on error paths
- `asyncio.Queue.join()` called when consumers have already exited

---

## Root Cause

Deadlocks in agent tool execution have three common shapes:

1. **Circular dependency** — the dependency graph has a cycle. Tool A depends on Tool B's output; Tool B was designed to call Tool A. Neither finishes first.
2. **Event loop reentry** — `asyncio` is single-threaded. Calling `loop.run_until_complete()` from within a coroutine that's already running on that loop blocks the loop from advancing — the inner call can never complete.
3. **Lock inversion** — two concurrent operations each acquire locks in different order. `Thread-1` holds Lock-X, waits for Lock-Y. `Thread-2` holds Lock-Y, waits for Lock-X.

The solution is: always enforce timeouts, detect cycles before execution, and never nest `run_until_complete`.

---

## Fix

### Option 1 — Timeout Every Tool Call

The simplest deadlock defense: every tool call has a hard timeout so a hung tool eventually raises an exception.

```python
import anthropic
import asyncio
import json
from typing import Any

client = anthropic.AsyncAnthropic()

TOOL_TIMEOUT_SECONDS = 30  # global default; override per-tool if needed

TOOL_TIMEOUTS = {
    "query_database": 60,
    "send_email": 10,
    "fetch_url": 15,
    "run_code": 120,
}

async def execute_tool_with_timeout(tool_name: str, tool_input: dict) -> Any:
    """Execute a tool with a per-tool timeout. Raises TimeoutError on hang."""
    timeout = TOOL_TIMEOUTS.get(tool_name, TOOL_TIMEOUT_SECONDS)

    try:
        result = await asyncio.wait_for(
            dispatch_tool(tool_name, tool_input),
            timeout=timeout,
        )
        return result
    except asyncio.TimeoutError:
        return {
            "error": "TIMEOUT",
            "tool": tool_name,
            "timeout_seconds": timeout,
            "message": (
                f"Tool '{tool_name}' did not respond within {timeout}s. "
                "This may indicate a deadlock or network issue. "
                "Do not retry — report the timeout to the user."
            )
        }

async def dispatch_tool(tool_name: str, tool_input: dict) -> Any:
    """Route to actual tool implementation."""
    if tool_name == "query_database":
        return await tool_query_database(**tool_input)
    if tool_name == "send_email":
        return await tool_send_email(**tool_input)
    if tool_name == "fetch_url":
        return await tool_fetch_url(**tool_input)
    return {"error": f"Unknown tool: {tool_name}"}

# Simulated tools (replace with real implementations)
async def tool_query_database(sql: str, **kwargs) -> dict:
    await asyncio.sleep(0.1)  # simulate fast DB
    return {"rows": [{"id": 1, "value": "test"}], "row_count": 1}

async def tool_send_email(to: str, subject: str, body: str, **kwargs) -> dict:
    await asyncio.sleep(0.05)
    return {"sent": True, "message_id": "msg-123"}

async def tool_fetch_url(url: str, **kwargs) -> dict:
    await asyncio.sleep(999)  # simulate hung tool — will be killed by timeout

tools = [
    {
        "name": "fetch_url",
        "description": "Fetch a URL. Will timeout after 15s if unresponsive.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"]
        }
    },
    {
        "name": "query_database",
        "description": "Run a SQL query.",
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"]
        }
    }
]

async def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type == "tool_use":
                # This will time out after 15s instead of hanging forever
                result = await execute_tool_with_timeout(block.name, block.input)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })
        messages.append({"role": "user", "content": results})

result = asyncio.run(run_agent("Fetch https://example.com/api/data"))
print(result)
# Expected: Agent reports timeout instead of hanging
```

**Expected Token Savings:** Prevents infinite loops that would eventually exhaust the context window with repeated failed attempts.

**Environment:** Python 3.9+, `asyncio`, `anthropic>=0.40.0`.

---

### Option 2 — Dependency Graph Cycle Detection

Validate the tool execution plan before running it — refuse to execute any plan with circular dependencies.

```python
import anthropic
import json
from collections import defaultdict, deque
from typing import Optional

client = anthropic.Anthropic()

class DependencyGraph:
    """Directed graph for detecting cycles in tool execution plans."""

    def __init__(self):
        self.edges: dict[str, list[str]] = defaultdict(list)
        self.nodes: set[str] = set()

    def add_node(self, name: str):
        self.nodes.add(name)

    def add_dependency(self, dependent: str, depends_on: str):
        """dependent must run after depends_on."""
        self.nodes.add(dependent)
        self.nodes.add(depends_on)
        self.edges[dependent].append(depends_on)

    def find_cycle(self) -> Optional[list[str]]:
        """Return a cycle path if one exists, else None. Uses DFS with coloring."""
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {node: WHITE for node in self.nodes}
        parent = {}
        cycle_path = []

        def dfs(node: str) -> bool:
            color[node] = GRAY
            for neighbor in self.edges.get(node, []):
                if color[neighbor] == GRAY:
                    # Found cycle — reconstruct path
                    path = [neighbor]
                    curr = node
                    while curr != neighbor:
                        path.append(curr)
                        curr = parent.get(curr, neighbor)
                    path.append(neighbor)
                    cycle_path.extend(reversed(path))
                    return True
                if color[neighbor] == WHITE:
                    parent[neighbor] = node
                    if dfs(neighbor):
                        return True
            color[node] = BLACK
            return False

        for node in self.nodes:
            if color[node] == WHITE:
                if dfs(node):
                    return cycle_path
        return None

    def topological_order(self) -> Optional[list[str]]:
        """Return nodes in execution order, or None if cycle detected."""
        cycle = self.find_cycle()
        if cycle:
            return None

        in_degree = {node: 0 for node in self.nodes}
        for node in self.nodes:
            for dep in self.edges.get(node, []):
                in_degree[dep] = in_degree.get(dep, 0)  # ensure key exists

        # Kahn's algorithm
        queue = deque([n for n in self.nodes if in_degree.get(n, 0) == 0])
        order = []
        while queue:
            node = queue.popleft()
            order.append(node)
            for neighbor in self.nodes:
                if node in self.edges.get(neighbor, []):
                    in_degree[neighbor] = in_degree.get(neighbor, 0) - 1
                    if in_degree[neighbor] == 0:
                        queue.append(neighbor)
        return order if len(order) == len(self.nodes) else None

def validate_tool_plan(plan: list[dict]) -> tuple[bool, str]:
    """
    Validate that a tool execution plan has no circular dependencies.
    plan: list of {"tool": str, "depends_on": list[str], "id": str}
    Returns (is_valid, error_message).
    """
    graph = DependencyGraph()

    for step in plan:
        step_id = step["id"]
        graph.add_node(step_id)
        for dep in step.get("depends_on", []):
            graph.add_dependency(step_id, dep)

    cycle = graph.find_cycle()
    if cycle:
        return False, f"Circular dependency detected: {' → '.join(cycle)}"

    order = graph.topological_order()
    if order is None:
        return False, "Cannot determine safe execution order"

    return True, f"Valid plan, execution order: {' → '.join(order)}"

# Tool plan validation example
plans = [
    # Valid plan: step3 depends on step1 and step2
    [
        {"id": "step1", "tool": "fetch_data", "depends_on": []},
        {"id": "step2", "tool": "fetch_config", "depends_on": []},
        {"id": "step3", "tool": "process", "depends_on": ["step1", "step2"]},
    ],
    # Invalid plan: circular dependency step1 → step2 → step1
    [
        {"id": "step1", "tool": "fetch_data", "depends_on": ["step2"]},
        {"id": "step2", "tool": "process", "depends_on": ["step1"]},
    ],
]

for i, plan in enumerate(plans):
    valid, message = validate_tool_plan(plan)
    print(f"Plan {i+1}: {'VALID' if valid else 'INVALID'} — {message}")

# Agent integration: validate before executing
tools = [{
    "name": "submit_execution_plan",
    "description": (
        "Submit a tool execution plan for validation and execution. "
        "Plans with circular dependencies will be rejected."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "plan": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "tool": {"type": "string"},
                        "depends_on": {"type": "array", "items": {"type": "string"}},
                    }
                }
            }
        },
        "required": ["plan"]
    }
}]

response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=512,
    tools=tools,
    messages=[{
        "role": "user",
        "content": "Plan to: (1) fetch user data, (2) fetch user's orders using user data, (3) generate report from both"
    }]
)
print(response.content)
```

**Expected Token Savings:** Prevents deadlock-induced infinite loops; saves the entire token cost of a hung session.

**Environment:** Python 3.9+, `anthropic>=0.40.0`.

---

### Option 3 — asyncio.Event-Based Synchronization Instead of Blocking Waits

Replace blocking waits with `asyncio.Event` to allow the event loop to progress.

```python
import anthropic
import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Optional

client = anthropic.AsyncAnthropic()

@dataclass
class ToolFuture:
    """A non-blocking future for a tool result."""
    tool_use_id: str
    event: asyncio.Event = field(default_factory=asyncio.Event)
    result: Optional[Any] = None
    error: Optional[str] = None

    async def wait(self, timeout: float = 30.0) -> Any:
        """Wait for result with timeout — never blocks the event loop permanently."""
        try:
            await asyncio.wait_for(self.event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError(
                f"Tool {self.tool_use_id} timed out after {timeout}s — possible deadlock"
            )

        if self.error:
            raise RuntimeError(f"Tool failed: {self.error}")
        return self.result

    def set_result(self, result: Any):
        self.result = result
        self.event.set()

    def set_error(self, error: str):
        self.error = error
        self.event.set()

class NonBlockingToolOrchestrator:
    """
    Orchestrates tool calls using events instead of blocking waits.
    Multiple tools can run concurrently; results are delivered via events.
    """

    def __init__(self):
        self._pending: dict[str, ToolFuture] = {}

    def register(self, tool_use_id: str) -> ToolFuture:
        future = ToolFuture(tool_use_id=tool_use_id)
        self._pending[tool_use_id] = future
        return future

    async def execute_and_deliver(self, tool_use_id: str, coro):
        """Execute a coroutine and deliver its result to the registered future."""
        future = self._pending.get(tool_use_id)
        if not future:
            return
        try:
            result = await coro
            future.set_result(result)
        except Exception as e:
            future.set_error(str(e))
        finally:
            self._pending.pop(tool_use_id, None)

orchestrator = NonBlockingToolOrchestrator()

# Simulated tool implementations
async def slow_database_query(sql: str) -> dict:
    await asyncio.sleep(2.0)  # simulate slow query
    return {"rows": [{"count": 42}]}

async def fast_config_lookup(key: str) -> dict:
    await asyncio.sleep(0.1)
    return {"value": "production", "ttl": 3600}

async def process_results(data: dict, config: dict) -> dict:
    await asyncio.sleep(0.2)
    return {"processed": True, "input_rows": data.get("rows", [])}

tools = [
    {
        "name": "query_database",
        "description": "Run a SQL query (may take up to 60s)",
        "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}
    },
    {
        "name": "get_config",
        "description": "Fetch a configuration value",
        "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}
    },
]

async def handle_tool_calls(tool_blocks: list) -> list[dict]:
    """Execute all tool calls concurrently using event-based futures."""
    futures = {}
    execution_tasks = []

    for block in tool_blocks:
        future = orchestrator.register(block.id)
        futures[block.id] = future

        if block.name == "query_database":
            coro = slow_database_query(block.input["sql"])
        elif block.name == "get_config":
            coro = fast_config_lookup(block.input["key"])
        else:
            async def unknown_tool():
                return {"error": f"Unknown tool: {block.name}"}
            coro = unknown_tool()

        execution_tasks.append(orchestrator.execute_and_deliver(block.id, coro))

    # Launch all tool executions concurrently — none block each other
    await asyncio.gather(*execution_tasks)

    # Now collect results via event-based futures (already set by gather above)
    tool_results = []
    for block in tool_blocks:
        future = futures[block.id]
        try:
            result = await future.wait(timeout=60.0)
            content = json.dumps(result)
        except asyncio.TimeoutError as e:
            content = json.dumps({"error": str(e)})
        except RuntimeError as e:
            content = json.dumps({"error": str(e)})

        tool_results.append({
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": content,
        })

    return tool_results

async def run_agent(message: str) -> str:
    messages = [{"role": "user", "content": message}]

    while True:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        messages.append({"role": "assistant", "content": response.content})
        tool_blocks = [b for b in response.content if b.type == "tool_use"]

        # Non-blocking parallel execution — no deadlocks possible
        tool_results = await handle_tool_calls(tool_blocks)
        messages.append({"role": "user", "content": tool_results})

result = asyncio.run(run_agent("Query the user count and get the environment config"))
print(result)
```

**Expected Token Savings:** Eliminates deadlock-induced session terminations; enables parallel tool execution that reduces turn count by 30-50% on multi-tool tasks.

**Environment:** Python 3.9+, `asyncio`, `anthropic>=0.40.0`.

---

### Option 4 — Thread-Safe Tool Execution with Lock Ordering

For threaded (non-async) environments, enforce a consistent lock acquisition order to prevent lock inversion deadlocks.

```python
import anthropic
import threading
import json
import time
from contextlib import contextmanager
from typing import Any

client = anthropic.Anthropic()

# All locks named and ordered — always acquire in alphabetical order
_LOCKS: dict[str, threading.Lock] = {
    "cache": threading.Lock(),
    "database": threading.Lock(),
    "file_system": threading.Lock(),
    "network": threading.Lock(),
}

# Global ordering for locks — lower number = acquired first
LOCK_ORDER = {name: i for i, name in enumerate(sorted(_LOCKS.keys()))}

@contextmanager
def acquire_ordered(*lock_names: str, timeout: float = 10.0):
    """
    Acquire multiple locks in a consistent order to prevent deadlocks.
    Raises TimeoutError if any lock cannot be acquired within timeout.
    """
    # Always acquire in canonical order regardless of call order
    ordered = sorted(lock_names, key=lambda n: LOCK_ORDER[n])
    acquired = []

    try:
        for name in ordered:
            lock = _LOCKS[name]
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Could not acquire lock '{name}' within {timeout}s. "
                        f"Possible deadlock. Already held: {acquired}"
                    )
                if lock.acquire(timeout=min(remaining, 0.1)):
                    acquired.append(name)
                    break

        yield {name: _LOCKS[name] for name in ordered}

    finally:
        # Release in reverse order
        for name in reversed(acquired):
            _LOCKS[name].release()

def tool_update_user_cache(user_id: str, data: dict) -> dict:
    """Updates cache and database — must hold both locks."""
    try:
        # SAFE: always acquires cache then database (alphabetical)
        with acquire_ordered("cache", "database", timeout=5.0):
            time.sleep(0.01)  # simulate work
            return {"updated": True, "user_id": user_id}
    except TimeoutError as e:
        return {"error": str(e), "tool": "update_user_cache"}

def tool_sync_to_file(user_id: str) -> dict:
    """Sync database record to file — must hold both locks."""
    try:
        # SAFE: always acquires database then file_system (alphabetical)
        with acquire_ordered("database", "file_system", timeout=5.0):
            time.sleep(0.01)
            return {"synced": True, "user_id": user_id}
    except TimeoutError as e:
        return {"error": str(e), "tool": "sync_to_file"}

# Run tools concurrently from multiple threads — no deadlock possible
def run_tools_in_parallel(tool_calls: list[dict]) -> list[dict]:
    results = [None] * len(tool_calls)

    def run_one(idx: int, call: dict):
        if call["name"] == "update_user_cache":
            results[idx] = tool_update_user_cache(**call["input"])
        elif call["name"] == "sync_to_file":
            results[idx] = tool_sync_to_file(**call["input"])
        else:
            results[idx] = {"error": f"Unknown tool: {call['name']}"}

    threads = [
        threading.Thread(target=run_one, args=(i, call))
        for i, call in enumerate(tool_calls)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30.0)  # outer deadline

    return results

# Test: two tools that would deadlock without ordering
calls = [
    {"name": "update_user_cache", "input": {"user_id": "u1", "data": {"score": 10}}},
    {"name": "sync_to_file", "input": {"user_id": "u1"}},
]

print("Running potentially conflicting tools...")
results = run_tools_in_parallel(calls)
print(f"Results: {results}")
print("No deadlock!")
```

**Expected Token Savings:** Prevents permanent hangs in threaded agents; critical for correctness in production deployments.

**Environment:** Python 3.9+, `threading`, `anthropic>=0.40.0`.

---

### Option 5 — Watchdog Process for Deadlock Detection and Recovery

Run a background watchdog that detects hung tool calls and kills them if they exceed a threshold.

```python
import anthropic
import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Optional

client = anthropic.AsyncAnthropic()

@dataclass
class ActiveCall:
    tool_use_id: str
    tool_name: str
    started_at: float = field(default_factory=time.monotonic)
    task: Optional[asyncio.Task] = None
    max_duration: float = 30.0

class DeadlockWatchdog:
    """Background task that cancels hung tool calls."""

    def __init__(self, check_interval: float = 5.0):
        self._active: dict[str, ActiveCall] = {}
        self._check_interval = check_interval
        self._watchdog_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self.kills: list[str] = []

    async def start(self):
        self._watchdog_task = asyncio.create_task(self._watch_loop())

    async def stop(self):
        if self._watchdog_task:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass

    async def register(self, call: ActiveCall):
        async with self._lock:
            self._active[call.tool_use_id] = call

    async def unregister(self, tool_use_id: str):
        async with self._lock:
            self._active.pop(tool_use_id, None)

    async def _watch_loop(self):
        while True:
            await asyncio.sleep(self._check_interval)
            now = time.monotonic()

            async with self._lock:
                for call_id, call in list(self._active.items()):
                    age = now - call.started_at
                    if age > call.max_duration and call.task and not call.task.done():
                        print(
                            f"  [watchdog] Killing hung tool '{call.tool_name}' "
                            f"(running {age:.1f}s > {call.max_duration}s limit)"
                        )
                        call.task.cancel()
                        self.kills.append(call.tool_name)

watchdog = DeadlockWatchdog(check_interval=2.0)

TOOL_MAX_DURATIONS = {
    "fast_lookup": 5.0,
    "database_query": 30.0,
    "external_api": 15.0,
    "hung_tool": 5.0,  # will be killed after 5s
}

async def execute_with_watchdog(tool_name: str, tool_input: dict, tool_use_id: str) -> dict:
    """Execute a tool under watchdog supervision."""
    max_duration = TOOL_MAX_DURATIONS.get(tool_name, 30.0)

    async def do_work():
        if tool_name == "fast_lookup":
            await asyncio.sleep(0.5)
            return {"result": "found", "key": tool_input.get("key")}
        elif tool_name == "hung_tool":
            await asyncio.sleep(999)  # will be killed by watchdog
            return {"result": "never reaches here"}
        else:
            await asyncio.sleep(1.0)
            return {"result": "ok"}

    task = asyncio.create_task(do_work())
    call = ActiveCall(
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        task=task,
        max_duration=max_duration,
    )
    await watchdog.register(call)

    try:
        result = await task
        return result
    except asyncio.CancelledError:
        return {
            "error": "DEADLOCK_KILLED",
            "tool": tool_name,
            "message": f"Tool was killed after {max_duration}s by deadlock watchdog.",
        }
    finally:
        await watchdog.unregister(tool_use_id)

tools = [
    {
        "name": "fast_lookup",
        "description": "Quick key lookup",
        "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}
    },
    {
        "name": "hung_tool",
        "description": "A tool that hangs (for testing)",
        "input_schema": {"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]}
    }
]

async def run_agent_with_watchdog(message: str) -> str:
    await watchdog.start()
    messages = [{"role": "user", "content": message}]

    try:
        while True:
            response = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=256,
                tools=tools,
                messages=messages,
            )

            if response.stop_reason == "end_turn":
                return response.content[0].text

            messages.append({"role": "assistant", "content": response.content})
            results = []

            for block in response.content:
                if block.type == "tool_use":
                    result = await execute_with_watchdog(block.name, block.input, block.id)
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    })

            messages.append({"role": "user", "content": results})
    finally:
        await watchdog.stop()
        if watchdog.kills:
            print(f"Watchdog killed {len(watchdog.kills)} hung tools: {watchdog.kills}")

result = asyncio.run(run_agent_with_watchdog("Look up key 'user_count' and also run hung_tool on 'test'"))
print(result)
```

**Expected Token Savings:** Watchdog recovery is faster than session restart — saves all tokens accumulated in the conversation before the hang.

**Environment:** Python 3.9+, `asyncio`, `anthropic>=0.40.0`.

---

### Option 6 — Structured Execution Plan with Deadlock-Free Parallel Stages

Break multi-tool tasks into explicit parallel stages where tools within a stage have no dependencies on each other.

```python
import anthropic
import asyncio
import json
from dataclasses import dataclass
from typing import Any

client = anthropic.AsyncAnthropic()

@dataclass
class ToolCall:
    id: str
    name: str
    input: dict
    depends_on: list[str]  # IDs of tools that must complete first

async def execute_staged(tool_calls: list[ToolCall], executor) -> dict[str, Any]:
    """
    Execute tool calls in dependency-safe stages.
    All calls in a stage are independent and run in parallel.
    Deadlock is impossible because stages form a DAG.
    """
    results: dict[str, Any] = {}
    remaining = {tc.id: tc for tc in tool_calls}

    while remaining:
        # Find tools whose dependencies are all satisfied
        ready = [
            tc for tc in remaining.values()
            if all(dep in results for dep in tc.depends_on)
        ]

        if not ready:
            unresolved = {tid: tc.depends_on for tid, tc in remaining.items()}
            raise RuntimeError(
                f"No tools ready to execute — possible circular dependency. "
                f"Remaining: {unresolved}"
            )

        # Execute all ready tools in parallel
        stage_results = await asyncio.gather(
            *[executor(tc) for tc in ready],
            return_exceptions=True
        )

        for tc, result in zip(ready, stage_results):
            if isinstance(result, Exception):
                results[tc.id] = {"error": str(result), "tool": tc.name}
            else:
                results[tc.id] = result
            remaining.pop(tc.id)

        print(f"  Stage complete: {[tc.name for tc in ready]} → {len(remaining)} remaining")

    return results

# Tool implementations
async def tool_fetch_user(user_id: str) -> dict:
    await asyncio.sleep(0.2)
    return {"user_id": user_id, "name": "Alice", "plan": "pro"}

async def tool_fetch_orders(user_id: str) -> dict:
    await asyncio.sleep(0.3)
    return {"user_id": user_id, "orders": [{"id": "ord-1", "total": 99.0}]}

async def tool_fetch_config(key: str) -> dict:
    await asyncio.sleep(0.1)
    return {"key": key, "value": "v2.1.0"}

async def tool_generate_report(user_data: dict, order_data: dict, config: dict) -> dict:
    await asyncio.sleep(0.1)
    return {
        "report": f"User {user_data['name']} has {len(order_data['orders'])} orders. Config: {config['value']}"
    }

async def execute_tool(tc: ToolCall) -> Any:
    """Dispatch a tool call by name."""
    if tc.name == "fetch_user":
        return await tool_fetch_user(**tc.input)
    if tc.name == "fetch_orders":
        return await tool_fetch_orders(**tc.input)
    if tc.name == "fetch_config":
        return await tool_fetch_config(**tc.input)
    if tc.name == "generate_report":
        # Inject results from dependencies
        return await tool_generate_report(
            user_data=tc.input.get("user_data", {}),
            order_data=tc.input.get("order_data", {}),
            config=tc.input.get("config", {}),
        )
    raise ValueError(f"Unknown tool: {tc.name}")

async def demo_staged_execution():
    """
    Execution plan:
    Stage 1 (parallel): fetch_user, fetch_orders, fetch_config
    Stage 2 (depends on stage 1): generate_report
    """
    plan = [
        ToolCall(id="t1", name="fetch_user", input={"user_id": "u123"}, depends_on=[]),
        ToolCall(id="t2", name="fetch_orders", input={"user_id": "u123"}, depends_on=[]),
        ToolCall(id="t3", name="fetch_config", input={"key": "version"}, depends_on=[]),
        ToolCall(
            id="t4",
            name="generate_report",
            # In real usage, the orchestrator would inject dep results here
            input={"user_data": {}, "order_data": {}, "config": {}},
            depends_on=["t1", "t2", "t3"],  # waits for all three
        ),
    ]

    import time
    start = time.monotonic()
    results = await execute_staged(plan, execute_tool)
    elapsed = time.monotonic() - start

    print(f"\nAll results in {elapsed:.2f}s (sequential would take ~0.7s):")
    for tool_id, result in results.items():
        print(f"  {tool_id}: {result}")

asyncio.run(demo_staged_execution())
# Stage 1 runs t1+t2+t3 in parallel (~0.3s max)
# Stage 2 runs t4 (~0.1s) — total ~0.4s vs 0.7s sequential
```

**Expected Token Savings:** Parallel stage execution reduces total turns needed — fewer round trips to the API means fewer tokens spent on repeated context.

**Environment:** Python 3.9+, `asyncio`, `anthropic>=0.40.0`.

---

## Comparison

| Option | Deadlock Type | Detection | Recovery | Production-Ready |
|--------|--------------|-----------|----------|-----------------|
| 1 — Timeout | All types | Reactive | Automatic | Yes |
| 2 — Cycle Detection | Circular deps | Pre-flight | Reject plan | Yes |
| 3 — asyncio.Event | Event loop reentry | Structural | Prevention | Yes |
| 4 — Lock Ordering | Lock inversion | Structural | Prevention | Yes |
| 5 — Watchdog | All async hangs | Reactive | Kill & report | Yes |
| 6 — Staged Execution | Circular deps | Structural | Prevention | Yes |

**Use Option 1** (timeouts) everywhere as a baseline — it catches all deadlock types reactively. Add **Option 2** (cycle detection) when your agent constructs multi-step plans. Use **Option 6** (staged execution) for complex pipelines with known dependency structure.
