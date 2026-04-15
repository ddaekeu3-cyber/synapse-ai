---
layout: solution
title: "Agent can't exit tool-use loop when tool always returns error"
category: loop-stuck
description: "When a tool consistently returns an error, the agent retries the same call indefinitely — consuming API quota, burning tokens, and never producing a user-facing answer."
tags: [loop-stuck, tool-failure, error-handling, retry-limit, fallback, exit-condition]
---

## Symptom

The agent enters an infinite loop:

```
Turn 1: [tool_use] search_database(query="user request")
Turn 2: [tool_result] ERROR: connection refused
Turn 3: [tool_use] search_database(query="user request")   ← identical call
Turn 4: [tool_result] ERROR: connection refused
Turn 5: [tool_use] search_database(query="user request")   ← still identical
...
```

The loop continues until the context window fills, the API rate limit is hit, or the user force-quits the agent. No answer is ever delivered.

## Root Cause

The agent loop only checks `stop_reason == "end_turn"` as its exit condition. When a tool call fails, the model receives the error as a tool result and decides to retry — which is reasonable behaviour for one attempt. Without an explicit retry counter or error pattern detector, the loop has no exit condition for persistent tool failure. The model keeps trying because it has no instruction to stop or fall back.

---

## Option 1 — Per-tool retry counter with hard exit

**Track consecutive failures per tool. After N failures, return an error result and stop calling the tool.**

```python
import json
from collections import defaultdict
import anthropic

client = anthropic.Anthropic()

MAX_TOOL_RETRIES = 3

DB_TOOL = {
    "name": "search_database",
    "description": "Search the database for records.",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
}


def search_database(query: str) -> str:
    # Simulate persistent failure
    raise ConnectionError("Database unreachable: connection refused")


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    tool_failures: dict[str, int] = defaultdict(int)
    exhausted_tools: set[str] = set()

    while True:
        # Remove exhausted tools from the available set
        available_tools = [t for t in [DB_TOOL] if t["name"] not in exhausted_tools]

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=available_tools if available_tools else [],
            messages=messages,
        )

        if response.stop_reason in ("end_turn", "stop_sequence"):
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason != "tool_use":
            return f"Unexpected stop reason: {response.stop_reason}"

        tool_call = next(b for b in response.content if b.type == "tool_use")
        tool_name = tool_call.name
        messages.append({"role": "assistant", "content": response.content})

        # Check if this tool is already exhausted
        if tool_name in exhausted_tools:
            tool_result = json.dumps({
                "error": f"Tool '{tool_name}' is unavailable after {MAX_TOOL_RETRIES} failures. Please answer without it."
            })
        else:
            try:
                if tool_name == "search_database":
                    result = search_database(tool_call.input["query"])
                    tool_result = result
                    tool_failures[tool_name] = 0   # reset on success
                else:
                    tool_result = json.dumps({"error": f"Unknown tool: {tool_name}"})
            except Exception as exc:
                tool_failures[tool_name] += 1
                attempts = tool_failures[tool_name]
                print(f"  [{tool_name}] failure {attempts}/{MAX_TOOL_RETRIES}: {exc}")

                if attempts >= MAX_TOOL_RETRIES:
                    exhausted_tools.add(tool_name)
                    tool_result = json.dumps({
                        "error": (
                            f"Tool '{tool_name}' failed {attempts} times with: {exc}. "
                            "It is now disabled. Please provide your best answer without this tool, "
                            "or ask the user for an alternative."
                        )
                    })
                else:
                    tool_result = json.dumps({"error": str(exc), "retry_hint": "You may try again."})

        messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_call.id, "content": tool_result}],
        })


result = run_agent("Find all orders placed in the last 7 days.")
print(f"Final answer: {result}")
```

**Expected Token Savings:** Hard exit after 3 failures prevents infinite loops — caps wasted tokens at 3 × (tool call + error result) ≈ 600 tokens vs. an unbounded loop that can consume the entire context window (200,000+ tokens).

**Environment:** Any agent with external tool dependencies that may be unavailable; Python 3.10+.

---

## Option 2 — Maximum total turn limit with graceful degradation

**Cap the total number of agent turns. When the limit is reached, instruct the model to answer with whatever it has.**

```python
import json
import anthropic

client = anthropic.Anthropic()

MAX_TURNS = 10   # absolute ceiling on agentic turns

TOOLS = [
    {
        "name": "fetch_data",
        "description": "Fetch data from the external API.",
        "input_schema": {
            "type": "object",
            "properties": {"endpoint": {"type": "string"}},
            "required": ["endpoint"],
        },
    }
]


def fetch_data(endpoint: str) -> str:
    raise TimeoutError(f"API timeout for {endpoint}")


def run_bounded_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    turn = 0

    while turn < MAX_TURNS:
        turn += 1
        print(f"  Turn {turn}/{MAX_TURNS}")

        # On the final turn, force end_turn by removing tools
        is_last_turn = (turn == MAX_TURNS)
        tools = [] if is_last_turn else TOOLS

        extra_instruction = ""
        if is_last_turn:
            extra_instruction = (
                "\n\n[System: Maximum turns reached. You must now provide your best answer "
                "based on available information. Do not attempt any more tool calls.]"
            )
            # Inject the instruction into the latest user message
            if messages and messages[-1]["role"] == "user":
                last = messages[-1]["content"]
                if isinstance(last, str):
                    messages[-1] = {"role": "user", "content": last + extra_instruction}

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason in ("end_turn", "stop_sequence"):
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason != "tool_use" or is_last_turn:
            return next(
                (b.text for b in response.content if hasattr(b, "text")),
                "I was unable to complete the task within the allowed steps.",
            )

        tool_call = next(b for b in response.content if b.type == "tool_use")
        messages.append({"role": "assistant", "content": response.content})

        try:
            result = fetch_data(tool_call.input["endpoint"])
            tool_result = result
        except Exception as exc:
            tool_result = json.dumps({"error": str(exc)})

        messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_call.id, "content": tool_result}],
        })

    return "Task exceeded maximum turn limit."


answer = run_bounded_agent("Pull the latest sales figures from the reporting API.")
print(f"Answer: {answer}")
```

**Expected Token Savings:** A 10-turn cap limits any runaway loop to at most 10 round-trips. For a 200k context window, this prevents consuming more than ~20,000 tokens in a stuck loop vs. potentially hundreds of thousands without a limit.

**Environment:** General-purpose agents; recommended as a universal safety net regardless of other error handling.

---

## Option 3 — Error pattern detector that identifies loops and breaks them

**Detect when the same tool is called with the same arguments multiple times and inject a loop-breaking instruction.**

```python
import hashlib
import json
from collections import Counter
import anthropic

client = anthropic.Anthropic()

LOOP_THRESHOLD = 2   # identical calls before intervention


def call_signature(tool_name: str, tool_input: dict) -> str:
    return hashlib.sha256(
        json.dumps({"name": tool_name, "input": tool_input}, sort_keys=True).encode()
    ).hexdigest()[:12]


SEARCH_TOOL = {
    "name": "web_search",
    "description": "Search the web.",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
}


def web_search(query: str) -> str:
    raise RuntimeError("Search service down for maintenance")


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    call_counts: Counter = Counter()
    blocked_sigs: set[str] = set()

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=[SEARCH_TOOL],
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason != "tool_use":
            return f"Done ({response.stop_reason})"

        tool_call = next(b for b in response.content if b.type == "tool_use")
        sig = call_signature(tool_call.name, tool_call.input)
        call_counts[sig] += 1
        messages.append({"role": "assistant", "content": response.content})

        if call_counts[sig] > LOOP_THRESHOLD:
            blocked_sigs.add(sig)
            print(f"  Loop detected: {tool_call.name}({tool_call.input}) called {call_counts[sig]}× — breaking.")
            tool_result = json.dumps({
                "error": (
                    f"Loop detected: this exact call has been attempted {call_counts[sig]} times "
                    f"and failed each time. Stop retrying this approach. "
                    f"Either answer from your own knowledge, ask the user for clarification, "
                    f"or try a completely different strategy."
                )
            })
        else:
            try:
                tool_result = web_search(tool_call.input["query"])
            except Exception as exc:
                tool_result = json.dumps({"error": str(exc)})

        messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_call.id, "content": tool_result}],
        })


answer = run_agent("What is the current gold price per ounce?")
print(f"Answer: {answer}")
```

**Expected Token Savings:** Loop detection triggers after exactly `LOOP_THRESHOLD + 1` identical calls — caps wasted tokens at ~3 × round-trip cost (~900 tokens) for any specific loop pattern, regardless of context window size.

**Environment:** Agents prone to semantic loops where the model rephrases the same underlying call; effective for search and retrieval tools.

---

## Option 4 — Fallback tool chain: try primary → secondary → graceful decline

**Register an ordered list of fallback tools. On primary tool failure, automatically switch to the next alternative.**

```python
import json
import anthropic

client = anthropic.Anthropic()

PRIMARY_TOOL = {
    "name": "query_live_database",
    "description": "Query the live production database (preferred).",
    "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
}
FALLBACK_TOOL = {
    "name": "query_read_replica",
    "description": "Query the read replica (use if live database is unavailable).",
    "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
}
CACHE_TOOL = {
    "name": "query_cached_snapshot",
    "description": "Query a cached data snapshot from last hour (use if both databases are down).",
    "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
}

FALLBACK_CHAIN = [PRIMARY_TOOL, FALLBACK_TOOL, CACHE_TOOL]
_disabled: set[str] = set()


def execute_tool(name: str, args: dict) -> str:
    if name == "query_live_database":
        raise ConnectionError("Primary DB: connection refused")
    if name == "query_read_replica":
        raise ConnectionError("Read replica: replication lag too high")
    if name == "query_cached_snapshot":
        return json.dumps({"rows": [{"order_id": 1, "total": 99.99}], "note": "data from 45 min ago"})
    return json.dumps({"error": f"Unknown tool: {name}"})


def run_agent_with_fallback(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        available = [t for t in FALLBACK_CHAIN if t["name"] not in _disabled]

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=available if available else [],
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason != "tool_use":
            return next(
                (b.text for b in response.content if hasattr(b, "text")),
                "Unable to retrieve data — all sources exhausted.",
            )

        tool_call = next(b for b in response.content if b.type == "tool_use")
        messages.append({"role": "assistant", "content": response.content})

        try:
            result = execute_tool(tool_call.name, tool_call.input)
            tool_result = result
        except Exception as exc:
            _disabled.add(tool_call.name)
            print(f"  [{tool_call.name}] failed: {exc} — disabling, next: {[t['name'] for t in available if t['name'] != tool_call.name]}")
            tool_result = json.dumps({
                "error": str(exc),
                "instruction": (
                    f"'{tool_call.name}' is unavailable. "
                    f"Please try the next available data source."
                ),
            })

        messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_call.id, "content": tool_result}],
        })


answer = run_agent_with_fallback("Show me recent orders.")
print(f"Answer: {answer}")
```

**Expected Token Savings:** Fallback chains resolve the request without a full re-prompt — saves the ~2,000 tokens of a fresh conversation start while still delivering an answer from an alternative source.

**Environment:** Production agents with redundant data sources; database agents, API agents.

---

## Option 5 — Error budget: track total failures and degrade gracefully

**Maintain a global error budget across all tools. When the budget is exhausted, switch to degraded mode and answer from model knowledge only.**

```python
import json
import anthropic

client = anthropic.Anthropic()

ERROR_BUDGET = 5   # total tool failures allowed per session

TOOLS = [
    {"name": "get_stock_price",  "description": "Get current stock price.",  "input_schema": {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]}},
    {"name": "get_company_info", "description": "Get company information.", "input_schema": {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]}},
]


def call_tool(name: str, args: dict) -> str:
    raise RuntimeError(f"Market data API offline: {name}")


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    errors_remaining = ERROR_BUDGET
    degraded_mode = False

    while True:
        if degraded_mode:
            # Force an end_turn by removing all tools and asking for best-effort answer
            messages.append({
                "role": "user",
                "content": (
                    "[System: All tool calls have failed. Answer from your training knowledge. "
                    "Clearly state that data may not be current.]"
                ),
            })
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                messages=messages,
            )
            return next(b.text for b in response.content if hasattr(b, "text"))

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason != "tool_use":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        tool_call = next(b for b in response.content if b.type == "tool_use")
        messages.append({"role": "assistant", "content": response.content})

        try:
            result = call_tool(tool_call.name, tool_call.input)
            tool_result = result
        except Exception as exc:
            errors_remaining -= 1
            print(f"  Error ({errors_remaining} budget remaining): {exc}")
            if errors_remaining <= 0:
                degraded_mode = True
                tool_result = json.dumps({
                    "error": str(exc),
                    "warning": "Error budget exhausted. Switching to degraded mode.",
                })
            else:
                tool_result = json.dumps({
                    "error": str(exc),
                    "errors_remaining": errors_remaining,
                })

        messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_call.id, "content": tool_result}],
        })


answer = run_agent("What is the current P/E ratio for AAPL and MSFT?")
print(f"Answer: {answer}")
```

**Expected Token Savings:** Error budget caps total tool-related turns at `ERROR_BUDGET` across all tools — prevents any combination of failing tools from exceeding the budget and looping forever.

**Environment:** Agents using multiple external APIs that may all be down simultaneously (market data, weather, news).

---

## Option 6 — Timeout-based loop breaker using `asyncio.wait_for`

**Wrap the entire agentic loop in a timeout. If the loop doesn't complete within N seconds, return the best partial answer.**

```python
import asyncio
import json
import anthropic

client = anthropic.AsyncAnthropic()

AGENT_TIMEOUT = 30   # seconds

TOOL = {
    "name": "slow_api_call",
    "description": "Call the external data API.",
    "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
}


async def call_tool(query: str) -> str:
    await asyncio.sleep(100)   # simulate hanging API
    return json.dumps({"data": "some result"})


async def _agent_loop(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    partial_answer = "I was unable to complete the request within the allowed time."

    for turn in range(20):   # also have a turn limit
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=[TOOL],
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            text = next((b.text for b in response.content if hasattr(b, "text")), "")
            if text:
                partial_answer = text
            break

        if response.stop_reason != "tool_use":
            break

        tool_call = next(b for b in response.content if b.type == "tool_use")
        messages.append({"role": "assistant", "content": response.content})

        try:
            result = await asyncio.wait_for(
                call_tool(tool_call.input["query"]),
                timeout=8,   # per-tool timeout
            )
            tool_result = result
        except asyncio.TimeoutError:
            tool_result = json.dumps({
                "error": "Tool call timed out after 8 seconds. Try a different approach or answer without this data."
            })
        except Exception as exc:
            tool_result = json.dumps({"error": str(exc)})

        messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_call.id, "content": tool_result}],
        })

    return partial_answer


async def run_agent(user_message: str) -> str:
    try:
        return await asyncio.wait_for(_agent_loop(user_message), timeout=AGENT_TIMEOUT)
    except asyncio.TimeoutError:
        print(f"  Agent loop timed out after {AGENT_TIMEOUT}s — returning partial answer.")
        return "The request could not be completed within the time limit. Please try again."


answer = asyncio.run(run_agent("Fetch the latest analytics report."))
print(f"Answer: {answer}")
```

**Expected Token Savings:** Wall-clock timeout is a universal exit condition that works regardless of why the loop is stuck — prevents the agent from consuming tokens for 60+ seconds on a hanging network call, saving 5,000–20,000 tokens in a typical runaway scenario.

**Environment:** asyncio agents; especially important for tools that make slow external HTTP calls; Python 3.10+.

---

## Comparison

| Option | Exit Trigger | Covers Infinite Loop | Covers Timeout | Complexity |
|--------|-------------|---------------------|---------------|------------|
| 1. Per-tool retry counter | N failures on same tool | Yes | No | Low |
| 2. Total turn limit | N total turns | Yes | No | Very Low |
| 3. Loop pattern detector | Identical call repeated N× | Yes | No | Low |
| 4. Fallback tool chain | Primary failure → secondary | Partial | No | Medium |
| 5. Error budget | N total failures | Yes | No | Low |
| 6. Async timeout | Wall-clock limit | Yes | Yes | Medium |

**Recommended path:** Apply Option 2 (turn limit) as a universal safety net — it's a 3-line change that prevents all runaway loops. Add Option 1 (per-tool counter) for tool-specific granularity. Use Option 6 (timeout) for asyncio agents where tools may hang on slow external calls.
