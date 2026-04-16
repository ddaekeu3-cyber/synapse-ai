---
layout: solution
title: "Agent Doesn't Implement Timeout Cascade Prevention"
category: concurrency
description: "Prevent a single task timeout from cascading to cancel unrelated concurrent tasks, using isolated task groups, bulkhead timeouts, and partial-failure recovery."
tags: [concurrency, timeout, cascade, bulkhead, asyncio, fault-isolation]
---

# Agent Doesn't Implement Timeout Cascade Prevention

When one slow task times out inside a shared asyncio scope, it can cancel sibling tasks that were progressing normally. The symptom is a timeout in one tool call killing the results of three others. Without cascade prevention, a single unresponsive downstream API takes down the entire agent turn. Isolated timeout scopes, bulkhead groups, and partial-failure recovery let the rest of the work succeed even when one task times out.

## Option 1: Per-Task Timeout with Independent Cancellation

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()


async def call_tool(tool_name: str, delay: float, timeout: float) -> dict:
    """Simulate a tool call with a per-task timeout — isolated from siblings."""
    try:
        result = await asyncio.wait_for(
            asyncio.sleep(delay),  # Simulate tool latency
            timeout=timeout,
        )
        return {"tool": tool_name, "status": "ok", "result": f"{tool_name} completed"}
    except asyncio.TimeoutError:
        return {"tool": tool_name, "status": "timeout", "result": None}


async def run_parallel_tools_isolated(tools: list[tuple[str, float, float]]) -> list[dict]:
    """
    Run tools in parallel with per-task timeouts.
    A timeout in one tool does NOT cancel others.
    """
    tasks = [asyncio.create_task(call_tool(name, delay, timeout)) for name, delay, timeout in tools]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [r if isinstance(r, dict) else {"tool": "unknown", "status": "error", "result": str(r)} for r in results]


async def run_agent(question: str) -> str:
    # Tool specs: (name, simulated_delay_s, timeout_s)
    tool_specs = [
        ("fast_lookup",   0.1, 2.0),   # fast — succeeds
        ("slow_api",      5.0, 1.0),   # times out — but siblings are unaffected
        ("medium_search", 0.5, 2.0),   # medium — succeeds
        ("db_query",      0.3, 2.0),   # fast — succeeds
    ]

    results = await run_parallel_tools_isolated(tool_specs)
    successful = [r for r in results if r["status"] == "ok"]
    timed_out  = [r for r in results if r["status"] == "timeout"]
    print(f"Succeeded: {[r['tool'] for r in successful]}")
    print(f"Timed out: {[r['tool'] for r in timed_out]}")

    context = "\n".join(f"- {r['tool']}: {r['result'] or 'unavailable'}" for r in results)
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"{question}\n\nTool results:\n{context}"}],
    )
    return response.content[0].text


result = asyncio.run(run_agent("Summarize what you found."))
print(f"\nAgent: {result}")

# Expected Token Savings: N/A (reliability pattern); partial success recovers 75% of results vs 0% on cascade
# Environment: Python 3.11+; asyncio.wait_for wraps each task independently — cancellation is scoped to that task only
```

## Option 2: Bulkhead Groups with Partial Result Collection

```python
import asyncio
import anthropic
import time
from dataclasses import dataclass, field
from typing import Any

client = anthropic.AsyncAnthropic()


@dataclass
class BulkheadResult:
    group: str
    name: str
    status: str  # "ok" | "timeout" | "error"
    value: Any = None
    elapsed: float = 0.0
    error: str = ""


async def run_with_bulkhead(name: str, group: str, coro, timeout: float) -> BulkheadResult:
    """Wrap a coroutine with an isolated timeout — failure stays within this bulkhead."""
    start = time.monotonic()
    try:
        value = await asyncio.wait_for(coro, timeout=timeout)
        return BulkheadResult(group=group, name=name, status="ok", value=value, elapsed=time.monotonic() - start)
    except asyncio.TimeoutError:
        return BulkheadResult(group=group, name=name, status="timeout", elapsed=timeout, error=f"timed out after {timeout}s")
    except Exception as e:
        return BulkheadResult(group=group, name=name, status="error", elapsed=time.monotonic() - start, error=str(e))


async def simulate_api_call(name: str, latency: float) -> str:
    await asyncio.sleep(latency)
    return f"{name}: data retrieved"


async def run_bulkhead_agent(question: str) -> str:
    # Define tasks with group membership and individual timeouts
    task_specs = [
        # (name, group, latency, timeout)
        ("primary_search",   "search",   0.2, 3.0),
        ("secondary_search", "search",   4.0, 1.0),  # will timeout — search group partially fails
        ("db_primary",       "database", 0.3, 3.0),
        ("db_replica",       "database", 0.4, 3.0),
        ("cache_lookup",     "cache",    0.1, 1.0),
        ("slow_enrichment",  "enrich",   6.0, 0.5),  # will timeout — enrich group fails entirely
    ]

    tasks = [
        asyncio.create_task(run_with_bulkhead(name, group, simulate_api_call(name, latency), timeout))
        for name, group, latency, timeout in task_specs
    ]
    results: list[BulkheadResult] = await asyncio.gather(*tasks)

    # Report by group
    by_group: dict[str, list[BulkheadResult]] = {}
    for r in results:
        by_group.setdefault(r.group, []).append(r)

    available_context = []
    for group, group_results in by_group.items():
        ok = [r for r in group_results if r.status == "ok"]
        failed = [r for r in group_results if r.status != "ok"]
        status = "partial" if (ok and failed) else ("ok" if ok else "unavailable")
        print(f"[{group}] {status}: {len(ok)}/{len(group_results)} tasks succeeded")
        for r in ok:
            available_context.append(f"{r.name}: {r.value}")

    if not available_context:
        available_context = ["No tool results available."]

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"{question}\n\nAvailable results (some tools unavailable):\n" + "\n".join(available_context)
        }],
    )
    return response.content[0].text


result = asyncio.run(run_bulkhead_agent("What information is available?"))
print(f"\nAgent: {result}")

# Expected Token Savings: N/A; bulkhead groups let database succeed even when search and enrich timeout
# Environment: Python 3.11+; group timeouts should be tighter than overall agent SLA to leave synthesis time
```

## Option 3: Timeout Budget Distributor with Remaining Time Propagation

```python
import asyncio
import anthropic
import time
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()


@dataclass
class TimeoutBudget:
    total: float
    start: float = 0.0
    overhead_reserve: float = 1.0  # seconds reserved for synthesis

    def __post_init__(self) -> None:
        self.start = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.start

    @property
    def remaining(self) -> float:
        return max(0.0, self.total - self.elapsed - self.overhead_reserve)

    def slot(self, fraction: float) -> float:
        """Allocate a fraction of remaining time to a task."""
        return max(0.1, self.remaining * fraction)


async def timed_tool(name: str, latency: float, budget: TimeoutBudget, fraction: float) -> dict:
    """Run a tool with a budget-proportional timeout."""
    alloc = budget.slot(fraction)
    if alloc <= 0:
        return {"tool": name, "status": "budget_exhausted"}
    try:
        await asyncio.wait_for(asyncio.sleep(latency), timeout=alloc)
        return {"tool": name, "status": "ok", "result": f"{name} data"}
    except asyncio.TimeoutError:
        return {"tool": name, "status": "timeout", "alloc": alloc}


async def run_with_budget(question: str, total_budget: float = 5.0) -> str:
    budget = TimeoutBudget(total=total_budget)

    # Phase 1: critical tools (60% of budget, run in parallel)
    phase1_tasks = [
        asyncio.create_task(timed_tool("user_profile", 0.3, budget, 0.3)),
        asyncio.create_task(timed_tool("permissions",  0.2, budget, 0.3)),
    ]
    phase1 = await asyncio.gather(*phase1_tasks)
    print(f"Phase 1 [{budget.elapsed:.1f}s elapsed]: {[r['status'] for r in phase1]}")

    # Phase 2: enrichment tools (40% of remaining budget)
    phase2_tasks = [
        asyncio.create_task(timed_tool("fast_enrichment", 0.4, budget, 0.2)),
        asyncio.create_task(timed_tool("slow_enrichment", 8.0, budget, 0.2)),  # will timeout
        asyncio.create_task(timed_tool("recent_activity", 0.5, budget, 0.2)),
    ]
    phase2 = await asyncio.gather(*phase2_tasks)
    print(f"Phase 2 [{budget.elapsed:.1f}s elapsed]: {[r['status'] for r in phase2]}")

    all_results = phase1 + phase2
    context = "\n".join(
        f"- {r['tool']}: {r.get('result', 'unavailable')}"
        for r in all_results
    )
    synthesis_budget = budget.remaining
    print(f"Synthesis budget remaining: {synthesis_budget:.1f}s")

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"{question}\n\nContext:\n{context}"}],
    )
    return response.content[0].text


result = asyncio.run(run_with_budget("Summarize available user context.", total_budget=4.0))
print(f"\nAgent: {result}")

# Expected Token Savings: N/A; budget propagation prevents synthesis stage from being starved by slow tools
# Environment: Python 3.11+; overhead_reserve should be >= p99 synthesis latency; tune fraction allocation per phase
```

## Option 4: Supervised Task Group with Health Monitoring

```python
import asyncio
import anthropic
import time
from dataclasses import dataclass, field
from typing import Any

client = anthropic.AsyncAnthropic()


@dataclass
class TaskHealth:
    name: str
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    status: str = "running"
    result: Any = None
    error: str = ""

    @property
    def elapsed(self) -> float:
        end = self.finished_at or time.monotonic()
        return end - self.started_at


async def supervised_task(name: str, coro, health: TaskHealth, timeout: float) -> None:
    """Run coro with timeout, updating health record regardless of outcome."""
    try:
        health.result = await asyncio.wait_for(coro, timeout=timeout)
        health.status = "ok"
    except asyncio.TimeoutError:
        health.status = "timeout"
        health.error = f"exceeded {timeout}s"
    except asyncio.CancelledError:
        health.status = "cancelled"
        raise  # Must re-raise CancelledError
    except Exception as e:
        health.status = "error"
        health.error = str(e)
    finally:
        health.finished_at = time.monotonic()


async def simulate_tool(name: str, latency: float) -> str:
    await asyncio.sleep(latency)
    return f"{name}: success"


async def run_supervised_group(question: str) -> str:
    specs = [
        ("tool_a", 0.2, 3.0),
        ("tool_b", 5.0, 0.8),  # times out
        ("tool_c", 0.4, 3.0),
        ("tool_d", 0.1, 3.0),
        ("tool_e", 2.0, 0.5),  # times out
    ]

    healths = {name: TaskHealth(name=name) for name, _, _ in specs}
    tasks = [
        asyncio.create_task(supervised_task(name, simulate_tool(name, latency), healths[name], timeout))
        for name, latency, timeout in specs
    ]

    await asyncio.gather(*tasks, return_exceptions=True)

    # Health report — none cancelled by timeout cascade
    print("Task health:")
    for h in healths.values():
        print(f"  [{h.name}] status={h.status} elapsed={h.elapsed:.2f}s")

    ok_results = [h.result for h in healths.values() if h.status == "ok"]
    timed_out = [h.name for h in healths.values() if h.status == "timeout"]

    if timed_out:
        print(f"Timed out (not cascaded): {timed_out}")

    context = "\n".join(ok_results) if ok_results else "No results available."
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"{question}\n\n{context}"}],
    )
    return response.content[0].text


result = asyncio.run(run_supervised_group("Analyze the available data."))
print(f"\nAgent: {result}")

# Expected Token Savings: N/A; health monitoring surfaces which tools are slow, enabling targeted timeout tuning
# Environment: Python 3.11+; log TaskHealth records to identify p95/p99 latencies per tool over time
```

## Option 5: Fallback Chain on Timeout with Secondary Sources

```python
import asyncio
import anthropic
from typing import Any

client = anthropic.AsyncAnthropic()


async def with_fallback(
    primary_name: str,
    primary_coro,
    fallback_name: str,
    fallback_coro,
    primary_timeout: float,
) -> tuple[str, Any]:
    """
    Try primary with timeout; on timeout/error, try fallback.
    Both are isolated — fallback is not affected by primary timeout.
    """
    try:
        result = await asyncio.wait_for(primary_coro, timeout=primary_timeout)
        return primary_name, result
    except (asyncio.TimeoutError, Exception) as e:
        print(f"Primary {primary_name} failed ({type(e).__name__}), trying fallback {fallback_name}")
        try:
            result = await fallback_coro
            return fallback_name, result
        except Exception as e2:
            return f"{primary_name}+{fallback_name}", f"both failed: {e2}"


async def simulate_source(name: str, latency: float, fail: bool = False) -> str:
    await asyncio.sleep(latency)
    if fail:
        raise RuntimeError(f"{name} returned error")
    return f"{name}: data ready"


async def run_fallback_agent(question: str) -> str:
    # Each pair runs independently; a timeout in pair A doesn't affect pair B
    pairs = [
        with_fallback("primary_db",    simulate_source("primary_db",    5.0),   # slow — will timeout
                      "replica_db",    simulate_source("replica_db",    0.3),   1.0),
        with_fallback("live_search",   simulate_source("live_search",   0.2),   # fast — succeeds
                      "cache_search",  simulate_source("cache_search",  0.1),   2.0),
        with_fallback("api_primary",   simulate_source("api_primary",   4.0),   # slow — will timeout
                      "api_secondary", simulate_source("api_secondary", 0.4),   0.8),
    ]

    results = await asyncio.gather(*[asyncio.create_task(p) for p in pairs])

    print("Fallback results:")
    for source, value in results:
        print(f"  [{source}] {value}")

    context = "\n".join(f"- {src}: {val}" for src, val in results)
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"{question}\n\nSources:\n{context}"}],
    )
    return response.content[0].text


result = asyncio.run(run_fallback_agent("What data is available?"))
print(f"\nAgent: {result}")

# Expected Token Savings: N/A; fallback chains recover ~100% of data availability vs ~50% without fallbacks
# Environment: Python 3.11+; primary_timeout should be p75 latency of primary so fallback fires before user notices
```

## Option 6: Structured Concurrency with TaskGroup and Partial Results

```python
import asyncio
import anthropic
from typing import Any

client = anthropic.AsyncAnthropic()


class PartialResultCollector:
    """Collects results from tasks that may individually timeout or fail."""

    def __init__(self) -> None:
        self.results: dict[str, Any] = {}
        self.errors: dict[str, str] = {}

    def record_ok(self, name: str, value: Any) -> None:
        self.results[name] = value

    def record_error(self, name: str, error: str) -> None:
        self.errors[name] = error

    @property
    def success_count(self) -> int:
        return len(self.results)

    @property
    def error_count(self) -> int:
        return len(self.errors)


async def isolated_tool_call(name: str, latency: float, timeout: float,
                              collector: PartialResultCollector) -> None:
    """Run a tool in isolation; record result or error without propagating."""
    try:
        await asyncio.wait_for(asyncio.sleep(latency), timeout=timeout)
        collector.record_ok(name, f"{name}: completed in {min(latency, timeout):.1f}s")
    except asyncio.TimeoutError:
        collector.record_error(name, f"timeout after {timeout}s")
    except Exception as e:
        collector.record_error(name, str(e))


async def run_structured_agent(question: str) -> str:
    collector = PartialResultCollector()

    tool_specs = [
        ("web_search",     0.3, 3.0),
        ("knowledge_base", 0.5, 3.0),
        ("slow_third_party", 8.0, 1.0),  # times out
        ("local_cache",    0.1, 3.0),
        ("analytics_api",  6.0, 0.7),   # times out
        ("user_history",   0.4, 3.0),
    ]

    # Python 3.11+ TaskGroup — each task is independently isolated
    async with asyncio.TaskGroup() as tg:
        for name, latency, timeout in tool_specs:
            tg.create_task(isolated_tool_call(name, latency, timeout, collector))

    print(f"Completed: {collector.success_count}/{len(tool_specs)} tools")
    print(f"Errors: {collector.errors}")

    context_parts = [f"- {name}: {value}" for name, value in collector.results.items()]
    unavailable = [f"- {name}: unavailable ({err})" for name, err in collector.errors.items()]

    context = "\n".join(context_parts + unavailable)

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"{question}\n\nResults ({collector.success_count} of {len(tool_specs)} sources available):\n"
                + context
            ),
        }],
    )
    return response.content[0].text


result = asyncio.run(run_structured_agent("Summarize all available information."))
print(f"\nAgent: {result}")

# Expected Token Savings: N/A; TaskGroup keeps all tasks alive regardless of siblings; partial results > zero results
# Environment: Python 3.11+; TaskGroup propagates ExceptionGroup only if all tasks raise, not on isolated timeouts
```

## Comparison

| Option | Isolation Mechanism | Partial Results | Fallback | Budget Tracking | Best For |
|--------|-------------------|-----------------|----------|-----------------|----------|
| 1. Per-Task wait_for | Independent asyncio.wait_for | Yes | No | No | Simple parallel tool calls |
| 2. Bulkhead Groups | Group-scoped timeouts | Yes (by group) | No | No | Multi-tier service dependencies |
| 3. Budget Distributor | Proportional time allocation | Yes | No | Yes | SLA-bound agent turns |
| 4. Supervised Group | Health record per task | Yes | No | No | Observability + diagnostics |
| 5. Fallback Chain | Primary → secondary on timeout | Yes (via fallback) | Yes | No | Redundant data sources |
| 6. TaskGroup + Collector | Structured concurrency | Yes | No | No | Python 3.11+ clean structured code |
