---
layout: solution
title: "Agent Retries Tool Calls with Identical Arguments After Failure"
category: tool-failure
description: "Agent receives a tool error and immediately retries with the exact same arguments, causing infinite retry loops or wasted quota."
tags: [tool-failure, retry, loop, error-handling, resilience]
---

## Symptom

Agent gets a tool error and retries identically without changing anything:

```
Turn 1: call search_web(query="latest anthropic pricing")
Tool result: {"error": "rate_limit_exceeded", "retry_after": 30}

Turn 2: call search_web(query="latest anthropic pricing")   ← same args
Tool result: {"error": "rate_limit_exceeded", "retry_after": 30}

Turn 3: call search_web(query="latest anthropic pricing")   ← same args again
Tool result: {"error": "rate_limit_exceeded", "retry_after": 30}

# Loops until max_tokens exhausted or hard stop
```

The agent treats every error as transient and assumes re-trying the same call will succeed. Deterministic errors (wrong schema, 404, auth failure) are retried endlessly. Even transient errors (rate limits) are retried immediately without honouring retry-after headers.

## Root Cause

LLMs have no built-in retry policy. Without explicit instructions distinguishing retryable vs non-retryable errors, the model defaults to "try again" for any failure. The tool result carries error information the model doesn't reliably parse or act on — it sees the error text but doesn't know whether to change arguments, wait, try a different tool, or give up.

## Fix

---

### Option 1: Error Classification Wrapper — Inject Retry Guidance into Tool Result

Classify errors before returning them to the model. Append explicit, unambiguous instructions about what to do next so the LLM doesn't have to guess.

```python
import time
import anthropic
from enum import Enum

class ErrorClass(Enum):
    RETRYABLE_WAIT = "retryable_wait"       # rate limit, 429, 503
    RETRYABLE_IMMEDIATE = "retryable_now"   # transient network glitch
    FATAL_ARGS = "fatal_args"              # wrong schema, 400, 422
    FATAL_AUTH = "fatal_auth"             # 401, 403
    FATAL_NOT_FOUND = "fatal_not_found"   # 404

def classify_error(error: dict) -> tuple[ErrorClass, str]:
    code = error.get("code") or error.get("status") or 0
    msg = str(error.get("message") or error.get("error") or "").lower()

    if "rate_limit" in msg or code == 429:
        retry_after = error.get("retry_after", 30)
        return ErrorClass.RETRYABLE_WAIT, f"Wait {retry_after}s then retry with SAME arguments."
    if "overloaded" in msg or code == 503:
        return ErrorClass.RETRYABLE_WAIT, "Service overloaded. Wait 10s then retry."
    if "timeout" in msg or "connection" in msg:
        return ErrorClass.RETRYABLE_IMMEDIATE, "Transient network error. You may retry once."
    if code in (400, 422) or "invalid" in msg or "schema" in msg:
        return ErrorClass.FATAL_ARGS, "Bad arguments — do NOT retry with same args. Fix the arguments or use a different tool."
    if code in (401, 403) or "auth" in msg or "forbidden" in msg:
        return ErrorClass.FATAL_AUTH, "Auth failure — do NOT retry. Report to user and stop."
    if code == 404 or "not found" in msg:
        return ErrorClass.FATAL_NOT_FOUND, "Resource not found — do NOT retry with same args. Try a different query or tool."
    return ErrorClass.RETRYABLE_IMMEDIATE, "Unknown error. You may retry once with modified arguments."

def tool_result_with_guidance(tool_name: str, raw_result: dict) -> str:
    if "error" not in raw_result:
        return str(raw_result)

    cls, guidance = classify_error(raw_result)
    return (
        f"TOOL ERROR from {tool_name}:\n"
        f"  Raw error: {raw_result}\n"
        f"  Class: {cls.value}\n"
        f"  INSTRUCTION: {guidance}"
    )

# Simulated agent loop
client = anthropic.Anthropic()
tools = [
    {
        "name": "search_web",
        "description": "Search the web for information",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }
]

messages = [{"role": "user", "content": "Find the latest Anthropic API pricing"}]

for _ in range(6):
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        tools=tools,
        messages=messages,
    )

    if response.stop_reason != "tool_use":
        print(response.content[0].text)
        break

    tool_use = next(b for b in response.content if b.type == "tool_use")
    messages.append({"role": "assistant", "content": response.content})

    # Simulate a rate limit error
    raw_error = {"error": "rate_limit_exceeded", "retry_after": 30, "status": 429}
    guided_result = tool_result_with_guidance(tool_use.name, raw_error)

    messages.append({
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": tool_use.id, "content": guided_result}],
    })
```

**Expected Token Savings:** Terminates retry loops in 1-2 turns instead of 10+. For fatal errors, saves ~8 wasted retry turns × ~500 tokens = 4,000 tokens per incident.
**Environment:** Works with any tool. Error classification logic must be tuned to the specific tools and APIs in use.

---

### Option 2: Stateful Retry Tracker — Block Identical Retries at the Orchestrator Level

Track (tool_name, args_hash) pairs and block re-submission of identical calls that previously failed with a non-retryable error.

```python
import hashlib
import json
import time
import anthropic
from dataclasses import dataclass, field

@dataclass
class RetryRecord:
    attempts: int = 0
    last_error_class: str = ""
    last_attempt_at: float = 0.0
    retry_after: float = 0.0

class RetryTracker:
    def __init__(self, max_retries: int = 2):
        self.max_retries = max_retries
        self.records: dict[str, RetryRecord] = {}

    def _key(self, tool_name: str, args: dict) -> str:
        args_str = json.dumps(args, sort_keys=True)
        return f"{tool_name}:{hashlib.sha256(args_str.encode()).hexdigest()[:16]}"

    def should_allow(self, tool_name: str, args: dict) -> tuple[bool, str]:
        key = self._key(tool_name, args)
        rec = self.records.get(key)

        if rec is None:
            return True, ""

        # Fatal errors: never retry with same args
        if rec.last_error_class in ("fatal_args", "fatal_auth", "fatal_not_found"):
            return False, (
                f"Blocked: {tool_name} with these exact arguments previously failed "
                f"with {rec.last_error_class}. Modify arguments or try a different approach."
            )

        # Rate limit: respect retry_after
        if rec.last_error_class == "retryable_wait":
            wait_remaining = rec.retry_after - (time.time() - rec.last_attempt_at)
            if wait_remaining > 0:
                return False, f"Blocked: must wait {wait_remaining:.0f}s before retrying."

        # Exceeded max retries
        if rec.attempts >= self.max_retries:
            return False, (
                f"Blocked: {tool_name} has failed {rec.attempts} times with these args. "
                "Give up and report the failure to the user."
            )

        return True, ""

    def record_failure(self, tool_name: str, args: dict, error_class: str, retry_after: float = 0):
        key = self._key(tool_name, args)
        if key not in self.records:
            self.records[key] = RetryRecord()
        rec = self.records[key]
        rec.attempts += 1
        rec.last_error_class = error_class
        rec.last_attempt_at = time.time()
        rec.retry_after = retry_after

    def record_success(self, tool_name: str, args: dict):
        key = self._key(tool_name, args)
        self.records.pop(key, None)

tracker = RetryTracker(max_retries=2)
client = anthropic.Anthropic()

def execute_tool(tool_name: str, args: dict) -> str:
    allowed, reason = tracker.should_allow(tool_name, args)
    if not allowed:
        return f"ORCHESTRATOR BLOCKED: {reason}"

    # Simulate tool execution (replace with real tool call)
    error = {"error": "invalid_argument", "message": "unknown field 'q'", "status": 400}
    tracker.record_failure(tool_name, args, "fatal_args")
    return f"TOOL ERROR: {error}\nINSTRUCTION: Bad arguments — do NOT retry with same args."

# Test: second call with same args is blocked before reaching the API
result1 = execute_tool("search_web", {"q": "anthropic pricing"})
result2 = execute_tool("search_web", {"q": "anthropic pricing"})  # blocked
print(result1)
print(result2)
```

**Expected Token Savings:** Blocks retry entirely at orchestrator level — zero additional API calls for fatal errors. Saves the full cost of each blocked turn (~500-1,500 tokens).
**Environment:** Stateful; tracker lives for the duration of one agent session. Reset between sessions or persist to disk for cross-session dedup.

---

### Option 3: Argument Mutation Strategy — Instruct Model to Modify Args on Retry

When a retryable error occurs, inject a specific prompt instructing the model to change at least one argument before retrying.

```python
import anthropic

MUTATION_INSTRUCTIONS = {
    "search_web": (
        "Retry with a rephrased or broader query. "
        "Example: if query='X Y Z', try query='X Y' or query='X Z'."
    ),
    "read_file": (
        "Retry with a different file path. "
        "Check if the file exists at a parent directory or alternative location."
    ),
    "call_api": (
        "Retry with simplified request body — remove optional fields. "
        "Or try a different endpoint if available."
    ),
}

def build_retry_instruction(tool_name: str, original_args: dict, error: dict) -> str:
    mutation_hint = MUTATION_INSTRUCTIONS.get(
        tool_name, "Retry with at least one argument changed."
    )
    return (
        f"Tool '{tool_name}' failed with: {error}\n\n"
        f"DO NOT retry with identical arguments {json.dumps(original_args)}.\n"
        f"Mutation required: {mutation_hint}\n"
        f"Try a different approach."
    )

import json

client = anthropic.Anthropic()
tools = [
    {
        "name": "search_web",
        "description": "Search the web",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    }
]

messages = [{"role": "user", "content": "Find Anthropic's latest model pricing"}]
last_tool_args: dict[str, dict] = {}  # tool_use_id → args

for turn in range(8):
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        tools=tools,
        messages=messages,
    )

    if response.stop_reason != "tool_use":
        print(f"Final answer: {response.content[0].text}")
        break

    tool_use = next(b for b in response.content if b.type == "tool_use")
    current_args = tool_use.input
    messages.append({"role": "assistant", "content": response.content})

    # Check if model is repeating the same args as a previous failed call
    prev_args = last_tool_args.get(tool_use.name)
    if prev_args and prev_args == current_args:
        result_content = (
            "ORCHESTRATOR: You submitted identical arguments as a previous failed call. "
            "You MUST change at least one argument. "
            + MUTATION_INSTRUCTIONS.get(tool_use.name, "Modify your approach.")
        )
    else:
        # Simulate transient error
        error = {"error": "service_unavailable", "status": 503}
        result_content = build_retry_instruction(tool_use.name, current_args, error)
        last_tool_args[tool_use.name] = current_args

    messages.append({
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": tool_use.id, "content": result_content}],
    })
```

**Expected Token Savings:** Forces model to take a different path on each retry, preventing 5-10 identical wasted turns. Net savings: ~3,000-8,000 tokens per stuck loop.
**Environment:** Mutation hints must be customised per tool. Works best when the tool supports natural argument variation (queries, search terms, filters).

---

### Option 4: Exponential Backoff with Jitter and Per-Tool Circuit Breaker

Implement a proper retry policy in the tool execution layer: exponential backoff for transient errors, circuit breaker to stop hammering failing services.

```python
import asyncio
import random
import time
from enum import Enum
from dataclasses import dataclass
import anthropic

class CircuitState(Enum):
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing; reject calls
    HALF_OPEN = "half_open"  # Testing recovery

@dataclass
class CircuitBreaker:
    failure_threshold: int = 3
    recovery_timeout: float = 60.0
    _failures: int = 0
    _last_failure_time: float = 0.0
    _state: CircuitState = CircuitState.CLOSED

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if time.time() - self._last_failure_time > self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
        return self._state

    def record_success(self):
        self._failures = 0
        self._state = CircuitState.CLOSED

    def record_failure(self):
        self._failures += 1
        self._last_failure_time = time.time()
        if self._failures >= self.failure_threshold:
            self._state = CircuitState.OPEN

async def execute_with_backoff(
    tool_name: str,
    args: dict,
    tool_fn,
    circuit: CircuitBreaker,
    max_attempts: int = 3,
    base_delay: float = 1.0,
) -> tuple[bool, str]:
    """Returns (success, result_string_for_model)."""

    if circuit.state == CircuitState.OPEN:
        return False, (
            f"CIRCUIT OPEN: {tool_name} is currently unavailable (too many recent failures). "
            "Do not retry. Use a fallback approach or inform the user."
        )

    for attempt in range(max_attempts):
        try:
            result = await tool_fn(tool_name, args)
            circuit.record_success()
            return True, str(result)
        except Exception as e:
            error_str = str(e).lower()
            is_retryable = any(k in error_str for k in ("timeout", "503", "502", "overloaded"))

            if not is_retryable or attempt == max_attempts - 1:
                circuit.record_failure()
                return False, (
                    f"TOOL FAILED after {attempt + 1} attempt(s): {e}\n"
                    "Do NOT retry with same arguments. Try a different approach."
                )

            # Exponential backoff with full jitter
            delay = base_delay * (2 ** attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)

    circuit.record_failure()
    return False, f"TOOL EXHAUSTED {max_attempts} retries. Use alternative approach."

# Usage in agent loop
async def run_agent():
    client = anthropic.AsyncAnthropic()
    circuits: dict[str, CircuitBreaker] = {}

    async def mock_tool(name: str, args: dict):
        raise TimeoutError("Connection timed out")

    tools = [
        {
            "name": "search_web",
            "description": "Search the web",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        }
    ]

    messages = [{"role": "user", "content": "Search for Anthropic news"}]
    response = await client.messages.create(
        model="claude-sonnet-4-6", max_tokens=512, tools=tools, messages=messages
    )

    if response.stop_reason == "tool_use":
        tool_use = next(b for b in response.content if b.type == "tool_use")
        circuit = circuits.setdefault(tool_use.name, CircuitBreaker())
        success, result = await execute_with_backoff(
            tool_use.name, tool_use.input, mock_tool, circuit
        )
        print(f"Success: {success}, Result: {result}")

asyncio.run(run_agent())
```

**Expected Token Savings:** Backoff happens outside the LLM loop — model is not called during wait periods. Circuit breaker stops retries after threshold, saving all tokens from subsequent attempts. Net: 60-80% reduction in tokens wasted on failing tools.
**Environment:** Async-first; works with `asyncio.gather` for parallel tool calls. Circuit state resets automatically after recovery_timeout.

---

### Option 5: Alternative Tool Fallback Chain

When a tool fails, automatically offer the model a ranked list of alternative tools to achieve the same goal.

```python
import anthropic
import json

# Fallback chains: primary tool → list of alternatives
FALLBACK_CHAINS: dict[str, list[str]] = {
    "search_web": ["search_news", "search_wikipedia", "use_cached_knowledge"],
    "read_file": ["read_url", "search_database", "ask_user_for_content"],
    "execute_code": ["describe_code_logic", "use_python_repl_sandbox"],
    "call_external_api": ["use_cached_response", "search_web_for_answer"],
}

def get_fallback_message(failed_tool: str, error: dict) -> str:
    alternatives = FALLBACK_CHAINS.get(failed_tool, [])
    if not alternatives:
        return f"Tool '{failed_tool}' failed: {error}. No alternatives available. Report failure to user."

    alt_list = "\n".join(f"  {i+1}. {alt}" for i, alt in enumerate(alternatives))
    return (
        f"Tool '{failed_tool}' failed: {error}\n\n"
        f"DO NOT retry '{failed_tool}' with same arguments.\n"
        f"Try one of these alternatives in order:\n{alt_list}"
    )

client = anthropic.Anthropic()

all_tools = [
    {
        "name": "search_web",
        "description": "Search the live web",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "search_wikipedia",
        "description": "Search Wikipedia for factual information",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "use_cached_knowledge",
        "description": "Answer from training knowledge without live search",
        "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]},
    },
]

messages = [{"role": "user", "content": "What is Anthropic's current model lineup?"}]

for _ in range(6):
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        tools=all_tools,
        messages=messages,
    )

    if response.stop_reason != "tool_use":
        print(response.content[0].text)
        break

    tool_use = next(b for b in response.content if b.type == "tool_use")
    messages.append({"role": "assistant", "content": response.content})

    if tool_use.name == "search_web":
        error = {"error": "dns_resolution_failed", "status": 0}
        result = get_fallback_message(tool_use.name, error)
    elif tool_use.name == "use_cached_knowledge":
        result = json.dumps({
            "answer": "Anthropic's current models include Claude Haiku, Sonnet, and Opus in the Claude 4 family."
        })
    else:
        result = json.dumps({"results": []})

    messages.append({
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": tool_use.id, "content": result}],
    })
```

**Expected Token Savings:** Model pivots to a working alternative in 1 turn instead of retrying the same broken tool. Saves 3-5 retry turns × ~600 tokens = 1,800-3,000 tokens per incident.
**Environment:** Fallback chains must be defined per domain. Works best when tools have genuine overlapping capabilities.

---

### Option 6: System Prompt Retry Policy — Teach the Model the Rules Upfront

Embed a clear retry policy in the system prompt so the model internalises the rules before the first tool call, eliminating the need for per-error injection.

```python
import anthropic

RETRY_POLICY = """
## Tool Retry Policy

You MUST follow these rules when a tool fails:

1. **Rate limit (429) or overloaded (503)**:
   - Wait the duration specified in retry_after (default 30s if not given)
   - Retry ONCE with identical arguments
   - If it fails again, inform the user and stop

2. **Invalid arguments (400, 422, "invalid", "schema error")**:
   - Do NOT retry with same arguments — it will fail again
   - Fix the argument values or schema, then retry
   - If you cannot determine the correct arguments, ask the user

3. **Not found (404)**:
   - Do NOT retry with same query/path
   - Try a different search term, path, or approach
   - If no alternative works, tell the user the resource doesn't exist

4. **Auth failure (401, 403)**:
   - Do NOT retry at all
   - Tell the user their credentials may be invalid or expired
   - Stop and wait for user action

5. **After 3 consecutive failures for any tool**:
   - Stop retrying that tool
   - Explain what you tried and what failed
   - Propose an alternative approach or ask the user for help

6. **General rule**: Never call a tool with identical (name + arguments) more than twice in a row.
"""

client = anthropic.Anthropic()
tools = [
    {
        "name": "fetch_data",
        "description": "Fetch data from an external source",
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "params": {"type": "object"},
            },
            "required": ["source"],
        },
    }
]

messages = [{"role": "user", "content": "Fetch the latest sales data from our API"}]

for _ in range(8):
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=RETRY_POLICY,
        tools=tools,
        messages=messages,
    )

    if response.stop_reason != "tool_use":
        print(response.content[0].text)
        break

    tool_use = next(b for b in response.content if b.type == "tool_use")
    messages.append({"role": "assistant", "content": response.content})

    # Return a 404 — model should not retry with same source
    result = '{"error": "not_found", "status": 404, "message": "endpoint /sales/latest not found"}'
    messages.append({
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": tool_use.id, "content": result}],
    })

# Comparison table
"""
| Approach | Enforcement | Overhead | Best For |
|---|---|---|---|
| Option 1: Error classification | Per-result injection | ~100 tokens/error | General purpose |
| Option 2: Stateful tracker | Orchestrator block | 0 LLM tokens | Fatal error prevention |
| Option 3: Mutation instruction | Per-retry prompt | ~150 tokens/retry | Retryable with variation |
| Option 4: Backoff + circuit | Pre-LLM layer | 0 LLM tokens | Network/transient errors |
| Option 5: Fallback chain | Tool result redirect | ~200 tokens | Multi-tool environments |
| Option 6: System prompt policy | Global rules | ~400 tokens once | Simple, broad coverage |
"""
```

**Expected Token Savings:** One-time ~400 token investment in system prompt prevents unlimited retry loops. A single prevented 10-retry loop saves ~6,000 tokens — 15× return on investment per incident.
**Environment:** Simplest to implement; no orchestrator changes needed. Effectiveness depends on model following system prompt instructions reliably. Combine with Option 2 for hard enforcement.
