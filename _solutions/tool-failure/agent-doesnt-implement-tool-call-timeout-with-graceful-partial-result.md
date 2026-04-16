---
layout: solution
title: "Agent Doesn't Implement Tool Call Timeout with Graceful Partial Result"
category: tool-failure
description: "Apply per-tool timeouts so a slow or hung tool call doesn't block the entire agent. Return partial results from completed tools while gracefully degrading for the timed-out ones."
tags: [tool-failure, timeout, partial-result, graceful-degradation, async, reliability, resilience]
---

# Agent Doesn't Implement Tool Call Timeout with Graceful Partial Result

## Problem

An agent calls three tools in sequence: a database lookup, a web search, and a calculation service. The web search hangs for 90 seconds waiting for a slow external API. The agent blocks, the user's request times out, and no partial answer is delivered — even though the database and calculation results were ready in 2 seconds. Per-tool timeouts with graceful partial result delivery allow the agent to use available results and clearly communicate which tools failed.

## Solution Options

### Option 1: asyncio.wait_for Per-Tool Timeout

```python
import anthropic
import asyncio
from dataclasses import dataclass


@dataclass
class ToolResult:
    tool_name: str
    result: str | None
    timed_out: bool = False
    error: str | None = None

    @property
    def available(self) -> bool:
        return self.result is not None


async def db_lookup(query: str) -> str:
    await asyncio.sleep(0.1)  # fast
    return f"DB: records for '{query}'"


async def web_search(query: str) -> str:
    await asyncio.sleep(10.0)  # slow — will timeout
    return f"Web: results for '{query}'"


async def calculate(query: str) -> str:
    await asyncio.sleep(0.2)  # medium
    return f"Calc: computed answer for '{query}'"


TOOL_TIMEOUTS = {"db_lookup": 2.0, "web_search": 1.5, "calculate": 3.0}
TOOL_FNS = {"db_lookup": db_lookup, "web_search": web_search, "calculate": calculate}


async def call_with_timeout(name: str, query: str) -> ToolResult:
    timeout = TOOL_TIMEOUTS.get(name, 5.0)
    try:
        result = await asyncio.wait_for(TOOL_FNS[name](query), timeout=timeout)
        return ToolResult(tool_name=name, result=result)
    except asyncio.TimeoutError:
        return ToolResult(tool_name=name, result=None, timed_out=True, error=f"Timed out after {timeout}s")
    except Exception as e:
        return ToolResult(tool_name=name, result=None, error=str(e))


async def agent_with_partial_results(query: str) -> str:
    # Call all tools concurrently with per-tool timeouts
    results = await asyncio.gather(
        call_with_timeout("db_lookup", query),
        call_with_timeout("web_search", query),
        call_with_timeout("calculate", query),
    )

    context_parts = []
    degraded = []
    for r in results:
        if r.available:
            context_parts.append(f"{r.tool_name}: {r.result}")
        else:
            degraded.append(f"{r.tool_name}: {r.error}")

    system = "Answer using available tool results. Acknowledge any unavailable tools."
    if degraded:
        system += f"\nNote: these tools failed: {'; '.join(degraded)}"

    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": f"Query: {query}\n\nAvailable results:\n" + "\n".join(context_parts)}],
    )
    await client.close()
    return resp.content[0].text


if __name__ == "__main__":
    result = asyncio.run(agent_with_partial_results("quarterly sales data"))
    print(result)

# Expected Token Savings: No extra tokens; partial results delivered without waiting for timed-out tools
# Environment: Agents calling multiple independent tools where individual tool SLAs differ significantly
```

---

### Option 2: Tool Budget with Cascading Fallback

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Awaitable


@dataclass
class ToolSpec:
    name: str
    fn: Callable[..., Awaitable[str]]
    timeout: float
    fallback: str | None = None   # static fallback when timed out
    required: bool = False        # if True, failure is propagated to caller


@dataclass
class ExecutionResult:
    name: str
    value: str
    source: str  # "tool" | "fallback" | "error"
    duration_ms: float


async def call_tool_spec(spec: ToolSpec, *args, **kwargs) -> ExecutionResult:
    start = time.perf_counter()
    try:
        value = await asyncio.wait_for(spec.fn(*args, **kwargs), timeout=spec.timeout)
        elapsed = (time.perf_counter() - start) * 1000
        return ExecutionResult(name=spec.name, value=value, source="tool", duration_ms=elapsed)
    except asyncio.TimeoutError:
        elapsed = (time.perf_counter() - start) * 1000
        if spec.required:
            raise RuntimeError(f"Required tool '{spec.name}' timed out after {spec.timeout}s")
        if spec.fallback is not None:
            return ExecutionResult(name=spec.name, value=spec.fallback, source="fallback", duration_ms=elapsed)
        return ExecutionResult(name=spec.name, value=f"[{spec.name} unavailable]", source="error", duration_ms=elapsed)
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        if spec.required:
            raise
        return ExecutionResult(name=spec.name, value=spec.fallback or f"[{spec.name} error: {e}]", source="error", duration_ms=elapsed)


# Tool implementations
async def user_profile(user_id: str) -> str:
    await asyncio.sleep(0.05)
    return f"User {user_id}: premium plan, joined 2023"


async def recommendation_engine(user_id: str) -> str:
    await asyncio.sleep(5.0)  # slow — will timeout
    return "Top recommendations: ..."


async def content_cache(user_id: str) -> str:
    await asyncio.sleep(0.1)
    return "Cached content: [homepage, trending, recent]"


TOOL_SPECS = [
    ToolSpec("user_profile",        user_profile,          timeout=1.0, required=True),
    ToolSpec("recommendations",     recommendation_engine, timeout=0.5, fallback="[Popular items: A, B, C]"),
    ToolSpec("content_cache",       content_cache,         timeout=2.0, fallback="[Default content]"),
]


async def agent_with_fallbacks(user_id: str, query: str) -> str:
    results = await asyncio.gather(
        *[call_tool_spec(spec, user_id) for spec in TOOL_SPECS],
        return_exceptions=False,
    )
    context = "\n".join(
        f"{r.name} ({r.source}, {r.duration_ms:.0f}ms): {r.value}"
        for r in results
    )
    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system="You are a personalized assistant. Use the tool results below.",
        messages=[{"role": "user", "content": f"Context:\n{context}\n\nUser query: {query}"}],
    )
    await client.close()
    return resp.content[0].text


if __name__ == "__main__":
    result = asyncio.run(agent_with_fallbacks("user-42", "Show me what's trending"))
    print(result)

# Expected Token Savings: Fallback strings replace timed-out results; required=True fails fast
# Environment: Personalization agents where some data sources are non-essential for a usable response
```

---

### Option 3: Streaming Tool Results with Per-Tool Deadline

```python
import anthropic
import asyncio
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass
class StreamedToolResult:
    name: str
    chunks: list[str]
    complete: bool
    timed_out: bool = False

    @property
    def text(self) -> str:
        return "".join(self.chunks)


async def streaming_tool(name: str, delay_per_chunk: float, num_chunks: int, query: str) -> AsyncIterator[str]:
    """Simulates a tool that yields partial results as they arrive."""
    for i in range(num_chunks):
        await asyncio.sleep(delay_per_chunk)
        yield f"[{name} chunk {i + 1}/{num_chunks}: {query[:20]}]"


async def call_streaming_with_deadline(
    name: str,
    gen: AsyncIterator[str],
    deadline: float,
) -> StreamedToolResult:
    chunks = []
    end_time = asyncio.get_event_loop().time() + deadline
    timed_out = False

    try:
        async for chunk in gen:
            if asyncio.get_event_loop().time() > end_time:
                timed_out = True
                break
            chunks.append(chunk)
    except Exception:
        pass

    return StreamedToolResult(
        name=name,
        chunks=chunks,
        complete=not timed_out,
        timed_out=timed_out,
    )


async def agent_streaming_tools(query: str) -> str:
    tool_tasks = [
        call_streaming_with_deadline(
            "fast_tool",
            streaming_tool("fast_tool", 0.05, 4, query),
            deadline=1.0,
        ),
        call_streaming_with_deadline(
            "slow_tool",
            streaming_tool("slow_tool", 0.4, 6, query),
            deadline=1.0,  # only gets ~2 chunks
        ),
        call_streaming_with_deadline(
            "medium_tool",
            streaming_tool("medium_tool", 0.15, 4, query),
            deadline=1.0,
        ),
    ]
    results = await asyncio.gather(*tool_tasks)

    context_parts = []
    for r in results:
        status = "partial" if r.timed_out else "complete"
        context_parts.append(f"{r.name} [{status}]: {r.text[:100]}")

    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system="Synthesize the available tool results. Note any partial results.",
        messages=[{"role": "user", "content": f"Query: {query}\n\n" + "\n".join(context_parts)}],
    )
    await client.close()
    return resp.content[0].text


if __name__ == "__main__":
    result = asyncio.run(agent_streaming_tools("analyze customer data"))
    print(result)

# Expected Token Savings: Partial streaming chunks used immediately; no wait for full completion
# Environment: Tools that produce incremental results (e.g., search with pagination, long SQL queries)
```

---

### Option 4: Tool Call Timeout Registry with Adaptive Learning

```python
import anthropic
import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
import statistics


@dataclass
class ToolTimingRecord:
    durations: list[float] = field(default_factory=list)
    timeouts: int = 0
    successes: int = 0

    def record_success(self, duration: float) -> None:
        self.durations.append(duration)
        self.successes += 1
        if len(self.durations) > 100:
            self.durations.pop(0)

    def record_timeout(self) -> None:
        self.timeouts += 1

    def adaptive_timeout(self, percentile: float = 0.95, min_t: float = 0.5, max_t: float = 30.0) -> float:
        if len(self.durations) < 5:
            return 5.0  # default before enough data
        sorted_d = sorted(self.durations)
        idx = int(len(sorted_d) * percentile)
        p_value = sorted_d[min(idx, len(sorted_d) - 1)]
        return max(min_t, min(p_value * 1.5, max_t))  # 1.5x p95 as safety margin


class AdaptiveTimeoutRegistry:
    def __init__(self) -> None:
        self._records: dict[str, ToolTimingRecord] = defaultdict(ToolTimingRecord)

    async def call(self, name: str, fn, *args, **kwargs):
        record = self._records[name]
        timeout = record.adaptive_timeout()
        start = time.perf_counter()

        try:
            result = await asyncio.wait_for(fn(*args, **kwargs), timeout=timeout)
            record.record_success(time.perf_counter() - start)
            return result, False  # (result, timed_out)
        except asyncio.TimeoutError:
            record.record_timeout()
            return None, True

    def stats(self) -> dict:
        return {
            name: {
                "adaptive_timeout": round(r.adaptive_timeout(), 2),
                "successes": r.successes,
                "timeouts": r.timeouts,
                "p50_ms": round(statistics.median(r.durations) * 1000) if r.durations else None,
            }
            for name, r in self._records.items()
        }


registry = AdaptiveTimeoutRegistry()

_call_n = 0
async def variable_latency_tool(name: str) -> str:
    global _call_n
    _call_n += 1
    # Simulate variable latency: occasionally slow
    delay = 0.1 if _call_n % 7 != 0 else 3.0
    await asyncio.sleep(delay)
    return f"{name}: result"


async def main() -> None:
    client = anthropic.AsyncAnthropic()

    for i in range(15):
        result, timed_out = await registry.call("data_api", variable_latency_tool, "data_api")
        ctx = result if result else "[data_api timed out]"
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=32,
            messages=[{"role": "user", "content": f"Request {i}: {ctx}. Confirm receipt."}],
        )
        print(f"[{i:02d}] timed_out={timed_out} timeout={registry.stats()['data_api']['adaptive_timeout']:.2f}s")

    print("\nFinal stats:", registry.stats())
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Adaptive timeouts shrink as tool becomes reliable; tight SLAs cut wait time
# Environment: Agents with tools whose latency varies and improves over time as infrastructure stabilizes
```

---

### Option 5: Hierarchical Tool Timeout with Priority Lanes

```python
import anthropic
import asyncio
from dataclasses import dataclass
from enum import Enum


class Priority(Enum):
    CRITICAL = 1   # must complete; agent blocks if it fails
    HIGH     = 2   # 2s timeout; partial degrades gracefully
    LOW      = 3   # 0.5s timeout; skipped if slow


PRIORITY_TIMEOUTS = {Priority.CRITICAL: 10.0, Priority.HIGH: 2.0, Priority.LOW: 0.5}


@dataclass
class PrioritizedTool:
    name: str
    priority: Priority
    fn: object  # async callable


async def agent_prioritized_tools(tools: list[PrioritizedTool], query: str) -> str:
    """
    Execute tools in priority order. CRITICAL tools block; HIGH/LOW are best-effort.
    LOW tools only run if time budget remains after CRITICAL + HIGH complete.
    """
    results: dict[str, str] = {}
    deadline = asyncio.get_event_loop().time() + 5.0  # overall agent deadline

    # Phase 1: CRITICAL tools (must complete)
    critical = [t for t in tools if t.priority == Priority.CRITICAL]
    for tool in critical:
        timeout = PRIORITY_TIMEOUTS[Priority.CRITICAL]
        try:
            result = await asyncio.wait_for(tool.fn(query), timeout=timeout)
            results[tool.name] = result
        except asyncio.TimeoutError:
            raise RuntimeError(f"Critical tool '{tool.name}' timed out")

    # Phase 2: HIGH priority (concurrent, 2s each)
    high = [t for t in tools if t.priority == Priority.HIGH]
    if high and asyncio.get_event_loop().time() < deadline:
        high_results = await asyncio.gather(
            *[asyncio.wait_for(t.fn(query), timeout=PRIORITY_TIMEOUTS[Priority.HIGH]) for t in high],
            return_exceptions=True,
        )
        for tool, res in zip(high, high_results):
            results[tool.name] = str(res) if not isinstance(res, Exception) else f"[{tool.name}: unavailable]"

    # Phase 3: LOW priority (only if budget remains)
    low = [t for t in tools if t.priority == Priority.LOW]
    if low and asyncio.get_event_loop().time() < deadline - 0.5:
        low_results = await asyncio.gather(
            *[asyncio.wait_for(t.fn(query), timeout=PRIORITY_TIMEOUTS[Priority.LOW]) for t in low],
            return_exceptions=True,
        )
        for tool, res in zip(low, low_results):
            if not isinstance(res, Exception):
                results[tool.name] = str(res)

    context = "\n".join(f"{k}: {v}" for k, v in results.items())
    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system="Answer using available results.",
        messages=[{"role": "user", "content": f"Query: {query}\n\n{context}"}],
    )
    await client.close()
    return resp.content[0].text


async def main() -> None:
    async def auth_check(q: str) -> str:
        await asyncio.sleep(0.1)
        return "authenticated"

    async def main_data(q: str) -> str:
        await asyncio.sleep(0.3)
        return f"main data for {q}"

    async def analytics(q: str) -> str:
        await asyncio.sleep(3.0)  # too slow for LOW
        return f"analytics for {q}"

    async def cache_hint(q: str) -> str:
        await asyncio.sleep(0.2)
        return "cache: warm"

    tools = [
        PrioritizedTool("auth",      Priority.CRITICAL, auth_check),
        PrioritizedTool("main_data", Priority.HIGH,     main_data),
        PrioritizedTool("analytics", Priority.LOW,      analytics),
        PrioritizedTool("cache",     Priority.LOW,      cache_hint),
    ]
    result = await agent_prioritized_tools(tools, "show dashboard")
    print(result)


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: LOW-priority tools skipped when budget exhausted; agent always delivers critical data
# Environment: Agents with strict latency SLAs where some tool data is nice-to-have, not essential
```

---

### Option 6: Tool Timeout with Checkpoint and Resume

```python
import anthropic
import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class ToolCheckpoint:
    tool_name: str
    status: str   # "pending" | "complete" | "timed_out"
    result: str | None = None
    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None


@dataclass
class AgentCheckpointState:
    query: str
    tools: dict[str, ToolCheckpoint] = field(default_factory=dict)
    final_answer: str | None = None


class CheckpointedToolAgent:
    """
    Saves progress after each tool completes.
    On timeout or crash, resumes from last checkpoint — skipping already-completed tools.
    """

    CHECKPOINT_PATH = Path("/tmp/agent_tool_checkpoint.json")

    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic()

    def _save(self, state: AgentCheckpointState) -> None:
        data = {
            "query": state.query,
            "tools": {k: asdict(v) for k, v in state.tools.items()},
            "final_answer": state.final_answer,
        }
        self.CHECKPOINT_PATH.write_text(json.dumps(data, indent=2))

    def _load(self) -> AgentCheckpointState | None:
        if not self.CHECKPOINT_PATH.exists():
            return None
        try:
            data = json.loads(self.CHECKPOINT_PATH.read_text())
            state = AgentCheckpointState(query=data["query"], final_answer=data.get("final_answer"))
            for name, cp in data.get("tools", {}).items():
                state.tools[name] = ToolCheckpoint(**cp)
            return state
        except Exception:
            return None

    async def _run_tool(self, name: str, fn, query: str, timeout: float) -> ToolCheckpoint:
        cp = ToolCheckpoint(tool_name=name, status="pending")
        try:
            result = await asyncio.wait_for(fn(query), timeout=timeout)
            cp.status = "complete"
            cp.result = result
            cp.completed_at = time.time()
        except asyncio.TimeoutError:
            cp.status = "timed_out"
        return cp

    async def run(self, query: str, resume: bool = False) -> str:
        state = self._load() if resume else None
        if state is None or state.query != query:
            state = AgentCheckpointState(query=query)

        if state.final_answer:
            print("[checkpoint] Returning cached final answer")
            return state.final_answer

        # Tool definitions
        async def tool_a(q: str) -> str:
            await asyncio.sleep(0.2)
            return f"tool_a: data for {q}"

        async def tool_b(q: str) -> str:
            await asyncio.sleep(5.0)  # will timeout
            return f"tool_b: result for {q}"

        async def tool_c(q: str) -> str:
            await asyncio.sleep(0.3)
            return f"tool_c: analysis for {q}"

        tools = [("tool_a", tool_a, 2.0), ("tool_b", tool_b, 1.0), ("tool_c", tool_c, 2.0)]

        for name, fn, timeout in tools:
            if name in state.tools and state.tools[name].status == "complete":
                print(f"[checkpoint] Skipping '{name}' (already complete)")
                continue
            cp = await self._run_tool(name, fn, query, timeout)
            state.tools[name] = cp
            self._save(state)
            print(f"[checkpoint] '{name}': {cp.status}")

        # Synthesize from available results
        context = "\n".join(
            f"{name}: {cp.result}" for name, cp in state.tools.items() if cp.result
        )
        missed = [n for n, cp in state.tools.items() if cp.status == "timed_out"]
        system = f"Tools timed out: {missed}. Use available results only." if missed else ""

        resp = await self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            system=system,
            messages=[{"role": "user", "content": f"Query: {query}\n\n{context}"}],
        )
        state.final_answer = resp.content[0].text
        self._save(state)
        await self._client.close()
        return state.final_answer


if __name__ == "__main__":
    agent = CheckpointedToolAgent()
    result = asyncio.run(agent.run("generate quarterly report"))
    print(f"\nAnswer: {result}")

    # Simulate resume after timeout
    agent2 = CheckpointedToolAgent()
    result2 = asyncio.run(agent2.run("generate quarterly report", resume=True))
    print(f"Resumed: {result2[:60]}")

# Expected Token Savings: Resumed runs skip completed tools; no re-calling successful tool calls
# Environment: Long-running agents with expensive tool calls where partial progress must survive restarts
```

---

## Comparison

| Option | Timeout Mechanism | Partial Result Handling | State Persistence | Complexity |
|--------|------------------|-----------------------|-------------------|------------|
| 1 | `asyncio.wait_for` per tool | Collect available + note failures | None | Very Low |
| 2 | Per-spec timeout + fallback value | Static fallback strings | None | Low |
| 3 | Deadline-based streaming chunk collection | Partial chunks used | None | Medium |
| 4 | Adaptive P95-based timeout learning | None returned on timeout | In-memory stats | Medium |
| 5 | Priority lanes (CRITICAL/HIGH/LOW) | Phase-based best-effort | None | Medium |
| 6 | Per-tool timeout + JSON checkpoint | Resume skips completed tools | JSON file | High |
