---
title: "Agent Doesn't Implement Per-Tool Timeout with Fallback"
description: "Agents that apply a single global timeout fail entirely when one slow tool call blocks the pipeline. Per-tool timeouts with fallback values let the agent degrade gracefully instead of hanging or crashing."
difficulty: intermediate
category: reliability
tags: [reliability, timeout, fallback, resilience, tools, graceful-degradation]
---

# Agent Doesn't Implement Per-Tool Timeout with Fallback

## Problem

A global request timeout doesn't distinguish between a slow database query (tolerable for 5s) and a fast cache lookup (should fail in 200ms). When one tool hangs, the entire agent pipeline stalls until the global deadline fires, killing work that could have succeeded with a smarter per-tool policy. Worse, no fallback means the agent returns an error instead of a degraded-but-useful response.

**Symptoms:**
- Search results arrive late and block summarization for seconds
- Slow third-party APIs cause complete request failures, not partial responses
- Database timeouts propagate to unrelated tool results in the same request
- No caching of last-known-good values for timed-out tools
- Users receive errors instead of slightly stale but usable data

---

## Solution 1: asyncio.wait_for per Tool with Static Fallback

Wrap every tool call in `asyncio.wait_for` with an individually configured timeout and a static fallback value.

```python
import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Optional
import anthropic


@dataclass
class ToolConfig:
    name: str
    timeout_seconds: float
    fallback: Any  # Returned on timeout instead of raising
    critical: bool = False  # If True, timeout raises rather than using fallback


TOOL_CONFIGS = {
    "search_web":       ToolConfig("search_web",       timeout_seconds=3.0,  fallback=[], critical=False),
    "query_database":   ToolConfig("query_database",   timeout_seconds=5.0,  fallback=None, critical=True),
    "get_user_profile": ToolConfig("get_user_profile", timeout_seconds=1.0,  fallback={"name": "User", "plan": "unknown"}),
    "send_notification":ToolConfig("send_notification",timeout_seconds=2.0,  fallback={"sent": False, "queued": True}),
}


async def execute_with_timeout(
    tool_name: str,
    coro: Any,  # coroutine to execute
) -> tuple[Any, bool]:
    """Execute a coroutine with per-tool timeout. Returns (result, timed_out)."""
    cfg = TOOL_CONFIGS.get(tool_name, ToolConfig(tool_name, 5.0, None))
    try:
        result = await asyncio.wait_for(coro, timeout=cfg.timeout_seconds)
        return result, False
    except asyncio.TimeoutError:
        if cfg.critical:
            raise RuntimeError(f"Critical tool '{tool_name}' timed out after {cfg.timeout_seconds}s")
        print(f"[timeout] {tool_name} timed out after {cfg.timeout_seconds}s → fallback={cfg.fallback!r}")
        return cfg.fallback, True


# Simulated tool implementations
async def search_web(query: str) -> list:
    await asyncio.sleep(0.5)  # Fast path
    return [{"title": f"Result for {query}", "url": "https://example.com"}]


async def query_database(user_id: str) -> dict:
    await asyncio.sleep(6.0)  # Simulates slow DB (will timeout)
    return {"user_id": user_id, "data": "..."}


async def get_user_profile(user_id: str) -> dict:
    await asyncio.sleep(0.2)
    return {"name": "Alice", "plan": "pro"}


class TimeoutAwareAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def run(self, user_id: str, query: str) -> str:
        # Execute tools concurrently, each with its own timeout
        results = await asyncio.gather(
            execute_with_timeout("search_web", search_web(query)),
            execute_with_timeout("query_database", query_database(user_id)),
            execute_with_timeout("get_user_profile", get_user_profile(user_id)),
        )

        search_data, search_timed_out = results[0]
        db_data, db_timed_out = results[1]
        profile, profile_timed_out = results[2]

        context = f"""
Search results: {search_data}
Database record: {db_data} {"(stale/unavailable)" if db_timed_out else ""}
User profile: {profile}
"""
        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": f"Answer this query using available context.\nQuery: {query}\n{context}"
            }],
        )
        return response.content[0].text


async def demo():
    agent = TimeoutAwareAgent(api_key="sk-...")
    result = await agent.run("user_42", "What's the latest on AI agents?")
    print(result)

# asyncio.run(demo())
```

---

## Solution 2: Dynamic Timeout Budget Allocation

Distribute a total request deadline across tools proportionally, with minimum guarantees per tool.

```python
import asyncio
import time
from dataclasses import dataclass
from typing import Any, Optional
import anthropic


@dataclass
class TimeoutBudget:
    total_deadline: float       # Absolute epoch time when entire request must complete
    tool_minimums: dict[str, float]  # Minimum seconds guaranteed per tool
    tool_weights: dict[str, float]   # Proportional weight for budget distribution

    def time_remaining(self) -> float:
        return max(0.0, self.total_deadline - time.monotonic())

    def allocate(self, tool_name: str) -> float:
        """Compute timeout for this tool from remaining budget."""
        remaining = self.time_remaining()
        weight = self.tool_weights.get(tool_name, 1.0)
        total_weight = sum(self.tool_weights.values())
        proportional = remaining * (weight / total_weight)
        minimum = self.tool_minimums.get(tool_name, 0.5)
        allocated = max(minimum, proportional)
        return min(allocated, remaining)


class BudgetedToolRunner:
    def __init__(self, total_timeout: float = 8.0):
        self.total_timeout = total_timeout

    def make_budget(self) -> TimeoutBudget:
        return TimeoutBudget(
            total_deadline=time.monotonic() + self.total_timeout,
            tool_minimums={"query_database": 1.0, "search_web": 0.5},
            tool_weights={"search_web": 2.0, "query_database": 3.0, "get_user_profile": 1.0},
        )

    async def run_tool(
        self,
        tool_name: str,
        coro,
        budget: TimeoutBudget,
        fallback: Any = None,
    ) -> tuple[Any, bool]:
        timeout = budget.allocate(tool_name)
        if timeout < 0.1:
            print(f"[budget] {tool_name}: budget exhausted, using fallback immediately")
            return fallback, True

        start = time.monotonic()
        try:
            result = await asyncio.wait_for(coro, timeout=timeout)
            elapsed = time.monotonic() - start
            print(f"[budget] {tool_name}: completed in {elapsed:.2f}s (budget={timeout:.2f}s)")
            return result, False
        except asyncio.TimeoutError:
            print(f"[budget] {tool_name}: timed out after {timeout:.2f}s → fallback")
            return fallback, True


async def demo():
    runner = BudgetedToolRunner(total_timeout=6.0)
    budget = runner.make_budget()

    async def slow_db():
        await asyncio.sleep(10)
        return {"record": "data"}

    async def fast_search():
        await asyncio.sleep(0.3)
        return [{"title": "Result"}]

    async def medium_profile():
        await asyncio.sleep(0.8)
        return {"name": "Bob"}

    r1, t1 = await runner.run_tool("search_web", fast_search(), budget, fallback=[])
    r2, t2 = await runner.run_tool("query_database", slow_db(), budget, fallback=None)
    r3, t3 = await runner.run_tool("get_user_profile", medium_profile(), budget, fallback={"name": "Unknown"})

    print(f"search={r1} (timed_out={t1})")
    print(f"db={r2} (timed_out={t2})")
    print(f"profile={r3} (timed_out={t3})")
    print(f"remaining_budget={budget.time_remaining():.2f}s")

# asyncio.run(demo())
```

---

## Solution 3: Last-Known-Good Cache as Fallback

Cache the last successful tool result; on timeout, return the cached value with a staleness annotation.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional
import anthropic


@dataclass
class CachedResult:
    value: Any
    cached_at: float
    tool_name: str

    def age_seconds(self) -> float:
        return time.time() - self.cached_at

    def is_fresh(self, max_age: float) -> bool:
        return self.age_seconds() <= max_age


class LastKnownGoodCache:
    def __init__(self, default_max_age: float = 300.0):  # 5 minutes
        self._cache: dict[str, CachedResult] = {}
        self._max_age = default_max_age

    def store(self, tool_name: str, key: str, value: Any) -> None:
        cache_key = f"{tool_name}:{key}"
        self._cache[cache_key] = CachedResult(
            value=value, cached_at=time.time(), tool_name=tool_name
        )

    def get(self, tool_name: str, key: str) -> Optional[CachedResult]:
        return self._cache.get(f"{tool_name}:{key}")


class CachedFallbackAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.cache = LastKnownGoodCache()

    async def _run_with_cache_fallback(
        self,
        tool_name: str,
        cache_key: str,
        coro,
        timeout: float,
    ) -> tuple[Any, str]:
        """Run coro; on timeout return cached value. Returns (result, source)."""
        try:
            result = await asyncio.wait_for(coro, timeout=timeout)
            self.cache.store(tool_name, cache_key, result)
            return result, "live"
        except asyncio.TimeoutError:
            cached = self.cache.get(tool_name, cache_key)
            if cached:
                age = cached.age_seconds()
                print(f"[cache] {tool_name} timed out; returning cached value ({age:.0f}s old)")
                return cached.value, f"cached_{age:.0f}s_ago"
            print(f"[cache] {tool_name} timed out; no cached value available")
            return None, "unavailable"

    async def answer(self, user_id: str, question: str) -> str:
        async def fetch_profile():
            await asyncio.sleep(0.3)
            return {"name": "Alice", "tier": "pro", "credits": 450}

        async def fetch_inventory():
            await asyncio.sleep(8.0)  # Slow — will timeout
            return [{"item": "Widget", "qty": 10}]

        profile, profile_src = await self._run_with_cache_fallback(
            "user_profile", user_id, fetch_profile(), timeout=1.0
        )
        inventory, inv_src = await self._run_with_cache_fallback(
            "inventory", user_id, fetch_inventory(), timeout=2.0
        )

        context = (
            f"User profile ({profile_src}): {profile}\n"
            f"Inventory ({inv_src}): {inventory}"
        )
        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=256,
            messages=[{"role": "user", "content": f"{question}\n\nContext:\n{context}"}],
        )
        return response.content[0].text


async def demo():
    agent = CachedFallbackAgent(api_key="sk-...")
    # First call: profile live, inventory unavailable
    r1 = await agent.answer("u1", "What items do I have?")
    # Second call: profile cached (fast), inventory still cached (None)
    r2 = await agent.answer("u1", "Can I buy more?")
    print(r1)

# asyncio.run(demo())
```

---

## Solution 4: Timeout Tree with Hierarchical Propagation

Nested tool calls inherit a shrinking deadline from their parent, preventing inner calls from outliving outer budgets.

```python
import asyncio
import time
from dataclasses import dataclass
from typing import Any, Optional
import anthropic


@dataclass
class DeadlineContext:
    deadline: float  # monotonic time

    @classmethod
    def with_timeout(cls, seconds: float) -> "DeadlineContext":
        return cls(deadline=time.monotonic() + seconds)

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def child(self, fraction: float = 0.5) -> "DeadlineContext":
        """Child gets a fraction of the remaining budget."""
        return DeadlineContext(deadline=time.monotonic() + self.remaining() * fraction)

    def enforce(self, label: str) -> float:
        r = self.remaining()
        if r <= 0:
            raise asyncio.TimeoutError(f"Deadline exceeded at: {label}")
        return r

    async def run(self, coro, label: str, fallback: Any = None) -> tuple[Any, bool]:
        r = self.remaining()
        if r <= 0:
            print(f"[deadline] {label}: no time left → fallback")
            return fallback, True
        try:
            result = await asyncio.wait_for(coro, timeout=r)
            return result, False
        except asyncio.TimeoutError:
            print(f"[deadline] {label}: timed out → fallback")
            return fallback, True


async def fetch_search_results(ctx: DeadlineContext, query: str) -> list:
    child_ctx = ctx.child(fraction=0.4)  # Can use 40% of remaining
    await asyncio.sleep(0.5)
    return [{"title": f"Result for {query}"}]


async def fetch_and_rank(ctx: DeadlineContext, query: str) -> list:
    """Multi-step tool: fetch then rank — each step respects deadline."""
    results, timed_out = await ctx.run(
        asyncio.sleep(0.3),  # simulate fetch
        label="fetch_raw",
        fallback=[],
    )
    if timed_out:
        return []

    ranked, timed_out = await ctx.run(
        asyncio.sleep(0.4),  # simulate ranking
        label="rank_results",
        fallback=results,
    )
    return ranked if not timed_out else results


class DeadlineTreeAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def run(self, query: str, total_budget: float = 5.0) -> str:
        root_ctx = DeadlineContext.with_timeout(total_budget)

        search_results, s_timeout = await root_ctx.run(
            fetch_and_rank(root_ctx.child(0.5), query),
            label="search_and_rank",
            fallback=[],
        )

        async def slow_db():
            await asyncio.sleep(10)
            return {}

        db_data, d_timeout = await root_ctx.run(
            slow_db(),
            label="database_lookup",
            fallback={"status": "unavailable"},
        )

        print(f"[deadline] remaining after tools: {root_ctx.remaining():.2f}s")

        llm_ctx = root_ctx.child(fraction=1.0)  # All remaining for LLM
        content = f"Search: {search_results}\nDB: {db_data}\nQuery: {query}"
        response, llm_timeout = await llm_ctx.run(
            self.client.messages.create(
                model="claude-opus-4-6",
                max_tokens=256,
                messages=[{"role": "user", "content": content}],
            ),
            label="llm_call",
            fallback=None,
        )
        if llm_timeout or response is None:
            return "Partial results available. Full response timed out."
        return response.content[0].text


async def demo():
    agent = DeadlineTreeAgent(api_key="sk-...")
    result = await agent.run("Latest ML papers", total_budget=4.0)
    print(result)

# asyncio.run(demo())
```

---

## Solution 5: Adaptive Timeout Based on Tool Historical P95

Track per-tool latency histograms and set timeouts at the historical P95, auto-adjusting as performance changes.

```python
import asyncio
import heapq
import time
from collections import defaultdict, deque
from typing import Any, Optional
import anthropic


class LatencyTracker:
    def __init__(self, window: int = 100, percentile: float = 0.95, min_timeout: float = 0.5):
        self._samples: dict[str, deque] = defaultdict(lambda: deque(maxlen=window))
        self._percentile = percentile
        self._min_timeout = min_timeout
        self._default_timeout = 5.0

    def record(self, tool_name: str, latency_s: float) -> None:
        self._samples[tool_name].append(latency_s)

    def timeout_for(self, tool_name: str) -> float:
        samples = list(self._samples.get(tool_name, []))
        if len(samples) < 5:
            return self._default_timeout
        sorted_s = sorted(samples)
        idx = int(len(sorted_s) * self._percentile)
        p95 = sorted_s[min(idx, len(sorted_s) - 1)]
        # Add 20% headroom above P95
        return max(self._min_timeout, p95 * 1.2)


class AdaptiveTimeoutRunner:
    def __init__(self):
        self.tracker = LatencyTracker(window=50, percentile=0.95)

    async def run(self, tool_name: str, coro, fallback: Any = None) -> tuple[Any, bool]:
        timeout = self.tracker.timeout_for(tool_name)
        start = time.perf_counter()
        try:
            result = await asyncio.wait_for(coro, timeout=timeout)
            elapsed = time.perf_counter() - start
            self.tracker.record(tool_name, elapsed)
            print(f"[adaptive] {tool_name}: {elapsed:.3f}s (timeout was {timeout:.2f}s)")
            return result, False
        except asyncio.TimeoutError:
            elapsed = time.perf_counter() - start
            self.tracker.record(tool_name, elapsed)  # Record timeout as a sample too
            print(f"[adaptive] {tool_name}: timed out at {timeout:.2f}s → fallback")
            return fallback, True


class AdaptiveAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.runner = AdaptiveTimeoutRunner()

    async def ask(self, query: str) -> str:
        async def fast_tool():
            await asyncio.sleep(0.1 + (time.time() % 0.3))  # Variable 0.1-0.4s
            return {"data": "fast_result"}

        async def variable_tool():
            import random
            await asyncio.sleep(random.uniform(0.5, 3.0))  # Highly variable
            return {"data": "variable_result"}

        r1, _ = await self.runner.run("fast_tool", fast_tool(), fallback=None)
        r2, _ = await self.runner.run("variable_tool", variable_tool(), fallback={"data": "fallback"})

        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=128,
            messages=[{"role": "user", "content": f"{query}\nData: {r1}, {r2}"}],
        )
        return response.content[0].text


async def demo():
    agent = AdaptiveAgent(api_key="sk-...")
    # Run multiple times so tracker learns latency distribution
    for i in range(5):
        result = await agent.ask(f"Query {i}")
        print(f"[{i}]: {result[:60]}")

# asyncio.run(demo())
```

---

## Solution 6: Tool Timeout with Structured Partial Response

When tools time out, include a structured `partial_results` field in the response so callers know exactly what's missing.

```python
import asyncio
from dataclasses import dataclass, field
from typing import Any, Optional
import anthropic


@dataclass
class PartialResponse:
    query: str
    results: dict[str, Any] = field(default_factory=dict)
    timed_out: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return not self.timed_out and not self.errors

    def to_context(self) -> str:
        lines = [f"Query: {self.query}"]
        for tool, value in self.results.items():
            lines.append(f"{tool}: {value}")
        if self.timed_out:
            lines.append(f"[Unavailable — timed out: {', '.join(self.timed_out)}]")
        if self.errors:
            lines.append(f"[Errors: {', '.join(self.errors)}]")
        return "\n".join(lines)


TOOL_TIMEOUTS = {
    "inventory": 1.5,
    "recommendations": 2.0,
    "user_history": 1.0,
    "pricing": 0.8,
}

TOOL_FALLBACKS = {
    "inventory": [],
    "recommendations": [],
    "user_history": None,
    "pricing": None,
}


class PartialResultAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def _execute_tools(self, tools: dict[str, Any]) -> PartialResponse:
        """Execute all tools concurrently; collect partial results."""
        partial = PartialResponse(query="")

        async def _run_one(name: str, coro) -> tuple[str, Any, bool]:
            try:
                result = await asyncio.wait_for(coro, timeout=TOOL_TIMEOUTS.get(name, 3.0))
                return name, result, False
            except asyncio.TimeoutError:
                return name, TOOL_FALLBACKS.get(name), True
            except Exception as exc:
                return name, None, True

        tasks = [_run_one(name, coro) for name, coro in tools.items()]
        outcomes = await asyncio.gather(*tasks)

        for name, result, timed_out in outcomes:
            if timed_out:
                partial.timed_out.append(name)
            else:
                partial.results[name] = result

        return partial

    async def answer(self, user_id: str, question: str) -> dict:
        async def get_inventory():
            await asyncio.sleep(0.5)
            return [{"sku": "A1", "qty": 10}]

        async def get_recommendations():
            await asyncio.sleep(3.0)  # Will timeout
            return [{"product": "Widget Pro"}]

        async def get_history():
            await asyncio.sleep(0.3)
            return [{"order": "ORD-001"}]

        partial = await self._execute_tools({
            "inventory": get_inventory(),
            "recommendations": get_recommendations(),
            "user_history": get_history(),
        })
        partial.query = question

        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=256,
            system=(
                "Answer the user's question using the available data. "
                "Acknowledge missing data if any tools were unavailable."
            ),
            messages=[{"role": "user", "content": partial.to_context()}],
        )

        return {
            "answer": response.content[0].text,
            "complete": partial.is_complete,
            "timed_out_tools": partial.timed_out,
        }


async def demo():
    agent = PartialResultAgent(api_key="sk-...")
    result = await agent.answer("u42", "What can you recommend for me?")
    print(f"Answer: {result['answer'][:100]}")
    print(f"Complete: {result['complete']}, Timed out: {result['timed_out_tools']}")

# asyncio.run(demo())
```

---

## Comparison

| Solution | Timeout Source | Fallback Type | Staleness Signal | Adaptive | Complexity |
|---|---|---|---|---|---|
| Static per-tool timeout | Config dict | Static value | No | No | Very Low |
| Dynamic budget allocation | Remaining request budget | Static value | No | No | Low |
| Last-known-good cache | Config dict | Cached result | Age in seconds | No | Medium |
| Deadline tree | Hierarchical propagation | Static/None | No | No | Medium |
| Adaptive P95 timeout | Historical latency | Static value | No | Yes | Medium |
| Structured partial response | Config dict | Structured object | Tool name list | No | Low |

**Recommendation:** Use Solution 1 for most agents — a simple config dict with per-tool timeouts and static fallbacks is easy to reason about and tune. Add Solution 3 (last-known-good cache) for any tool whose data changes slowly (user profile, catalog, feature flags). Use Solution 6 when callers need to know precisely which tools succeeded so they can display partial UI rather than a generic error.
