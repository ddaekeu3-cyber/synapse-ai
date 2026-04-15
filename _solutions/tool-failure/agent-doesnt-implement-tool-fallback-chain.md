---
layout: solution
title: "Agent Doesn't Implement Tool Fallback Chain"
category: tool-failure
description: "Agents that depend on a single tool implementation fail completely when that tool is unavailable, rate-limited, or returning errors. A fallback chain tries backup tools in order — primary → secondary → tertiary — degrading gracefully instead of halting the task."
tags: [tool-failure, fallback, resilience, circuit-breaker, tool-use, graceful-degradation, retry]
---

## Problem

An agent that calls a single weather API, search engine, or database has a single point of failure. When the primary tool fails — network timeout, API outage, rate limit, bad credentials — the entire agent task fails. Fallback chains define ordered alternatives: if tool A fails, try tool B; if B fails, try C or produce a degraded result. This pattern keeps agents functional during partial outages without requiring code changes.

## Solutions

### Option 1: Sequential Fallback with First-Success Semantics

```python
import anthropic
from typing import Callable, Any

client = anthropic.Anthropic()

def make_tool(name: str, succeed: bool = True, result: str = ""):
    """Simulates a tool that may succeed or fail."""
    def tool_fn(**kwargs) -> str:
        if not succeed:
            raise RuntimeError(f"{name} is unavailable")
        return result or f"[{name} result for {kwargs}]"
    tool_fn.__name__ = name
    return tool_fn

class FallbackChain:
    """Try each tool in order, return first success."""
    def __init__(self, tools: list[tuple[str, Callable]]):
        self._tools = tools  # [(name, fn), ...]

    def execute(self, **kwargs) -> tuple[str, str]:
        """Returns (result, tool_name_used)."""
        errors = []
        for name, fn in self._tools:
            try:
                result = fn(**kwargs)
                if errors:
                    print(f"  [fallback] Used '{name}' after {len(errors)} failure(s): {errors}")
                else:
                    print(f"  [primary ] Used '{name}'")
                return result, name
            except Exception as e:
                errors.append(f"{name}: {e}")

        # All tools failed
        raise RuntimeError(f"All tools failed: {errors}")

def execute_tool_with_fallback(tool_name: str, tool_input: dict, chain: FallbackChain) -> str:
    result, used = chain.execute(**tool_input)
    return result

def run_agent(task: str, search_chain: FallbackChain) -> str:
    tools = [
        {
            "name": "web_search",
            "description": "Search the web for information",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        }
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
            return next((b.text for b in resp.content if hasattr(b, "text")), "")

        tool_results = []
        for block in resp.content:
            if block.type == "tool_use":
                result = execute_tool_with_fallback(block.name, block.input, search_chain)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages.append({"role": "assistant", "content": resp.content})
        messages.append({"role": "user", "content": tool_results})

if __name__ == "__main__":
    # Primary fails, secondary is used
    chain = FallbackChain([
        ("google_search", make_tool("google_search", succeed=False)),
        ("bing_search", make_tool("bing_search", succeed=True, result="Paris is the capital of France.")),
        ("local_cache", make_tool("local_cache", succeed=True, result="[cached] Paris is the capital of France.")),
    ])
    result = run_agent("What is the capital of France?", chain)
    print(f"\nAgent answer: {result[:100]}")

# Expected Token Savings: agent never knows about failures; no retry tokens wasted on re-planning
# Environment: search, database, or API tools with multiple provider options
```

### Option 2: Typed Fallback with Error Classification

```python
import anthropic
import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Awaitable

client = anthropic.AsyncAnthropic()

class FailureClass(Enum):
    TRANSIENT = "transient"   # retry same tool
    PERMANENT = "permanent"   # skip to next tool
    DEGRADED = "degraded"     # accept partial result

@dataclass
class ToolError:
    tool_name: str
    error: Exception
    failure_class: FailureClass

def classify_error(e: Exception) -> FailureClass:
    msg = str(e).lower()
    if "timeout" in msg or "503" in msg or "429" in msg:
        return FailureClass.TRANSIENT
    if "auth" in msg or "forbidden" in msg or "not found" in msg:
        return FailureClass.PERMANENT
    return FailureClass.TRANSIENT  # default: treat as transient

class TypedFallbackChain:
    def __init__(
        self,
        tools: list[tuple[str, Callable[..., Awaitable[str]]]],
        transient_retries: int = 1,
    ):
        self._tools = tools
        self._transient_retries = transient_retries

    async def execute(self, **kwargs) -> tuple[str, str]:
        all_errors: list[ToolError] = []
        for name, fn in self._tools:
            for attempt in range(self._transient_retries + 1):
                try:
                    result = await fn(**kwargs)
                    if all_errors:
                        print(f"  [fallback] Used '{name}' after {len(all_errors)} error(s)")
                    return result, name
                except Exception as e:
                    fc = classify_error(e)
                    all_errors.append(ToolError(name, e, fc))
                    if fc == FailureClass.PERMANENT:
                        print(f"  [skip] '{name}' PERMANENT failure: {e}")
                        break
                    if attempt < self._transient_retries:
                        print(f"  [retry] '{name}' TRANSIENT failure, retrying: {e}")
                        await asyncio.sleep(0.5)
                    else:
                        print(f"  [exhaust] '{name}' gave up after {attempt+1} attempts")

        raise RuntimeError(f"All tools exhausted: {[(e.tool_name, str(e.error)) for e in all_errors]}")

async def demo():
    async def primary(**kw): raise TimeoutError("Connection timed out (503)")
    async def secondary(**kw): raise PermissionError("403 Forbidden — bad API key (auth)")
    async def tertiary(**kw): return f"tertiary result for {kw}"

    chain = TypedFallbackChain(
        tools=[("primary", primary), ("secondary", secondary), ("tertiary", tertiary)],
        transient_retries=1,
    )
    result, used = await chain.execute(query="test query")
    print(f"\nFinal result from '{used}': {result}")

if __name__ == "__main__":
    asyncio.run(demo())

# Expected Token Savings: transient errors retry same tool (avoids unnecessary model roundtrip)
# Environment: async agents; error classification prevents retrying tools that will always fail (auth errors)
```

### Option 3: Health-Gated Fallback with Pre-Call Availability Check

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class ToolHealth:
    name: str
    last_success: float = 0.0
    last_failure: float = 0.0
    consecutive_failures: int = 0
    _failure_threshold: int = 3
    _recovery_seconds: float = 60.0

    @property
    def is_healthy(self) -> bool:
        if self.consecutive_failures < self._failure_threshold:
            return True
        # Allow recovery attempt after cooldown
        return time.time() - self.last_failure > self._recovery_seconds

    def record_success(self):
        self.last_success = time.time()
        self.consecutive_failures = 0

    def record_failure(self):
        self.last_failure = time.time()
        self.consecutive_failures += 1

class HealthGatedFallbackChain:
    def __init__(self):
        self._tools: list[tuple[ToolHealth, any]] = []

    def add(self, name: str, fn, failure_threshold: int = 3, recovery_seconds: float = 60.0):
        health = ToolHealth(name, _failure_threshold=failure_threshold, _recovery_seconds=recovery_seconds)
        self._tools.append((health, fn))

    async def execute(self, **kwargs) -> tuple[str, str]:
        for health, fn in self._tools:
            if not health.is_healthy:
                print(f"  [skip] '{health.name}' unhealthy ({health.consecutive_failures} failures)")
                continue
            try:
                result = await fn(**kwargs)
                health.record_success()
                return result, health.name
            except Exception as e:
                health.record_failure()
                print(f"  [fail] '{health.name}': {e} (failures={health.consecutive_failures})")
        raise RuntimeError("No healthy tools available")

    def health_report(self) -> str:
        lines = []
        for h, _ in self._tools:
            status = "HEALTHY" if h.is_healthy else "UNHEALTHY"
            lines.append(f"  {h.name:20s}: {status} (consecutive_failures={h.consecutive_failures})")
        return "\n".join(lines)

async def demo():
    chain = HealthGatedFallbackChain()
    call_count = {"primary": 0}

    async def primary(**kw):
        call_count["primary"] += 1
        if call_count["primary"] <= 4:
            raise RuntimeError("primary overloaded")
        return "primary result"

    async def secondary(**kw):
        return "secondary result (fallback)"

    chain.add("primary", primary, failure_threshold=3, recovery_seconds=5.0)
    chain.add("secondary", secondary)

    for i in range(6):
        try:
            result, used = await chain.execute(query=f"call {i}")
            print(f"  Call {i}: '{used}' → {result}")
        except RuntimeError as e:
            print(f"  Call {i}: FAILED — {e}")

    print("\nHealth report:")
    print(chain.health_report())

if __name__ == "__main__":
    asyncio.run(demo())

# Expected Token Savings: skipping unhealthy tools avoids timeout latency and wasted API tokens
# Environment: agents with external tool dependencies; health gate prevents cascading slowdowns
```

### Option 4: Partial Result Aggregation Across Fallbacks

```python
import anthropic
import asyncio
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

@dataclass
class PartialResult:
    source: str
    data: dict
    confidence: float  # 0.0-1.0
    complete: bool

class AggregatingFallbackChain:
    """
    Instead of first-success, collect partial results from all reachable tools
    and merge them into a best-effort complete result.
    """
    def __init__(self, min_confidence: float = 0.5):
        self._tools: list[tuple[str, any]] = []
        self._min_confidence = min_confidence

    def add(self, name: str, fn):
        self._tools.append((name, fn))

    async def _try_tool(self, name: str, fn, **kwargs) -> PartialResult | None:
        try:
            return await fn(**kwargs)
        except Exception as e:
            print(f"  [skip] '{name}': {e}")
            return None

    async def execute(self, **kwargs) -> dict:
        partials = await asyncio.gather(*[
            self._try_tool(name, fn, **kwargs)
            for name, fn in self._tools
        ])
        valid = [p for p in partials if p and p.confidence >= self._min_confidence]
        if not valid:
            raise RuntimeError("No tools returned usable partial results")

        # Merge: higher-confidence sources override lower-confidence ones
        merged = {}
        for partial in sorted(valid, key=lambda p: p.confidence):
            merged.update(partial.data)
            if partial.complete:
                break  # No need to merge further if we have a complete result

        sources = [p.source for p in valid]
        print(f"  [merged] Sources: {sources}")
        return merged

async def demo():
    async def fast_source(**kw):
        return PartialResult("fast_db", {"name": "Alice", "dept": "Engineering"}, confidence=0.8, complete=False)

    async def slow_source(**kw):
        raise TimeoutError("slow_source timed out")

    async def cache_source(**kw):
        return PartialResult("cache", {"name": "Alice", "email": "alice@example.com"}, confidence=0.6, complete=False)

    chain = AggregatingFallbackChain(min_confidence=0.5)
    chain.add("fast_db", fast_source)
    chain.add("slow_source", slow_source)
    chain.add("cache", cache_source)

    result = await chain.execute(user_id="alice")
    print(f"\nMerged result: {result}")
    assert "name" in result and "email" in result, "Should have merged fields from both sources"

if __name__ == "__main__":
    asyncio.run(demo())

# Expected Token Savings: single gather call instead of sequential retries; partial data better than failure
# Environment: agents querying multiple data sources; missing one source returns partial answer, not error
```

### Option 5: SQLite-Backed Fallback Metrics and Automatic Chain Ordering

```python
import anthropic
import sqlite3
import time
from pathlib import Path
from typing import Callable

DB = Path("/tmp/tool_fallback.db")
client = anthropic.Anthropic()

def init_db():
    con = sqlite3.connect(DB)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS tool_stats (
            tool_name TEXT PRIMARY KEY,
            successes INTEGER DEFAULT 0,
            failures INTEGER DEFAULT 0,
            total_latency_ms REAL DEFAULT 0.0
        );
    """)
    con.commit()
    con.close()

def record_call(tool_name: str, success: bool, latency_ms: float):
    con = sqlite3.connect(DB)
    col = "successes" if success else "failures"
    con.execute(f"""
        INSERT INTO tool_stats (tool_name, {col}, total_latency_ms)
        VALUES (?, 1, ?)
        ON CONFLICT(tool_name) DO UPDATE SET
            {col} = {col} + 1,
            total_latency_ms = total_latency_ms + ?
    """, (tool_name, latency_ms, latency_ms))
    con.commit()
    con.close()

def get_tool_order(tools: list[str]) -> list[str]:
    """Order tools by success rate descending, then by average latency."""
    con = sqlite3.connect(DB)
    stats = {}
    for name in tools:
        row = con.execute(
            "SELECT successes, failures, total_latency_ms FROM tool_stats WHERE tool_name=?", (name,)
        ).fetchone()
        if row:
            s, f, lat = row
            total = s + f
            rate = s / total if total > 0 else 0.5
            avg_lat = lat / total if total > 0 else 9999.0
            stats[name] = (rate, -avg_lat)  # higher rate better; lower latency better (negated)
        else:
            stats[name] = (0.5, 0.0)  # unknown tools get neutral score
    con.close()
    return sorted(tools, key=lambda n: stats[n], reverse=True)

def execute_chain(tools: dict[str, Callable], query: str) -> tuple[str, str]:
    ordered = get_tool_order(list(tools.keys()))
    print(f"  [order] {ordered}")
    for name in ordered:
        fn = tools[name]
        t0 = time.time()
        try:
            result = fn(query)
            record_call(name, True, (time.time() - t0) * 1000)
            return result, name
        except Exception as e:
            record_call(name, False, (time.time() - t0) * 1000)
            print(f"  [fail] {name}: {e}")
    raise RuntimeError("All tools failed")

if __name__ == "__main__":
    init_db()

    def tool_a(q): raise RuntimeError("A is down")
    def tool_b(q): return f"B result for '{q}'"
    def tool_c(q): return f"C result for '{q}'"

    tools = {"tool_a": tool_a, "tool_b": tool_b, "tool_c": tool_c}

    # Run several times — chain should auto-reorder to prefer B
    for i in range(4):
        result, used = execute_chain(tools, f"query {i}")
        print(f"  Run {i+1}: used='{used}' result='{result}'")

    # Check auto-ordering
    print("\nOptimal order:", get_tool_order(list(tools.keys())))

# Expected Token Savings: preferred tools are tried first, reducing fallback latency and token overhead
# Environment: long-running agents; chain auto-optimizes toward fastest, most-reliable tool over time
```

### Option 6: Graceful Degradation — Return Cached or Default on Full Failure

```python
import anthropic
import asyncio
import time
from typing import Any

client = anthropic.AsyncAnthropic()

_RESPONSE_CACHE: dict[str, tuple[Any, float]] = {}
_CACHE_TTL = 3600.0  # 1 hour

async def primary_tool(query: str) -> str:
    raise RuntimeError("Primary API is down for maintenance")

async def secondary_tool(query: str) -> str:
    raise RuntimeError("Secondary API rate limited")

async def cached_response(query: str) -> str | None:
    entry = _RESPONSE_CACHE.get(query)
    if entry:
        value, ts = entry
        if time.time() - ts < _CACHE_TTL:
            return value
    return None

def default_response(query: str) -> str:
    return f"I'm unable to retrieve live data for '{query}' right now. Please try again shortly."

async def tool_with_graceful_degradation(query: str) -> tuple[str, str]:
    """
    Tries: primary → secondary → cache → default.
    Always returns a result (never raises).
    """
    # 1. Primary
    try:
        result = await asyncio.wait_for(primary_tool(query), timeout=5.0)
        _RESPONSE_CACHE[query] = (result, time.time())
        return result, "primary"
    except Exception as e:
        print(f"  [primary fail] {e}")

    # 2. Secondary
    try:
        result = await asyncio.wait_for(secondary_tool(query), timeout=3.0)
        _RESPONSE_CACHE[query] = (result, time.time())
        return result, "secondary"
    except Exception as e:
        print(f"  [secondary fail] {e}")

    # 3. Cache
    cached = await cached_response(query)
    if cached:
        print(f"  [cache hit] serving stale response")
        return f"[Cached] {cached}", "cache"

    # 4. Default degraded response
    print(f"  [degraded] serving default response")
    return default_response(query), "default"

async def run_agent_task(task: str) -> str:
    tools = [{"name": "lookup", "description": "Look up information",
               "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}]
    messages = [{"role": "user", "content": task}]

    while True:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=tools,
            messages=messages,
        )
        if resp.stop_reason == "end_turn":
            return next((b.text for b in resp.content if hasattr(b, "text")), "")

        tool_results = []
        for block in resp.content:
            if block.type == "tool_use":
                result, source = await tool_with_graceful_degradation(block.input.get("query", ""))
                print(f"  [used: {source}]")
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages.append({"role": "assistant", "content": resp.content})
        messages.append({"role": "user", "content": tool_results})

async def main():
    # Seed cache
    _RESPONSE_CACHE["capital of France"] = ("Paris is the capital of France.", time.time() - 100)

    answer = await run_agent_task("What is the capital of France?")
    print(f"\nAgent answer: {answer[:100]}")

    # Query with no cache
    answer2 = await run_agent_task("What is the population of Japan?")
    print(f"Agent answer 2: {answer2[:100]}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: agent receives a result (never None); avoids re-prompting the model for error recovery
# Environment: user-facing agents where "service unavailable" error is worse than a degraded answer
```

## Comparison

| Option | Fallback Strategy | Error Classification | Persistence | Partial Results |
|--------|-----------------|---------------------|-------------|----------------|
| 1 — Sequential first-success | Try in order | No | No | No |
| 2 — Typed error classification | TRANSIENT retry / PERMANENT skip | Yes | No | No |
| 3 — Health-gated | Skip unhealthy tools | Via health score | No | No |
| 4 — Partial aggregation | Merge all partial results | No | No | Yes |
| 5 — SQLite auto-ordering | Self-optimizing order by success rate | No | SQLite | No |
| 6 — Graceful degradation | Cache → default fallback | No | In-memory cache | No (but never fails) |
