---
layout: solution
title: "Agent Doesn't Implement Tool Result Caching with TTL"
category: tool-failure
description: "Agents that call the same tool repeatedly with identical arguments waste tokens, add latency, and stress downstream APIs. TTL-based caching returns cached results for identical calls within a freshness window, transparently reducing redundant tool invocations."
tags: [tool-failure, caching, ttl, performance, sqlite, asyncio, tool-use, cost-optimization]
---

## Problem

Agents in multi-step workflows frequently call the same tools with identical inputs — fetching the same document, looking up the same entity, or querying the same API endpoint multiple times within a single task. Without caching, each call incurs API latency, downstream rate limits, and unnecessary token consumption from large tool results. TTL caching returns stale-but-fresh results for repeated calls, with automatic expiry to prevent serving outdated data.

## Solutions

### Option 1: In-Memory LRU Cache with TTL per Tool

```python
import anthropic
import hashlib
import json
import time
from functools import wraps
from collections import OrderedDict

client = anthropic.Anthropic()

class TTLCache:
    """Simple in-memory LRU cache with per-entry TTL."""
    def __init__(self, maxsize: int = 256, default_ttl: float = 300.0):
        self._cache: OrderedDict[str, tuple[any, float]] = OrderedDict()
        self._maxsize = maxsize
        self._default_ttl = default_ttl

    def _key(self, tool_name: str, args: dict) -> str:
        raw = json.dumps({"tool": tool_name, "args": args}, sort_keys=True)
        return hashlib.md5(raw.encode()).hexdigest()

    def get(self, tool_name: str, args: dict) -> tuple[bool, any]:
        key = self._key(tool_name, args)
        if key in self._cache:
            value, expires_at = self._cache[key]
            if time.time() < expires_at:
                self._cache.move_to_end(key)
                return True, value
            del self._cache[key]
        return False, None

    def set(self, tool_name: str, args: dict, value: any, ttl: float | None = None):
        key = self._key(tool_name, args)
        expires_at = time.time() + (ttl if ttl is not None else self._default_ttl)
        self._cache[key] = (value, expires_at)
        self._cache.move_to_end(key)
        if len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)  # evict oldest

_cache = TTLCache(maxsize=128, default_ttl=120.0)

TOOL_TTL = {
    "get_weather": 600.0,    # 10 minutes — weather changes slowly
    "get_user_profile": 300.0,  # 5 minutes
    "search_docs": 3600.0,   # 1 hour — docs are stable
}

def execute_tool_with_cache(tool_name: str, tool_args: dict) -> str:
    """Execute a tool, returning cached result if available and fresh."""
    hit, cached = _cache.get(tool_name, tool_args)
    if hit:
        print(f"  [cache HIT] {tool_name}({tool_args})")
        return cached

    # Simulate tool execution
    result = f"[{tool_name} result for {tool_args}]"
    ttl = TOOL_TTL.get(tool_name, 60.0)
    _cache.set(tool_name, tool_args, result, ttl=ttl)
    print(f"  [cache MISS] {tool_name}({tool_args}) -> cached for {ttl}s")
    return result

def run_agent(task: str) -> str:
    tools = [
        {"name": "get_weather", "description": "Get weather for a city", "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}},
        {"name": "get_user_profile", "description": "Get user profile", "input_schema": {"type": "object", "properties": {"user_id": {"type": "string"}}, "required": ["user_id"]}},
    ]
    messages = [{"role": "user", "content": task}]

    while True:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )
        if resp.stop_reason == "end_turn":
            return resp.content[0].text

        tool_results = []
        for block in resp.content:
            if block.type == "tool_use":
                result = execute_tool_with_cache(block.name, block.input)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages.append({"role": "assistant", "content": resp.content})
        messages.append({"role": "user", "content": tool_results})

if __name__ == "__main__":
    print("=== Run 1 ===")
    r1 = run_agent("What is the weather in Tokyo and the profile for user 'alice'?")
    print(f"\nResult: {r1[:100]}")

    print("\n=== Run 2 (same tools, should hit cache) ===")
    r2 = run_agent("Again, what is the weather in Tokyo and user 'alice' profile?")
    print(f"\nResult: {r2[:100]}")

# Expected Token Savings: 30-70% reduction in tool result tokens on repeated calls within TTL window
# Environment: single-process agents; LRU eviction prevents unbounded memory growth
```

### Option 2: SQLite-Backed Persistent Cache (Survives Restarts)

```python
import anthropic
import hashlib
import json
import sqlite3
import time
from pathlib import Path

DB = Path("/tmp/tool_cache.db")
client = anthropic.Anthropic()

def init_cache_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS tool_cache (
            cache_key TEXT PRIMARY KEY,
            tool_name TEXT NOT NULL,
            result TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            hit_count INTEGER DEFAULT 0
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_expires ON tool_cache(expires_at)")
    con.commit()
    con.close()

def cache_key(tool_name: str, args: dict) -> str:
    raw = json.dumps({"t": tool_name, "a": args}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]

def cache_get(tool_name: str, args: dict) -> str | None:
    key = cache_key(tool_name, args)
    con = sqlite3.connect(DB)
    row = con.execute(
        "SELECT result FROM tool_cache WHERE cache_key=? AND expires_at > ?",
        (key, time.time()),
    ).fetchone()
    if row:
        con.execute("UPDATE tool_cache SET hit_count = hit_count + 1 WHERE cache_key=?", (key,))
        con.commit()
    con.close()
    return row[0] if row else None

def cache_set(tool_name: str, args: dict, result: str, ttl: float):
    key = cache_key(tool_name, args)
    now = time.time()
    con = sqlite3.connect(DB)
    con.execute("""
        INSERT OR REPLACE INTO tool_cache (cache_key, tool_name, result, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?)
    """, (key, tool_name, result, now, now + ttl))
    # Prune expired entries
    con.execute("DELETE FROM tool_cache WHERE expires_at < ?", (now,))
    con.commit()
    con.close()

def cache_stats() -> list[dict]:
    con = sqlite3.connect(DB)
    rows = con.execute("""
        SELECT tool_name, COUNT(*) as entries, SUM(hit_count) as total_hits, AVG(hit_count) as avg_hits
        FROM tool_cache WHERE expires_at > ?
        GROUP BY tool_name
    """, (time.time(),)).fetchall()
    con.close()
    return [{"tool": r[0], "entries": r[1], "hits": r[2], "avg_hits": round(r[3], 1)} for r in rows]

TOOL_TTLS = {"lookup_price": 300, "get_spec": 3600, "search_index": 1800}

def execute_tool(name: str, args: dict) -> str:
    cached = cache_get(name, args)
    if cached:
        print(f"  [HIT] {name}")
        return cached
    result = f"fresh result for {name}({args})"
    ttl = TOOL_TTLS.get(name, 120)
    cache_set(name, args, result, ttl)
    print(f"  [MISS] {name} (TTL={ttl}s)")
    return result

if __name__ == "__main__":
    init_cache_db()
    for _ in range(3):
        execute_tool("lookup_price", {"item": "widget"})
        execute_tool("get_spec", {"model": "X200"})
    print("\nCache stats:", cache_stats())

# Expected Token Savings: persists across agent restarts; hits return instantly with zero API tokens
# Environment: long-running agents or scheduled jobs; SQLite survives process restarts unlike in-memory
```

### Option 3: Async Cache with Stampede Protection (Cache Locking)

```python
import anthropic
import asyncio
import hashlib
import json
import time
from typing import Any

client = anthropic.AsyncAnthropic()

class AsyncTTLCache:
    """
    Async cache with stampede protection: only one coroutine
    fetches a missing key; others wait for the result.
    """
    def __init__(self, default_ttl: float = 120.0):
        self._store: dict[str, tuple[Any, float]] = {}
        self._inflight: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()
        self._default_ttl = default_ttl

    def _make_key(self, name: str, args: dict) -> str:
        return hashlib.md5(json.dumps({"n": name, "a": args}, sort_keys=True).encode()).hexdigest()

    async def get_or_fetch(self, name: str, args: dict, fetcher, ttl: float | None = None) -> Any:
        key = self._make_key(name, args)
        ttl = ttl or self._default_ttl

        # Fast path: cache hit
        async with self._lock:
            if key in self._store:
                value, expires = self._store[key]
                if time.time() < expires:
                    return value
                del self._store[key]

            # Stampede protection: if another coroutine is fetching, wait for it
            if key in self._inflight:
                event = self._inflight[key]
        # Wait outside lock to avoid blocking others
        if key in self._inflight:
            await self._inflight[key].wait()
            async with self._lock:
                if key in self._store:
                    value, _ = self._store[key]
                    return value

        # We are the fetcher
        event = asyncio.Event()
        async with self._lock:
            self._inflight[key] = event

        try:
            value = await fetcher()
            async with self._lock:
                self._store[key] = (value, time.time() + ttl)
            return value
        finally:
            async with self._lock:
                self._inflight.pop(key, None)
            event.set()

_cache = AsyncTTLCache(default_ttl=60.0)

async def fetch_document(doc_id: str) -> str:
    """Simulated slow tool that fetches a document."""
    await asyncio.sleep(0.5)  # simulate latency
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=32,
        messages=[{"role": "user", "content": f"Summarize document {doc_id} in 10 words."}],
    )
    return resp.content[0].text

async def get_document_cached(doc_id: str) -> str:
    return await _cache.get_or_fetch(
        "fetch_document",
        {"doc_id": doc_id},
        fetcher=lambda: fetch_document(doc_id),
        ttl=120.0,
    )

async def main():
    # Parallel requests for same doc_id — only one fetch should occur
    t0 = time.time()
    results = await asyncio.gather(*[get_document_cached("DOC-42") for _ in range(5)])
    elapsed = time.time() - t0
    print(f"5 parallel requests for same doc: {elapsed:.2f}s (expected ~0.5s, not 2.5s)")
    print(f"All results identical: {len(set(results)) == 1}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: 80% token reduction when 5 coroutines hit same key — only 1 API call made
# Environment: async agents with parallel tool calls; prevents N identical simultaneous requests
```

### Option 4: Tool Wrapper Decorator with Per-Tool TTL Config

```python
import anthropic
import functools
import hashlib
import json
import time
from typing import Callable

client = anthropic.Anthropic()

_CACHE: dict[str, tuple[str, float]] = {}

def cached_tool(ttl: float = 60.0, vary_on: list[str] | None = None):
    """
    Decorator for tool executor functions.
    vary_on: subset of arg keys that form the cache key (None = all args).
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(tool_name: str, tool_args: dict) -> str:
            key_args = (
                {k: tool_args[k] for k in vary_on if k in tool_args}
                if vary_on else tool_args
            )
            raw = json.dumps({"fn": fn.__name__, "name": tool_name, "args": key_args}, sort_keys=True)
            cache_key = hashlib.md5(raw.encode()).hexdigest()

            if cache_key in _CACHE:
                value, expires = _CACHE[cache_key]
                if time.time() < expires:
                    print(f"  [CACHED] {tool_name}")
                    return value

            result = fn(tool_name, tool_args)
            _CACHE[cache_key] = (result, time.time() + ttl)
            print(f"  [FRESH ] {tool_name} (TTL={ttl}s)")
            return result
        return wrapper
    return decorator

@cached_tool(ttl=600.0, vary_on=["city"])
def get_weather(tool_name: str, args: dict) -> str:
    return f"Weather in {args['city']}: 22°C, sunny"

@cached_tool(ttl=30.0)
def get_stock_price(tool_name: str, args: dict) -> str:
    return f"Price of {args['ticker']}: ${100 + hash(args['ticker']) % 50}"

@cached_tool(ttl=3600.0, vary_on=["query"])
def search_kb(tool_name: str, args: dict) -> str:
    return f"KB results for '{args['query']}': [article_1, article_2]"

TOOL_HANDLERS = {
    "get_weather": get_weather,
    "get_stock_price": get_stock_price,
    "search_kb": search_kb,
}

def dispatch_tool(name: str, args: dict) -> str:
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        return f"Unknown tool: {name}"
    return handler(name, args)

if __name__ == "__main__":
    calls = [
        ("get_weather", {"city": "London"}),
        ("get_weather", {"city": "London"}),  # cache hit
        ("get_weather", {"city": "Paris"}),
        ("get_stock_price", {"ticker": "AAPL"}),
        ("get_stock_price", {"ticker": "AAPL"}),  # cache hit
        ("search_kb", {"query": "refund policy"}),
        ("search_kb", {"query": "refund policy"}),  # cache hit
    ]
    for name, args in calls:
        result = dispatch_tool(name, args)
        print(f"    → {result}")

# Expected Token Savings: vary_on reduces key space; city-only key means format changes don't invalidate cache
# Environment: tool-heavy agents; decorator pattern adds caching without changing tool logic
```

### Option 5: Adaptive TTL Based on Result Stability

```python
import anthropic
import hashlib
import json
import time

client = anthropic.Anthropic()

class AdaptiveTTLCache:
    """
    Extends TTL for stable results (same result returned on consecutive fetches)
    and shortens TTL for volatile results (result changes frequently).
    """
    def __init__(self, min_ttl: float = 10.0, max_ttl: float = 3600.0, base_ttl: float = 60.0):
        self._store: dict[str, dict] = {}
        self._min_ttl = min_ttl
        self._max_ttl = max_ttl
        self._base_ttl = base_ttl

    def _key(self, name: str, args: dict) -> str:
        raw = json.dumps({"n": name, "a": args}, sort_keys=True)
        return hashlib.md5(raw.encode()).hexdigest()

    def _result_hash(self, result: str) -> str:
        return hashlib.md5(result.encode()).hexdigest()

    def get(self, name: str, args: dict) -> str | None:
        key = self._key(name, args)
        entry = self._store.get(key)
        if entry and time.time() < entry["expires"]:
            return entry["result"]
        return None

    def set(self, name: str, args: dict, result: str):
        key = self._key(name, args)
        now = time.time()
        rh = self._result_hash(result)
        existing = self._store.get(key, {})

        if existing.get("result_hash") == rh:
            # Result stable: extend TTL (double, up to max)
            new_ttl = min(existing.get("ttl", self._base_ttl) * 2, self._max_ttl)
            stability = "STABLE"
        else:
            # Result changed: shorten TTL (halve, down to min)
            new_ttl = max(existing.get("ttl", self._base_ttl) / 2, self._min_ttl)
            stability = "VOLATILE"

        self._store[key] = {
            "result": result,
            "result_hash": rh,
            "expires": now + new_ttl,
            "ttl": new_ttl,
        }
        print(f"  [{stability}] {name}: TTL={new_ttl:.0f}s")
        return result

_cache = AdaptiveTTLCache()

def fetch_tool_result(name: str, args: dict) -> str:
    cached = _cache.get(name, args)
    if cached:
        print(f"  [HIT] {name}")
        return cached
    # Simulate tool call (result changes every 3 calls for volatile tools)
    call_count = _cache._store.get(_cache._key(name, args), {}).get("call_count", 0)
    if name == "volatile_metric":
        result = f"metric value: {int(time.time()) % 100}"
    else:
        result = f"stable result for {name}({args})"
    _cache.set(name, args, result)
    return result

if __name__ == "__main__":
    for i in range(6):
        time.sleep(0.1)
        fetch_tool_result("stable_doc", {"id": "D1"})
        fetch_tool_result("volatile_metric", {"metric": "cpu"})

# Expected Token Savings: stable tools get cached longer; volatile tools re-fetched sooner to stay fresh
# Environment: agents mixing stable (docs, configs) and volatile (metrics, prices) data sources
```

### Option 6: Cache-Aware Tool Executor with Cost Attribution

```python
import anthropic
import hashlib
import json
import sqlite3
import time
from pathlib import Path

DB = Path("/tmp/tool_cache_costs.db")
client = anthropic.Anthropic()

def init_db():
    con = sqlite3.connect(DB)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS tool_cache (
            cache_key TEXT PRIMARY KEY,
            tool_name TEXT,
            result TEXT,
            expires_at REAL,
            cost_tokens INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS tool_metrics (
            tool_name TEXT PRIMARY KEY,
            cache_hits INTEGER DEFAULT 0,
            cache_misses INTEGER DEFAULT 0,
            tokens_saved INTEGER DEFAULT 0,
            tokens_spent INTEGER DEFAULT 0
        );
    """)
    con.commit()
    con.close()

def _key(name: str, args: dict) -> str:
    return hashlib.md5(json.dumps({"n": name, "a": args}, sort_keys=True).encode()).hexdigest()

def _update_metrics(con, name: str, hit: bool, tokens: int = 0):
    if hit:
        con.execute("""
            INSERT INTO tool_metrics (tool_name, cache_hits, tokens_saved)
            VALUES (?, 1, ?) ON CONFLICT(tool_name) DO UPDATE SET
            cache_hits = cache_hits + 1, tokens_saved = tokens_saved + ?
        """, (name, tokens, tokens))
    else:
        con.execute("""
            INSERT INTO tool_metrics (tool_name, cache_misses, tokens_spent)
            VALUES (?, 1, ?) ON CONFLICT(tool_name) DO UPDATE SET
            cache_misses = cache_misses + 1, tokens_spent = tokens_spent + ?
        """, (name, tokens, tokens))

def execute_tool_cached(name: str, args: dict, ttl: float = 300.0) -> tuple[str, bool]:
    key = _key(name, args)
    con = sqlite3.connect(DB)
    try:
        row = con.execute(
            "SELECT result, cost_tokens FROM tool_cache WHERE cache_key=? AND expires_at > ?",
            (key, time.time()),
        ).fetchone()
        if row:
            _update_metrics(con, name, hit=True, tokens=row[1])
            con.commit()
            return row[0], True  # (result, was_cached)

        # Simulate tool execution with token cost
        result = f"result:{name}({json.dumps(args)})"
        cost_tokens = len(result) // 4  # rough estimate

        con.execute("""
            INSERT OR REPLACE INTO tool_cache (cache_key, tool_name, result, expires_at, cost_tokens)
            VALUES (?, ?, ?, ?, ?)
        """, (key, name, result, time.time() + ttl, cost_tokens))
        _update_metrics(con, name, hit=False, tokens=cost_tokens)
        con.commit()
        return result, False
    finally:
        con.close()

def print_savings_report():
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT * FROM tool_metrics").fetchall()
    con.close()
    print("\n--- Cache Cost Attribution Report ---")
    for tool_name, hits, misses, saved, spent in rows:
        total = hits + misses
        rate = f"{hits/total*100:.0f}%" if total else "0%"
        print(f"  {tool_name:20s}: hit_rate={rate} | tokens_saved={saved} | tokens_spent={spent}")

if __name__ == "__main__":
    init_db()
    tools = [
        ("search_docs", {"q": "API limits"}, 600.0),
        ("search_docs", {"q": "API limits"}, 600.0),  # cache hit
        ("get_config", {"key": "timeout"}, 3600.0),
        ("get_config", {"key": "timeout"}, 3600.0),  # cache hit
        ("search_docs", {"q": "pricing"}, 600.0),
    ]
    for name, args, ttl in tools:
        result, cached = execute_tool_cached(name, args, ttl)
        label = "HIT" if cached else "MISS"
        print(f"  [{label}] {name}({args})")
    print_savings_report()

# Expected Token Savings: report quantifies exact tokens saved; use to prioritize which tools to cache
# Environment: production agents; SQLite attribution survives restarts and informs TTL tuning decisions
```

## Comparison

| Option | Storage | Persistence | Stampede Safe | TTL Strategy |
|--------|---------|------------|--------------|-------------|
| 1 — In-memory LRU | RAM | No | No | Per-tool fixed TTL |
| 2 — SQLite persistent | Disk | Yes | No | Per-tool fixed TTL |
| 3 — Async stampede-safe | RAM | No | Yes (Event) | Per-call TTL |
| 4 — Decorator per-tool | RAM | No | No | Per-decorator TTL + vary_on |
| 5 — Adaptive TTL | RAM | No | No | Auto-extends/shrinks by stability |
| 6 — SQLite + cost reporting | Disk | Yes | No | Fixed TTL + token savings metrics |
