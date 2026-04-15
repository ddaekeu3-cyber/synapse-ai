---
layout: solution
title: "Agent Doesn't Handle Tool Timeouts"
category: tool-failure
description: "Agent hangs indefinitely waiting for a tool call that never returns — a slow database query, an unresponsive external API, or a stuck subprocess."
tags: [tool-failure, timeout, asyncio, reliability, async, tools]
---

## Symptom

The agent stops responding mid-conversation. The user sees a spinner that never resolves. In logs, the last event is a tool call that was dispatched but never returned. A slow external API or database query has stalled the entire agent loop. Sometimes the tool call eventually completes after 5 minutes, by which time the user has closed the tab and the HTTP connection has been dropped.

## Root Cause

Tool functions that make network calls, database queries, or shell executions are unbounded by default. Python's `requests.get()`, `subprocess.run()`, and raw socket operations will wait forever unless a timeout is set explicitly. In async code, `await some_tool()` will await indefinitely if `some_tool` never raises or returns. The agent has no mechanism to detect or recover from a stalled tool call.

## Fix

### Option 1 — `asyncio.wait_for` with per-tool timeout

```python
import asyncio
import anthropic
import json

client = anthropic.AsyncAnthropic()

# Simulated tools — some fast, some slow, some stalling
async def fast_tool(query: str) -> dict:
    await asyncio.sleep(0.2)
    return {"result": f"fast result for: {query}"}

async def slow_tool(query: str) -> dict:
    await asyncio.sleep(30)   # simulates a hanging external API
    return {"result": "this never arrives"}

TOOL_REGISTRY = {
    "fast_lookup": (fast_tool, 5.0),    # (coroutine_fn, timeout_seconds)
    "slow_api":    (slow_tool, 3.0),    # will timeout after 3s
}

TOOLS = [
    {
        "name": "fast_lookup",
        "description": "Fast in-memory lookup.",
        "input_schema": {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}},
    },
    {
        "name": "slow_api",
        "description": "External API call (may be slow).",
        "input_schema": {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}},
    },
]

async def execute_tool_with_timeout(name: str, args: dict) -> tuple[str, bool]:
    """Execute a tool with its configured timeout. Returns (result_json, is_error)."""
    fn, timeout = TOOL_REGISTRY[name]
    try:
        result = await asyncio.wait_for(fn(**args), timeout=timeout)
        return json.dumps(result), False
    except asyncio.TimeoutError:
        print(f"  [timeout] {name} exceeded {timeout}s limit")
        return json.dumps({"error": f"Tool '{name}' timed out after {timeout}s. Try a simpler query or retry later."}), True

async def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    for _ in range(6):
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for b in response.content:
            if b.type == "tool_use":
                result, is_error = await execute_tool_with_timeout(b.name, b.input)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": result, "is_error": is_error})
        messages.append({"role": "user", "content": results})
    return "max steps reached"

import time
t0 = time.perf_counter()
result = asyncio.run(run_agent("Look up 'hello' using the slow API."))
print(f"Result: {result[:200]} (in {time.perf_counter()-t0:.1f}s)")
```

**Expected Token Savings:** `asyncio.wait_for` caps tool execution at N seconds; without it a single stalled tool can block the entire agent loop indefinitely, preventing all users from getting responses.
**Environment:** All async tool-using agents; every tool call must have an explicit timeout.

---

### Option 2 — `httpx` async client with request-level timeouts

```python
import asyncio
import json
import httpx
import anthropic

client = anthropic.AsyncAnthropic()

# Configured httpx client with per-phase timeouts
HTTP_CLIENT = httpx.AsyncClient(
    timeout=httpx.Timeout(
        connect=5.0,   # time to establish TCP connection
        read=10.0,     # time to read response body
        write=5.0,     # time to write request body
        pool=2.0,      # time to acquire connection from pool
    )
)

async def fetch_url(url: str) -> dict:
    try:
        response = await HTTP_CLIENT.get(url)
        response.raise_for_status()
        return {"status": response.status_code, "body": response.text[:500]}
    except httpx.ConnectTimeout:
        return {"error": "Connection timed out after 5s — server may be down."}
    except httpx.ReadTimeout:
        return {"error": "Response timed out after 10s — server is responding slowly."}
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}: {e.response.text[:100]}"}
    except httpx.RequestError as e:
        return {"error": f"Request failed: {e}"}

TOOLS = [
    {
        "name": "fetch_url",
        "description": "Fetch content from a URL.",
        "input_schema": {
            "type": "object",
            "required": ["url"],
            "properties": {"url": {"type": "string", "description": "URL to fetch"}},
        },
    }
]

async def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    for _ in range(6):
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for b in response.content:
            if b.type == "tool_use":
                if b.name == "fetch_url":
                    result = await fetch_url(b.input["url"])
                    is_err = "error" in result
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": b.id,
                        "content": json.dumps(result),
                        "is_error": is_err,
                    })
        messages.append({"role": "user", "content": results})
    return "max steps reached"

result = asyncio.run(run_agent("Fetch the content of https://httpbin.org/delay/1"))
print(f"Result: {result[:200]}")
asyncio.run(HTTP_CLIENT.aclose())
```

**Expected Token Savings:** Per-phase httpx timeouts prevent indefinite hangs on slow HTTP tools; granular timeouts (connect vs read) provide precise error messages that guide retry strategy.
**Environment:** All agents that make outbound HTTP calls in tools; replace `requests` with `httpx.AsyncClient` in all async tool implementations.

---

### Option 3 — Timeout wrapper decorator for tool functions

```python
import asyncio
import functools
import json
import time
import anthropic

client = anthropic.AsyncAnthropic()

def with_timeout(seconds: float, fallback_message: str | None = None):
    """Decorator that wraps an async tool function with a timeout."""
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await asyncio.wait_for(fn(*args, **kwargs), timeout=seconds)
            except asyncio.TimeoutError:
                msg = fallback_message or f"{fn.__name__} timed out after {seconds}s"
                print(f"  [timeout] {msg}")
                return {"error": msg, "timed_out": True}
        return wrapper
    return decorator

@with_timeout(seconds=2.0, fallback_message="Database query timed out — try a narrower search")
async def query_database(table: str, filter: str) -> dict:
    # Simulate slow DB query
    await asyncio.sleep(5)
    return {"rows": [{"id": 1, "data": "example"}]}

@with_timeout(seconds=3.0)
async def call_external_api(endpoint: str) -> dict:
    await asyncio.sleep(0.5)   # fast tool — succeeds
    return {"endpoint": endpoint, "result": "ok"}

@with_timeout(seconds=1.0)
async def run_code_snippet(code: str) -> dict:
    await asyncio.sleep(0.3)   # fast — succeeds
    return {"output": "execution result", "exit_code": 0}

TOOLS = [
    {
        "name": "query_database",
        "description": "Query a database table.",
        "input_schema": {
            "type": "object", "required": ["table", "filter"],
            "properties": {"table": {"type": "string"}, "filter": {"type": "string"}},
        },
    },
    {
        "name": "call_external_api",
        "description": "Call an external API endpoint.",
        "input_schema": {
            "type": "object", "required": ["endpoint"],
            "properties": {"endpoint": {"type": "string"}},
        },
    },
]

TOOL_FNS = {
    "query_database":  query_database,
    "call_external_api": call_external_api,
}

async def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    for _ in range(6):
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for b in response.content:
            if b.type == "tool_use":
                fn = TOOL_FNS.get(b.name)
                result = await fn(**b.input) if fn else {"error": f"unknown tool: {b.name}"}
                is_err = "error" in result or result.get("timed_out", False)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(result), "is_error": is_err})
        messages.append({"role": "user", "content": results})
    return "max steps reached"

t0 = time.perf_counter()
r = asyncio.run(run_agent("Query the users table with filter 'active=true'"))
print(f"Result: {r[:200]} ({time.perf_counter()-t0:.1f}s)")
```

**Expected Token Savings:** Decorator-based timeouts are applied at definition time and cannot be forgotten; consistent timeout enforcement across all tool functions without boilerplate.
**Environment:** Projects with many tool functions; decorator pattern ensures timeouts are never omitted when adding new tools.

---

### Option 4 — Global agent loop timeout with `asyncio.wait_for`

```python
import asyncio
import json
import anthropic

client = anthropic.AsyncAnthropic()

async def slow_tool_fn(query: str) -> dict:
    await asyncio.sleep(60)
    return {"result": "never"}

TOOLS = [
    {
        "name": "slow_search",
        "description": "Search that may hang.",
        "input_schema": {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}},
    }
]

async def _agent_loop(user_message: str) -> str:
    """Inner agent loop — no timeout enforcement here."""
    messages = [{"role": "user", "content": user_message}]
    for step in range(8):
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for b in response.content:
            if b.type == "tool_use":
                try:
                    result = await asyncio.wait_for(slow_tool_fn(**b.input), timeout=5.0)
                    payload = json.dumps(result)
                    is_error = False
                except asyncio.TimeoutError:
                    payload = json.dumps({"error": "tool timed out"})
                    is_error = True
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": payload, "is_error": is_error})
        messages.append({"role": "user", "content": results})
    return "max steps reached"

async def run_agent(user_message: str, total_timeout: float = 30.0) -> str:
    """
    Wrap the entire agent loop with a hard wall-clock timeout.
    Guarantees the agent returns within total_timeout seconds regardless of what tools do.
    """
    try:
        return await asyncio.wait_for(_agent_loop(user_message), timeout=total_timeout)
    except asyncio.TimeoutError:
        print(f"  [timeout] agent loop exceeded {total_timeout}s hard limit")
        return "I'm sorry, your request took too long to process. Please try a simpler query."

import time
t0 = time.perf_counter()
result = asyncio.run(run_agent("Search for recent AI papers.", total_timeout=8.0))
print(f"Result: {result} ({time.perf_counter()-t0:.1f}s)")
```

**Expected Token Savings:** Hard wall-clock timeout on the agent loop guarantees SLA compliance; without it, a single stalled tool can block a request indefinitely, consuming connection pool resources and preventing other requests from being served.
**Environment:** HTTP-serving agents where request handlers must return within a defined SLA (e.g., 30 seconds); combine with per-tool timeouts for defence in depth.

---

### Option 5 — Retry with shorter timeout on fallback tool

```python
import asyncio
import json
import anthropic

client = anthropic.AsyncAnthropic()

# Primary tool (may be slow) and fallback tool (always fast)
async def primary_search(query: str) -> dict:
    await asyncio.sleep(10)   # simulates slow primary index
    return {"source": "primary", "results": [f"primary result for {query}"]}

async def fallback_search(query: str) -> dict:
    await asyncio.sleep(0.2)   # fast fallback
    return {"source": "fallback", "results": [f"cached result for {query}"], "note": "Using fast cache — results may be less fresh."}

async def search_with_fallback(query: str, primary_timeout: float = 3.0) -> dict:
    """Try primary; fall back to fast cache if primary times out."""
    try:
        result = await asyncio.wait_for(primary_search(query), timeout=primary_timeout)
        return result
    except asyncio.TimeoutError:
        print(f"  [fallback] primary timed out after {primary_timeout}s — using fallback")
        return await fallback_search(query)

TOOLS = [
    {
        "name": "search",
        "description": "Search the knowledge base.",
        "input_schema": {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}},
    }
]

async def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    for _ in range(6):
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for b in response.content:
            if b.type == "tool_use" and b.name == "search":
                result = await search_with_fallback(b.input["query"])
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(result)})
        messages.append({"role": "user", "content": results})
    return "max steps reached"

import time
t0 = time.perf_counter()
r = asyncio.run(run_agent("Search for information about transformers."))
print(f"Result: {r[:200]} ({time.perf_counter()-t0:.1f}s)")
```

**Expected Token Savings:** Graceful fallback means the agent always returns a useful (if stale) result instead of timing out entirely; eliminates user-facing errors while communicating freshness caveats.
**Environment:** Agents with tiered data sources (real-time vs cached); fallback pattern is essential for high-availability search or lookup tools.

---

### Option 6 — Background tool execution with status polling

```python
import asyncio
import uuid
import json
import time
import anthropic

client = anthropic.AsyncAnthropic()

# Background job registry
_jobs: dict[str, dict] = {}

async def _run_long_job(job_id: str, query: str) -> None:
    """Long-running tool that executes in the background."""
    try:
        await asyncio.sleep(4)   # simulate long computation
        _jobs[job_id] = {"status": "done", "result": f"Computed result for: {query}"}
    except asyncio.CancelledError:
        _jobs[job_id] = {"status": "cancelled"}

async def start_job(query: str) -> dict:
    """Start a background job and return a job ID immediately."""
    job_id = str(uuid.uuid4())[:8]
    _jobs[job_id] = {"status": "running"}
    asyncio.create_task(_run_long_job(job_id, query))
    print(f"  [job] started {job_id!r}")
    return {"job_id": job_id, "status": "running", "message": "Job started. Poll with check_job."}

async def check_job(job_id: str) -> dict:
    """Poll job status — returns immediately."""
    return _jobs.get(job_id, {"status": "not_found", "error": f"Job {job_id!r} not found"})

TOOLS = [
    {
        "name": "start_job",
        "description": "Start a long-running computation. Returns a job_id to poll.",
        "input_schema": {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}},
    },
    {
        "name": "check_job",
        "description": "Check status of a running job by job_id.",
        "input_schema": {"type": "object", "required": ["job_id"], "properties": {"job_id": {"type": "string"}}},
    },
]

async def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    for step in range(10):
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for b in response.content:
            if b.type == "tool_use":
                if b.name == "start_job":
                    result = await start_job(**b.input)
                elif b.name == "check_job":
                    # If job still running, wait briefly before polling
                    status = await check_job(**b.input)
                    if status.get("status") == "running":
                        print(f"  [poll] job still running, waiting 1.5s")
                        await asyncio.sleep(1.5)
                        status = await check_job(**b.input)
                    result = status
                else:
                    result = {"error": f"unknown tool: {b.name}"}
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(result)})
        messages.append({"role": "user", "content": results})
    return "max steps reached"

t0 = time.perf_counter()
r = asyncio.run(run_agent("Run a deep analysis on neural network architectures."))
print(f"Result: {r[:200]} ({time.perf_counter()-t0:.1f}s)")
```

**Expected Token Savings:** Background job pattern decouples execution time from agent loop latency; the agent remains responsive while long tools run asynchronously; no tool timeout is needed because each poll returns immediately.
**Environment:** Agents with tools that genuinely require minutes of computation (code execution, large data analysis, rendering); polling pattern is the only viable approach for tools with multi-minute execution times.

---

## Comparison

| Option | Timeout Scope | Handles Stalled HTTP | Graceful Fallback | Best For |
|---|---|---|---|---|
| 1. `asyncio.wait_for` per call | Per tool call | Yes | No | Simple per-tool timeouts |
| 2. `httpx` timeout config | Per HTTP phase | Yes | No | HTTP-based tools |
| 3. Decorator `@with_timeout` | Per tool function | Yes | No | Many tool functions, DRY enforcement |
| 4. Agent loop hard timeout | Entire agent loop | Yes | No | SLA-bound HTTP request handlers |
| 5. Fallback on timeout | Per tool call | Yes | Yes | High-availability tools with cache fallback |
| 6. Background job + polling | Async — no timeout needed | N/A | N/A | Multi-minute tools (computation, rendering) |
