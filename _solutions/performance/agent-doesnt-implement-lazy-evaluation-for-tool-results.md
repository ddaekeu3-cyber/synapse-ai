---
layout: solution
title: "Agent Doesn't Implement Lazy Evaluation for Tool Results"
category: performance
description: "Defer expensive tool calls until their results are actually needed — using lazy wrappers, demand-driven evaluation, and memoization to avoid executing tools whose outputs are never consumed by the model."
tags: [performance, lazy-evaluation, tool-calls, memoization, cost-optimization, python]
---

# Agent Doesn't Implement Lazy Evaluation for Tool Results

Agents that eagerly execute all tool calls upfront waste API quota and latency on results the model may never use — especially in multi-branch reasoning where only one path is ultimately taken. Lazy evaluation defers execution until the result is first accessed, then caches it so repeated access is free.

## Option 1: Lazy Wrapper with On-Demand Evaluation

```python
import anthropic
import time

client = anthropic.Anthropic()

class Lazy:
    """Wraps a callable; evaluates only on first .get() call, then caches."""
    def __init__(self, fn, *args, **kwargs):
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self._value = None
        self._evaluated = False
        self._eval_count = 0

    def get(self):
        if not self._evaluated:
            self._value = self._fn(*self._args, **self._kwargs)
            self._evaluated = True
            self._eval_count += 1
        return self._value

    @property
    def evaluated(self):
        return self._evaluated

# Simulated expensive tools
def fetch_user_profile(user_id: str) -> dict:
    print(f"  [tool] fetch_user_profile({user_id!r}) called")
    time.sleep(0.01)  # simulate I/O
    return {"id": user_id, "name": "Alice", "tier": "pro"}

def fetch_billing_info(user_id: str) -> dict:
    print(f"  [tool] fetch_billing_info({user_id!r}) called")
    time.sleep(0.02)  # more expensive
    return {"user_id": user_id, "plan": "annual", "balance": 99.0}

def fetch_usage_stats(user_id: str) -> dict:
    print(f"  [tool] fetch_usage_stats({user_id!r}) called")
    time.sleep(0.015)
    return {"user_id": user_id, "calls_today": 47, "quota": 1000}

# Define all tools lazily — nothing executes yet
user_id = "user-123"
profile  = Lazy(fetch_user_profile, user_id)
billing  = Lazy(fetch_billing_info, user_id)
usage    = Lazy(fetch_usage_stats,  user_id)

print("Lazy objects created. No tools called yet.")

# Simulate: agent only needs profile and usage for this request
resp = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=128,
    messages=[{
        "role": "user",
        "content": (
            f"User profile: {profile.get()}\n"
            f"Usage stats: {usage.get()}\n"
            "Is this user close to their quota? Answer yes/no with a reason."
        ),
    }],
)
print(f"\nAgent: {resp.content[0].text[:100]}")
print(f"\nEvaluated: profile={profile.evaluated}, billing={billing.evaluated}, usage={usage.evaluated}")
# billing was never accessed — tool never called

# Expected Token Savings: Skipped billing call saves latency and any downstream quota cost
# Environment: Lazy wrapper is zero-dependency; compose with async for I/O-bound tools
```

## Option 2: Async Lazy with Concurrent Background Prefetch

```python
import anthropic
import asyncio
import time

client = anthropic.AsyncAnthropic()

class AsyncLazy:
    """Async lazy wrapper; evaluates on first await, caches result."""
    def __init__(self, coro_fn, *args, **kwargs):
        self._fn = coro_fn
        self._args = args
        self._kwargs = kwargs
        self._result = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    def prefetch(self):
        """Start evaluation in background without blocking."""
        if self._task is None:
            self._task = asyncio.create_task(self._fn(*self._args, **self._kwargs))

    async def get(self):
        async with self._lock:
            if self._result is None:
                if self._task is None:
                    self._task = asyncio.create_task(self._fn(*self._args, **self._kwargs))
                self._result = await self._task
            return self._result

# Simulated async tools
async def async_fetch_profile(uid: str) -> dict:
    await asyncio.sleep(0.05)
    return {"id": uid, "name": "Bob", "role": "admin"}

async def async_fetch_permissions(uid: str) -> list[str]:
    await asyncio.sleep(0.08)
    return ["read", "write", "admin"]

async def async_fetch_audit_log(uid: str) -> list[str]:
    await asyncio.sleep(0.12)  # expensive — skip if not needed
    return [f"action-{i}" for i in range(100)]

async def main():
    uid = "user-456"
    profile     = AsyncLazy(async_fetch_profile,     uid)
    permissions = AsyncLazy(async_fetch_permissions, uid)
    audit_log   = AsyncLazy(async_fetch_audit_log,   uid)

    # Prefetch profile + permissions (likely needed); skip audit_log
    profile.prefetch()
    permissions.prefetch()
    # audit_log NOT prefetched

    t0 = time.monotonic()
    # Access only profile + permissions
    p = await profile.get()
    perms = await permissions.get()
    elapsed = (time.monotonic() - t0) * 1000

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": f"User: {p}, Permissions: {perms}. Is this user an admin?",
        }],
    )
    print(f"Answer: {resp.content[0].text[:80]}")
    print(f"Tool fetch time: {elapsed:.0f}ms (audit_log skipped — saved ~120ms)")

asyncio.run(main())

# Expected Token Savings: Prefetch overlaps with model thinking time; skipped audit_log saves 120ms per call
# Environment: asyncio; prefetch() is optional — call get() directly for pure on-demand evaluation
```

## Option 3: Lazy Tool Graph — Only Evaluate What the Model Requests

```python
import anthropic
import json
import time

client = anthropic.Anthropic()

# Tool implementations (expensive)
_TOOL_IMPLS = {
    "get_weather": lambda city: {"city": city, "temp": 22, "condition": "sunny"},
    "get_forecast": lambda city, days: [{"day": i+1, "temp": 20+i} for i in range(days)],
    "get_uv_index": lambda city: {"city": city, "uv": 7, "risk": "high"},
    "get_air_quality": lambda city: {"city": city, "aqi": 42, "quality": "good"},
}

_call_log: list[str] = []

def lazy_tool_executor(tool_name: str, tool_input: dict) -> str:
    """Only called when model actually requests a tool."""
    _call_log.append(tool_name)
    print(f"  [EXEC] {tool_name}({tool_input})")
    time.sleep(0.01)  # simulate I/O
    impl = _TOOL_IMPLS.get(tool_name)
    if not impl:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})
    try:
        result = impl(**tool_input)
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})

# Define all tools to the model — but only execute what it calls
TOOLS = [
    {"name": "get_weather",     "description": "Current weather for a city",
     "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}},
    {"name": "get_forecast",    "description": "Multi-day forecast",
     "input_schema": {"type": "object", "properties": {
         "city": {"type": "string"}, "days": {"type": "integer"}}, "required": ["city", "days"]}},
    {"name": "get_uv_index",    "description": "UV index for a city",
     "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}},
    {"name": "get_air_quality", "description": "Air quality index",
     "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}},
]

def run_agent(question: str) -> str:
    messages = [{"role": "user", "content": question}]
    while True:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        )
        if resp.stop_reason == "end_turn":
            return next((b.text for b in resp.content if hasattr(b, "text")), "")

        tool_results = []
        for block in resp.content:
            if block.type == "tool_use":
                result = lazy_tool_executor(block.name, block.input)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages.append({"role": "assistant", "content": resp.content})
        messages.append({"role": "user", "content": tool_results})

# Question that only needs weather — model won't call uv_index or air_quality
_call_log.clear()
answer = run_agent("What's the weather in Paris right now?")
print(f"Answer: {answer[:80]}")
print(f"Tools called: {_call_log}")
print(f"Tools skipped: {set(_TOOL_IMPLS) - set(_call_log)}")

# Expected Token Savings: 4 tools defined but only 1 executed; lazy dispatch avoids 3 unnecessary tool calls
# Environment: tool_use stop_reason drives demand; add tool result caching for repeated calls
```

## Option 4: Lazy Evaluation with Memoization and TTL

```python
import anthropic
import time
import hashlib
import json

client = anthropic.Anthropic()

class MemoizedLazy:
    """Lazy evaluator with TTL-based cache. Shared across instances by key."""
    _cache: dict[str, tuple[object, float]] = {}

    def __init__(self, fn, cache_key: str, ttl_s: int = 60, *args, **kwargs):
        self._fn = fn
        self._key = cache_key
        self._ttl = ttl_s
        self._args = args
        self._kwargs = kwargs

    def get(self) -> object:
        cached = self._cache.get(self._key)
        if cached:
            value, expires_at = cached
            if time.time() < expires_at:
                print(f"  [CACHE HIT ] {self._key}")
                return value
            print(f"  [CACHE MISS] {self._key} (expired)")
        else:
            print(f"  [CACHE MISS] {self._key} (cold)")

        value = self._fn(*self._args, **self._kwargs)
        self._cache[self._key] = (value, time.time() + self._ttl)
        return value

    @classmethod
    def invalidate(cls, key: str):
        cls._cache.pop(key, None)

# Simulated expensive lookups
def db_lookup(query: str) -> dict:
    print(f"    -> DB query: {query!r}")
    time.sleep(0.02)
    return {"query": query, "result": f"data-{hash(query) % 1000}"}

def api_call(endpoint: str) -> dict:
    print(f"    -> API call: {endpoint!r}")
    time.sleep(0.03)
    return {"endpoint": endpoint, "status": "ok", "data": "response-xyz"}

# Build lazy+memoized tools
user_data  = MemoizedLazy(db_lookup,  "user-profile-123",  ttl_s=300, "SELECT * FROM users WHERE id=123")
config     = MemoizedLazy(db_lookup,  "agent-config",      ttl_s=600, "SELECT * FROM config")
ext_status = MemoizedLazy(api_call,   "service-status",    ttl_s=30,  "/health")

print("=== First request ===")
t0 = time.monotonic()
resp = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=64,
    messages=[{"role": "user", "content":
               f"User: {user_data.get()}, Config: {config.get()}. Is config valid?"}],
)
print(f"Answer: {resp.content[0].text[:60]} ({(time.monotonic()-t0)*1000:.0f}ms)")

print("\n=== Second request (same session) ===")
t0 = time.monotonic()
resp2 = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=64,
    messages=[{"role": "user", "content":
               f"User: {user_data.get()}, Config: {config.get()}. Still valid?"}],
)
print(f"Answer: {resp2.content[0].text[:60]} ({(time.monotonic()-t0)*1000:.0f}ms)")
# Second request: both cache hits — zero DB/API calls

# Expected Token Savings: TTL cache turns N repeated evaluations into 1; stale data surfaced via TTL expiry
# Environment: class-level cache is per-process; use Redis for multi-process cache with same TTL semantics
```

## Option 5: Demand-Driven Pipeline with Skip Tracking

```python
import anthropic
import time
from dataclasses import dataclass, field
from typing import Callable, Any

client = anthropic.Anthropic()

@dataclass
class LazyStage:
    name: str
    fn: Callable
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    _result: Any = field(default=None, init=False)
    _executed: bool = field(default=False, init=False)
    _duration_ms: float = field(default=0.0, init=False)

    def execute(self) -> Any:
        if not self._executed:
            t0 = time.monotonic()
            self._result = self.fn(*self.args, **self.kwargs)
            self._duration_ms = (time.monotonic() - t0) * 1000
            self._executed = True
            print(f"  [RUN ] {self.name} ({self._duration_ms:.1f}ms)")
        return self._result

    @property
    def skipped(self) -> bool:
        return not self._executed

class DemandPipeline:
    def __init__(self):
        self._stages: dict[str, LazyStage] = {}

    def add(self, stage: LazyStage):
        self._stages[stage.name] = stage

    def get(self, name: str) -> Any:
        if name not in self._stages:
            raise KeyError(f"Stage not found: {name}")
        return self._stages[name].execute()

    def report(self):
        executed = [s for s in self._stages.values() if s._executed]
        skipped  = [s for s in self._stages.values() if s.skipped]
        total_ms = sum(s._duration_ms for s in executed)
        print(f"\nPipeline: {len(executed)} run / {len(skipped)} skipped | {total_ms:.1f}ms total")
        for s in executed:
            print(f"  ✓ {s.name} ({s._duration_ms:.1f}ms)")
        for s in skipped:
            print(f"  - {s.name} (skipped)")

# Build pipeline
pipeline = DemandPipeline()
pipeline.add(LazyStage("user_info",    lambda: {"name": "Carol", "role": "engineer"}, ()))
pipeline.add(LazyStage("repo_list",    lambda: ["repo-a", "repo-b", "repo-c"], ()))
pipeline.add(LazyStage("ci_status",    lambda: {"passing": 12, "failing": 1}, ()))
pipeline.add(LazyStage("deploy_queue", lambda: [{"env": "prod", "ts": "2026-01-15"}], ()))

# Agent only fetches what it needs for this question
resp = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=100,
    messages=[{
        "role": "user",
        "content": (
            f"User: {pipeline.get('user_info')}\n"
            f"CI: {pipeline.get('ci_status')}\n"
            "Is CI healthy enough to merge? Answer in one sentence."
        ),
    }],
)
print(f"Answer: {resp.content[0].text[:80]}")
pipeline.report()

# Expected Token Savings: repo_list + deploy_queue never evaluated — saves I/O time proportional to skipped stages
# Environment: pure Python; pipeline.report() surfaces which stages can be pruned from future requests
```

## Option 6: Lazy Tool Result with SQLite Evaluation Log

```python
import anthropic
import sqlite3
import time
import hashlib

client = anthropic.Anthropic()
DB = "lazy_eval.db"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS eval_log (
            ts REAL, tool TEXT, cache_key TEXT,
            cache_hit INTEGER, duration_ms REAL, result_len INTEGER
        )
    """)
    con.commit(); con.close()

class LoggedLazy:
    _cache: dict = {}

    def __init__(self, tool_name: str, fn, *args, ttl_s: int = 120, **kwargs):
        self._name = tool_name
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self._ttl = ttl_s
        self._key = hashlib.md5(f"{tool_name}{args}{kwargs}".encode()).hexdigest()[:10]
        init_db()

    def get(self):
        cached = self._cache.get(self._key)
        cache_hit = 0
        t0 = time.monotonic()
        if cached and time.time() < cached["exp"]:
            value = cached["value"]
            cache_hit = 1
        else:
            value = self._fn(*self._args, **self._kwargs)
            self._cache[self._key] = {"value": value, "exp": time.time() + self._ttl}

        duration_ms = (time.monotonic() - t0) * 1000
        con = sqlite3.connect(DB)
        con.execute(
            "INSERT INTO eval_log VALUES (?,?,?,?,?,?)",
            (time.time(), self._name, self._key, cache_hit,
             duration_ms, len(str(value))),
        )
        con.commit(); con.close()
        return value

def eval_report():
    con = sqlite3.connect(DB)
    rows = con.execute("""
        SELECT tool,
               COUNT(*) calls,
               SUM(cache_hit) hits,
               ROUND(AVG(CASE WHEN cache_hit=0 THEN duration_ms END), 2) avg_miss_ms,
               ROUND(AVG(CASE WHEN cache_hit=1 THEN duration_ms END), 3) avg_hit_ms
        FROM eval_log GROUP BY tool
    """).fetchall()
    con.close()
    print("\nLazy Eval Report:")
    for r in rows:
        hit_rate = (r[2] / r[1] * 100) if r[1] else 0
        print(f"  {r[0]:18s} calls={r[1]} hits={hit_rate:.0f}% "
              f"miss={r[3]}ms hit={r[4]}ms")

# Define lazy tools
def slow_db(query): time.sleep(0.03); return {"rows": 42, "query": query}
def slow_api(ep):   time.sleep(0.05); return {"status": "ok", "endpoint": ep}

db_result  = LoggedLazy("database",    slow_db, "SELECT count FROM events", ttl_s=60)
api_result = LoggedLazy("external_api", slow_api, "/metrics", ttl_s=30)
db_result2 = LoggedLazy("database",    slow_db, "SELECT count FROM events", ttl_s=60)

# Simulate 3 requests
for i in range(3):
    _ = db_result.get()   # first call executes, subsequent are cache hits
    if i == 0:
        _ = api_result.get()  # only called once

eval_report()

# Expected Token Savings: Cache hit rate in eval_log shows ROI of lazy caching; near-zero hit latency vs 30-50ms miss
# Environment: class-level dict cache is per-process; TTL prevents stale reads; SQLite log survives restarts
```

## Comparison

| Option | Evaluation Trigger | Caching | Async | Observability |
|--------|-------------------|---------|-------|--------------|
| 1 — Lazy Wrapper | First `.get()` | In-object | No | Evaluated flag |
| 2 — Async Lazy + Prefetch | `await .get()` or prefetch | In-object | Yes | Prefetch flag |
| 3 — Demand-Driven Dispatch | Model tool_use request | No | No | Call log list |
| 4 — Memoized + TTL | First `.get()` | Class dict + TTL | No | Cache hit/miss print |
| 5 — Demand Pipeline | Explicit `.get(name)` | No | No | Stage report |
| 6 — Logged Lazy SQLite | First `.get()` | Class dict + TTL | No | SQLite eval log |
