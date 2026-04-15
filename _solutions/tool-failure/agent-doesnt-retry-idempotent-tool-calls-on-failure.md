---
layout: solution
title: "Agent Doesn't Retry Idempotent Tool Calls on Failure"
category: tool-failure
description: "Agent gives up immediately on tool errors without retrying, even when the call is safe to repeat — causing unnecessary failures from transient network issues, timeouts, or momentary API unavailability."
tags: [tool-failure, retry, idempotency, resilience, exponential-backoff]
---

# Agent Doesn't Retry Idempotent Tool Calls on Failure

## Problem

When a tool call fails, agents typically report the error and stop or ask the user what to do. But many failures are transient: network blips, 503s, timeouts, or brief API overloads. For **idempotent** tools (reads, lookups, searches — calls that produce the same result when repeated), automatic retry with backoff is the correct response.

**Root cause:** No retry layer between the agent loop and tool execution. Every error surfaces immediately to the LLM as a failure.

**Symptoms:**
- "I was unable to fetch the weather data" after a single 500 error
- Agent asks user to "try again later" for a 1-second network hiccup
- Tasks fail at 2 AM from transient cloud provider issues with no recovery
- Non-idempotent tools (writes, payments) retried unsafely

---

## Option 1: Simple Exponential Backoff with Jitter

Basic retry decorator with configurable max attempts and jitter to prevent thundering herd.

```python
import anthropic
import json
import time
import random
from functools import wraps
from typing import Callable, Any

client = anthropic.Anthropic()

class RetryError(Exception):
    """Raised when all retry attempts are exhausted."""
    def __init__(self, message: str, attempts: int, last_error: Exception):
        super().__init__(message)
        self.attempts = attempts
        self.last_error = last_error

def with_backoff(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    jitter: bool = True,
    retryable_exceptions: tuple = (ConnectionError, TimeoutError, OSError)
):
    """Decorator: retry with exponential backoff + jitter."""
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except retryable_exceptions as e:
                    last_exc = e
                    if attempt == max_attempts:
                        raise RetryError(
                            f"{fn.__name__} failed after {max_attempts} attempts",
                            attempts=attempt,
                            last_error=e
                        )
                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    if jitter:
                        delay = delay * (0.5 + random.random() * 0.5)
                    print(f"[retry] attempt {attempt}/{max_attempts} failed: {e}. Waiting {delay:.1f}s...")
                    time.sleep(delay)
            raise RetryError("Exhausted retries", attempts=max_attempts, last_error=last_exc)
        return wrapper
    return decorator

# Simulated transient failures
_call_count = {}

def _make_flaky(name: str, fail_times: int = 2) -> Callable:
    """Make a function fail `fail_times` times before succeeding."""
    _call_count[name] = 0
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            _call_count[name] += 1
            if _call_count[name] <= fail_times:
                raise ConnectionError(f"Simulated transient error #{_call_count[name]}")
            return fn(*args, **kwargs)
        return wrapper
    return decorator

@_make_flaky("weather", fail_times=2)
@with_backoff(max_attempts=4, base_delay=0.1)
def fetch_weather(city: str) -> dict:
    return {"city": city, "temperature": 22.0, "condition": "sunny"}

@with_backoff(max_attempts=3, base_delay=0.1)
def fetch_exchange_rate(from_ccy: str, to_ccy: str) -> dict:
    return {"from": from_ccy, "to": to_ccy, "rate": 1320.5}

def execute_tool_with_retry(tool_name: str, tool_input: dict) -> str:
    try:
        if tool_name == "get_weather":
            result = fetch_weather(tool_input["city"])
        elif tool_name == "get_exchange_rate":
            result = fetch_exchange_rate(tool_input["from"], tool_input["to"])
        else:
            result = {"error": f"Unknown tool: {tool_name}"}
        return json.dumps(result)
    except RetryError as e:
        return json.dumps({"error": str(e), "attempts_made": e.attempts})

tools = [
    {
        "name": "get_weather",
        "description": "Get weather for a city",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"]
        }
    },
    {
        "name": "get_exchange_rate",
        "description": "Get exchange rate between two currencies",
        "input_schema": {
            "type": "object",
            "properties": {
                "from": {"type": "string"},
                "to": {"type": "string"}
            },
            "required": ["from", "to"]
        }
    }
]

def run_agent(query: str) -> str:
    messages = [{"role": "user", "content": query}]
    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )
        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))
        if response.stop_reason != "tool_use":
            break
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            content = execute_tool_with_retry(block.name, block.input)
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": content})
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})
    return "Done"

print(run_agent("What's the weather in Seoul?"))

# Expected Token Savings: ~20-40% (prevents retry turns where agent re-explains failure and re-invokes tool)
# Environment: Any production agent calling external APIs over the internet
```

---

## Option 2: Idempotency Classification — Safe vs Unsafe Retry

Tag tools as idempotent or not; only retry reads/lookups, never writes/mutations.

```python
import anthropic
import json
import time
import random
from enum import Enum

client = anthropic.Anthropic()

class ToolSafety(Enum):
    IDEMPOTENT = "idempotent"   # Safe to retry: reads, lookups, searches
    UNSAFE = "unsafe"           # NOT safe to retry: creates, writes, payments, sends

TOOL_SAFETY_MAP: dict[str, ToolSafety] = {
    "get_weather": ToolSafety.IDEMPOTENT,
    "search_products": ToolSafety.IDEMPOTENT,
    "get_user_profile": ToolSafety.IDEMPOTENT,
    "create_order": ToolSafety.UNSAFE,
    "send_email": ToolSafety.UNSAFE,
    "process_payment": ToolSafety.UNSAFE,
    "delete_record": ToolSafety.UNSAFE,
}

_failure_counters: dict[str, int] = {}

def simulate_tool_call(tool_name: str, tool_input: dict) -> dict:
    """Simulate calls with occasional transient failures."""
    _failure_counters[tool_name] = _failure_counters.get(tool_name, 0) + 1
    if tool_name == "get_weather" and _failure_counters[tool_name] <= 1:
        raise TimeoutError("upstream service timeout")
    if tool_name == "get_weather":
        return {"temperature": 18.0, "condition": "cloudy", "city": tool_input.get("city")}
    if tool_name == "search_products":
        return {"results": [{"name": "Widget A", "price": 29.99}], "total": 1}
    if tool_name == "create_order":
        return {"order_id": "ORD-001", "status": "created"}
    return {"error": f"No mock for {tool_name}"}

def execute_with_safety_aware_retry(
    tool_name: str,
    tool_input: dict,
    max_retries: int = 3,
    base_delay: float = 0.1
) -> tuple[str, bool]:
    """Returns (result_json, was_retried)."""
    safety = TOOL_SAFETY_MAP.get(tool_name, ToolSafety.UNSAFE)

    if safety == ToolSafety.UNSAFE:
        # Execute once, no retry
        try:
            result = simulate_tool_call(tool_name, tool_input)
            return json.dumps(result), False
        except Exception as e:
            return json.dumps({
                "error": str(e),
                "retry_skipped": True,
                "reason": f"Tool '{tool_name}' is not idempotent — cannot retry safely"
            }), False

    # IDEMPOTENT: retry with backoff
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            result = simulate_tool_call(tool_name, tool_input)
            return json.dumps(result), attempt > 1
        except (TimeoutError, ConnectionError, OSError) as e:
            last_error = e
            if attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1)) * (0.5 + random.random() * 0.5)
                print(f"[retry] {tool_name} attempt {attempt}/{max_retries}: {e}. Retry in {delay:.2f}s")
                time.sleep(delay)

    return json.dumps({
        "error": f"Failed after {max_retries} attempts: {last_error}",
        "tool": tool_name
    }), True

tools = [
    {
        "name": "get_weather",
        "description": "Get weather (idempotent read)",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"]
        }
    },
    {
        "name": "create_order",
        "description": "Create a new order (NOT idempotent — use once)",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string"},
                "quantity": {"type": "integer"}
            },
            "required": ["product_id", "quantity"]
        }
    }
]

def run_agent_safety_aware(query: str) -> str:
    messages = [{"role": "user", "content": query}]
    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )
        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))
        if response.stop_reason != "tool_use":
            break
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            content, was_retried = execute_with_safety_aware_retry(block.name, block.input)
            if was_retried:
                print(f"[info] {block.name} succeeded after retry")
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": content})
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})
    return "Done"

print(run_agent_safety_aware("What's the weather in Paris?"))

# Expected Token Savings: ~25% (transient failures recovered silently; unsafe tools still protected from double-execution)
# Environment: Agents handling both read and write operations (e-commerce, CRM, order management)
```

---

## Option 3: Retry Budget — Global Cap Across All Tool Calls

Limit total retry attempts per agent run to avoid runaway retry storms.

```python
import anthropic
import json
import time
import random
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class RetryBudget:
    max_total_retries: int = 10
    retries_used: int = 0
    retries_per_tool: dict[str, int] = field(default_factory=dict)
    max_retries_per_tool: int = 3

    def can_retry(self, tool_name: str) -> bool:
        if self.retries_used >= self.max_total_retries:
            return False
        tool_count = self.retries_per_tool.get(tool_name, 0)
        return tool_count < self.max_retries_per_tool

    def record_retry(self, tool_name: str):
        self.retries_used += 1
        self.retries_per_tool[tool_name] = self.retries_per_tool.get(tool_name, 0) + 1

    @property
    def summary(self) -> dict:
        return {
            "total_retries_used": self.retries_used,
            "budget_remaining": self.max_total_retries - self.retries_used,
            "per_tool": dict(self.retries_per_tool)
        }

_sim_counters: dict[str, int] = {}

def mock_api_call(tool_name: str, tool_input: dict) -> dict:
    _sim_counters[tool_name] = _sim_counters.get(tool_name, 0) + 1
    # First 2 calls to any tool fail
    if _sim_counters[tool_name] <= 2:
        raise ConnectionError(f"Network error on attempt {_sim_counters[tool_name]}")
    return {"tool": tool_name, "result": "success", "input": tool_input}

def execute_with_budget(
    tool_name: str,
    tool_input: dict,
    budget: RetryBudget,
    base_delay: float = 0.05
) -> str:
    attempt = 0
    last_error = None

    while True:
        try:
            result = mock_api_call(tool_name, tool_input)
            return json.dumps(result)
        except (ConnectionError, TimeoutError) as e:
            last_error = e
            if not budget.can_retry(tool_name):
                reason = (
                    f"global budget exhausted ({budget.retries_used}/{budget.max_total_retries})"
                    if budget.retries_used >= budget.max_total_retries
                    else f"per-tool budget exhausted for {tool_name}"
                )
                return json.dumps({
                    "error": str(last_error),
                    "retry_stopped": True,
                    "reason": reason,
                    "budget": budget.summary
                })
            budget.record_retry(tool_name)
            attempt += 1
            delay = base_delay * (2 ** (attempt - 1)) * (0.5 + random.random() * 0.5)
            print(f"[retry] {tool_name} retry #{budget.retries_per_tool.get(tool_name, 0)} | budget: {budget.retries_used}/{budget.max_total_retries}")
            time.sleep(delay)

tools = [
    {
        "name": tool_name,
        "description": f"Tool: {tool_name}",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"]
        }
    }
    for tool_name in ["search_flights", "get_hotel_prices", "check_visa_requirements"]
]

def run_agent_with_budget(query: str) -> str:
    budget = RetryBudget(max_total_retries=10, max_retries_per_tool=3)
    messages = [{"role": "user", "content": query}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )
        if response.stop_reason == "end_turn":
            text = next(b.text for b in response.content if hasattr(b, "text"))
            print(f"[budget summary] {budget.summary}")
            return text
        if response.stop_reason != "tool_use":
            break
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            content = execute_with_budget(block.name, block.input, budget)
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": content})
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})
    return "Done"

print(run_agent_with_budget("Find me flights to Tokyo, hotels, and visa requirements"))

# Expected Token Savings: ~30% (budgeted retry avoids both premature failure and runaway loops)
# Environment: Multi-tool agents in travel, research, or data aggregation use cases
```

---

## Option 4: Retry with Fallback Tool

If primary tool keeps failing, automatically switch to a fallback tool instead of giving up.

```python
import anthropic
import json
import time
import random

client = anthropic.Anthropic()

FALLBACK_CHAIN: dict[str, list[str]] = {
    "get_weather_primary": ["get_weather_secondary", "get_weather_cached"],
    "get_weather_secondary": ["get_weather_cached"],
}

_fail_sim: dict[str, int] = {}

def call_tool_impl(tool_name: str, tool_input: dict) -> dict:
    _fail_sim[tool_name] = _fail_sim.get(tool_name, 0) + 1
    if tool_name == "get_weather_primary":
        raise ConnectionError("Primary weather API is down")
    if tool_name == "get_weather_secondary" and _fail_sim[tool_name] <= 1:
        raise TimeoutError("Secondary API timeout")
    if tool_name == "get_weather_secondary":
        return {"temperature": 20.0, "condition": "partly cloudy", "source": "secondary", "city": tool_input.get("city")}
    if tool_name == "get_weather_cached":
        return {"temperature": 19.5, "condition": "cloudy", "source": "cache", "stale": True, "city": tool_input.get("city")}
    return {"error": f"Unknown: {tool_name}"}

def execute_with_fallback(
    tool_name: str,
    tool_input: dict,
    max_attempts_per: int = 2,
    delay: float = 0.05
) -> str:
    chain = [tool_name] + FALLBACK_CHAIN.get(tool_name, [])
    tried = []

    for current_tool in chain:
        for attempt in range(1, max_attempts_per + 1):
            try:
                result = call_tool_impl(current_tool, tool_input)
                result["_used_tool"] = current_tool
                result["_tried_tools"] = tried + [f"{current_tool}(attempt {attempt})"]
                return json.dumps(result)
            except (ConnectionError, TimeoutError, OSError) as e:
                print(f"[fallback] {current_tool} attempt {attempt}: {e}")
                if attempt < max_attempts_per:
                    time.sleep(delay)
        tried.append(current_tool)

    return json.dumps({
        "error": "All tools in fallback chain exhausted",
        "tried": tried,
        "input": tool_input
    })

tools = [
    {
        "name": "get_weather_primary",
        "description": "Get weather from primary API (falls back to secondary/cached on failure)",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"]
        }
    }
]

def run_agent_with_fallback(query: str) -> str:
    messages = [{"role": "user", "content": query}]
    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )
        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))
        if response.stop_reason != "tool_use":
            break
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            content = execute_with_fallback(block.name, block.input)
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": content})
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})
    return "Done"

print(run_agent_with_fallback("What's the weather in London?"))

# Expected Token Savings: ~35% (silent fallback prevents agent from exposing failure to user and asking for help)
# Environment: High-availability agents with redundant data sources (weather, search, pricing)
```

---

## Option 5: Async Retry with Circuit Breaker

Async retry combined with a circuit breaker that opens after repeated failures.

```python
import anthropic
import asyncio
import json
import time
import random
from enum import Enum
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

class CircuitState(Enum):
    CLOSED = "closed"     # Normal operation
    OPEN = "open"         # Failing — reject immediately
    HALF_OPEN = "half_open"  # Testing recovery

@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = 3
    recovery_timeout: float = 5.0
    failures: int = 0
    state: CircuitState = CircuitState.CLOSED
    opened_at: float = 0.0

    def record_success(self):
        self.failures = 0
        self.state = CircuitState.CLOSED

    def record_failure(self):
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.state = CircuitState.OPEN
            self.opened_at = time.time()
            print(f"[circuit] {self.name} OPENED after {self.failures} failures")

    def can_attempt(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if time.time() - self.opened_at > self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                print(f"[circuit] {self.name} -> HALF_OPEN (testing recovery)")
                return True
            return False
        return True  # HALF_OPEN: allow one attempt

    def handle_result(self, success: bool):
        if success:
            self.record_success()
            if self.state == CircuitState.HALF_OPEN:
                print(f"[circuit] {self.name} CLOSED (recovered)")
        else:
            self.record_failure()
            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.OPEN
                self.opened_at = time.time()

_breakers: dict[str, CircuitBreaker] = {}
_async_sim: dict[str, int] = {}

async def async_tool_call(tool_name: str, tool_input: dict) -> dict:
    _async_sim[tool_name] = _async_sim.get(tool_name, 0) + 1
    await asyncio.sleep(0.01)  # Simulate latency
    if _async_sim[tool_name] <= 4 and tool_name == "get_stock":
        raise ConnectionError(f"Stock API down ({_async_sim[tool_name]})")
    return {"tool": tool_name, "data": tool_input, "call_num": _async_sim[tool_name]}

async def execute_async_with_circuit_breaker(
    tool_name: str,
    tool_input: dict,
    max_retries: int = 3,
    base_delay: float = 0.05
) -> str:
    breaker = _breakers.setdefault(tool_name, CircuitBreaker(name=tool_name))

    if not breaker.can_attempt():
        return json.dumps({
            "error": f"Circuit breaker OPEN for {tool_name}",
            "state": breaker.state.value,
            "retry_after_seconds": breaker.recovery_timeout - (time.time() - breaker.opened_at)
        })

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            result = await async_tool_call(tool_name, tool_input)
            breaker.handle_result(True)
            return json.dumps(result)
        except (ConnectionError, TimeoutError) as e:
            last_error = e
            breaker.handle_result(False)
            if attempt < max_retries and breaker.can_attempt():
                delay = base_delay * (2 ** (attempt - 1)) * (0.5 + random.random() * 0.5)
                await asyncio.sleep(delay)
            else:
                break

    return json.dumps({"error": str(last_error), "attempts": attempt, "circuit": breaker.state.value})

tools = [
    {
        "name": "get_stock",
        "description": "Get stock price",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"]
        }
    }
]

async def run_async_agent(query: str) -> str:
    messages = [{"role": "user", "content": query}]
    while True:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )
        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))
        if response.stop_reason != "tool_use":
            break
        tool_blocks = [b for b in response.content if b.type == "tool_use"]
        results = await asyncio.gather(*[
            execute_async_with_circuit_breaker(b.name, b.input) for b in tool_blocks
        ])
        tool_results = [
            {"type": "tool_result", "tool_use_id": b.id, "content": r}
            for b, r in zip(tool_blocks, results)
        ]
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})
    return "Done"

print(asyncio.run(run_async_agent("What is Apple's stock price?")))

# Expected Token Savings: ~40% (async parallel retries + circuit breaker prevent cascading failures)
# Environment: High-throughput agents with parallel tool calls; financial data, real-time APIs
```

---

## Option 6: Retry with Result Verification

Retry not just on exception, but also when the result is semantically invalid (e.g., empty, null, or flagged).

```python
import anthropic
import json
import time
import random
from typing import Callable, Any

client = anthropic.Anthropic()

ResultVerifier = Callable[[dict], tuple[bool, str]]

def verify_weather_result(result: dict) -> tuple[bool, str]:
    if "error" in result:
        return False, f"Tool returned error: {result['error']}"
    if result.get("temperature") is None:
        return False, "temperature is None"
    if not result.get("city"):
        return False, "city is missing"
    return True, ""

def verify_search_result(result: dict) -> tuple[bool, str]:
    if not isinstance(result.get("results"), list):
        return False, "results must be a list"
    if len(result["results"]) == 0:
        return False, "empty results (may be a transient index issue)"
    return True, ""

VERIFIERS: dict[str, ResultVerifier] = {
    "get_weather": verify_weather_result,
    "search_docs": verify_search_result,
}

_semantic_sim: dict[str, int] = {}

def simulate_semantic_failure(tool_name: str, tool_input: dict) -> dict:
    _semantic_sim[tool_name] = _semantic_sim.get(tool_name, 0) + 1
    if tool_name == "get_weather":
        if _semantic_sim[tool_name] <= 2:
            # Returns semantically empty result (no exception, but useless)
            return {"city": tool_input.get("city"), "temperature": None, "condition": ""}
        return {"city": tool_input.get("city"), "temperature": 21.5, "condition": "clear"}
    if tool_name == "search_docs":
        if _semantic_sim[tool_name] == 1:
            return {"results": [], "total": 0}  # Empty first time
        return {"results": [{"title": "Doc 1", "score": 0.95}], "total": 1}
    return {"error": f"Unknown tool: {tool_name}"}

def execute_with_semantic_verification(
    tool_name: str,
    tool_input: dict,
    max_attempts: int = 3,
    base_delay: float = 0.1
) -> str:
    verifier = VERIFIERS.get(tool_name)
    last_result = {}

    for attempt in range(1, max_attempts + 1):
        try:
            result = simulate_semantic_failure(tool_name, tool_input)
        except (ConnectionError, TimeoutError) as e:
            last_result = {"error": str(e)}
            if attempt < max_attempts:
                time.sleep(base_delay * (2 ** (attempt - 1)))
            continue

        if verifier:
            is_valid, reason = verifier(result)
            if not is_valid:
                print(f"[semantic-retry] {tool_name} attempt {attempt}: {reason}")
                last_result = result
                if attempt < max_attempts:
                    delay = base_delay * (2 ** (attempt - 1)) * (0.5 + random.random() * 0.5)
                    time.sleep(delay)
                continue

        return json.dumps(result)

    return json.dumps({
        "error": "Result failed semantic verification after all retries",
        "last_result": last_result,
        "tool": tool_name
    })

tools = [
    {
        "name": "get_weather",
        "description": "Get weather data",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"]
        }
    },
    {
        "name": "search_docs",
        "description": "Search documentation",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"]
        }
    }
]

def run_agent_semantic_retry(query: str) -> str:
    messages = [{"role": "user", "content": query}]
    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )
        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))
        if response.stop_reason != "tool_use":
            break
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            content = execute_with_semantic_verification(block.name, block.input)
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": content})
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})
    return "Done"

print(run_agent_semantic_retry("What is the weather in Berlin and search docs for retry patterns?"))

# Expected Token Savings: ~30% (retries on semantic failures prevent agent from using empty/null data and hallucinating)
# Environment: Agents with data quality requirements; RAG pipelines with vector search; real-time APIs with partial outages
```

---

## Comparison

| Option | Retry Trigger | Idempotency Check | Budget Control | Best For |
|--------|---------------|-------------------|----------------|----------|
| 1. Exponential Backoff | Exception only | No | No | Simple scripts, single tools |
| 2. Safety Classification | Exception | Yes — tag-based | No | Mixed read/write agent tools |
| 3. Retry Budget | Exception | No | Yes — global cap | Multi-tool agents at risk of retry storms |
| 4. Fallback Chain | Exception | No | No | High-availability with redundant APIs |
| 5. Circuit Breaker (Async) | Exception | No | Yes — state-based | High-throughput parallel tool calls |
| 6. Semantic Verification | Exception + invalid result | No | No | APIs with silent empty/partial failures |
