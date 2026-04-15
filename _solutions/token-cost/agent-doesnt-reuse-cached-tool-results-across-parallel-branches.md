---
layout: solution
title: "Agent Doesn't Reuse Cached Tool Results Across Parallel Branches"
category: token-cost
description: "Multiple parallel agent branches independently call the same expensive tool (web search, database query, embedding), paying full cost each time instead of sharing the result."
tags: [token-cost, caching, tool-results, concurrency, production]
---

## Symptom

A fan-out agent spawns three branches to answer different sub-questions, but all three branches call `search_web("current GDP of France")` independently. The same HTTP request fires three times, the same result is embedded three times, and the same tokens are injected into three separate context windows. Costs multiply proportionally to the number of branches, even when the underlying data is identical.

## Root Cause

Each parallel branch maintains its own context window and tool call history. Without a shared result cache, there is no mechanism for one branch to observe that another already fetched the same data. The agent treats every tool call as independent even when the inputs are identical. A request-scoped or session-scoped cache keyed on tool name + arguments eliminates redundant calls.

## Fix

### Option 1 — In-memory dict cache keyed on tool name + args hash

```python
import anthropic
import asyncio
import hashlib
import json
import time
from typing import Any

client = anthropic.AsyncAnthropic()

# Shared cache for this request batch
_tool_cache: dict[str, tuple[Any, float]] = {}
CACHE_TTL_SECONDS = 300  # 5 minutes

def _cache_key(tool_name: str, tool_input: dict) -> str:
    serialised = json.dumps({"tool": tool_name, "input": tool_input}, sort_keys=True)
    return hashlib.sha256(serialised.encode()).hexdigest()

def _cached_tool_result(tool_name: str, tool_input: dict) -> Any | None:
    key = _cache_key(tool_name, tool_input)
    entry = _tool_cache.get(key)
    if entry:
        result, ts = entry
        if time.monotonic() - ts < CACHE_TTL_SECONDS:
            print(f"[cache] HIT  {tool_name}({tool_input})")
            return result
    return None

def _store_tool_result(tool_name: str, tool_input: dict, result: Any) -> None:
    key = _cache_key(tool_name, tool_input)
    _tool_cache[key] = (result, time.monotonic())
    print(f"[cache] MISS {tool_name}({tool_input}) → stored")

def execute_tool(tool_name: str, tool_input: dict) -> str:
    """Simulate an expensive tool call."""
    time.sleep(0.1)  # simulate latency
    if tool_name == "search_web":
        return f"Search result for: {tool_input['query']}"
    return f"Tool {tool_name} result"

def execute_tool_cached(tool_name: str, tool_input: dict) -> str:
    cached = _cached_tool_result(tool_name, tool_input)
    if cached is not None:
        return cached
    result = execute_tool(tool_name, tool_input)
    _store_tool_result(tool_name, tool_input, result)
    return result

async def run_branch(question: str, shared_context: str) -> str:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"Context: {shared_context}\n\nQuestion: {question}"}],
    )
    return resp.content[0].text

async def main():
    # Shared expensive tool call — fetched once, reused across branches
    search_result = execute_tool_cached("search_web", {"query": "GDP France 2024"})

    questions = [
        "How does France's GDP compare to Germany?",
        "What does France's GDP growth imply for EU policy?",
        "Which French sectors drive GDP most?",
    ]
    results = await asyncio.gather(*[run_branch(q, search_result) for q in questions])
    for q, r in zip(questions, results):
        print(f"Q: {q}\nA: {r[:100]}\n")

asyncio.run(main())
```

**Expected Token Savings:** 3 branches × 1 duplicate tool call each = 2 redundant calls eliminated; savings scale linearly with branch count and tool result size.
**Environment:** Fan-out agents with a shared research phase; any multi-branch agent where sub-questions share common data dependencies.

---

### Option 2 — asyncio.Lock-based cache preventing duplicate concurrent fetches

```python
import asyncio
import hashlib
import json
import time
import anthropic

client = anthropic.AsyncAnthropic()

class ConcurrentToolCache:
    """Prevents duplicate concurrent fetches via per-key locks (cache stampede protection)."""

    def __init__(self, ttl: float = 300.0):
        self._ttl    = ttl
        self._store: dict[str, tuple[str, float]] = {}
        self._locks: dict[str, asyncio.Lock]       = {}
        self._meta_lock = asyncio.Lock()

    def _key(self, name: str, args: dict) -> str:
        return hashlib.sha256(
            json.dumps({"n": name, "a": args}, sort_keys=True).encode()
        ).hexdigest()

    async def get_or_fetch(self, name: str, args: dict, fetch_fn) -> str:
        key = self._key(name, args)

        # Fast path: already cached
        entry = self._store.get(key)
        if entry and time.monotonic() - entry[1] < self._ttl:
            print(f"[cache] HIT {name}")
            return entry[0]

        # Acquire per-key lock so only one coroutine fetches
        async with self._meta_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
        lock = self._locks[key]

        async with lock:
            # Re-check after acquiring lock (another coroutine may have fetched)
            entry = self._store.get(key)
            if entry and time.monotonic() - entry[1] < self._ttl:
                print(f"[cache] HIT (post-lock) {name}")
                return entry[0]

            print(f"[cache] MISS {name} — fetching")
            result = await fetch_fn(name, args)
            self._store[key] = (result, time.monotonic())
            return result

cache = ConcurrentToolCache(ttl=120.0)

async def fetch_tool(name: str, args: dict) -> str:
    await asyncio.sleep(0.2)  # simulate network latency
    return f"{name}({args}) → real API result"

async def agent_branch(branch_id: int, query: str) -> str:
    shared_data = await cache.get_or_fetch(
        "database_lookup", {"table": "products", "filter": "active=true"}, fetch_tool
    )
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": f"Data: {shared_data}\n\nAnswer: {query}"}],
    )
    return f"[branch {branch_id}] {resp.content[0].text[:80]}"

async def main():
    branches = [
        agent_branch(i, q)
        for i, q in enumerate([
            "Which products are most expensive?",
            "How many active products are there?",
            "Which product has the lowest stock?",
            "What is the average price?",
        ])
    ]
    results = await asyncio.gather(*branches)
    for r in results:
        print(r)

asyncio.run(main())
```

**Expected Token Savings:** Lock prevents 4 concurrent branches from all fetching the same DB record simultaneously; exactly 1 fetch happens regardless of fan-out width.
**Environment:** High-concurrency async agents; multi-branch pipelines where race conditions on cache population would otherwise trigger duplicate API calls.

---

### Option 3 — Redis-backed distributed cache for multi-process agents

```python
import anthropic
import hashlib
import json
import time

# pip install redis
try:
    import redis
    _redis = redis.Redis(host="localhost", port=6379, decode_responses=True)
    _redis.ping()
    REDIS_AVAILABLE = True
except Exception:
    REDIS_AVAILABLE = False
    print("[cache] Redis unavailable — falling back to in-memory")
    _local_cache: dict = {}

client = anthropic.Anthropic()

CACHE_TTL = 300  # seconds

def _key(tool_name: str, args: dict) -> str:
    raw = json.dumps({"tool": tool_name, "args": args}, sort_keys=True)
    return f"agent:tool:{hashlib.sha256(raw.encode()).hexdigest()}"

def get_cached(tool_name: str, args: dict) -> str | None:
    key = _key(tool_name, args)
    if REDIS_AVAILABLE:
        val = _redis.get(key)
        if val:
            print(f"[redis] HIT {tool_name}")
        return val
    entry = _local_cache.get(key)
    if entry and time.monotonic() - entry[1] < CACHE_TTL:
        return entry[0]
    return None

def set_cached(tool_name: str, args: dict, result: str) -> None:
    key = _key(tool_name, args)
    if REDIS_AVAILABLE:
        _redis.setex(key, CACHE_TTL, result)
    else:
        _local_cache[key] = (result, time.monotonic())

def run_tool(tool_name: str, args: dict) -> str:
    """Expensive tool — only called on cache miss."""
    time.sleep(0.1)
    return f"[real] {tool_name}({args['query']}) result at {time.time():.0f}"

def cached_tool(tool_name: str, args: dict) -> str:
    cached = get_cached(tool_name, args)
    if cached:
        return cached
    result = run_tool(tool_name, args)
    set_cached(tool_name, args, result)
    print(f"[redis] MISS {tool_name} → cached")
    return result

def ask(sub_question: str, shared_tool_result: str) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"Data: {shared_tool_result}\nQ: {sub_question}"}],
    )
    return resp.content[0].text

# Shared tool result is fetched once and reused across all workers (even separate processes)
data = cached_tool("web_search", {"query": "oil price today"})
for q in ["What does this mean for airlines?", "How does it affect shipping?", "Impact on plastics industry?"]:
    print(ask(q, data)[:100])
```

**Expected Token Savings:** Redis cache survives process restarts; workers in a distributed agent cluster all share the same tool result without individual fetches.
**Environment:** Multi-process agents; Celery/RQ task workers; distributed agent networks on Kubernetes.

---

### Option 4 — Anthropic prompt caching for repeated tool results in context

```python
import anthropic

client = anthropic.Anthropic()

def build_system_with_cached_data(tool_result: str) -> list[dict]:
    """Put expensive tool result in the system prompt with cache_control."""
    return [
        {
            "type": "text",
            "text": (
                "You are a research assistant. Use the data below to answer questions. "
                "Be concise and cite specific figures.\n\n"
                f"=== RETRIEVED DATA ===\n{tool_result}\n=== END DATA ==="
            ),
            "cache_control": {"type": "ephemeral"},  # cache this block
        }
    ]

def run_expensive_tool() -> str:
    """Simulate a slow, expensive tool call (e.g., web search, DB query)."""
    import time; time.sleep(0.1)
    return (
        "Global semiconductor market size 2024: $611 billion. "
        "TSMC market share: 57%. Samsung: 13%. Intel Foundry: 9%. "
        "AI chip segment growth: 40% YoY. "
        "Top customers: Apple (24%), Nvidia (11%), AMD (8%)."
    )

# Fetch tool result ONCE
tool_result = run_expensive_tool()
system_blocks = build_system_with_cached_data(tool_result)

questions = [
    "Which company leads in market share?",
    "How fast is the AI chip segment growing?",
    "Who are the top three customers?",
]

for q in questions:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=system_blocks,
        messages=[{"role": "user", "content": q}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    cache_read = getattr(resp.usage, "cache_read_input_tokens", 0)
    cache_write = getattr(resp.usage, "cache_creation_input_tokens", 0)
    print(f"Q: {q}")
    print(f"A: {resp.content[0].text[:100]}")
    print(f"   cache_write={cache_write} cache_read={cache_read}\n")
```

**Expected Token Savings:** After the first call writes the cache, subsequent calls pay cache_read price (~10% of standard input token price); 3 branches → ~90% savings on repeated context.
**Environment:** Any multi-turn or multi-branch agent using the Anthropic API; best when tool result is ≥ 1,024 tokens (prompt cache minimum).

---

### Option 5 — Request-scoped dependency injection for tool result sharing

```python
import asyncio
import anthropic
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class RequestContext:
    """Shared context injected into all branches of a single agent request."""
    _resolved: dict[str, str] = field(default_factory=dict)
    _lock: asyncio.Lock        = field(default_factory=asyncio.Lock)

    async def resolve(self, key: str, fetch_fn) -> str:
        """Return cached value or fetch and cache it (once, even under concurrency)."""
        if key in self._resolved:
            return self._resolved[key]
        async with self._lock:
            if key not in self._resolved:
                print(f"[ctx] fetching: {key}")
                self._resolved[key] = await fetch_fn()
            else:
                print(f"[ctx] reused:   {key}")
        return self._resolved[key]

async def fetch_market_data() -> str:
    await asyncio.sleep(0.15)  # simulate API latency
    return "EUR/USD: 1.085, S&P500: 5,240, Gold: $2,330/oz"

async def branch(ctx: RequestContext, question: str) -> str:
    market_data = await ctx.resolve("market_data", fetch_market_data)
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=96,
        messages=[{"role": "user", "content": f"Data: {market_data}\n\nQ: {question}"}],
    )
    return resp.content[0].text

async def main():
    ctx = RequestContext()  # one context per top-level request
    questions = [
        "Is the dollar strong against the euro today?",
        "How is the S&P 500 performing?",
        "What is the current gold price?",
    ]
    results = await asyncio.gather(*[branch(ctx, q) for q in questions])
    for q, r in zip(questions, results):
        print(f"Q: {q}\nA: {r[:100]}\n")
    print(f"[ctx] total fetches: {len(ctx._resolved)}")

asyncio.run(main())
```

**Expected Token Savings:** Exactly one fetch per unique dependency per request; dependency injection pattern makes sharing explicit and testable without global state.
**Environment:** Structured multi-agent pipelines; systems where branches are functions rather than separate processes.

---

### Option 6 — LRU cache with TTL decorator for tool functions

```python
import anthropic
import asyncio
import time
import functools
from typing import Any

client = anthropic.AsyncAnthropic()

def lru_ttl_cache(maxsize: int = 128, ttl: float = 300.0):
    """LRU cache that expires entries after TTL seconds."""
    def decorator(fn):
        _cache: dict[str, tuple[Any, float]] = {}
        _order: list[str] = []

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            key = str(args) + str(sorted(kwargs.items()))
            entry = _cache.get(key)
            if entry:
                result, ts = entry
                if time.monotonic() - ts < ttl:
                    print(f"[lru] HIT  {fn.__name__}{args}")
                    return result
                del _cache[key]
                _order.remove(key)

            print(f"[lru] MISS {fn.__name__}{args}")
            result = await fn(*args, **kwargs)

            if len(_cache) >= maxsize:
                oldest = _order.pop(0)
                del _cache[oldest]

            _cache[key] = (result, time.monotonic())
            _order.append(key)
            return result

        return wrapper
    return decorator

@lru_ttl_cache(maxsize=64, ttl=120.0)
async def web_search(query: str) -> str:
    await asyncio.sleep(0.1)
    return f"Results for '{query}': [article1, article2, article3]"

@lru_ttl_cache(maxsize=32, ttl=60.0)
async def database_lookup(table: str, where: str) -> str:
    await asyncio.sleep(0.05)
    return f"DB {table} WHERE {where}: [row1, row2]"

async def agent_branch(topic: str) -> str:
    search = await web_search("AI regulation Europe 2024")  # same across all branches
    db     = await database_lookup("companies", "sector=AI")
    resp   = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=96,
        messages=[{"role": "user", "content": f"Search: {search}\nDB: {db}\nTopic: {topic}"}],
    )
    return resp.content[0].text

async def main():
    topics = [
        "regulatory risk for startups",
        "compliance costs for enterprises",
        "impact on open-source AI",
    ]
    results = await asyncio.gather(*[agent_branch(t) for t in topics])
    for t, r in zip(topics, results):
        print(f"[{t}] {r[:100]}")

asyncio.run(main())
```

**Expected Token Savings:** LRU eviction prevents unbounded memory growth; TTL ensures stale data is refreshed; 3 branches sharing 2 cached tools = 4 avoided fetches.
**Environment:** Long-running agent processes with diverse queries; tool functions that are pure (same input → same output within TTL).

---

## Comparison

| Option | Scope | Concurrency Safe | Persistence | Cache Stampede Protection | Best For |
|---|---|---|---|---|---|
| 1. Dict cache + hash | In-process | No (sync) | None | No | Simple sequential fan-out |
| 2. asyncio.Lock cache | In-process | Yes | None | Yes | Async concurrent branches |
| 3. Redis-backed | Distributed | Yes (Redis atomic) | TTL-based | Yes (SETNX) | Multi-process / multi-host agents |
| 4. Prompt caching | API-level | Yes | API-managed | N/A | Repeated large context blocks |
| 5. Request context DI | Per-request | Yes | None | Yes (Lock) | Structured pipelines with DI pattern |
| 6. LRU + TTL decorator | In-process | Partial | None | No | Long-running processes; pure tool functions |
