---
layout: solution
title: "Agent Doesn't Set Request Timeouts — Hangs Indefinitely on Slow or Unresponsive Services"
category: general
description: "An agent calls an external API or tool that stops responding. Without a timeout, the agent thread blocks forever — consuming a connection, a worker slot, and leaving the user with a spinning indicator. One slow downstream service can freeze the entire agent. The fix is to set explicit timeouts at every I/O boundary."
tags: [reliability, timeout, hang, http, networking, deadlock, responsiveness, fault-tolerance]
---

# Agent Doesn't Set Request Timeouts — Hangs Indefinitely on Slow or Unresponsive Services

## Symptom
- Agent stops responding mid-task; no error, no output, just silence
- A tool call never returns — the agent appears frozen
- One slow external API brings down the entire worker process
- After 10 minutes of waiting, the user refreshes the page — session is lost
- Logs show the request was sent but no response was ever received
- Agent works fine until one downstream service has a network hiccup

## Root Cause
HTTP clients, subprocess calls, and database connections default to either infinite timeouts or very long ones (60–300 seconds). When a downstream service stalls — due to a network partition, a slow response body, or a server-side hang — the agent's connection blocks indefinitely. The fix is to set conservative timeouts at every I/O boundary, catch `TimeoutError`, and fail fast with a clear error rather than hanging.

## Fix

### Option 1: httpx with explicit timeouts — replace requests library

```python
import anthropic
import httpx
import json

client = anthropic.Anthropic()

# WRONG — requests library with no timeout:
# import requests
# response = requests.get("https://api.example.com/data")  # hangs forever

# RIGHT — httpx with per-phase timeouts:
HTTP_TIMEOUT = httpx.Timeout(
    connect=5.0,    # TCP handshake must complete within 5s
    read=30.0,      # reading the response body: 30s max
    write=10.0,     # sending request body: 10s max
    pool=5.0        # waiting for a connection from the pool: 5s max
)

def fetch_external_data(url: str, params: dict | None = None) -> dict:
    """
    HTTP GET with explicit timeouts. Raises TimeoutError on hang.
    """
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as http:
            response = http.get(url, params=params)
            response.raise_for_status()
            return response.json()
    except httpx.TimeoutException as e:
        raise TimeoutError(f"Request to {url} timed out: {e}") from e
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"HTTP {e.response.status_code} from {url}") from e


# Tool definition for the agent:
TOOLS = [
    {
        "name": "fetch_data",
        "description": "Fetch data from an external API. Times out after 30 seconds.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "query": {"type": "string"}
            },
            "required": ["url"]
        }
    }
]

def handle_tool_call(name: str, inputs: dict) -> str:
    if name == "fetch_data":
        try:
            data = fetch_external_data(inputs["url"], {"q": inputs.get("query", "")})
            return json.dumps(data)
        except TimeoutError as e:
            return f"Error: {e}. The service did not respond in time — try again later."
        except Exception as e:
            return f"Error: {e}"
    return "Unknown tool"


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages
        )
        if response.stop_reason == "end_turn":
            return response.content[0].text

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = handle_tool_call(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})
```

### Option 2: asyncio.wait_for — timeout any coroutine

```python
import anthropic
import asyncio
import httpx
from typing import Any, Callable, Coroutine

client = anthropic.AsyncAnthropic()

async def with_timeout(
    coro: Coroutine,
    timeout_seconds: float,
    operation_name: str = "operation"
) -> Any:
    """
    Wrap any coroutine with a timeout.
    Returns the result or raises TimeoutError with a descriptive message.
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        raise TimeoutError(
            f"{operation_name} did not complete within {timeout_seconds}s"
        )


async def call_database(query: str) -> dict:
    """Simulated slow database call."""
    await asyncio.sleep(100)  # simulates a hung query
    return {"rows": []}


async def call_search_api(query: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as http:
        r = await http.get("https://api.search.example.com", params={"q": query})
        return r.json()


async def run_research_agent(topic: str) -> str:
    """
    Agent that calls multiple tools, each with individual timeouts.
    A hung tool fails fast instead of freezing the whole agent.
    """
    # Each tool has its own timeout appropriate to its expected latency:
    try:
        db_result = await with_timeout(
            call_database(topic),
            timeout_seconds=5.0,
            operation_name="database_query"
        )
    except TimeoutError as e:
        db_result = {"error": str(e), "rows": []}

    try:
        search_result = await with_timeout(
            call_search_api(topic),
            timeout_seconds=15.0,
            operation_name="search_api"
        )
    except TimeoutError as e:
        search_result = {"error": str(e), "results": []}

    context = f"DB: {db_result}\nSearch: {search_result}"

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"Summarize findings about '{topic}':\n{context}"
        }]
    )
    return response.content[0].text


# Global timeout: if the entire agent takes >60s, abort:
async def main():
    try:
        result = await with_timeout(
            run_research_agent("quantum computing"),
            timeout_seconds=60.0,
            operation_name="research_agent"
        )
        print(result)
    except TimeoutError as e:
        print(f"Agent timed out: {e}")
```

### Option 3: subprocess with timeout — tool calls that run shell commands

```python
import anthropic
import subprocess
import json

client = anthropic.Anthropic()

def run_command(
    cmd: list[str],
    timeout_seconds: float = 30.0,
    capture_output: bool = True
) -> dict:
    """
    Run a shell command with a hard timeout.
    Returns {stdout, stderr, returncode} or {error} on timeout.
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=capture_output,
            text=True,
            timeout=timeout_seconds,  # ← key: POSIX SIGKILL after N seconds
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode
        }
    except subprocess.TimeoutExpired:
        return {
            "error": f"Command timed out after {timeout_seconds}s: {' '.join(cmd)}",
            "stdout": "",
            "stderr": "",
            "returncode": -1
        }
    except FileNotFoundError as e:
        return {"error": f"Command not found: {e}", "returncode": -1}


TOOLS = [
    {
        "name": "run_script",
        "description": "Run a shell script. Hard timeout: 30 seconds.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"]
        }
    }
]

def handle_run_script(command: str) -> str:
    # Safety: only allow specific commands
    allowed_prefixes = ["python3 ", "node ", "git ", "ls ", "cat "]
    if not any(command.startswith(p) for p in allowed_prefixes):
        return "Error: Command not allowed."

    result = run_command(["bash", "-c", command], timeout_seconds=30.0)
    if "error" in result and result.get("returncode") == -1:
        return result["error"]
    return result["stdout"] or result["stderr"] or f"Exit code: {result['returncode']}"
```

### Option 4: Timeout budget — distribute a total budget across multiple calls

```python
import anthropic
import httpx
import time
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class TimeoutBudget:
    """
    A shared timeout budget for a multi-step operation.
    Each step claims some of the remaining budget.
    """
    total_seconds: float
    _start: float = 0.0

    def __post_init__(self):
        self._start = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._start

    @property
    def remaining(self) -> float:
        return max(0.0, self.total_seconds - self.elapsed)

    @property
    def is_expired(self) -> bool:
        return self.remaining <= 0.0

    def claim(self, max_seconds: float) -> float:
        """Claim up to max_seconds from the remaining budget."""
        if self.is_expired:
            raise TimeoutError(f"Budget exhausted after {self.elapsed:.1f}s")
        return min(max_seconds, self.remaining)


def multi_step_agent_with_budget(user_query: str, total_budget: float = 45.0) -> str:
    """
    Agent that distributes a timeout budget across all its I/O steps.
    Total response time is bounded no matter what.
    """
    budget = TimeoutBudget(total_seconds=total_budget)

    # Step 1: Fetch context (claim up to 10s)
    try:
        t = budget.claim(10.0)
        with httpx.Client(timeout=t) as http:
            ctx_response = http.get("https://api.example.com/context", params={"q": user_query})
            context = ctx_response.json().get("text", "")
    except (httpx.TimeoutException, TimeoutError):
        context = ""  # degrade gracefully

    # Step 2: LLM call (claim up to 30s)
    try:
        t = budget.claim(30.0)
        # Note: Anthropic SDK doesn't directly take a timeout in create(),
        # but we can use a thread + signal, or the httpx-level timeout:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": f"Context: {context}\n\nQuery: {user_query}"
            }]
        )
        answer = response.content[0].text
    except TimeoutError as e:
        return f"Request timed out: {e}"

    # Step 3: Post-process (claim up to 5s)
    try:
        t = budget.claim(5.0)
        with httpx.Client(timeout=t) as http:
            http.post("https://api.example.com/log", json={"query": user_query, "answer": answer})
    except (httpx.TimeoutException, TimeoutError):
        pass  # logging failure is non-critical

    print(f"[budget] Completed in {budget.elapsed:.2f}s / {total_budget}s")
    return answer
```

### Option 5: Timeout middleware — centralized policy for all outbound calls

```python
import anthropic
import httpx
import functools
import time
import logging
from typing import Callable

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

# Centralized timeout policy — one place to tune:
TIMEOUT_POLICY = {
    "fast_api": 5.0,         # health checks, lightweight endpoints
    "standard_api": 15.0,    # typical external APIs
    "slow_api": 60.0,        # batch endpoints, data exports
    "llm": 120.0,            # LLM inference
    "database": 10.0,        # DB queries
    "default": 30.0,         # anything not listed
}

class TimeoutMiddleware:
    """
    Wraps an httpx.Client to apply per-domain timeout policies.
    Add new domains without changing calling code.
    """
    DOMAIN_POLICIES = {
        "api.fast-service.com": "fast_api",
        "api.slow-data.com": "slow_api",
        "db.internal": "database",
    }

    def __init__(self):
        self._clients: dict[str, httpx.Client] = {}

    def _get_client(self, url: str) -> tuple[httpx.Client, float]:
        import urllib.parse
        domain = urllib.parse.urlparse(url).netloc
        policy_key = self.DOMAIN_POLICIES.get(domain, "default")
        timeout = TIMEOUT_POLICY[policy_key]

        if policy_key not in self._clients:
            self._clients[policy_key] = httpx.Client(timeout=timeout)

        return self._clients[policy_key], timeout

    def get(self, url: str, **kwargs) -> httpx.Response:
        client_inst, timeout = self._get_client(url)
        t0 = time.monotonic()
        try:
            response = client_inst.get(url, **kwargs)
            logger.debug(f"GET {url} → {response.status_code} in {time.monotonic()-t0:.2f}s")
            return response
        except httpx.TimeoutException:
            elapsed = time.monotonic() - t0
            logger.warning(f"GET {url} timed out after {elapsed:.2f}s (policy: {timeout}s)")
            raise TimeoutError(f"GET {url} timed out after {timeout}s")

    def close(self):
        for c in self._clients.values():
            c.close()


# Usage in agent tools:
http = TimeoutMiddleware()

def fetch_with_policy(url: str) -> dict:
    try:
        return http.get(url).json()
    except TimeoutError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}
```

### Option 6: Anthropic SDK timeout configuration + retry

```python
import anthropic
import httpx

# The Anthropic SDK uses httpx internally.
# Configure timeouts at the client level so every API call has a bound:

client = anthropic.Anthropic(
    timeout=httpx.Timeout(
        connect=5.0,
        read=60.0,    # LLM streaming can take up to 60s to complete
        write=10.0,
        pool=5.0
    ),
    max_retries=2     # retry on transient network errors, not timeouts
)

# For streaming responses, use a per-stream read timeout:
async def stream_with_timeout(user_message: str, read_timeout: float = 60.0):
    async_client = anthropic.AsyncAnthropic(
        timeout=httpx.Timeout(read=read_timeout, connect=5.0)
    )
    async with async_client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": user_message}]
    ) as stream:
        async for text in stream.text_stream:
            print(text, end="", flush=True)
    print()


# For tool-calling loops, enforce a per-turn timeout:
import asyncio

async def agent_loop_with_turn_timeout(
    user_message: str,
    turn_timeout: float = 30.0,
    max_turns: int = 10
) -> str:
    async_client = anthropic.AsyncAnthropic(
        timeout=httpx.Timeout(read=turn_timeout, connect=5.0)
    )
    messages = [{"role": "user", "content": user_message}]

    for turn in range(max_turns):
        try:
            response = await asyncio.wait_for(
                async_client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=512,
                    messages=messages
                ),
                timeout=turn_timeout
            )
        except asyncio.TimeoutError:
            return f"Agent timed out on turn {turn + 1} (>{turn_timeout}s)"

        if response.stop_reason == "end_turn":
            return response.content[0].text

        # Process tool calls...
        messages.append({"role": "assistant", "content": response.content})

    return "Max turns reached"
```

## Timeout Checklist

| I/O Type | Recommended Timeout | How to Set |
|---|---|---|
| HTTP connect | 3–5s | `httpx.Timeout(connect=5)` |
| HTTP read | 10–60s | `httpx.Timeout(read=30)` |
| LLM non-streaming | 30–120s | `httpx.Timeout(read=120)` |
| LLM streaming | 60–300s | `httpx.Timeout(read=300)` |
| Subprocess | 10–60s | `subprocess.run(timeout=30)` |
| Database query | 5–30s | driver-specific option |
| Total agent budget | 30–120s | `asyncio.wait_for(budget=90)` |

## Expected Token Savings
No direct token impact — but hanging agents consume worker slots and delay all subsequent requests, effectively reducing throughput. One hung request at 100% CPU can block dozens of queued requests.

## Environment
- Any agent calling external HTTP APIs, shell commands, or databases; mandatory for production agents; the Anthropic SDK itself times out by default at 600s (httpx default) — set it to ≤120s for non-streaming and ≤300s for streaming; use `asyncio.wait_for` for coroutine-level timeouts and `subprocess.run(timeout=N)` for shell commands; always degrade gracefully (return partial result or error message) rather than propagating the TimeoutError to the user as a crash
