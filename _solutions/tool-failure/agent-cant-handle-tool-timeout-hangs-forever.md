---
layout: solution
title: "Agent Hangs Forever When Tool Call Times Out — No Timeout Handling"
category: tool-failure
description: "Agent calls an external tool or API with no timeout. The remote server is slow or unresponsive. Agent blocks indefinitely. No error is returned. The entire agent loop freezes waiting for a response that never arrives."
tags: [tool-failure, timeout, hang, asyncio, httpx, deadlock, circuit-breaker, watchdog]
---

# Agent Hangs Forever When Tool Call Times Out — No Timeout Handling

## Symptom
- Agent stops responding mid-task — no output, no error
- Tool call to an external API hangs for 10+ minutes with no timeout
- `asyncio.gather()` never completes because one coroutine is blocked
- Agent process stays alive but does nothing — CPU at 0%, no log output
- Thread pool exhausted because all threads are waiting on stuck I/O
- Kubernetes liveness probe eventually kills the pod after 5 minutes of silence

## Root Cause
HTTP clients, database drivers, and subprocess calls have no timeout by default — or very long ones (300s+). When the remote service is slow, overloaded, or drops the connection silently, the call blocks indefinitely. `requests.get(url)` with no `timeout` parameter will wait forever. `asyncio` coroutines without `asyncio.wait_for()` wrappers block the event loop. Without explicit timeouts at every I/O boundary, a single slow tool can freeze the entire agent.

## Fix

### Option 1: Always set timeouts on HTTP clients

```python
import httpx
import asyncio

# WRONG — hangs forever if server is slow
# response = requests.get("https://api.example.com/data")

# RIGHT — explicit connect + read timeout
async def call_api_with_timeout(url: str, payload: dict) -> dict:
    """
    All HTTP calls must have explicit timeouts.
    connect: time to establish TCP connection
    read: time to receive response body
    write: time to send request body
    pool: time to acquire connection from pool
    """
    timeout = httpx.Timeout(
        connect=5.0,    # 5s to connect
        read=30.0,      # 30s to read response
        write=10.0,     # 10s to write request
        pool=5.0        # 5s to get connection from pool
    )

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            return response.json()
        except httpx.TimeoutException as e:
            raise TimeoutError(f"API call to {url} timed out: {e}") from e
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"API error {e.response.status_code}: {e.response.text}") from e

# Global default timeout for all calls in the agent:
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0)

async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
    # All requests through this client use the default timeout
    response = await client.get("https://slow-api.example.com/data")
```

### Option 2: asyncio.wait_for — timeout any coroutine

```python
import asyncio
from typing import TypeVar, Callable, Awaitable

T = TypeVar("T")

async def with_timeout(
    coro: Awaitable[T],
    timeout_seconds: float,
    operation_name: str = "operation"
) -> T:
    """
    Wrap any coroutine with a timeout.
    Raises TimeoutError with a clear message if it exceeds the limit.
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        raise TimeoutError(
            f"{operation_name} timed out after {timeout_seconds}s. "
            f"The remote service may be unavailable."
        )

# Usage — apply to any tool call:
async def run_agent_tool(tool_name: str, params: dict) -> dict:
    match tool_name:
        case "search_web":
            result = await with_timeout(
                search_web(params["query"]),
                timeout_seconds=15.0,
                operation_name="web search"
            )
        case "run_code":
            result = await with_timeout(
                execute_code(params["code"]),
                timeout_seconds=30.0,
                operation_name="code execution"
            )
        case "fetch_document":
            result = await with_timeout(
                fetch_document(params["url"]),
                timeout_seconds=20.0,
                operation_name="document fetch"
            )
        case _:
            raise ValueError(f"Unknown tool: {tool_name}")
    return result

# For multiple tools in parallel — all with individual timeouts:
async def run_tools_parallel(tool_calls: list[dict]) -> list[dict]:
    tasks = [
        with_timeout(
            run_single_tool(tc),
            timeout_seconds=30.0,
            operation_name=tc["name"]
        )
        for tc in tool_calls
    ]
    # gather with return_exceptions=True — one timeout doesn't cancel others
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [
        {"error": str(r)} if isinstance(r, Exception) else r
        for r in results
    ]
```

### Option 3: Timeout decorator for tool functions

```python
import asyncio
import functools
import time
from typing import Callable

def timeout(seconds: float, error_message: str = None):
    """
    Decorator that adds a timeout to any async function.
    Use on all tool implementation functions.
    """
    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            msg = error_message or f"{func.__name__} timed out after {seconds}s"
            try:
                return await asyncio.wait_for(func(*args, **kwargs), timeout=seconds)
            except asyncio.TimeoutError:
                raise TimeoutError(msg)
        return wrapper
    return decorator

# Apply to every tool:
@timeout(15.0, "Web search took too long — search engine may be slow")
async def search_web(query: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get("https://search-api.example.com/search", params={"q": query})
        return response.json()["results"]

@timeout(30.0, "Code execution timed out — infinite loop or resource limit reached")
async def execute_code(code: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "python3", "-c", code,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    return stdout.decode() or stderr.decode()

@timeout(10.0, "Database query timed out — query may need optimization")
async def query_database(sql: str, params: tuple) -> list[dict]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
        return [dict(row) for row in rows]
```

### Option 4: Watchdog — detect and recover from agent hangs

```python
import asyncio
import time
import signal
import threading

class AgentWatchdog:
    """
    External watchdog that monitors agent activity.
    If no progress is made within the timeout window, raises an alert or restarts.
    """

    def __init__(self, timeout_seconds: float = 120.0):
        self.timeout = timeout_seconds
        self._last_activity = time.monotonic()
        self._running = False
        self._thread: threading.Thread | None = None

    def heartbeat(self, activity: str = ""):
        """Call this regularly to signal the agent is making progress"""
        self._last_activity = time.monotonic()
        if activity:
            print(f"[watchdog] Activity: {activity}")

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()
        print(f"[watchdog] Started — will alert if no activity for {self.timeout}s")

    def stop(self):
        self._running = False

    def _watch(self):
        while self._running:
            time.sleep(10)
            elapsed = time.monotonic() - self._last_activity
            if elapsed > self.timeout:
                print(
                    f"[watchdog] ALERT: No agent activity for {elapsed:.0f}s "
                    f"(timeout: {self.timeout}s). Agent may be hung."
                )
                self._on_hang_detected(elapsed)

    def _on_hang_detected(self, elapsed: float):
        """Override to customize hang response"""
        # Option 1: Log and alert
        import logging
        logging.critical(f"Agent hang detected after {elapsed:.0f}s of inactivity")
        # Option 2: Send SIGTERM to trigger graceful restart
        # signal.raise_signal(signal.SIGTERM)
        # Option 3: Raise in main thread via thread-safe flag
        self._hang_detected = True

watchdog = AgentWatchdog(timeout_seconds=120.0)

async def agent_loop(messages: list[dict]) -> str:
    watchdog.start()
    try:
        while True:
            watchdog.heartbeat("calling model")
            response = await call_model(messages)

            if not response.tool_calls:
                return response.content

            for tool_call in response.tool_calls:
                watchdog.heartbeat(f"running tool: {tool_call.name}")
                result = await run_agent_tool(tool_call.name, tool_call.params)
                messages.append({"role": "tool", "content": str(result)})
    finally:
        watchdog.stop()
```

### Option 5: Circuit breaker — stop calling timed-out services

```python
import time
from enum import Enum
from dataclasses import dataclass, field

class CircuitState(Enum):
    CLOSED = "closed"       # Normal — calls go through
    OPEN = "open"           # Tripped — calls fail fast
    HALF_OPEN = "half_open" # Testing — allow one call through

@dataclass
class CircuitBreaker:
    """
    Circuit breaker for external tool calls.
    After N timeouts, stops calling the service for a cooldown period.
    """
    name: str
    failure_threshold: int = 3       # Trips after 3 failures
    recovery_timeout: float = 60.0   # Retry after 60 seconds
    call_timeout: float = 30.0       # Timeout per call

    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failure_count: int = field(default=0, init=False)
    _last_failure_time: float = field(default=0.0, init=False)

    async def call(self, coro) -> any:
        if self._state == CircuitState.OPEN:
            if time.monotonic() - self._last_failure_time > self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                print(f"[circuit:{self.name}] Half-open — testing recovery")
            else:
                remaining = self.recovery_timeout - (time.monotonic() - self._last_failure_time)
                raise RuntimeError(
                    f"Circuit breaker OPEN for {self.name}. "
                    f"Retry in {remaining:.0f}s (service was timing out)."
                )

        try:
            result = await asyncio.wait_for(coro, timeout=self.call_timeout)
            # Success — reset
            if self._state == CircuitState.HALF_OPEN:
                print(f"[circuit:{self.name}] Recovery successful — circuit closed")
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            return result

        except (asyncio.TimeoutError, TimeoutError, Exception) as e:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()

            if self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                print(
                    f"[circuit:{self.name}] OPEN after {self._failure_count} failures. "
                    f"Will retry in {self.recovery_timeout:.0f}s."
                )
            raise

# One breaker per external service:
search_breaker = CircuitBreaker("web_search", failure_threshold=3, recovery_timeout=60.0)
db_breaker = CircuitBreaker("database", failure_threshold=5, recovery_timeout=30.0)

async def search_with_circuit_breaker(query: str) -> list[dict]:
    return await search_breaker.call(search_web(query))
```

### Option 6: Per-tool timeout configuration

```python
from dataclasses import dataclass

@dataclass
class ToolConfig:
    """Centralized timeout configuration for all tools"""
    name: str
    timeout_seconds: float
    retry_on_timeout: bool = True
    max_retries: int = 2

TOOL_TIMEOUTS: dict[str, ToolConfig] = {
    "search_web":       ToolConfig("search_web",       timeout_seconds=15.0),
    "fetch_document":   ToolConfig("fetch_document",   timeout_seconds=20.0),
    "run_code":         ToolConfig("run_code",         timeout_seconds=60.0, max_retries=1),
    "query_database":   ToolConfig("query_database",   timeout_seconds=10.0),
    "send_email":       ToolConfig("send_email",       timeout_seconds=30.0, retry_on_timeout=False),
    "call_llm":         ToolConfig("call_llm",         timeout_seconds=120.0),
    "read_file":        ToolConfig("read_file",        timeout_seconds=5.0),
    "list_files":       ToolConfig("list_files",       timeout_seconds=5.0),
}

async def execute_tool_with_config(tool_name: str, params: dict) -> dict:
    config = TOOL_TIMEOUTS.get(tool_name)
    if not config:
        raise ValueError(f"No timeout config for tool: {tool_name}")

    for attempt in range(config.max_retries + 1):
        try:
            return await asyncio.wait_for(
                dispatch_tool(tool_name, params),
                timeout=config.timeout_seconds
            )
        except asyncio.TimeoutError:
            if not config.retry_on_timeout or attempt >= config.max_retries:
                raise TimeoutError(
                    f"Tool '{tool_name}' timed out after {config.timeout_seconds}s "
                    f"(attempt {attempt + 1}/{config.max_retries + 1})"
                )
            wait = 2 ** attempt
            print(f"Tool '{tool_name}' timed out — retrying in {wait}s...")
            await asyncio.sleep(wait)
```

## Timeout Values by Tool Type

| Tool Type | Recommended Timeout | Notes |
|---|---|---|
| DNS / TCP connect | 3–5s | Should be near-instant on healthy network |
| REST API (simple) | 10–30s | Add jitter if many concurrent callers |
| Web scrape / fetch | 20–60s | Some pages are slow |
| LLM API call | 60–180s | Long outputs take time |
| Code execution | 30–120s | Depends on task complexity |
| Database query | 5–30s | Simple queries: 5s; complex: 30s |
| File I/O (local) | 1–5s | Slow only if disk is thrashing |
| Subprocess | Task-dependent | Always set; never leave at None |

## Expected Token Savings
Agent hangs for 10 minutes → user kills process → restarts → re-explains task: ~20,000 tokens
Timeout after 30s → clear error → retry or escalate: minimal wasted tokens

## Environment
- Any agent making external I/O calls; critical for agents with tool use, web search, code execution, or database access
- Source: direct experience; missing timeouts are the most common cause of silent agent hangs in production
