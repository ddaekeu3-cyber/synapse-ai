---
layout: solution
title: "Agent Doesn't Implement Timeout Per Tool Call"
category: general
description: "Agent calls external tools — APIs, shell commands, database queries — without per-call timeouts. A single hanging tool blocks the entire pipeline indefinitely, burning session time and preventing graceful failure."
tags: [reliability, timeouts, tool-use, resilience, hanging]
---

## Symptom

An agent calls a database query tool that encounters a lock contention issue. The query never returns. The agent waits:

```
[10:00:00] Tool call: query_database(sql="SELECT ...")
[10:00:00] Waiting for result...
[10:05:00] Waiting for result...
[10:10:00] Waiting for result...
[10:30:00] Waiting for result...  ← session still blocked
[10:45:00] User gives up, cancels session
```

No error was ever raised. The agent had no way to detect the hang or try a fallback.

## Root Cause

Tool execution is awaited without a deadline. The agent relies on the tool's own timeout (if any), which may be absent or set to `None`:

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# Anti-pattern: no timeout on tool execution
def execute_tool(name: str, input_data: dict) -> str:
    if name == "query_database":
        return run_query(input_data["sql"])  # ← May block forever
```

---

## Fix

### Option 1 — asyncio.wait_for with per-tool timeout

Wrap every tool execution in `asyncio.wait_for`. Each tool type gets a configurable deadline.

```python
import anthropic
import asyncio
import json

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

# Per-tool timeouts (seconds)
TOOL_TIMEOUTS = {
    "query_database": 10.0,
    "call_api": 15.0,
    "run_shell": 30.0,
    "read_file": 5.0,
    "default": 20.0,
}


async def execute_tool_async(name: str, input_data: dict) -> str:
    """Simulate async tool execution."""
    await asyncio.sleep(input_data.get("_sim_delay", 0.1))
    return json.dumps({"tool": name, "result": "ok", "input": input_data})


async def execute_with_timeout(name: str, input_data: dict) -> str:
    """Execute a tool with a per-tool-type timeout."""
    timeout = TOOL_TIMEOUTS.get(name, TOOL_TIMEOUTS["default"])
    try:
        result = await asyncio.wait_for(
            execute_tool_async(name, input_data),
            timeout=timeout
        )
        return result
    except asyncio.TimeoutError:
        return json.dumps({
            "error": f"Tool '{name}' timed out after {timeout}s",
            "tool": name,
            "timeout_seconds": timeout
        })


tools = [
    {
        "name": "query_database",
        "description": "Run a SQL query. Times out after 10s.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string"},
                "_sim_delay": {"type": "number", "description": "Simulated delay for testing"}
            },
            "required": ["sql"]
        }
    }
]


async def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return next(b.text for b in response.content if b.type == "text")

        tool_results = []
        for tu in tool_uses:
            result = await execute_with_timeout(tu.name, tu.input)
            parsed = json.loads(result)
            if "error" in parsed:
                print(f"[timeout] {parsed['error']}")
            tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": result})

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})


result = asyncio.run(run_agent("Query the database for user count"))
print(result)

# Expected Token Savings: timeout prevents indefinite hang → session ends cleanly, not via user abort
# Environment: any async agent calling external tools (DB, API, shell, filesystem)
```

---

### Option 2 — Threading-based timeout for synchronous tools

For tools that are synchronous and can't be made async, use a thread with a join timeout to enforce a deadline.

```python
import anthropic
import threading
import json
import time

client = anthropic.Anthropic(api_key="sk-live-...")

TOOL_TIMEOUTS = {
    "query_database": 10.0,
    "call_external_api": 15.0,
    "default": 20.0,
}


def execute_tool_sync(name: str, input_data: dict) -> str:
    """Synchronous tool execution (simulated)."""
    time.sleep(input_data.get("_sim_delay", 0.05))
    return json.dumps({"tool": name, "status": "success"})


def execute_with_thread_timeout(name: str, input_data: dict) -> str:
    """Execute a synchronous tool in a thread with timeout enforcement."""
    timeout = TOOL_TIMEOUTS.get(name, TOOL_TIMEOUTS["default"])
    result_container: list[str] = []
    exception_container: list[Exception] = []

    def target():
        try:
            result_container.append(execute_tool_sync(name, input_data))
        except Exception as e:
            exception_container.append(e)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=timeout)

    if thread.is_alive():
        # Thread is still running — timeout exceeded
        # Note: Python cannot forcibly kill threads; daemon=True ensures process-level cleanup
        return json.dumps({
            "error": f"Tool '{name}' exceeded {timeout}s timeout — still running in background",
            "timed_out": True
        })

    if exception_container:
        return json.dumps({"error": str(exception_container[0])})

    return result_container[0] if result_container else json.dumps({"error": "No result"})


def run_agent_sync(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    tools = [
        {
            "name": "query_database",
            "description": "Run a database query",
            "input_schema": {
                "type": "object",
                "properties": {"sql": {"type": "string"}},
                "required": ["sql"]
            }
        }
    ]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return next(b.text for b in response.content if b.type == "text")

        tool_results = []
        for tu in tool_uses:
            result = execute_with_thread_timeout(tu.name, tu.input)
            tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": result})

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})


print(run_agent_sync("Count active users in the database"))

# Expected Token Savings: thread-based timeout works for synchronous tool libraries (psycopg2, boto3, etc.)
# Environment: sync agent frameworks; tools using blocking I/O libraries
```

---

### Option 3 — Timeout with automatic fallback strategy

When a tool times out, provide a fallback result (cached, estimated, or degraded) so the agent can continue rather than failing.

```python
import anthropic
import asyncio
import json
import time
from functools import lru_cache

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

# Fallback cache for degraded operation
_fallback_cache: dict[str, tuple[str, float]] = {}
FALLBACK_TTL = 300.0  # Use stale fallback for up to 5 minutes


async def query_database_live(sql: str) -> str:
    """Live database query — may be slow."""
    await asyncio.sleep(0.2)  # Simulated OK response
    return json.dumps({"rows": [{"count": 1234}], "source": "live"})


async def execute_with_fallback(name: str, input_data: dict, timeout: float = 8.0) -> str:
    """Execute tool; on timeout, return stale cached result if available."""
    cache_key = f"{name}:{json.dumps(input_data, sort_keys=True)}"

    try:
        result = await asyncio.wait_for(
            query_database_live(input_data.get("sql", "")),
            timeout=timeout
        )
        # Update fallback cache on success
        _fallback_cache[cache_key] = (result, time.monotonic())
        return result

    except asyncio.TimeoutError:
        print(f"[timeout] '{name}' timed out after {timeout}s — checking fallback cache")

        # Try stale cache
        if cache_key in _fallback_cache:
            cached_result, cached_at = _fallback_cache[cache_key]
            age = time.monotonic() - cached_at
            if age < FALLBACK_TTL:
                stale = json.loads(cached_result)
                stale["_stale"] = True
                stale["_cache_age_seconds"] = round(age)
                print(f"[fallback] Returning {age:.0f}s stale cached result")
                return json.dumps(stale)

        # No usable cache — return degraded response
        return json.dumps({
            "error": f"Tool '{name}' timed out and no fallback available",
            "degraded": True,
            "recommendation": "Retry later or use an alternative data source"
        })


async def run_agent_with_fallback(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    system = "If a tool returns a stale or degraded result, acknowledge it in your response."
    tools = [
        {
            "name": "query_database",
            "description": "Query the database. May return stale data if the database is slow.",
            "input_schema": {
                "type": "object",
                "properties": {"sql": {"type": "string"}},
                "required": ["sql"]
            }
        }
    ]

    while True:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=system,
            tools=tools,
            messages=messages
        )

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return next(b.text for b in response.content if b.type == "text")

        tool_results = []
        for tu in tool_uses:
            result = await execute_with_fallback(tu.name, tu.input)
            tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": result})

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})


result = asyncio.run(run_agent_with_fallback("Get the current user count"))
print(result)

# Expected Token Savings: fallback keeps session alive → no user re-prompt; stale data > no data
# Environment: production agents where partial data is better than complete failure
```

---

### Option 4 — Global session deadline with per-tool budget allocation

Set a global deadline for the entire agent session and allocate per-tool time budgets proportionally.

```python
import anthropic
import asyncio
import json
import time

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

SESSION_DEADLINE_SECS = 60.0  # Total session must complete within 60s
TOOL_TIME_SHARE = 0.7         # Tools get 70% of remaining time


class DeadlineTracker:
    def __init__(self, total_seconds: float):
        self._deadline = time.monotonic() + total_seconds
        self._tool_calls = 0

    def remaining(self) -> float:
        return max(0.0, self._deadline - time.monotonic())

    def budget_for_next_tool(self) -> float:
        """Give next tool a share of remaining time, reserving some for the model."""
        remaining = self.remaining()
        return remaining * TOOL_TIME_SHARE

    def is_expired(self) -> bool:
        return time.monotonic() >= self._deadline


async def timed_tool_call(name: str, input_data: dict, tracker: DeadlineTracker) -> str:
    if tracker.is_expired():
        return json.dumps({"error": "Session deadline exceeded — tool call skipped", "skipped": True})

    budget = tracker.budget_for_next_tool()
    tracker._tool_calls += 1
    print(f"[deadline] tool '{name}' budget: {budget:.1f}s, session remaining: {tracker.remaining():.1f}s")

    try:
        await asyncio.wait_for(asyncio.sleep(0.1), timeout=budget)  # Simulated tool
        return json.dumps({"tool": name, "result": "ok", "budget_used": 0.1})
    except asyncio.TimeoutError:
        return json.dumps({
            "error": f"'{name}' exceeded {budget:.1f}s budget (session deadline enforcement)",
            "budget_seconds": budget,
            "timed_out": True
        })


async def run_with_deadline(user_message: str) -> str:
    tracker = DeadlineTracker(SESSION_DEADLINE_SECS)
    messages = [{"role": "user", "content": user_message}]
    tools = [
        {
            "name": "fetch_data",
            "description": "Fetch data from an external source",
            "input_schema": {
                "type": "object",
                "properties": {"source": {"type": "string"}},
                "required": ["source"]
            }
        }
    ]
    system = "You have a limited time budget. If tools report timeouts, summarise what you have so far."

    while not tracker.is_expired():
        # Reserve time for model call
        if tracker.remaining() < 5.0:
            break

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=system,
            tools=tools,
            messages=messages
        )

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return next(b.text for b in response.content if b.type == "text")

        tool_results = []
        for tu in tool_uses:
            result = await timed_tool_call(tu.name, tu.input, tracker)
            tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": result})

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return f"Session completed: {tracker._tool_calls} tool calls in {SESSION_DEADLINE_SECS - tracker.remaining():.1f}s"


result = asyncio.run(run_with_deadline("Fetch data from source A, B, and C"))
print(result)

# Expected Token Savings: hard session deadline prevents runaway sessions; bounded cost per request
# Environment: user-facing agents with SLA requirements; serverless with function timeouts
```

---

### Option 5 — Timeout with exponential backoff retry

On timeout, retry the tool call up to N times with exponential backoff, then fail gracefully.

```python
import anthropic
import asyncio
import json
import random

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

TOOL_CONFIG = {
    "query_database": {"timeout": 8.0, "max_retries": 2, "base_backoff": 1.0},
    "call_webhook": {"timeout": 5.0, "max_retries": 3, "base_backoff": 0.5},
    "default": {"timeout": 15.0, "max_retries": 1, "base_backoff": 2.0},
}


async def simulate_flaky_tool(name: str, input_data: dict) -> str:
    """Simulated flaky tool — sometimes slow, sometimes fast."""
    if random.random() < 0.4:  # 40% chance of being slow
        await asyncio.sleep(20)  # Triggers timeout
    await asyncio.sleep(0.2)
    return json.dumps({"tool": name, "result": "success"})


async def execute_with_retry(name: str, input_data: dict) -> str:
    """Execute tool with per-attempt timeout and exponential backoff retry."""
    config = TOOL_CONFIG.get(name, TOOL_CONFIG["default"])
    timeout = config["timeout"]
    max_retries = config["max_retries"]
    base_backoff = config["base_backoff"]

    last_error = ""
    for attempt in range(max_retries + 1):
        if attempt > 0:
            wait = base_backoff * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            print(f"[retry] '{name}' attempt {attempt + 1}/{max_retries + 1} after {wait:.1f}s backoff")
            await asyncio.sleep(wait)

        try:
            result = await asyncio.wait_for(
                simulate_flaky_tool(name, input_data),
                timeout=timeout
            )
            if attempt > 0:
                print(f"[retry] '{name}' succeeded on attempt {attempt + 1}")
            return result

        except asyncio.TimeoutError:
            last_error = f"timed out after {timeout}s"
            print(f"[timeout] '{name}' attempt {attempt + 1}: {last_error}")

    return json.dumps({
        "error": f"'{name}' failed after {max_retries + 1} attempts: {last_error}",
        "exhausted": True,
        "attempts": max_retries + 1
    })


async def run_agent_with_retry(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    tools = [
        {
            "name": "query_database",
            "description": "Query the database",
            "input_schema": {
                "type": "object",
                "properties": {"sql": {"type": "string"}},
                "required": ["sql"]
            }
        }
    ]

    while True:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=tools,
            messages=messages
        )

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return next(b.text for b in response.content if b.type == "text")

        tool_results = []
        for tu in tool_uses:
            result = await execute_with_retry(tu.name, tu.input)
            tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": result})

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})


result = asyncio.run(run_agent_with_retry("Get total revenue from the database"))
print(result)

# Expected Token Savings: transient timeouts auto-heal; persistent failures surface quickly
# Environment: agents calling flaky external APIs or occasionally congested databases
```

---

### Option 6 — Tool timeout middleware: decorator-based enforcement

Wrap tool executor functions with a timeout decorator. New tools automatically get timeout protection without modifying their implementation.

```python
import anthropic
import asyncio
import json
import functools
from typing import Callable

client = anthropic.AsyncAnthropic(api_key="sk-live-...")


def with_timeout(seconds: float, tool_name: str | None = None):
    """Decorator that enforces a timeout on any async tool function."""
    def decorator(fn: Callable) -> Callable:
        name = tool_name or fn.__name__

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs) -> str:
            try:
                return await asyncio.wait_for(fn(*args, **kwargs), timeout=seconds)
            except asyncio.TimeoutError:
                return json.dumps({
                    "error": f"Tool '{name}' timed out after {seconds}s",
                    "tool": name,
                    "timed_out": True,
                    "suggestion": "Narrow the query, reduce scope, or retry later"
                })
            except Exception as e:
                return json.dumps({"error": f"Tool '{name}' raised: {type(e).__name__}: {e}"})
        return wrapper
    return decorator


# Tool implementations — each protected by its own timeout
@with_timeout(8.0, "query_database")
async def query_database(sql: str) -> str:
    await asyncio.sleep(0.15)  # Simulated fast query
    return json.dumps({"rows": [{"total": 42000}]})


@with_timeout(5.0, "call_webhook")
async def call_webhook(url: str, payload: dict) -> str:
    await asyncio.sleep(0.1)
    return json.dumps({"status": 200, "body": "ok"})


@with_timeout(30.0, "run_report")
async def run_report(report_type: str) -> str:
    await asyncio.sleep(25)  # Simulated long-running report (within timeout)
    return json.dumps({"report": report_type, "generated": True})


TOOL_REGISTRY: dict[str, Callable] = {
    "query_database": query_database,
    "call_webhook": call_webhook,
    "run_report": run_report,
}

tools_spec = [
    {
        "name": "query_database",
        "description": "SQL query (8s timeout)",
        "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}
    },
    {
        "name": "run_report",
        "description": "Generate a report (30s timeout)",
        "input_schema": {"type": "object", "properties": {"report_type": {"type": "string"}}, "required": ["report_type"]}
    }
]


async def run_middleware_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=tools_spec,
            messages=messages
        )

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return next(b.text for b in response.content if b.type == "text")

        tool_results = []
        for tu in tool_uses:
            fn = TOOL_REGISTRY.get(tu.name)
            if fn:
                result = await fn(**tu.input)
            else:
                result = json.dumps({"error": f"Unknown tool: {tu.name}"})
            tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": result})

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})


result = asyncio.run(run_middleware_agent("Query the database for revenue"))
print(result)

# Expected Token Savings: decorator pattern means all new tools are protected by default
# Environment: growing tool libraries; agent frameworks where tools are added incrementally
```

---

## Comparison

| Option | Timeout Mechanism | Fallback | Retry | Complexity |
|--------|------------------|---------|-------|------------|
| 1 | asyncio.wait_for per tool | No | No | Low |
| 2 | Thread join timeout | No | No | Low |
| 3 | wait_for + stale cache fallback | Yes | No | Medium |
| 4 | Global session deadline | No | No | Medium |
| 5 | wait_for + exponential backoff | No | Yes | Medium |
| 6 | Decorator middleware | No | No | Low |

**Recommended starting point:** Option 1 (asyncio.wait_for) for async agents — wrap the tool dispatch loop with a per-tool timeout dict and a single `try/except asyncio.TimeoutError`. Add Option 3's stale cache fallback for tools whose data can tolerate some staleness, and Option 5's retry for flaky external APIs.
