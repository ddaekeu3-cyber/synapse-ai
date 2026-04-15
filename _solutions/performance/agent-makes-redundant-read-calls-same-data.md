---
layout: solution
title: "Agent Makes Redundant Read Calls for Same Data — Unnecessary Latency and Cost"
category: performance
description: "Agent calls the same API endpoint or reads the same file multiple times in a single task. Fetches user profile 4 times for 4 different operations. Each call adds latency and tokens, but the data hasn't changed."
tags: [performance, caching, memoization, redundant-calls, latency, deduplication, reads]
---

# Agent Makes Redundant Read Calls for Same Data — Unnecessary Latency and Cost

## Symptom
- Agent calls `GET /users/123` four times in one task execution
- Same database row read 10 times across 10 tool calls — data hasn't changed
- Agent re-reads a config file on every operation instead of once at startup
- Logs show identical API requests within milliseconds of each other
- Task that should take 2 seconds takes 20 because of repeated network round-trips

## Root Cause
Agent tools are stateless by design — each tool call is independent and has no memory of previous calls. Without an explicit cache layer, the agent will re-fetch the same data whenever it's needed in a new tool, even if the same data was fetched two steps earlier in the same task.

## Fix

### Option 1: In-memory request cache with TTL

```python
import time
import hashlib
import json
from typing import Callable, Any

class RequestCache:
    """
    Cache tool call results for the duration of a task.
    Prevents redundant reads for data that doesn't change mid-task.
    """

    def __init__(self, default_ttl: float = 300.0):
        self.default_ttl = default_ttl
        self._cache: dict[str, dict] = {}

    def _key(self, fn_name: str, args: tuple, kwargs: dict) -> str:
        payload = json.dumps({"fn": fn_name, "args": args, "kwargs": kwargs}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def get(self, key: str) -> tuple[bool, Any]:
        if key not in self._cache:
            return False, None
        entry = self._cache[key]
        if time.monotonic() > entry["expires"]:
            del self._cache[key]
            return False, None
        return True, entry["value"]

    def set(self, key: str, value: Any, ttl: float = None):
        self._cache[key] = {
            "value": value,
            "expires": time.monotonic() + (ttl or self.default_ttl)
        }

    def cached(self, ttl: float = None):
        """Decorator: cache the return value of a tool function"""
        def decorator(fn: Callable) -> Callable:
            def wrapper(*args, **kwargs):
                key = self._key(fn.__name__, args, kwargs)
                hit, value = self.get(key)
                if hit:
                    print(f"Cache hit: {fn.__name__}({args}, {kwargs})")
                    return value
                value = fn(*args, **kwargs)
                self.set(key, value, ttl)
                return value
            return wrapper
        return decorator

cache = RequestCache(default_ttl=300)

@cache.cached(ttl=60)
def get_user(user_id: str) -> dict:
    print(f"Fetching user {user_id} from API...")  # Only prints once per 60s
    return api.get(f"/users/{user_id}")

# In one task, called 4 times:
user = get_user("123")  # → API call
user = get_user("123")  # → Cache hit (no API call)
user = get_user("123")  # → Cache hit
user = get_user("123")  # → Cache hit
```

### Option 2: Task-scoped data prefetch

```python
class TaskContext:
    """
    Prefetch all data needed for a task at the start.
    Single read per entity — tools read from context, not from API.
    """

    def __init__(self):
        self._data: dict[str, Any] = {}

    async def prefetch(self, keys: dict[str, Callable]):
        """
        keys: {"user": lambda: api.get_user(user_id), "config": lambda: load_config()}
        Fetches all in parallel, stores results.
        """
        import asyncio
        tasks = {name: asyncio.create_task(fn()) for name, fn in keys.items()}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for name, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                raise RuntimeError(f"Failed to prefetch '{name}': {result}")
            self._data[name] = result
        print(f"Prefetched: {list(self._data.keys())}")

    def get(self, key: str) -> Any:
        if key not in self._data:
            raise KeyError(f"'{key}' not in task context. Add it to prefetch().")
        return self._data[key]

# At task start — one parallel fetch for all needed data:
ctx = TaskContext()
await ctx.prefetch({
    "user": lambda: api.get_user(user_id),
    "account": lambda: api.get_account(account_id),
    "permissions": lambda: api.get_permissions(user_id),
    "config": lambda: load_config(),
})

# Tools read from context — zero additional API calls:
def process_order(order: dict) -> dict:
    user = ctx.get("user")          # No API call
    perms = ctx.get("permissions")  # No API call
    if "place_order" not in perms["allowed"]:
        raise PermissionError("User cannot place orders")
    return create_order(order, user)
```

### Option 3: Memoize tool calls within agent execution

```python
import functools
import asyncio

def memoize_for_task(fn):
    """
    Memoize an async tool function for the duration of the current task.
    Call clear_task_memo() at task completion.
    """
    _cache = {}

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        key = (args, tuple(sorted(kwargs.items())))
        if key not in _cache:
            _cache[key] = await fn(*args, **kwargs)
            print(f"Fetched: {fn.__name__}({args})")
        else:
            print(f"Memoized: {fn.__name__}({args})")
        return _cache[key]

    wrapper.clear = lambda: _cache.clear()
    return wrapper

@memoize_for_task
async def fetch_product(product_id: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"https://api.example.com/products/{product_id}")
        return resp.json()

# In a task that needs the same product in 5 different operations:
p = await fetch_product("prod_123")  # → HTTP call
p = await fetch_product("prod_123")  # → Memoized
p = await fetch_product("prod_123")  # → Memoized
# → 1 HTTP call instead of 5

# After task completes:
fetch_product.clear()  # Reset for next task
```

### Option 4: Detect and log duplicate calls automatically

```python
import collections
import time

class CallTracker:
    """
    Track tool calls and warn about duplicates — helps identify redundant reads.
    """

    def __init__(self, warn_after: int = 2):
        self.warn_after = warn_after
        self._calls: dict[str, list[float]] = collections.defaultdict(list)

    def record(self, fn_name: str, args: tuple) -> bool:
        """Returns True if this is a duplicate call"""
        key = f"{fn_name}:{args}"
        self._calls[key].append(time.monotonic())
        count = len(self._calls[key])

        if count >= self.warn_after:
            print(
                f"DUPLICATE CALL WARNING: {fn_name}{args} called {count} times. "
                f"Consider caching this read."
            )
            return True
        return False

    def report(self) -> dict:
        """Summarize duplicate calls after task completes"""
        duplicates = {k: v for k, v in self._calls.items() if len(v) > 1}
        if duplicates:
            print("\nDuplicate calls this task:")
            for key, timestamps in duplicates.items():
                print(f"  {key}: {len(timestamps)} calls")
        return duplicates

tracker = CallTracker(warn_after=2)

def tracked_tool(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        tracker.record(fn.__name__, args)
        return fn(*args, **kwargs)
    return wrapper

@tracked_tool
def get_config(key: str) -> str:
    return load_from_db(key)
```

### Option 5: Batch reads instead of individual repeated calls

```python
async def batch_fetch_users(user_ids: list[str]) -> dict[str, dict]:
    """
    Fetch multiple users in one API call instead of N individual calls.
    """
    if not user_ids:
        return {}

    # Deduplicate IDs before fetching
    unique_ids = list(set(user_ids))

    async with httpx.AsyncClient() as client:
        # Single batch request (if API supports it)
        resp = await client.post(
            "https://api.example.com/users/batch",
            json={"ids": unique_ids}
        )
        users = resp.json()

    # Return dict keyed by user_id for easy lookup
    return {u["id"]: u for u in users}

# Instead of:
# for user_id in task_user_ids:
#     user = await get_user(user_id)  # N API calls

# Do:
users_map = await batch_fetch_users(task_user_ids)  # 1 API call
for user_id in task_user_ids:
    user = users_map.get(user_id)  # Dict lookup, no API call
```

### Option 6: System prompt to discourage redundant reads

```
System prompt:
"Efficiency rules for data access:

1. Read each data source ONCE per task. Store the result in your working context.
   Do NOT call the same tool with the same arguments more than once.

2. If you need the same data in multiple steps, read it first, then reference
   the stored result throughout the task.

3. Before calling a read tool, check: 'Did I already fetch this in an earlier step?'
   If yes, use the result from that step.

4. Batch reads when possible:
   - Fetch all users you'll need at the start
   - Read config files once at task initialization
   - Prefer GET /resources?ids=1,2,3 over three GET /resources/1 calls"
```

## Read Deduplication Impact

| Pattern | API calls (10-step task) | Latency |
|---|---|---|
| No cache — N reads per resource | 30-50 | 15-25s |
| Task-scoped cache | 3-5 (unique resources only) | 1.5-2.5s |
| Prefetch all at start | 1 parallel batch | 0.5s |
| Batch API + cache | 1 call | 0.3s |

## Expected Token Savings
30 redundant API reads × 500 tokens each: ~15,000 tokens + 25s latency
Task-scoped cache + prefetch: ~2,500 tokens + 2s latency

## Environment
- Any agent executing multi-step tasks that access shared resources (users, configs, products)
- Source: direct experience; redundant reads are the most common performance bottleneck in agent pipelines
