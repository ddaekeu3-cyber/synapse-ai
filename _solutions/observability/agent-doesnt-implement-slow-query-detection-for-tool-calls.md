---
title: "Agent Doesn't Implement Slow Query Detection for Tool Calls"
slug: agent-doesnt-implement-slow-query-detection-for-tool-calls
category: observability
tags: [observability, performance, tool-calls, slow-query, profiling, anthropic-sdk]
description: >
  The agent invokes tools (database queries, web searches, code execution) without
  tracking individual execution times. Slow tool calls silently inflate end-to-end
  latency with no log evidence, no alerting threshold, and no automatic escalation
  to faster alternatives.
symptoms:
  - p99 latency is high but the cause is invisible in traces
  - One tool call occasionally takes 15 s but the agent never logs it as slow
  - No histogram of tool call durations to guide optimization
  - Slow queries recur indefinitely because there is no detection to trigger a fix
related_solutions:
  - agent-doesnt-implement-distributed-trace-propagation
  - agent-doesnt-implement-agent-decision-explainability-dashboard
  - agent-doesnt-implement-cost-per-conversation-tracking
---

## Problem

Tool calls in an agentic loop can have wildly variable latency: a cached DB
lookup might take 2 ms while a cold full-table scan takes 8 s. Without per-tool
timing instrumentation, engineers have no data to prioritize optimization work.
Slow-query detection — borrowed from database engineering — logs any tool call
that exceeds a threshold, records its arguments for reproducibility, and
optionally triggers automatic mitigation (cached fallback, faster tool variant,
or user-visible progress update).

---

## Solution 1 — Simple Threshold Logger

Wrap every tool call in a timer and emit a structured log entry when duration
exceeds a configurable threshold.

```python
import anthropic
import asyncio
import json
import time
from dataclasses import dataclass


SLOW_THRESHOLD_S = 2.0   # log if tool call takes longer than this


@dataclass
class ToolCallRecord:
    tool_name:   str
    input:       dict
    duration_s:  float
    is_slow:     bool
    output:      str


def log_slow_query(record: ToolCallRecord) -> None:
    print(json.dumps({
        "level":      "WARN" if record.is_slow else "DEBUG",
        "event":      "tool_call",
        "tool":       record.tool_name,
        "duration_s": round(record.duration_s, 3),
        "slow":       record.is_slow,
        "input":      str(record.input)[:256],
        "output":     record.output[:128],
    }))


async def timed_tool(tool_name: str, tool_input: dict, slow_threshold_s: float = SLOW_THRESHOLD_S) -> str:
    """Execute a tool and log if it is slow. Replace the body with real tool logic."""
    t0 = time.monotonic()

    # --- Simulated tool execution ---
    if tool_name == "db_query":
        await asyncio.sleep(0.05 if tool_input.get("cached") else 3.0)
        result = f"DB result for {tool_input}"
    elif tool_name == "web_search":
        await asyncio.sleep(1.2)
        result = f"Search results for {tool_input.get('query', '')}"
    else:
        await asyncio.sleep(0.01)
        result = f"Result from {tool_name}"
    # --- End simulated execution ---

    duration = time.monotonic() - t0
    record = ToolCallRecord(
        tool_name=tool_name,
        input=tool_input,
        duration_s=duration,
        is_slow=duration >= slow_threshold_s,
        output=result,
    )
    log_slow_query(record)
    return result


TOOLS = [
    {
        "name": "db_query",
        "description": "Query the database.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql":    {"type": "string"},
                "cached": {"type": "boolean"},
            },
            "required": ["sql"],
        },
    },
    {
        "name": "web_search",
        "description": "Search the web.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
]


async def agent_loop(user_query: str) -> str:
    client = anthropic.AsyncAnthropic()
    messages = [{"role": "user", "content": user_query}]

    while True:
        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )

        if resp.stop_reason == "end_turn":
            return next((b.text for b in resp.content if hasattr(b, "text")), "")

        tool_results = []
        for block in resp.content:
            if block.type == "tool_use":
                result = await timed_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "assistant", "content": resp.content})
        messages.append({"role": "user", "content": tool_results})


result = asyncio.run(agent_loop("Search for Python async best practices and query the database."))
print(f"\nAgent response: {result[:80]}")
```

---

## Solution 2 — Rolling Histogram for Per-Tool Latency Percentiles

Maintain a rolling window of latency samples per tool and compute p50/p95/p99
on demand, enabling dashboard queries and adaptive threshold tuning.

```python
import anthropic
import asyncio
import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Callable, Awaitable


@dataclass
class LatencyHistogram:
    window_size: int = 500
    _samples: deque = field(default_factory=deque)

    def record(self, duration_s: float) -> None:
        self._samples.append(duration_s)
        if len(self._samples) > self.window_size:
            self._samples.popleft()

    def percentile(self, pct: float) -> float | None:
        if not self._samples:
            return None
        s = sorted(self._samples)
        idx = math.ceil(pct / 100 * len(s)) - 1
        return s[max(0, idx)]

    def stats(self) -> dict:
        if not self._samples:
            return {}
        s = sorted(self._samples)
        return {
            "count": len(s),
            "p50":   round(s[len(s) // 2], 3),
            "p95":   round(s[int(0.95 * len(s))], 3),
            "p99":   round(s[int(0.99 * len(s))], 3),
            "max":   round(s[-1], 3),
        }


_histograms: dict[str, LatencyHistogram] = defaultdict(LatencyHistogram)


async def instrumented_tool(
    tool_name: str,
    tool_input: dict,
    executor: Callable[[str, dict], Awaitable[str]],
    slow_pct_threshold: float = 95.0,
) -> str:
    t0 = time.monotonic()
    result = await executor(tool_name, tool_input)
    duration = time.monotonic() - t0

    hist = _histograms[tool_name]
    hist.record(duration)

    p95 = hist.percentile(slow_pct_threshold)
    is_slow = p95 is not None and duration > p95
    if is_slow:
        print(f"[slow-tool] {tool_name} took {duration:.2f}s > p{slow_pct_threshold:.0f}={p95:.2f}s")
    return result


def tool_stats() -> dict[str, dict]:
    return {name: hist.stats() for name, hist in _histograms.items()}


# Demo executor stub
async def _stub_executor(tool_name: str, tool_input: dict) -> str:
    import random
    base = {"db_query": 0.1, "web_search": 0.8, "code_exec": 0.3}.get(tool_name, 0.1)
    await asyncio.sleep(base * random.uniform(0.5, 8.0))
    return f"{tool_name} result"


async def demo_histogram():
    # Simulate 20 tool calls to build histogram
    for i in range(20):
        for tool in ["db_query", "web_search", "code_exec"]:
            await instrumented_tool(tool, {"i": i}, _stub_executor)

    print("\nTool latency statistics:")
    for name, stats in tool_stats().items():
        print(f"  {name}: {stats}")


asyncio.run(demo_histogram())
```

---

## Solution 3 — Adaptive Threshold with Automatic Fallback

Use the historical p95 as a dynamic slow threshold. When a tool call exceeds
this threshold, automatically invoke a faster fallback (cached lookup, simpler
query, or lightweight tool variant) instead of waiting for the slow call.

```python
import anthropic
import asyncio
import math
import time
from collections import defaultdict, deque
from typing import Callable, Awaitable


_latency_windows: dict[str, deque] = defaultdict(lambda: deque(maxlen=100))


def _p95(tool_name: str) -> float:
    samples = list(_latency_windows[tool_name])
    if len(samples) < 5:
        return 5.0   # cold-start default
    s = sorted(samples)
    idx = int(0.95 * len(s))
    return s[min(idx, len(s) - 1)]


ToolFn = Callable[[dict], Awaitable[str]]


async def with_adaptive_fallback(
    tool_name: str,
    tool_input: dict,
    primary_fn: ToolFn,
    fallback_fn: ToolFn,
    deadline_multiplier: float = 1.5,
) -> tuple[str, str]:
    """
    Run primary_fn with a deadline = p95 * multiplier.
    If it times out, run fallback_fn and return that result.
    Returns (result, "primary" | "fallback").
    """
    deadline = _p95(tool_name) * deadline_multiplier
    t0 = time.monotonic()

    try:
        result = await asyncio.wait_for(primary_fn(tool_input), timeout=deadline)
        duration = time.monotonic() - t0
        _latency_windows[tool_name].append(duration)
        return result, "primary"
    except asyncio.TimeoutError:
        duration = time.monotonic() - t0
        _latency_windows[tool_name].append(duration)
        print(
            f"[adaptive-fallback] {tool_name} timed out after {duration:.2f}s "
            f"(p95={_p95(tool_name):.2f}s) — using fallback"
        )
        fallback_result = await fallback_fn(tool_input)
        return fallback_result, "fallback"


# Stubs
async def slow_db(inp: dict) -> str:
    await asyncio.sleep(4.0)
    return f"DB(slow): {inp}"

async def fast_cache(inp: dict) -> str:
    await asyncio.sleep(0.05)
    return f"CACHE(fast): {inp}"

async def slow_search(inp: dict) -> str:
    await asyncio.sleep(2.0)
    return f"Search(slow): {inp}"

async def fast_search_stub(inp: dict) -> str:
    await asyncio.sleep(0.2)
    return f"Search(fast-stub): {inp}"


async def demo_adaptive():
    # Warm up with a few fast calls to establish baseline p95
    for i in range(5):
        _latency_windows["db_query"].append(0.1 * (i + 1))
    _latency_windows["db_query"].append(0.8)

    result, source = await with_adaptive_fallback(
        "db_query", {"sql": "SELECT * FROM users WHERE id=1"},
        primary_fn=slow_db,
        fallback_fn=fast_cache,
    )
    print(f"[db_query] source={source}  result={result}")

    result, source = await with_adaptive_fallback(
        "web_search", {"query": "asyncio best practices"},
        primary_fn=slow_search,
        fallback_fn=fast_search_stub,
    )
    print(f"[web_search] source={source}  result={result}")


asyncio.run(demo_adaptive())
```

---

## Solution 4 — Tool Call Profiler with Flame-Graph-Style Output

Record a nested call tree for the entire agentic loop turn, then render it as
an ASCII flame graph showing which tool dominated latency.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CallNode:
    name:     str
    start:    float = field(default_factory=time.monotonic)
    end:      float = 0.0
    children: list["CallNode"] = field(default_factory=list)
    meta:     dict = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        return (self.end - self.start) * 1000

    def finish(self, **meta) -> None:
        self.end = time.monotonic()
        self.meta.update(meta)

    def render(self, indent: int = 0, total_ms: float = 0.0) -> str:
        dur = self.duration_ms
        pct = f"{dur / total_ms * 100:.0f}%" if total_ms else ""
        bar = "█" * int(dur / max(total_ms, 1) * 40)
        line = f"{'  ' * indent}{self.name:<35} {dur:8.1f}ms {pct:5s} {bar}"
        children_lines = "".join(c.render(indent + 1, total_ms) for c in self.children)
        return line + "\n" + children_lines


@dataclass
class TurnProfiler:
    root: CallNode = field(default_factory=lambda: CallNode("agent_turn"))

    def tool_span(self, tool_name: str) -> "ToolSpanCtx":
        node = CallNode(f"tool:{tool_name}")
        self.root.children.append(node)
        return ToolSpanCtx(node)

    def llm_span(self) -> "ToolSpanCtx":
        node = CallNode("llm.create")
        self.root.children.append(node)
        return ToolSpanCtx(node)

    def finish(self) -> None:
        self.root.finish()

    def report(self) -> str:
        total = self.root.duration_ms
        return f"\n{'='*60}\nAgent Turn Profile  ({total:.1f}ms total)\n{'='*60}\n" + \
               self.root.render(total_ms=total)


class ToolSpanCtx:
    def __init__(self, node: CallNode):
        self._node = node

    async def __aenter__(self) -> CallNode:
        return self._node

    async def __aexit__(self, *_) -> None:
        self._node.finish()


# Simulated tools for demo
async def _run_tool(name: str, inp: dict) -> str:
    delays = {"db_query": 0.12, "web_search": 0.85, "code_exec": 0.45}
    await asyncio.sleep(delays.get(name, 0.05))
    return f"{name}({inp}) => result"


TOOLS = [
    {"name": "db_query",   "description": "Query DB.",  "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}},
    {"name": "web_search", "description": "Web search.", "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
]


async def profiled_agent_turn(user_query: str) -> str:
    profiler = TurnProfiler()
    client   = anthropic.AsyncAnthropic()
    messages = [{"role": "user", "content": user_query}]

    while True:
        async with profiler.llm_span() as llm_node:
            resp = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=512,
                tools=TOOLS,
                messages=messages,
            )
            llm_node.meta["tokens"] = resp.usage.output_tokens

        if resp.stop_reason == "end_turn":
            break

        tool_results = []
        for block in resp.content:
            if block.type == "tool_use":
                async with profiler.tool_span(block.name) as tool_node:
                    result = await _run_tool(block.name, block.input)
                    tool_node.meta["input"] = str(block.input)[:64]
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages.append({"role": "assistant", "content": resp.content})
        messages.append({"role": "user",      "content": tool_results})

    profiler.finish()
    print(profiler.report())
    return next((b.text for b in resp.content if hasattr(b, "text")), "")


asyncio.run(profiled_agent_turn("Search the web and query the database about caching strategies."))
```

---

## Solution 5 — Slow Tool Alerting with Rate-Limited Notifications

Alert on slow tool calls but rate-limit notifications so a sustained slowdown
doesn't flood the alerting channel. Uses a per-tool cooldown window.

```python
import anthropic
import asyncio
import time
from collections import defaultdict
from typing import Callable, Awaitable


SLOW_THRESHOLD_S  = 2.0
ALERT_COOLDOWN_S  = 60.0   # only alert once per minute per tool


_last_alert: dict[str, float] = defaultdict(float)


async def _default_alert(tool_name: str, duration_s: float, tool_input: dict) -> None:
    print(
        f"🚨 [ALERT] Slow tool call detected!\n"
        f"   tool={tool_name}  duration={duration_s:.2f}s\n"
        f"   input={str(tool_input)[:128]}"
    )


async def rate_limited_slow_alert(
    tool_name: str,
    duration_s: float,
    tool_input: dict,
    threshold_s: float = SLOW_THRESHOLD_S,
    cooldown_s: float  = ALERT_COOLDOWN_S,
    alert_fn: Callable = _default_alert,
) -> None:
    if duration_s < threshold_s:
        return
    now = time.monotonic()
    if now - _last_alert[tool_name] < cooldown_s:
        return   # cooldown active — suppress alert
    _last_alert[tool_name] = now
    await alert_fn(tool_name, duration_s, tool_input)


ToolExecutor = Callable[[str, dict], Awaitable[str]]


async def monitored_tool(
    tool_name: str,
    tool_input: dict,
    executor: ToolExecutor,
) -> str:
    t0 = time.monotonic()
    result = await executor(tool_name, tool_input)
    duration = time.monotonic() - t0
    await rate_limited_slow_alert(tool_name, duration, tool_input)
    return result


async def _stub(name: str, inp: dict) -> str:
    await asyncio.sleep(3.0 if name == "db_query" else 0.1)
    return f"{name} done"


async def demo_alert():
    for i in range(3):
        result = await monitored_tool("db_query", {"sql": f"SELECT {i}"}, _stub)
        result = await monitored_tool("web_search", {"q": f"query {i}"}, _stub)
        await asyncio.sleep(0.1)   # fast repeat — alert should only fire once


asyncio.run(demo_alert())
```

---

## Solution 6 — Per-Tool Slow Query Log with Sampling and Replay

Record the full input/output of every slow tool call to a structured log.
Subsample fast calls at 1 % to keep log volume manageable. The log can be
replayed in development to reproduce and fix slow queries.

```python
import anthropic
import asyncio
import json
import random
import time
import uuid
from dataclasses import dataclass, field


SLOW_THRESHOLD_S  = 1.5
FAST_SAMPLE_RATE  = 0.01   # log 1% of fast calls for baseline metrics


@dataclass
class ToolCallLog:
    id:         str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    tool_name:  str = ""
    input:      dict = field(default_factory=dict)
    output:     str  = ""
    duration_s: float = 0.0
    slow:       bool  = False
    sampled:    bool  = False
    ts:         float = field(default_factory=time.time)

    def emit(self) -> None:
        record = {
            "id":         self.id,
            "tool":       self.tool_name,
            "duration_s": round(self.duration_s, 3),
            "slow":       self.slow,
            "sampled":    self.sampled,
            "input":      self.input,
            "output":     self.output[:256],
            "ts":         self.ts,
        }
        level = "SLOW" if self.slow else ("SAMPLE" if self.sampled else "FAST")
        print(f"[{level}] {json.dumps(record)}")


async def logged_tool_call(
    tool_name: str,
    tool_input: dict,
    executor,
    slow_threshold_s: float = SLOW_THRESHOLD_S,
    fast_sample_rate: float = FAST_SAMPLE_RATE,
) -> str:
    log = ToolCallLog(tool_name=tool_name, input=tool_input)
    t0 = time.monotonic()
    result = await executor(tool_name, tool_input)
    log.duration_s = time.monotonic() - t0
    log.output = str(result)
    log.slow    = log.duration_s >= slow_threshold_s
    log.sampled = not log.slow and random.random() < fast_sample_rate

    if log.slow or log.sampled:
        log.emit()

    return result


async def _stub_executor(name: str, inp: dict) -> str:
    import random
    delays = {"db_full_scan": 3.0, "db_indexed": 0.05, "api_call": 0.4}
    base = delays.get(name, 0.1)
    jitter = random.uniform(0.8, 1.5)
    await asyncio.sleep(base * jitter)
    return f"{name}({inp}) => ok"


async def demo_logged():
    calls = [
        ("db_full_scan",  {"table": "events", "where": "ts > 0"}),
        ("db_indexed",    {"table": "users",  "where": "id=42"}),
        ("api_call",      {"url": "https://api.example.com/data"}),
        ("db_indexed",    {"table": "orders", "where": "user_id=1"}),
        ("db_full_scan",  {"table": "logs",   "where": "level='error'"}),
    ]
    for name, inp in calls:
        await logged_tool_call(name, inp, _stub_executor)


asyncio.run(demo_logged())
```

---

## Comparison

| Approach | Detection method | Mitigation | Alerting | Data for optimization | Complexity |
|---|---|---|---|---|---|
| Simple threshold logger | Fixed wall-clock threshold | None (log only) | No | Timestamps only | Very low |
| Rolling histogram percentiles | p95/p99 from recent samples | None | No | Full distribution | Low |
| Adaptive fallback | Dynamic p95 threshold + timeout | Yes — automatic fallback | No | Implicit in fallback rate | Medium |
| Flame graph profiler | Full call tree timing | None (visibility) | No | Nested breakdown | Medium |
| Rate-limited alerting | Fixed threshold + cooldown | None (alert only) | Yes | Timestamps only | Low |
| Sampled slow query log | Threshold + sampling | None (log only) | No | Full input/output for replay | Medium |

**Rule of thumb:**
- Start with Solution 1 (threshold logger) — zero overhead, immediate value
- Add Solution 2 (histogram) once you have >100 calls/day — reveals p99 surprises
- Add Solution 3 (adaptive fallback) for tools that have a cheap alternative (cache, stub)
- Use Solution 4 (flame graph) when debugging a specific latency regression
- Add Solution 5 (alerting) to PagerDuty / Slack for production on-call coverage
- Solution 6 (replay log) is invaluable for reproducing intermittent slowdowns in dev
