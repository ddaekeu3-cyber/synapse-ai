---
layout: solution
title: "Agent Makes Redundant API Calls for Same Data"
category: performance
description: "Agent fetches the same external API data multiple times within a single task — driving up latency, cost, and rate-limit consumption when a single cached result would suffice."
tags: [performance, caching, deduplication, async, redis, rate-limit]
---

## Symptom

Logs show the same endpoint called 5–20 times per agent run:

```
GET /api/products/42  200  320ms
GET /api/products/42  200  318ms   ← identical
GET /api/products/42  200  315ms   ← identical again
```

Latency compounds, rate-limit budgets drain, and external API bills spike — all for data that never changed between calls.

## Root Cause

The agent calls tools without checking whether the same tool+args combination was already resolved earlier in the same task. Each tool invocation is independent; there is no deduplication layer between the model's output and the actual HTTP request.

## Fix

---

### Option 1 — In-Process Request Cache with TTL

Wrap every tool function in a simple TTL cache. Identical call signatures return the cached result immediately without touching the network.

```python
import time
import hashlib
import json
import anthropic
from functools import wraps
from typing import Any, Callable

class TTLCache:
    def __init__(self, ttl_seconds: int = 60):
        self._store: dict[str, tuple[Any, float]] = {}
        self._ttl = ttl_seconds
        self.hits = 0
        self.misses = 0

    def _key(self, fn_name: str, args: tuple, kwargs: dict) -> str:
        raw = json.dumps({"fn": fn_name, "args": args, "kwargs": kwargs}, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def get(self, key: str) -> tuple[bool, Any]:
        if key in self._store:
            value, expires_at = self._store[key]
            if time.time() < expires_at:
                self.hits += 1
                return True, value
            del self._store[key]
        self.misses += 1
        return False, None

    def set(self, key: str, value: Any):
        self._store[key] = (value, time.time() + self._ttl)

_cache = TTLCache(ttl_seconds=300)

def cached_tool(fn: Callable) -> Callable:
    @wraps(fn)
    def wrapper(*args, **kwargs):
        key = _cache._key(fn.__name__, args, kwargs)
        hit, value = _cache.get(key)
        if hit:
            print(f"[CACHE HIT]  {fn.__name__}({args}, {kwargs})")
            return value
        print(f"[CACHE MISS] {fn.__name__}({args}, {kwargs}) — calling API")
        result = fn(*args, **kwargs)
        _cache.set(key, result)
        return result
    return wrapper

@cached_tool
def get_product(product_id: int) -> dict:
    # Simulated API call
    return {"id": product_id, "name": f"Product {product_id}", "price": 9.99}

@cached_tool
def get_user(user_id: str) -> dict:
    return {"id": user_id, "name": "Alice", "tier": "premium"}

TOOLS = [
    {
        "name": "get_product",
        "description": "Fetch product details by ID",
        "input_schema": {
            "type": "object",
            "properties": {"product_id": {"type": "integer"}},
            "required": ["product_id"],
        },
    },
    {
        "name": "get_user",
        "description": "Fetch user profile by ID",
        "input_schema": {
            "type": "object",
            "properties": {"user_id": {"type": "string"}},
            "required": ["user_id"],
        },
    },
]

TOOL_FNS = {"get_product": get_product, "get_user": get_user}

def run_agent(task: str) -> str:
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": task}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                fn = TOOL_FNS[block.name]
                result = fn(**block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })

        messages.append({"role": "user", "content": tool_results})

result = run_agent(
    "Compare product 42 details with user Alice's tier. Also show product 42 again."
)
print(result)
print(f"\nCache stats: {_cache.hits} hits, {_cache.misses} misses")
```

**Expected Token Savings:** 0 LLM tokens saved; API call reduction: 50–80% on repeated data
**Environment:** `pip install anthropic`

---

### Option 2 — Async Request Deduplication with Inflight Tracking

When multiple concurrent coroutines request the same resource simultaneously, deduplicate at the inflight level — only one real HTTP call fires, and all waiters receive the same result.

```python
import asyncio
import json
import time
import anthropic
from typing import Any

class InflightCache:
    """
    Deduplicates concurrent identical requests.
    If a request for key K is already in-flight, subsequent callers
    await the same Future instead of firing duplicate requests.
    """
    def __init__(self, ttl_seconds: int = 120):
        self._results: dict[str, tuple[Any, float]] = {}
        self._inflight: dict[str, asyncio.Future] = {}
        self._ttl = ttl_seconds

    async def get_or_fetch(self, key: str, fetch_fn) -> Any:
        # Check result cache
        if key in self._results:
            value, expires_at = self._results[key]
            if time.time() < expires_at:
                return value
            del self._results[key]

        # Attach to inflight request if one exists
        if key in self._inflight:
            return await self._inflight[key]

        # We are first — create the Future
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._inflight[key] = future

        try:
            result = await fetch_fn()
            self._results[key] = (result, time.time() + self._ttl)
            future.set_result(result)
            return result
        except Exception as e:
            future.set_exception(e)
            raise
        finally:
            self._inflight.pop(key, None)

cache = InflightCache(ttl_seconds=300)

async def fetch_product(product_id: int) -> dict:
    await asyncio.sleep(0.1)  # Simulate network latency
    return {"id": product_id, "name": f"Product {product_id}", "price": 9.99}

async def get_product_cached(product_id: int) -> dict:
    key = f"product:{product_id}"
    return await cache.get_or_fetch(key, lambda: fetch_product(product_id))

async def tool_call(name: str, args: dict) -> Any:
    if name == "get_product":
        return await get_product_cached(args["product_id"])
    raise ValueError(f"Unknown tool: {name}")

async def process_tool_calls(tool_uses: list[dict]) -> list[dict]:
    # All tool calls run concurrently — duplicates are deduplicated
    tasks = [tool_call(t["name"], t["input"]) for t in tool_uses]
    results = await asyncio.gather(*tasks)

    return [
        {
            "type": "tool_result",
            "tool_use_id": t["id"],
            "content": json.dumps(r),
        }
        for t, r in zip(tool_uses, results)
    ]

async def run_async_agent(task: str) -> str:
    client = anthropic.AsyncAnthropic()
    messages = [{"role": "user", "content": task}]

    TOOLS = [
        {
            "name": "get_product",
            "description": "Fetch product details",
            "input_schema": {
                "type": "object",
                "properties": {"product_id": {"type": "integer"}},
                "required": ["product_id"],
            },
        }
    ]

    while True:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        messages.append({"role": "assistant", "content": response.content})
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        results = await process_tool_calls(
            [{"name": b.name, "input": b.input, "id": b.id} for b in tool_uses]
        )
        messages.append({"role": "user", "content": results})

asyncio.run(run_async_agent("Compare product 42, product 42, and product 42 pricing."))
```

**Expected Token Savings:** 0 LLM tokens; eliminates all duplicate concurrent network calls
**Environment:** `pip install anthropic`

---

### Option 3 — Pre-Execution Tool Call Deduplication

Before executing tool calls, scan the pending list for duplicates. Merge identical calls and fan out the single result to all requestors.

```python
import json
import anthropic
from collections import defaultdict

def deduplicate_tool_calls(tool_uses: list) -> tuple[list, dict[str, list[str]]]:
    """
    Returns (deduplicated_tool_uses, alias_map).
    alias_map maps canonical_id -> [duplicate_ids]
    """
    seen: dict[str, str] = {}  # signature -> canonical_id
    canonical: list = []
    alias_map: dict[str, list[str]] = defaultdict(list)

    for block in tool_uses:
        sig = json.dumps(
            {"name": block.name, "input": block.input}, sort_keys=True
        )
        if sig in seen:
            canonical_id = seen[sig]
            alias_map[canonical_id].append(block.id)
            print(f"[DEDUP] {block.name}({block.input}) already queued — skipping")
        else:
            seen[sig] = block.id
            canonical.append(block)

    return canonical, dict(alias_map)

def execute_tool(name: str, args: dict) -> dict:
    # Simulated tool execution
    print(f"[EXEC] {name}({args})")
    if name == "get_weather":
        return {"city": args["city"], "temp": 22, "condition": "sunny"}
    if name == "get_stock":
        return {"ticker": args["ticker"], "price": 189.42}
    return {}

def run_agent_with_dedup(task: str) -> str:
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": task}]

    TOOLS = [
        {
            "name": "get_weather",
            "description": "Get current weather for a city",
            "input_schema": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
        {
            "name": "get_stock",
            "description": "Get current stock price",
            "input_schema": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}},
                "required": ["ticker"],
            },
        },
    ]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        messages.append({"role": "assistant", "content": response.content})

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        canonical, alias_map = deduplicate_tool_calls(tool_uses)

        # Execute only unique calls
        results_by_id: dict[str, dict] = {}
        for block in canonical:
            result = execute_tool(block.name, block.input)
            results_by_id[block.id] = result
            # Copy result to all alias IDs
            for alias_id in alias_map.get(block.id, []):
                results_by_id[alias_id] = result

        tool_results = [
            {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(results_by_id[block.id]),
            }
            for block in tool_uses
        ]

        messages.append({"role": "user", "content": tool_results})

result = run_agent_with_dedup(
    "What is the weather in Paris? Also tell me the weather in Paris again, "
    "and the stock price of AAPL twice."
)
print(result)
```

**Expected Token Savings:** 0 LLM tokens; eliminates redundant network calls within one turn
**Environment:** `pip install anthropic`

---

### Option 4 — Redis Shared Cache Across Agent Instances

In distributed deployments, multiple agent instances share a Redis cache so one instance's fetch benefits all others. Use `SETNX` for atomic cache population.

```python
import json
import hashlib
import time
import anthropic
import redis

r = redis.Redis(host="localhost", port=6379, decode_responses=True)
client = anthropic.Anthropic()

CACHE_TTL = 300  # 5 minutes

def cache_key(tool_name: str, args: dict) -> str:
    raw = json.dumps({"tool": tool_name, "args": args}, sort_keys=True)
    return f"tool_cache:{hashlib.sha256(raw.encode()).hexdigest()[:20]}"

def call_tool_cached(name: str, args: dict, real_fn) -> dict:
    key = cache_key(name, args)

    cached = r.get(key)
    if cached:
        print(f"[REDIS HIT]  {name}({args})")
        return json.loads(cached)

    print(f"[REDIS MISS] {name}({args}) — fetching")
    result = real_fn(**args)

    # SET with NX (only if not exists) prevents race conditions
    r.set(key, json.dumps(result), nx=True, ex=CACHE_TTL)
    return result

def fetch_product(product_id: int) -> dict:
    # Real API call would go here
    return {"id": product_id, "name": f"Product {product_id}", "stock": 42}

def fetch_exchange_rate(from_currency: str, to_currency: str) -> dict:
    return {"from": from_currency, "to": to_currency, "rate": 1.08}

TOOLS = [
    {
        "name": "get_product",
        "description": "Fetch product details",
        "input_schema": {
            "type": "object",
            "properties": {"product_id": {"type": "integer"}},
            "required": ["product_id"],
        },
    },
    {
        "name": "get_exchange_rate",
        "description": "Get currency exchange rate",
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

TOOL_FNS = {
    "get_product": fetch_product,
    "get_exchange_rate": fetch_exchange_rate,
}

def run_agent(task: str) -> str:
    messages = [{"role": "user", "content": task}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = call_tool_cached(block.name, block.input, TOOL_FNS[block.name])
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })

        messages.append({"role": "user", "content": tool_results})

print(run_agent("Show me product 42 details and the USD to EUR rate."))
# Second agent instance — hits Redis cache
print(run_agent("Tell me about product 42 and also USD to EUR exchange."))
```

**Expected Token Savings:** 0 LLM tokens; shared cache eliminates cross-instance redundancy
**Environment:** `pip install anthropic redis`

---

### Option 5 — Tool Result Memoisation with Conversation-Scoped Store

Persist tool results in a per-conversation memo dict. Subsequent identical tool calls within the same multi-turn conversation look up the memo before hitting the network.

```python
import json
import anthropic
from dataclasses import dataclass, field

@dataclass
class MemoAgent:
    system_prompt: str
    tools: list[dict]
    tool_fns: dict
    memo: dict = field(default_factory=dict)
    messages: list = field(default_factory=list)
    _call_count: int = 0
    _memo_hit_count: int = 0

    def _memo_key(self, name: str, args: dict) -> str:
        return json.dumps({"n": name, "a": args}, sort_keys=True)

    def _execute(self, name: str, args: dict) -> dict:
        key = self._memo_key(name, args)
        if key in self.memo:
            self._memo_hit_count += 1
            print(f"[MEMO HIT]  {name}({args})")
            return self.memo[key]

        self._call_count += 1
        print(f"[MEMO MISS] {name}({args})")
        result = self.tool_fns[name](**args)
        self.memo[key] = result
        return result

    def chat(self, user_message: str) -> str:
        client = anthropic.Anthropic()
        self.messages.append({"role": "user", "content": user_message})

        while True:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                system=self.system_prompt,
                tools=self.tools,
                messages=self.messages,
            )

            if response.stop_reason == "end_turn":
                reply = next(b.text for b in response.content if b.type == "text")
                self.messages.append({"role": "assistant", "content": reply})
                return reply

            self.messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = self._execute(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    })
            self.messages.append({"role": "user", "content": tool_results})

    @property
    def stats(self) -> dict:
        return {
            "real_calls": self._call_count,
            "memo_hits": self._memo_hit_count,
            "savings_pct": (
                self._memo_hit_count / (self._call_count + self._memo_hit_count) * 100
                if (self._call_count + self._memo_hit_count) > 0 else 0
            ),
        }

def get_inventory(item_id: str) -> dict:
    return {"item": item_id, "qty": 150, "warehouse": "WH-3"}

agent = MemoAgent(
    system_prompt="You are an inventory assistant.",
    tools=[{
        "name": "get_inventory",
        "description": "Check inventory for an item",
        "input_schema": {
            "type": "object",
            "properties": {"item_id": {"type": "string"}},
            "required": ["item_id"],
        },
    }],
    tool_fns={"get_inventory": get_inventory},
)

agent.chat("What is the inventory for item SKU-100?")
agent.chat("Can you confirm the inventory level for SKU-100 again?")
agent.chat("And one more time — what's the stock for SKU-100?")

print(agent.stats)
# real_calls: 1, memo_hits: 2, savings_pct: 66.7
```

**Expected Token Savings:** 0 LLM tokens; 60–80% API call reduction on repeated lookups
**Environment:** `pip install anthropic`

---

### Option 6 — Batch Tool Calls with Response Fan-Out

Instead of allowing the model to call the same tool N times, intercept multiple tool_use blocks, batch them into a single external API call, then fan the results back out to each tool_use ID.

```python
import asyncio
import json
import anthropic
from collections import defaultdict

async def batch_fetch_products(product_ids: list[int]) -> dict[int, dict]:
    """Fetch multiple products in one API call (simulated)."""
    await asyncio.sleep(0.1)  # One network round-trip
    return {
        pid: {"id": pid, "name": f"Product {pid}", "price": pid * 1.5}
        for pid in product_ids
    }

async def dispatch_tool_calls(tool_uses: list) -> list[dict]:
    """
    Groups identical tool types into batches.
    One batch call per tool type instead of N individual calls.
    """
    # Group by tool name
    by_name: dict[str, list] = defaultdict(list)
    for block in tool_uses:
        by_name[block.name].append(block)

    results_by_id: dict[str, dict] = {}

    # Process get_product calls as a batch
    if "get_product" in by_name:
        blocks = by_name["get_product"]
        ids = [b.input["product_id"] for b in blocks]
        # Deduplicate IDs for the batch request
        unique_ids = list(set(ids))
        print(f"[BATCH] Fetching {len(unique_ids)} unique products (was {len(ids)} calls)")
        batch_result = await batch_fetch_products(unique_ids)

        for block in blocks:
            pid = block.input["product_id"]
            results_by_id[block.id] = batch_result[pid]

    # Other tool types fall back to individual calls
    for name, blocks in by_name.items():
        if name == "get_product":
            continue
        for block in blocks:
            results_by_id[block.id] = {"error": f"Unknown tool: {name}"}

    return [
        {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": json.dumps(results_by_id[block.id]),
        }
        for block in tool_uses
    ]

async def run_batch_agent(task: str) -> str:
    client = anthropic.AsyncAnthropic()
    messages = [{"role": "user", "content": task}]

    TOOLS = [{
        "name": "get_product",
        "description": "Fetch product details by ID",
        "input_schema": {
            "type": "object",
            "properties": {"product_id": {"type": "integer"}},
            "required": ["product_id"],
        },
    }]

    while True:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        messages.append({"role": "assistant", "content": response.content})
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        tool_results = await dispatch_tool_calls(tool_uses)
        messages.append({"role": "user", "content": tool_results})

result = asyncio.run(run_batch_agent(
    "Compare products 10, 20, 10, and 30. Product 10 appears twice."
))
print(result)
```

**Expected Token Savings:** 0 LLM tokens; reduces N network calls to 1 batch call per type
**Environment:** `pip install anthropic`

---

## Comparison

| Option | Scope | Distributed | Dedup Level | Best For |
|--------|-------|-------------|-------------|----------|
| TTL Cache | Process | No | Call signature | Single-process agents |
| Inflight Dedup | Async coroutine | No | Concurrent | High-concurrency async agents |
| Pre-exec Dedup | Single turn | No | Turn level | Simple synchronous agents |
| Redis Cache | Cluster | Yes | Cross-instance | Distributed deployments |
| Conversation Memo | Conversation | No | Conversation | Multi-turn stateful agents |
| Batch Fan-Out | Single turn | No | Tool type | Bulk data retrieval |

**Recommended starting point:** Option 5 (Conversation Memo) for most agents; add Option 4 (Redis) when scaling horizontally.
