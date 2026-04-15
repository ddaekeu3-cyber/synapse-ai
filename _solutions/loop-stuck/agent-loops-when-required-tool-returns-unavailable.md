---
layout: solution
title: "Agent Loops When Required Tool Returns Unavailable"
category: loop-stuck
description: "Agent repeatedly calls a tool that returns 'service unavailable' and cannot exit the loop because its plan has no fallback path when a required tool is inaccessible."
tags: [loop-stuck, tool-failure, fallback, reliability, production]
---

## Symptom

A tool returns `{"status": "unavailable", "message": "Search service is down"}`. The agent tries the same call again, gets the same response, and enters an infinite loop of `tool_call → unavailable → tool_call → ...`. After exhausting its turn budget, the agent either returns a garbled response or crashes with a max-iteration error. The user sees no output and the agent consumed tokens for every failed call.

## Root Cause

The agent's plan was formulated assuming the tool is available. When the tool signals unavailability (rather than a data error), the agent interprets this as a transient failure and retries — which is correct behaviour for a 503 that might recover. But without a circuit breaker or a maximum-unavailability threshold, the agent has no escape hatch. The fix is a combination of: (1) structured unavailability signals, (2) an explicit retry limit, and (3) a fallback action path.

## Fix

### Option 1 — Explicit unavailability signal with fallback instruction in prompt

```python
import anthropic
import json

client = anthropic.Anthropic()

# Simulate a tool that is sometimes unavailable
_tool_call_count = 0

def search_knowledge_base(query: str) -> dict:
    global _tool_call_count
    _tool_call_count += 1
    if _tool_call_count <= 2:  # simulate two unavailable responses
        return {
            "status":   "unavailable",
            "code":     "SERVICE_DOWN",
            "message":  "Knowledge base search is temporarily unavailable.",
            "retry_ok": False,       # <-- explicit signal: do not retry
            "fallback": "Answer from your training knowledge instead.",
        }
    return {"status": "ok", "results": ["Result A", "Result B"]}

tools = [
    {
        "name": "search_knowledge_base",
        "description": (
            "Search the internal knowledge base. If the response has status='unavailable' "
            "and retry_ok=false, do NOT retry — use your general knowledge to answer instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }
]

def agent_loop(user_msg: str) -> str:
    messages = [{"role": "user", "content": user_msg}]
    for turn in range(6):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )
        if resp.stop_reason != "tool_use":
            return resp.content[0].text
        messages.append({"role": "assistant", "content": resp.content})
        tool_results = []
        for block in resp.content:
            if block.type == "tool_use":
                result = search_knowledge_base(block.input.get("query", ""))
                print(f"[tool] attempt {turn+1}: {result['status']}")
                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": block.id,
                    "content":     json.dumps(result),
                })
        messages.append({"role": "user", "content": tool_results})
    return "Max turns reached"

answer = agent_loop("What is the recommended database schema for multi-tenant SaaS?")
print(f"[agent] {answer[:200]}")
```

**Expected Token Savings:** `retry_ok: false` stops the retry loop immediately — the agent uses general knowledge (zero extra API calls) instead of burning tokens on repeated unavailable calls.
**Environment:** Any agent with tool dependencies; the `retry_ok` flag is the cheapest possible fallback signal.

---

### Option 2 — Client-side retry counter with hard stop

```python
import anthropic
import json

client = anthropic.Anthropic()

_unavailable_count: dict[str, int] = {}
MAX_UNAVAILABLE_RETRIES = 2

def call_tool_with_limit(tool_name: str, tool_fn, *args, **kwargs) -> dict:
    """Wrap a tool call with an unavailability counter."""
    if _unavailable_count.get(tool_name, 0) >= MAX_UNAVAILABLE_RETRIES:
        return {
            "status":  "permanently_unavailable",
            "message": f"{tool_name} has been unavailable for {MAX_UNAVAILABLE_RETRIES} consecutive calls. "
                       f"Stop calling it and use an alternative approach.",
            "do_not_retry": True,
        }
    result = tool_fn(*args, **kwargs)
    if result.get("status") == "unavailable":
        _unavailable_count[tool_name] = _unavailable_count.get(tool_name, 0) + 1
        print(f"[limit] {tool_name} unavailable ({_unavailable_count[tool_name]}/{MAX_UNAVAILABLE_RETRIES})")
    else:
        _unavailable_count[tool_name] = 0  # reset on success
    return result

# Simulated tools
def weather_api(location: str) -> dict:
    return {"status": "unavailable", "message": "Weather service is down for maintenance."}

def news_search(query: str) -> dict:
    return {"status": "ok", "results": [f"News about {query}: latest developments..."]}

tools = [
    {"name": "get_weather",
     "description": "Get weather for a location. If permanently_unavailable, describe weather generally instead.",
     "input_schema": {"type": "object", "properties": {"location": {"type": "string"}}, "required": ["location"]}},
    {"name": "search_news",
     "description": "Search recent news.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
]

DISPATCH = {
    "get_weather": lambda i: call_tool_with_limit("get_weather", weather_api, i["location"]),
    "search_news": lambda i: call_tool_with_limit("search_news", news_search, i["query"]),
}

def agent_loop(user_msg: str) -> str:
    messages = [{"role": "user", "content": user_msg}]
    for _ in range(10):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=512, tools=tools, messages=messages
        )
        if resp.stop_reason != "tool_use":
            return resp.content[0].text
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                result = DISPATCH[block.name](block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
        messages.append({"role": "user", "content": results})
    return "Max turns reached"

answer = agent_loop("What's the weather in Tokyo and any relevant news about it?")
print(f"[agent] {answer[:200]}")
```

**Expected Token Savings:** Hard stop after 2 unavailable calls prevents unbounded retry loops; the `do_not_retry: true` flag gives the agent a clear instruction to route around the broken tool.
**Environment:** Tools with known failure modes (external APIs, internal services with maintenance windows); essential for agents that run unattended overnight.

---

### Option 3 — Fallback tool chain: primary → secondary → default

```python
import anthropic
import json

client = anthropic.Anthropic()

# Three levels of fallback for the same logical operation
def primary_search(query: str) -> dict:
    return {"status": "unavailable", "source": "primary"}

def secondary_search(query: str) -> dict:
    return {"status": "unavailable", "source": "secondary"}

def default_search(query: str) -> dict:
    return {"status": "ok", "source": "default_cache", "results": [f"Cached result for: {query}"]}

def search_with_fallback(query: str) -> dict:
    """Try primary → secondary → default cache in order."""
    for fn, label in [(primary_search, "primary"), (secondary_search, "secondary"), (default_search, "cache")]:
        result = fn(query)
        print(f"[fallback] trying {label}: {result['status']}")
        if result["status"] == "ok":
            result["source_used"] = label
            return result
    return {"status": "all_sources_unavailable", "message": "No search sources available. Answer from memory."}

tools = [
    {
        "name": "search",
        "description": (
            "Search for information. Automatically tries primary, secondary, and cache sources. "
            "If all return unavailable, answer from your training knowledge."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }
]

def agent_loop(user_msg: str) -> str:
    messages = [{"role": "user", "content": user_msg}]
    for _ in range(6):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=512, tools=tools, messages=messages
        )
        if resp.stop_reason != "tool_use":
            return resp.content[0].text
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                result = search_with_fallback(block.input["query"])
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
        messages.append({"role": "user", "content": results})
    return "Max turns"

answer = agent_loop("Find information about Python 3.12 release notes.")
print(f"[agent] {answer[:200]}")
```

**Expected Token Savings:** The agent always gets a result (even a cached or general-knowledge one) — no retry loop, no wasted turns; the fallback chain is transparent to the agent's planning.
**Environment:** Agents with redundant data sources; production systems where degraded operation is acceptable but total failure is not.

---

### Option 4 — Circuit breaker state machine

```python
import anthropic
import json
import time

client = anthropic.Anthropic()

class CircuitBreaker:
    """
    States: CLOSED (normal) → OPEN (failing) → HALF_OPEN (testing recovery)
    """
    def __init__(self, failure_threshold: int = 2, recovery_timeout: float = 30.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout  = recovery_timeout
        self.failures:   int   = 0
        self.state:      str   = "CLOSED"
        self.opened_at:  float = 0.0

    def call(self, fn, *args, **kwargs) -> dict:
        if self.state == "OPEN":
            if time.monotonic() - self.opened_at > self.recovery_timeout:
                self.state = "HALF_OPEN"
                print("[circuit] HALF_OPEN — testing recovery")
            else:
                return {
                    "status":       "circuit_open",
                    "message":      "Service circuit is open — tool is unavailable. Use alternative approach.",
                    "retry_after":  int(self.recovery_timeout - (time.monotonic() - self.opened_at)),
                    "do_not_retry": True,
                }

        try:
            result = fn(*args, **kwargs)
            if result.get("status") == "unavailable":
                raise RuntimeError("unavailable")
            if self.state == "HALF_OPEN":
                self.state    = "CLOSED"
                self.failures = 0
                print("[circuit] CLOSED — service recovered")
            return result
        except Exception as e:
            self.failures += 1
            if self.failures >= self.failure_threshold or self.state == "HALF_OPEN":
                self.state     = "OPEN"
                self.opened_at = time.monotonic()
                print(f"[circuit] OPEN after {self.failures} failures")
            return {
                "status":       "unavailable",
                "message":      f"Service failed: {e}",
                "circuit_state": self.state,
            }

# One circuit breaker per tool
_circuit = CircuitBreaker(failure_threshold=2, recovery_timeout=5.0)

def external_api(query: str) -> dict:
    return {"status": "unavailable"}  # simulated down service

tools = [{"name": "query_api",
          "description": "Query external API. If circuit_open or do_not_retry=true, answer from memory.",
          "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}]

def agent_loop(user_msg: str) -> str:
    messages = [{"role": "user", "content": user_msg}]
    for _ in range(8):
        resp = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=512, tools=tools, messages=messages)
        if resp.stop_reason != "tool_use":
            return resp.content[0].text
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                result = _circuit.call(external_api, block.input["query"])
                print(f"[circuit] state={_circuit.state} result={result['status']}")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
        messages.append({"role": "user", "content": results})
    return "Max turns"

answer = agent_loop("What are the latest Python best practices?")
print(f"[agent] {answer[:200]}")
```

**Expected Token Savings:** Circuit breaker opens after 2 failures and returns immediately on subsequent calls — zero additional tool invocations until recovery timeout; the agent pivots to general knowledge in 1 turn instead of retrying indefinitely.
**Environment:** External API dependencies (search, weather, stock data); multi-agent systems where one agent's tool failure shouldn't cascade.

---

### Option 5 — Unavailability budget: max tokens per unavailable tool

```python
import anthropic
import json

client = anthropic.Anthropic()

class UnavailabilityBudget:
    """Track tokens spent on unavailable tool calls; abort when budget exceeded."""

    def __init__(self, max_tokens_on_unavailable: int = 500):
        self.max_tokens = max_tokens_on_unavailable
        self.spent:  dict[str, int] = {}

    def record(self, tool_name: str, tokens_used: int, was_unavailable: bool) -> dict | None:
        if was_unavailable:
            self.spent[tool_name] = self.spent.get(tool_name, 0) + tokens_used
            if self.spent[tool_name] >= self.max_tokens:
                return {
                    "status":          "budget_exceeded",
                    "tool":            tool_name,
                    "tokens_wasted":   self.spent[tool_name],
                    "message":         f"Stop calling {tool_name} — it has consumed {self.spent[tool_name]} tokens while unavailable. Use an alternative.",
                    "do_not_retry":    True,
                }
        else:
            self.spent.pop(tool_name, None)  # reset on success
        return None

budget = UnavailabilityBudget(max_tokens_on_unavailable=200)

_call_n = 0
def flaky_tool(query: str) -> dict:
    global _call_n
    _call_n += 1
    return {"status": "unavailable", "message": "Tool is down."}

tools = [{"name": "flaky_search",
          "description": "Search tool. If budget_exceeded, do not retry — answer from memory.",
          "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}]

def agent_loop(user_msg: str) -> str:
    messages = [{"role": "user", "content": user_msg}]
    for _ in range(10):
        resp = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=512, tools=tools, messages=messages)
        if resp.stop_reason != "tool_use":
            return resp.content[0].text
        messages.append({"role": "assistant", "content": resp.content})
        tokens_this_turn = resp.usage.input_tokens + resp.usage.output_tokens
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                raw = flaky_tool(block.input["query"])
                budget_violation = budget.record("flaky_search", tokens_this_turn, raw["status"] == "unavailable")
                result = budget_violation if budget_violation else raw
                print(f"[budget] spent={budget.spent.get('flaky_search', 0)} tokens on unavailable calls")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
        messages.append({"role": "user", "content": results})
    return "Max turns"

answer = agent_loop("Find information about machine learning model compression.")
print(f"[agent] {answer[:200]}")
```

**Expected Token Savings:** Token budget makes the cost of unavailability explicit; once the budget is exceeded the loop stops unconditionally — bounded token waste regardless of how long the service is down.
**Environment:** Cost-sensitive production agents; agents with token budgets per request; multi-step pipelines where one stuck tool shouldn't consume the entire session budget.

---

### Option 6 — Graceful degradation: partial answer from available tools

```python
import anthropic
import json

client = anthropic.Anthropic()

# Some tools available, some not
TOOL_STATUS = {
    "search_web":      "unavailable",
    "search_local_kb": "ok",
    "get_metadata":    "ok",
}

def call_tool(name: str, query: str) -> dict:
    if TOOL_STATUS.get(name) == "unavailable":
        return {"status": "unavailable", "tool": name,
                "message": f"{name} is down. Other tools may still be available."}
    return {"status": "ok", "tool": name, "results": [f"Result from {name} for: {query}"]}

tools = [
    {"name": "search_web",
     "description": "Search the internet. May be unavailable — use search_local_kb as fallback.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "search_local_kb",
     "description": "Search internal knowledge base. Usually available.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "get_metadata",
     "description": "Get metadata about a topic.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
]

DISPATCH = {t["name"]: lambda i, n=t["name"]: call_tool(n, i["query"]) for t in tools}

def agent_loop(user_msg: str) -> str:
    # Inform the agent upfront which tools are unavailable
    unavailable = [name for name, status in TOOL_STATUS.items() if status == "unavailable"]
    system = (
        f"Unavailable tools today: {', '.join(unavailable) or 'none'}. "
        f"Do not call unavailable tools. Use available alternatives and provide the best partial answer you can."
    )
    messages = [{"role": "user", "content": user_msg}]

    for _ in range(8):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            tools=tools,
            messages=messages,
        )
        if resp.stop_reason != "tool_use":
            return resp.content[0].text
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                result = DISPATCH[block.name](block.input)
                print(f"[tool] {block.name}: {result['status']}")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
        messages.append({"role": "user", "content": results})
    return "Max turns"

answer = agent_loop("Research the latest trends in AI model training and give me a summary.")
print(f"[agent] {answer[:300]}")
```

**Expected Token Savings:** Declaring unavailable tools in the system prompt prevents the agent from attempting them at all — zero failed tool calls, zero retry tokens; the agent self-routes to available alternatives from turn 1.
**Environment:** Agents running during known maintenance windows; any agent with a health-check or status-page integration that can populate the unavailability list at startup.

---

## Comparison

| Option | Loop Prevention | Extra API Calls | Fallback Path | Automatic | Best For |
|---|---|---|---|---|---|
| 1. retry_ok flag | Via agent instruction | 0 | Via prompt | Partial | Simple tools with controllable responses |
| 2. Client-side counter | Hard stop after N | 0 | do_not_retry signal | Yes | Unattended agents; bounded retry |
| 3. Fallback chain | Never exhausts | 0 (within tool) | Primary→secondary→cache | Yes | Redundant data sources |
| 4. Circuit breaker | OPEN state | 0 | General knowledge | Yes | External APIs; cascading failure prevention |
| 5. Token budget | Budget exhausted | 0 | do_not_retry | Yes | Cost-sensitive; token-budgeted requests |
| 6. Upfront declaration | Never attempts bad tool | 0 | Available tools used | Yes | Planned maintenance; health-check integration |
