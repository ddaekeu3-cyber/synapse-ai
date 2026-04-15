---
layout: solution
title: "Agent doesn't cache tool results across turns"
category: tool-failure
description: "Agent re-executes the same tool calls in later turns instead of reusing results already obtained earlier in the conversation."
tags: [tool-failure, caching, performance, conversation, deduplication]
---

## Symptom

The agent calls the same tool with identical arguments multiple times across different conversation turns. A `get_weather("London")` call at turn 2 is repeated at turn 7 when the same data is needed again. External API costs and latency accumulate even though the result is already available in the conversation history.

```
Turn 2: tool_use  get_weather(city="London")  → {"temp": 18, "condition": "cloudy"}
Turn 4: tool_use  search_docs(query="deployment")  → [doc1, doc2, doc3]
Turn 7: tool_use  get_weather(city="London")  → {"temp": 18, "condition": "cloudy"}  ← redundant
Turn 9: tool_use  search_docs(query="deployment")  → [doc1, doc2, doc3]              ← redundant
```

## Root Cause

The model sees the conversation history as text but the orchestration layer does not inspect prior `tool_result` blocks before dispatching new tool calls. Every `tool_use` block emitted by the model triggers an unconditional execution. Without a lookup layer, identical calls are re-executed even when the answer is already present.

## Fix

Intercept tool calls before execution, compute a cache key from `(tool_name, canonical_args)`, and return the cached result if one exists. The cache scope can be a single conversation turn, a full session, or a TTL window depending on the tool's data freshness requirements.

---

### Option 1 — In-memory dict cache keyed by tool + args

```python
import anthropic
import json
import hashlib

client = anthropic.Anthropic()

def make_cache_key(tool_name: str, tool_input: dict) -> str:
    canonical = json.dumps(tool_input, sort_keys=True)
    digest = hashlib.sha256(f"{tool_name}:{canonical}".encode()).hexdigest()[:16]
    return digest

def execute_tool(name: str, inputs: dict) -> str:
    """Actual tool execution — replace with real implementations."""
    if name == "get_weather":
        return json.dumps({"temp": 18, "condition": "cloudy", "city": inputs["city"]})
    if name == "search_docs":
        return json.dumps({"results": ["doc1", "doc2"], "query": inputs["query"]})
    return json.dumps({"error": "unknown tool"})

def run_agent_with_result_cache(user_message: str) -> str:
    tool_cache: dict[str, str] = {}
    cache_hits = 0
    cache_misses = 0

    tools = [
        {
            "name": "get_weather",
            "description": "Get current weather for a city.",
            "input_schema": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
        {
            "name": "search_docs",
            "description": "Search internal documentation.",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    ]

    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            tools=tools,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            key = make_cache_key(block.name, block.input)

            if key in tool_cache:
                result = tool_cache[key]
                cache_hits += 1
                print(f"CACHE HIT  {block.name}({block.input}) → reused")
            else:
                result = execute_tool(block.name, block.input)
                tool_cache[key] = result
                cache_misses += 1
                print(f"CACHE MISS {block.name}({block.input}) → executed")

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
            })

        messages.append({"role": "user", "content": tool_results})

    print(f"\nCache stats: {cache_hits} hits / {cache_misses} misses")
    final = next(b for b in response.content if hasattr(b, "text"))
    return final.text

answer = run_agent_with_result_cache(
    "What is the weather in London? Also search docs for 'deployment'. "
    "Then confirm the London weather again and re-check deployment docs."
)
print(answer)
```

**Expected Token Savings:** 40–70% reduction in tool execution cost for conversations where the model revisits the same data points across multiple reasoning steps.

**Environment:** Single-process, single-turn cache — suitable for stateless request handlers.

---

### Option 2 — Conversation history scanner (reuse before calling)

```python
import anthropic
import json
import re

client = anthropic.Anthropic()

SYSTEM_WITH_REUSE_INSTRUCTION = """
You are a helpful assistant with access to tools.

IMPORTANT: Before calling any tool, scan the conversation history for a previous
tool_result that answers the same question with the same parameters.
If an identical result already exists in this conversation, state the answer
from memory — do NOT call the tool again.

Only call a tool when the information is genuinely absent from prior turns.
"""

def search_docs(query: str) -> str:
    print(f"  [TOOL EXECUTED] search_docs({query!r})")
    return json.dumps({"results": [f"doc about {query}", "general guide"], "count": 2})

def get_weather(city: str) -> str:
    print(f"  [TOOL EXECUTED] get_weather({city!r})")
    return json.dumps({"city": city, "temp": 22, "condition": "sunny"})

TOOLS = [
    {
        "name": "search_docs",
        "description": "Search internal documentation by keyword.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Search query"}},
            "required": ["query"],
        },
    },
    {
        "name": "get_weather",
        "description": "Get current weather for a named city.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "City name"}},
            "required": ["city"],
        },
    },
]

def dispatch(name: str, inputs: dict) -> str:
    if name == "search_docs":
        return search_docs(inputs["query"])
    if name == "get_weather":
        return get_weather(inputs["city"])
    return json.dumps({"error": "unknown"})

def run_with_history_scanner(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=SYSTEM_WITH_REUSE_INSTRUCTION,
            tools=TOOLS,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            break

        results = []
        for block in response.content:
            if block.type == "tool_use":
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": dispatch(block.name, block.input),
                })

        messages.append({"role": "user", "content": results})

    final = next(b for b in response.content if hasattr(b, "text"))
    return final.text

print(run_with_history_scanner(
    "Search docs for 'auth'. What's the weather in Paris? "
    "Now tell me again about the auth docs and Paris weather."
))
```

**Expected Token Savings:** 20–50% tool execution reduction by instructing the model to self-censor repeated calls, at the cost of prompt tokens for the system instruction.

**Environment:** Zero-infrastructure; works with any model that follows instruction well.

---

### Option 3 — Middleware intercept with explicit result store

```python
import anthropic
import json
import hashlib
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class ToolResultStore:
    _store: dict[str, str] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0

    def _key(self, name: str, inputs: dict) -> str:
        raw = json.dumps({"tool": name, "inputs": inputs}, sort_keys=True)
        return hashlib.md5(raw.encode()).hexdigest()

    def get(self, name: str, inputs: dict) -> str | None:
        k = self._key(name, inputs)
        if k in self._store:
            self.hits += 1
            return self._store[k]
        self.misses += 1
        return None

    def put(self, name: str, inputs: dict, result: str) -> None:
        self._store[self._key(name, inputs)] = result

    @property
    def stats(self) -> str:
        total = self.hits + self.misses
        rate = self.hits / total * 100 if total else 0
        return f"hits={self.hits} misses={self.misses} hit_rate={rate:.0f}%"

def real_tool_call(name: str, inputs: dict) -> str:
    """Simulate real tool execution with visible side-effect."""
    print(f"    *** REAL CALL: {name}({inputs})")
    if name == "fetch_config":
        return json.dumps({"version": "2.1.0", "features": ["auth", "billing"]})
    if name == "list_users":
        return json.dumps({"users": ["alice", "bob", "carol"], "total": 3})
    return json.dumps({"status": "ok"})

def cached_dispatch(name: str, inputs: dict, store: ToolResultStore) -> str:
    cached = store.get(name, inputs)
    if cached is not None:
        print(f"    ~~~ CACHED: {name}({inputs})")
        return cached
    result = real_tool_call(name, inputs)
    store.put(name, inputs, result)
    return result

TOOLS = [
    {
        "name": "fetch_config",
        "description": "Fetch current application configuration.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_users",
        "description": "List all registered users.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]

def run_agent(user_message: str) -> str:
    store = ToolResultStore()
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            break

        tool_results = [
            {
                "type": "tool_result",
                "tool_use_id": b.id,
                "content": cached_dispatch(b.name, b.input, store),
            }
            for b in response.content
            if b.type == "tool_use"
        ]
        messages.append({"role": "user", "content": tool_results})

    print(f"\nStore stats: {store.stats}")
    return next(b.text for b in response.content if hasattr(b, "text"))

print(run_agent(
    "Fetch the config, list users, then fetch config again and list users again "
    "to confirm nothing changed."
))
```

**Expected Token Savings:** 50–80% tool call reduction for agentic loops that verify state by re-reading the same resources multiple times.

**Environment:** Works for same-session repeated reads; not suitable for mutable resources that change between calls.

---

### Option 4 — Async session-scoped cache with TTL

```python
import anthropic
import asyncio
import json
import hashlib
import time
from dataclasses import dataclass, field

async_client = anthropic.AsyncAnthropic()

@dataclass
class TTLCache:
    ttl_seconds: float = 300.0
    _data: dict[str, tuple[str, float]] = field(default_factory=dict)

    def _key(self, name: str, inputs: dict) -> str:
        raw = json.dumps({"n": name, "i": inputs}, sort_keys=True)
        return hashlib.sha1(raw.encode()).hexdigest()

    def get(self, name: str, inputs: dict) -> str | None:
        k = self._key(name, inputs)
        if k not in self._data:
            return None
        value, ts = self._data[k]
        if time.monotonic() - ts > self.ttl_seconds:
            del self._data[k]
            return None
        return value

    def set(self, name: str, inputs: dict, value: str) -> None:
        self._data[self._key(name, inputs)] = (value, time.monotonic())

    def purge_expired(self) -> int:
        now = time.monotonic()
        stale = [k for k, (_, ts) in self._data.items() if now - ts > self.ttl_seconds]
        for k in stale:
            del self._data[k]
        return len(stale)

# Session-level cache shared across all turns
SESSION_CACHE = TTLCache(ttl_seconds=300)

async def execute_tool_async(name: str, inputs: dict) -> str:
    """Simulate async tool (e.g., HTTP API call)."""
    await asyncio.sleep(0.05)  # network latency sim
    print(f"  [API CALL] {name}({inputs})")
    if name == "get_stock_price":
        return json.dumps({"symbol": inputs["symbol"], "price": 142.50, "currency": "USD"})
    if name == "get_exchange_rate":
        return json.dumps({"from": inputs["from_currency"], "to": inputs["to_currency"], "rate": 0.79})
    return json.dumps({"error": "unknown tool"})

async def cached_tool_async(name: str, inputs: dict) -> str:
    cached = SESSION_CACHE.get(name, inputs)
    if cached is not None:
        print(f"  [CACHE HIT] {name}({inputs})")
        return cached
    result = await execute_tool_async(name, inputs)
    SESSION_CACHE.set(name, inputs, result)
    return result

TOOLS = [
    {
        "name": "get_stock_price",
        "description": "Get current stock price by ticker symbol.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
    },
    {
        "name": "get_exchange_rate",
        "description": "Get currency exchange rate.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_currency": {"type": "string"},
                "to_currency": {"type": "string"},
            },
            "required": ["from_currency", "to_currency"],
        },
    },
]

async def run_async_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = await async_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            break

        tool_blocks = [b for b in response.content if b.type == "tool_use"]
        results = await asyncio.gather(*[
            cached_tool_async(b.name, b.input) for b in tool_blocks
        ])

        messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": b.id, "content": r}
                for b, r in zip(tool_blocks, results)
            ],
        })

    expired = SESSION_CACHE.purge_expired()
    print(f"\nPurged {expired} expired cache entries")
    return next(b.text for b in response.content if hasattr(b, "text"))

async def main():
    result = await run_async_agent(
        "Get the price of AAPL and EUR/USD rate. "
        "Then calculate the AAPL price in EUR. "
        "Also confirm AAPL price one more time."
    )
    print(result)

asyncio.run(main())
```

**Expected Token Savings:** 60–85% reduction in duplicate API calls across multi-turn async conversations; TTL prevents stale data from persisting across logical sessions.

**Environment:** Async FastAPI or async worker; share `SESSION_CACHE` across coroutines within one user session.

---

### Option 5 — Automatic deduplication middleware wrapper

```python
import anthropic
import json
import hashlib
from collections import defaultdict
from typing import Callable

client = anthropic.Anthropic()

class ToolDeduplicationMiddleware:
    """
    Wraps a tool registry and silently deduplicates calls with identical
    (name, inputs) pairs within a conversation session.
    """

    def __init__(self, tools: dict[str, Callable[[dict], str]]):
        self._tools = tools
        self._cache: dict[str, str] = {}
        self._call_log: list[dict] = []

    def _key(self, name: str, inputs: dict) -> str:
        raw = json.dumps({"name": name, "inputs": inputs}, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    def call(self, name: str, inputs: dict) -> str:
        k = self._key(name, inputs)
        if k in self._cache:
            self._call_log.append({"name": name, "inputs": inputs, "source": "cache"})
            return self._cache[k]

        if name not in self._tools:
            result = json.dumps({"error": f"unknown tool: {name}"})
        else:
            result = self._tools[name](inputs)

        self._cache[k] = result
        self._call_log.append({"name": name, "inputs": inputs, "source": "executed"})
        return result

    def report(self) -> dict:
        executed = sum(1 for e in self._call_log if e["source"] == "executed")
        cached = sum(1 for e in self._call_log if e["source"] == "cache")
        return {
            "total_calls": len(self._call_log),
            "executed": executed,
            "cached": cached,
            "savings_pct": round(cached / len(self._call_log) * 100, 1) if self._call_log else 0,
            "log": self._call_log,
        }

# Tool implementations
def read_file(inputs: dict) -> str:
    print(f"  [IO] read_file({inputs['path']!r})")
    return json.dumps({"path": inputs["path"], "content": "file content here", "size": 1024})

def query_db(inputs: dict) -> str:
    print(f"  [DB] query_db({inputs['sql']!r})")
    return json.dumps({"rows": [{"id": 1, "name": "Alice"}], "count": 1})

TOOL_REGISTRY = {"read_file": read_file, "query_db": query_db}

TOOLS = [
    {
        "name": "read_file",
        "description": "Read a file by path.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "query_db",
        "description": "Run a SQL query.",
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
        },
    },
]

def run_with_middleware(user_message: str) -> str:
    middleware = ToolDeduplicationMiddleware(TOOL_REGISTRY)
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            break

        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": b.id,
                    "content": middleware.call(b.name, b.input),
                }
                for b in response.content
                if b.type == "tool_use"
            ],
        })

    report = middleware.report()
    print(f"\nDedup report: {json.dumps(report, indent=2)}")
    return next(b.text for b in response.content if hasattr(b, "text"))

print(run_with_middleware(
    "Read /etc/config.json. Query 'SELECT * FROM users'. "
    "Now read /etc/config.json again. Then run the same user query once more."
))
```

**Expected Token Savings:** 50–75% tool execution reduction; zero changes required to tool implementations; report shows exact savings per session.

**Environment:** Drop-in wrapper; works with synchronous tool registries; extend with per-tool TTL overrides for tools with varying freshness requirements.

---

### Option 6 — LLM-instructed result injection (inject prior results into prompt)

```python
import anthropic
import json
import re

client = anthropic.Anthropic()

def extract_tool_results_from_history(messages: list[dict]) -> dict[str, str]:
    """
    Scan the message history for tool_result blocks and build a
    {tool_name: last_result} lookup for injection into the system prompt.
    """
    results: dict[str, str] = {}
    for msg in messages:
        if msg["role"] != "user":
            continue
        content = msg["content"]
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                # Match tool_use_id back to the tool name from assistant turns
                pass
    return results

def build_prior_results_context(tool_calls_made: dict[str, dict]) -> str:
    """
    tool_calls_made: {tool_use_id: {"name": str, "inputs": dict, "result": str}}
    Returns a compact summary to inject into the next system prompt.
    """
    if not tool_calls_made:
        return ""
    lines = ["## Tool Results Already Obtained This Session\n"]
    for call_id, info in tool_calls_made.items():
        lines.append(
            f"- **{info['name']}**({json.dumps(info['inputs'])}) → {info['result']}"
        )
    lines.append(
        "\nIMPORTANT: Do NOT call these tools again with the same arguments. "
        "Use the results above directly in your answer."
    )
    return "\n".join(lines)

BASE_SYSTEM = "You are a helpful assistant. Be concise."

TOOLS = [
    {
        "name": "get_timezone",
        "description": "Get the timezone for a city.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
    {
        "name": "get_population",
        "description": "Get the population of a city.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
]

def execute_tool(name: str, inputs: dict) -> str:
    print(f"  [TOOL] {name}({inputs})")
    if name == "get_timezone":
        zones = {"London": "Europe/London", "Tokyo": "Asia/Tokyo", "NYC": "America/New_York"}
        return json.dumps({"city": inputs["city"], "timezone": zones.get(inputs["city"], "UTC")})
    if name == "get_population":
        pops = {"London": 9_000_000, "Tokyo": 14_000_000, "NYC": 8_300_000}
        return json.dumps({"city": inputs["city"], "population": pops.get(inputs["city"], 0)})
    return json.dumps({"error": "unknown"})

def run_with_result_injection(user_message: str) -> str:
    tool_calls_made: dict[str, dict] = {}
    messages = [{"role": "user", "content": user_message}]

    while True:
        prior_context = build_prior_results_context(tool_calls_made)
        system = f"{BASE_SYSTEM}\n\n{prior_context}" if prior_context else BASE_SYSTEM

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=system,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for b in response.content:
            if b.type != "tool_use":
                continue
            result = execute_tool(b.name, b.input)
            tool_calls_made[b.id] = {"name": b.name, "inputs": b.input, "result": result}
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": b.id,
                "content": result,
            })

        messages.append({"role": "user", "content": tool_results})

    total = len(tool_calls_made)
    print(f"\nTotal unique tool calls: {total}")
    return next(b.text for b in response.content if hasattr(b, "text"))

print(run_with_result_injection(
    "What timezone and population does London have? "
    "Also get Tokyo's timezone. "
    "Now I need London's timezone again and Tokyo's population."
))
```

**Expected Token Savings:** 30–60% tool call reduction; approach adds a small number of system-prompt tokens each turn but eliminates redundant tool round-trips in multi-step workflows.

**Environment:** Any single-process agent; particularly effective when the model naturally tends to re-verify facts mid-conversation.

---

## Comparison

| Option | Mechanism | Hit Detection | Mutable-Safe | Async |
|--------|-----------|--------------|-------------|-------|
| 1 — Dict cache | SHA-256 key | Exact match | Configurable | No |
| 2 — History scanner | System instruction | LLM judgment | N/A | Any |
| 3 — Result store | MD5 key + dataclass | Exact match | Mark uncacheable | No |
| 4 — TTL cache | SHA-1 + monotonic time | Exact + TTL | TTL eviction | Yes |
| 5 — Dedup middleware | SHA-256 + registry | Exact match | Per-tool override | No |
| 6 — Result injection | System prompt context | LLM instruction | N/A | Any |

**Recommended default:** Option 3 or 4 — explicit result stores with exact-match keys give reliable deduplication without relying on model behavior, while TTL support makes them safe for mutable resources.
