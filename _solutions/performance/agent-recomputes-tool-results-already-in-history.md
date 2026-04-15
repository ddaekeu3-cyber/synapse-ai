---
layout: solution
title: "Agent Recomputes Tool Results Already in Conversation History"
category: performance
description: "The agent calls the same tool with the same arguments multiple times in one session. Prior results are already in the conversation history, but the agent ignores them and pays for the same API call again."
tags: [performance, caching, tool-use, deduplication, session-cache, idempotency, efficiency]
---

## Symptom

In a multi-turn session, the agent calls `get_user_profile(user_id=42)` on turn 2, then again on turn 7 because it "forgot" the earlier result was in the history. Or a tool is called 3 times with the same date argument in a single response. Each call hits an external API, pays latency, and may bill the external service — for data already available in context.

## Root Cause

The language model does not reliably track "which tool calls I already made this session" when context grows long. It reasons about what data it needs, determines the tool to call, and may not notice that an identical call already appears 10 turns ago. Without an explicit caching or deduplication layer, every tool call is treated as fresh.

## Fix

---

### Option 1: Session-Scoped Tool Result Cache

Cache every tool result by `(tool_name, args_hash)` for the duration of the session. Return cached results instantly.

```python
import json
import hashlib
import time
import asyncio
import anthropic
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()


@dataclass
class CacheEntry:
    result: dict
    created_at: float
    hit_count: int = 0

    def is_fresh(self, ttl_seconds: float) -> bool:
        return time.monotonic() - self.created_at < ttl_seconds


class ToolResultCache:
    """
    Session-scoped cache for tool results.
    Keyed by (tool_name, stable_args_hash).
    """
    def __init__(self, ttl_seconds: float = 300.0):
        self._cache: dict[str, CacheEntry] = {}
        self.ttl = ttl_seconds
        self.hits = 0
        self.misses = 0

    def _key(self, tool_name: str, args: dict) -> str:
        stable = json.dumps(args, sort_keys=True)
        return f"{tool_name}:{hashlib.sha256(stable.encode()).hexdigest()[:12]}"

    def get(self, tool_name: str, args: dict) -> dict | None:
        key = self._key(tool_name, args)
        entry = self._cache.get(key)
        if entry and entry.is_fresh(self.ttl):
            entry.hit_count += 1
            self.hits += 1
            return entry.result
        self.misses += 1
        return None

    def set(self, tool_name: str, args: dict, result: dict):
        key = self._key(tool_name, args)
        self._cache[key] = CacheEntry(result=result, created_at=time.monotonic())

    def stats(self) -> str:
        total = self.hits + self.misses
        rate = self.hits / total * 100 if total else 0
        return f"Cache: {self.hits} hits / {self.misses} misses ({rate:.0f}% hit rate)"


TOOLS = [
    {
        "name": "get_user_profile",
        "description": "Fetch user profile by ID.",
        "input_schema": {
            "type": "object",
            "properties": {"user_id": {"type": "integer"}},
            "required": ["user_id"],
        },
    },
    {
        "name": "get_exchange_rate",
        "description": "Get currency exchange rate.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_currency": {"type": "string"},
                "to_currency":   {"type": "string"},
            },
            "required": ["from_currency", "to_currency"],
        },
    },
    {
        "name": "get_weather",
        "description": "Get current weather for a city.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
]

# Simulate slow external calls
async def _real_tool_call(name: str, args: dict) -> dict:
    await asyncio.sleep(0.2)  # Simulate network latency
    if name == "get_user_profile":
        return {"user_id": args["user_id"], "name": "Alice Smith", "email": "alice@example.com", "plan": "pro"}
    elif name == "get_exchange_rate":
        return {"from": args["from_currency"], "to": args["to_currency"], "rate": 1.08, "ts": time.time()}
    elif name == "get_weather":
        return {"city": args["city"], "temp": "22°C", "condition": "sunny"}
    return {"error": "unknown tool"}


cache = ToolResultCache(ttl_seconds=300)


async def execute_tool(name: str, args: dict) -> tuple[dict, bool]:
    """Execute tool with caching. Returns (result, from_cache)."""
    cached = cache.get(name, args)
    if cached is not None:
        print(f"  [Cache HIT] {name}({args})")
        return cached, True

    print(f"  [Cache MISS] {name}({args}) — calling API...")
    result = await _real_tool_call(name, args)
    cache.set(name, args, result)
    return result, False


async def run_cached_agent(turns: list[str]) -> list[str]:
    messages = []
    replies = []

    for user_message in turns:
        messages.append({"role": "user", "content": user_message})
        start = time.monotonic()

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result, was_cached = await execute_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })

        if tool_results:
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
            final = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                tools=TOOLS,
                messages=messages,
            )
            reply = next(b.text for b in final.content if b.type == "text")
            messages.append({"role": "assistant", "content": reply})
        else:
            reply = next(b.text for b in response.content if b.type == "text")
            messages.append({"role": "assistant", "content": reply})

        elapsed = (time.monotonic() - start) * 1000
        replies.append(reply)
        print(f"  Turn done in {elapsed:.0f}ms")

    print(f"\n{cache.stats()}")
    return replies


# Simulate a session with repeated tool calls
turns = [
    "What's the weather in Tokyo?",
    "Tell me about user 42.",
    "What's the EUR to USD rate?",
    "Remind me — what was the weather in Tokyo again?",  # cache hit
    "And what's user 42's email address?",              # cache hit
    "Convert some EUR to USD — what's the rate?",      # cache hit
]

results = asyncio.run(run_cached_agent(turns))
for i, r in enumerate(results):
    print(f"Turn {i+1}: {r[:80]}")
```

**Expected Token Savings**: Cache hits save external API latency (200ms → 0ms) and any per-call external API costs. Especially valuable for rate-limited external services.
**Environment**: Async Python. In-memory cache per session. TTL configurable per tool type.

---

### Option 2: Duplicate Detection — Reject Redundant Tool Calls Before Execution

Before executing a tool call, check if an identical call was already made this session.

```python
import json
import hashlib
import anthropic
from collections import defaultdict

client = anthropic.Anthropic()

TOOLS = [
    {
        "name": "fetch_document",
        "description": "Fetch a document by ID from the knowledge base.",
        "input_schema": {
            "type": "object",
            "properties": {"doc_id": {"type": "string"}},
            "required": ["doc_id"],
        },
    },
    {
        "name": "run_sql_query",
        "description": "Execute a read-only SQL query.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
]

# Track all tool calls made in this session: key → result
session_tool_calls: dict[str, dict] = {}
duplicate_count = defaultdict(int)


def tool_key(name: str, args: dict) -> str:
    return f"{name}:{hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()[:16]}"


def get_or_execute_tool(name: str, args: dict) -> tuple[dict, str]:
    """
    Returns (result, source) where source is 'cache' or 'fresh'.
    Prevents duplicate tool executions in a session.
    """
    key = tool_key(name, args)

    if key in session_tool_calls:
        duplicate_count[name] += 1
        print(f"  [Duplicate blocked] {name}({args}) — returning session result (duplicate #{duplicate_count[name]})")
        return session_tool_calls[key], "cache"

    # Execute for real
    if name == "fetch_document":
        result = {"doc_id": args["doc_id"], "title": "API Reference", "content": "...content..."}
    elif name == "run_sql_query":
        result = {"rows": [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}], "count": 2}
    else:
        result = {"error": "unknown tool"}

    session_tool_calls[key] = result
    return result, "fresh"


def log_duplicate_report():
    if duplicate_count:
        print("\n[Duplicate Tool Call Report]")
        for tool, count in duplicate_count.items():
            print(f"  {tool}: {count} duplicate call(s) prevented")
    else:
        print("\n[No duplicate tool calls this session]")


def run_dedup_agent(messages_to_send: list[str]) -> list[str]:
    history = []
    replies = []

    for msg in messages_to_send:
        history.append({"role": "user", "content": msg})

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=history,
        )

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result, source = get_or_execute_tool(block.name, block.input)
                content = json.dumps({**result, "_source": source})
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": content})

        if tool_results:
            history.append({"role": "assistant", "content": response.content})
            history.append({"role": "user", "content": tool_results})
            final = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                tools=TOOLS,
                messages=history,
            )
            reply = next(b.text for b in final.content if b.type == "text")
            history.append({"role": "assistant", "content": reply})
        else:
            reply = next(b.text for b in response.content if b.type == "text")
            history.append({"role": "assistant", "content": reply})

        replies.append(reply)

    log_duplicate_report()
    return replies


conversation = [
    "Fetch document DOC-001.",
    "What were the contents of DOC-001?",          # May re-fetch — will be blocked
    "Run this query: SELECT * FROM users LIMIT 10",
    "Summarize the document DOC-001 one more time.", # May re-fetch — will be blocked
    "Show me the SQL results again.",               # May re-query — will be blocked
]
results = run_dedup_agent(conversation)
```

**Expected Token Savings**: Each blocked duplicate saves external API cost + latency. For a 10-turn session with 3 repeated tool calls: ~3× tool execution savings.
**Environment**: In-memory dict per session. Thread-safe with `threading.Lock()` for concurrent sessions.

---

### Option 3: History-Aware Tool Execution with Context Injection

Before executing a tool, scan the existing message history for a prior result. Inject it instead of re-executing.

```python
import json
import re
import anthropic

client = anthropic.Anthropic()

TOOLS = [
    {
        "name": "get_stock_price",
        "description": "Get the current stock price for a ticker symbol.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string", "description": "Stock ticker (e.g., AAPL)"}},
            "required": ["symbol"],
        },
    },
]


def find_prior_result_in_history(history: list[dict], tool_name: str, args: dict) -> dict | None:
    """
    Scan message history for a prior tool result matching (tool_name, args).
    Returns the result dict if found, None otherwise.
    """
    target_args = json.dumps(args, sort_keys=True)

    for i, msg in enumerate(history):
        # Look for tool_use blocks
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue

        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == tool_name
            ):
                block_args = json.dumps(block.get("input", {}), sort_keys=True)
                if block_args == target_args:
                    # Found matching tool call — look for its result in the next message
                    block_id = block.get("id")
                    for next_msg in history[i + 1:]:
                        next_content = next_msg.get("content", [])
                        if not isinstance(next_content, list):
                            continue
                        for result_block in next_content:
                            if (
                                isinstance(result_block, dict)
                                and result_block.get("type") == "tool_result"
                                and result_block.get("tool_use_id") == block_id
                            ):
                                try:
                                    return json.loads(result_block.get("content", "{}"))
                                except json.JSONDecodeError:
                                    return {"raw": result_block.get("content")}

    return None


def smart_execute(tool_name: str, args: dict, history: list[dict]) -> tuple[dict, bool]:
    """Execute tool or return prior result from history."""
    prior = find_prior_result_in_history(history, tool_name, args)
    if prior is not None:
        print(f"  [History hit] Found prior result for {tool_name}({args}) in history")
        return prior, True

    # Execute fresh
    print(f"  [Executing] {tool_name}({args})")
    if tool_name == "get_stock_price":
        return {"symbol": args["symbol"], "price": 182.50, "change": "+1.2%", "ts": "2025-04-15T10:00:00Z"}, False
    return {"error": "unknown tool"}, False


def run_history_aware_agent(messages_to_send: list[str]) -> list[str]:
    history = []
    replies = []
    history_hits = 0

    for msg in messages_to_send:
        history.append({"role": "user", "content": msg})

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=history,
        )

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result, was_from_history = smart_execute(block.name, block.input, history)
                if was_from_history:
                    history_hits += 1
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })

        if tool_results:
            history.append({"role": "assistant", "content": response.content})
            history.append({"role": "user", "content": tool_results})
            final = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                tools=TOOLS,
                messages=history,
            )
            reply = next(b.text for b in final.content if b.type == "text")
            history.append({"role": "assistant", "content": reply})
        else:
            reply = next(b.text for b in response.content if b.type == "text")
            history.append({"role": "assistant", "content": reply})

        replies.append(reply)

    print(f"\n[History-aware execution: {history_hits} duplicate calls avoided]")
    return replies


conversation = [
    "What's the stock price for AAPL?",
    "Is AAPL a good buy at that price?",       # may re-fetch AAPL — blocked
    "Compare AAPL's current price to MSFT.",   # AAPL from cache, MSFT fresh
    "What was AAPL's price again?",            # cache hit
]
run_history_aware_agent(conversation)
```

**Expected Token Savings**: History scan finds prior results without any extra API call. Works even without an explicit cache — uses the context window as the cache.
**Environment**: Pure Python, no extra state. History scan is O(n) turns — for very long sessions, cap the scan depth.

---

### Option 4: Pre-Flight Dedup Check via LLM Self-Query

Before executing a tool call, ask the LLM if it already has the answer in context.

```python
import json
import anthropic

client = anthropic.Anthropic()

TOOLS = [
    {
        "name": "lookup_customer",
        "description": "Look up customer information by ID.",
        "input_schema": {
            "type": "object",
            "properties": {"customer_id": {"type": "string"}},
            "required": ["customer_id"],
        },
    },
]


def has_result_in_context(tool_name: str, args: dict, history: list[dict]) -> bool:
    """
    Use a lightweight Haiku call to check if the information
    is already available in the conversation history.
    """
    if len(history) < 2:
        return False

    # Build a compact history summary (last 6 messages only)
    recent = history[-6:]
    history_text = "\n".join(
        f"{m['role']}: {str(m['content'])[:200]}"
        for m in recent
    )

    check_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=32,
        system="Answer with exactly one word: YES if the information is already in the history, NO if not.",
        messages=[{
            "role": "user",
            "content": (
                f"Is the result of {tool_name}({json.dumps(args)}) already present "
                f"in this conversation history?\n\n{history_text}"
            ),
        }],
    )

    answer = check_response.content[0].text.strip().upper()
    return "YES" in answer


def run_precheck_agent(messages_to_send: list[str]) -> list[str]:
    history = []
    replies = []
    precheck_blocks = 0

    for msg in messages_to_send:
        history.append({"role": "user", "content": msg})

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=history,
        )

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                # Pre-flight check
                already_known = has_result_in_context(block.name, block.input, history)
                if already_known:
                    precheck_blocks += 1
                    print(f"  [Pre-check] {block.name}({block.input}) — already in context, skipping")
                    result = {"note": "Result already available in conversation history. Refer to prior response.", "skipped": True}
                else:
                    print(f"  [Executing] {block.name}({block.input})")
                    result = {"customer_id": block.input.get("customer_id"), "name": "Bob Jones", "plan": "enterprise"}

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })

        if tool_results:
            history.append({"role": "assistant", "content": response.content})
            history.append({"role": "user", "content": tool_results})
            final = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                tools=TOOLS,
                messages=history,
            )
            reply = next(b.text for b in final.content if b.type == "text")
            history.append({"role": "assistant", "content": reply})
        else:
            reply = next(b.text for b in response.content if b.type == "text")
            history.append({"role": "assistant", "content": reply})

        replies.append(reply)

    print(f"\n[Pre-check blocked {precheck_blocks} redundant tool call(s)]")
    return replies


run_precheck_agent([
    "Look up customer C-001.",
    "What plan is customer C-001 on?",   # May re-lookup — pre-check fires
    "Remind me about C-001's details.",  # Pre-check blocks re-fetch
])
```

**Expected Token Savings**: Pre-check costs ~20 Haiku tokens. Blocked tool call saves external API cost + latency. Breakeven at any external call that costs more than $0.000016.
**Environment**: Two Haiku calls per suspected duplicate. Best for expensive or rate-limited external tools.

---

### Option 5: Memoized Tool Decorator

Wrap tool implementations with a memoization decorator. Caching is transparent to the agent loop.

```python
import json
import time
import hashlib
import asyncio
import functools
from typing import Callable, Any
import anthropic

client = anthropic.AsyncAnthropic()


def memoize_tool(ttl_seconds: float = 300.0):
    """
    Decorator that caches async tool function results.
    Cache key: (function_name, sorted_kwargs).
    """
    def decorator(fn: Callable) -> Callable:
        cache: dict[str, tuple[Any, float]] = {}
        hit_count = 0
        miss_count = 0

        @functools.wraps(fn)
        async def wrapper(**kwargs) -> Any:
            nonlocal hit_count, miss_count
            key = hashlib.sha256(
                json.dumps({"fn": fn.__name__, **kwargs}, sort_keys=True).encode()
            ).hexdigest()[:16]

            if key in cache:
                result, ts = cache[key]
                if time.monotonic() - ts < ttl_seconds:
                    hit_count += 1
                    print(f"  [Memo HIT] {fn.__name__}({kwargs}) — {hit_count} hits total")
                    return result

            miss_count += 1
            result = await fn(**kwargs)
            cache[key] = (result, time.monotonic())
            return result

        wrapper.cache_stats = lambda: {"hits": hit_count, "misses": miss_count}
        return wrapper

    return decorator


# Tool implementations — memoization is transparent to the agent
@memoize_tool(ttl_seconds=60)
async def get_user_profile(user_id: int) -> dict:
    """Fetch user profile. Results cached 60 seconds."""
    await asyncio.sleep(0.15)  # Simulate network
    return {"user_id": user_id, "name": "Carol White", "tier": "gold"}


@memoize_tool(ttl_seconds=300)
async def get_product_details(product_id: str) -> dict:
    """Fetch product details. Results cached 5 minutes."""
    await asyncio.sleep(0.15)
    return {"product_id": product_id, "name": "Widget Pro", "price": 49.99, "stock": 142}


@memoize_tool(ttl_seconds=30)
async def get_inventory_count(sku: str) -> dict:
    """Get inventory count. Cached 30 seconds (more volatile)."""
    await asyncio.sleep(0.1)
    return {"sku": sku, "count": 87, "reserved": 10}


TOOLS = [
    {"name": "get_user_profile", "description": "Get user profile by ID.",
     "input_schema": {"type": "object", "properties": {"user_id": {"type": "integer"}}, "required": ["user_id"]}},
    {"name": "get_product_details", "description": "Get product info by ID.",
     "input_schema": {"type": "object", "properties": {"product_id": {"type": "string"}}, "required": ["product_id"]}},
    {"name": "get_inventory_count", "description": "Get inventory count for a SKU.",
     "input_schema": {"type": "object", "properties": {"sku": {"type": "string"}}, "required": ["sku"]}},
]

TOOL_FNS = {
    "get_user_profile":   get_user_profile,
    "get_product_details": get_product_details,
    "get_inventory_count": get_inventory_count,
}


async def run_memoized_agent(messages: list[str]) -> list[str]:
    history = []
    replies = []

    for msg in messages:
        history.append({"role": "user", "content": msg})
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=history,
        )

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                fn = TOOL_FNS.get(block.name)
                if fn:
                    result = await fn(**block.input)
                else:
                    result = {"error": "unknown tool"}
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})

        if tool_results:
            history.append({"role": "assistant", "content": response.content})
            history.append({"role": "user", "content": tool_results})
            final = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                tools=TOOLS,
                messages=history,
            )
            reply = next(b.text for b in final.content if b.type == "text")
            history.append({"role": "assistant", "content": reply})
        else:
            reply = next(b.text for b in response.content if b.type == "text")
            history.append({"role": "assistant", "content": reply})

        replies.append(reply)

    # Print cache stats for all tools
    for name, fn in TOOL_FNS.items():
        if hasattr(fn, "cache_stats"):
            print(f"  {name}: {fn.cache_stats()}")

    return replies


asyncio.run(run_memoized_agent([
    "Get profile for user 42 and details for product P-100.",
    "What tier is user 42 on?",                 # memo hit for user 42
    "Check inventory for SKU-500.",
    "How much stock does P-100 have? Also re-check user 42.", # 2 memo hits
]))
```

**Expected Token Savings**: Decorator-based caching is zero-effort to add — just `@memoize_tool(ttl=60)` per function. No changes to the agent loop.
**Environment**: Async Python. TTL is configurable per tool based on data volatility.

---

### Option 6: Semantic Deduplication for Near-Identical Calls

Catch logically equivalent tool calls that differ only in argument formatting (e.g., `"AAPL"` vs `"aapl"`, `42` vs `"42"`).

```python
import json
import hashlib
import re
import anthropic

client = anthropic.Anthropic()


def normalize_args(args: dict) -> dict:
    """
    Normalize arguments to catch logically equivalent calls.
    - String numbers → actual numbers
    - Uppercase/lowercase → lowercase for string IDs
    - Trim whitespace
    """
    normalized = {}
    for k, v in args.items():
        if isinstance(v, str):
            v = v.strip()
            # Normalize numeric strings
            if re.match(r"^\d+$", v):
                v = int(v)
            elif re.match(r"^\d+\.\d+$", v):
                v = float(v)
            # Normalize ticker symbols and IDs to uppercase
            elif re.match(r"^[A-Za-z][A-Za-z0-9\-_]{0,20}$", v):
                v = v.upper()
        normalized[k] = v
    return normalized


def semantic_key(tool_name: str, args: dict) -> str:
    norm = normalize_args(args)
    stable = json.dumps(norm, sort_keys=True)
    return f"{tool_name}:{hashlib.sha256(stable.encode()).hexdigest()[:12]}"


# Semantic cache
_semantic_cache: dict[str, dict] = {}
_semantic_hits = 0


def semantic_execute(tool_name: str, raw_args: dict) -> tuple[dict, bool]:
    global _semantic_hits
    key = semantic_key(tool_name, raw_args)
    norm = normalize_args(raw_args)

    if key in _semantic_cache:
        _semantic_hits += 1
        print(f"  [Semantic HIT] {tool_name}({raw_args}) → normalized: {norm}")
        return _semantic_cache[key], True

    print(f"  [Executing] {tool_name}({norm})")
    # Simulate execution
    if tool_name == "get_stock_price":
        result = {"symbol": norm.get("symbol", ""), "price": 150.00, "change": "+0.5%"}
    elif tool_name == "get_user":
        result = {"user_id": norm.get("user_id", 0), "name": "Dave Green", "status": "active"}
    else:
        result = {"status": "ok"}

    _semantic_cache[key] = result
    return result, False


TOOLS = [
    {"name": "get_stock_price", "description": "Get stock price.", "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "get_user", "description": "Get user by ID.", "input_schema": {"type": "object", "properties": {"user_id": {"type": "integer"}}, "required": ["user_id"]}},
]


def run_semantic_dedup_agent(messages_to_send: list[str]) -> list[str]:
    history = []
    replies = []

    for msg in messages_to_send:
        history.append({"role": "user", "content": msg})
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=history,
        )

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result, cached = semantic_execute(block.name, block.input)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})

        if tool_results:
            history.append({"role": "assistant", "content": response.content})
            history.append({"role": "user", "content": tool_results})
            final = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                tools=TOOLS,
                messages=history,
            )
            reply = next(b.text for b in final.content if b.type == "text")
            history.append({"role": "assistant", "content": reply})
        else:
            reply = next(b.text for b in response.content if b.type == "text")
            history.append({"role": "assistant", "content": reply})

        replies.append(reply)

    print(f"\n[Semantic dedup: {_semantic_hits} equivalent calls caught]")
    return replies


run_semantic_dedup_agent([
    "Get stock price for AAPL.",
    "What's the price of aapl?",      # same as AAPL — semantic hit
    "Check aapl stock price.",         # same — semantic hit
    "Get user with ID 42.",
    "What's user '42' details?",      # '42' normalized to 42 — semantic hit
])
```

**Expected Token Savings**: Semantic normalization catches ~20% more duplicates than exact-match caching. Particularly effective for agents that call tools with user-provided arguments in varied formats.
**Environment**: Pure Python. Normalization rules are extensible per domain (date formats, units, etc.).

---

| Option | Cache Type | Catches | Overhead | Best For |
|--------|-----------|--------|---------|---------|
| 1 | Session TTL cache | Exact duplicates | None | General session caching |
| 2 | Duplicate detector | Exact duplicates | None | Blocking with audit log |
| 3 | History scanner | In-history results | O(n) scan | No external state needed |
| 4 | LLM pre-check | Semantically known | +1 Haiku call | Expensive external tools |
| 5 | Memoize decorator | Exact duplicates | None | Clean function-level caching |
| 6 | Semantic normalization | Format variants | Minimal | Mixed-format arguments |
