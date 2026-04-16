---
layout: solution
title: "Agent Doesn't Implement Progressive Timeout Escalation"
category: tool-failure
description: "Start with an aggressive timeout and escalate on each retry — preventing indefinite hangs on the first attempt while still allowing slower operations to complete on retry."
tags: [tool-failure, timeout, retry, escalation, resilience, python]
---

# Agent Doesn't Implement Progressive Timeout Escalation

A fixed timeout is always wrong: too short causes spurious failures on legitimate slow operations; too long lets stuck calls block the agent for minutes. Progressive escalation starts tight, then extends the budget on each retry — fast recovery for transient hangs, patient tolerance for genuinely slow work.

## Option 1: Simple Escalating Timeout Per Retry

```python
import anthropic
import asyncio
import time

client = anthropic.AsyncAnthropic()

TIMEOUT_SCHEDULE = [2.0, 5.0, 15.0, 30.0]  # seconds per attempt

async def fake_tool(query: str, delay: float = 0.5) -> str:
    """Simulated tool with variable latency."""
    await asyncio.sleep(delay)
    return f"Result for: {query}"

async def call_with_escalating_timeout(
    tool_fn,
    *args,
    timeout_schedule: list[float] = TIMEOUT_SCHEDULE,
    **kwargs,
) -> str:
    last_error = None
    for attempt, timeout in enumerate(timeout_schedule, start=1):
        print(f"Attempt {attempt}/{len(timeout_schedule)}, timeout={timeout}s")
        try:
            result = await asyncio.wait_for(tool_fn(*args, **kwargs), timeout=timeout)
            print(f"  Success on attempt {attempt}")
            return result
        except asyncio.TimeoutError:
            last_error = f"Timed out after {timeout}s"
            print(f"  Timeout on attempt {attempt}: {last_error}")
        except Exception as e:
            raise  # Non-timeout errors propagate immediately
    raise TimeoutError(f"All {len(timeout_schedule)} attempts timed out. Last: {last_error}")

async def main():
    # Fast call — succeeds on first attempt (0.5s < 2s timeout)
    result = await call_with_escalating_timeout(fake_tool, "fast query", delay=0.5)
    print(f"Fast result: {result}\n")

    # Slow call — fails first 2 attempts, succeeds on 3rd (10s < 15s timeout)
    async def slow_tool(q):
        await asyncio.sleep(10)
        return f"Slow result for {q}"

    result = await call_with_escalating_timeout(slow_tool, "slow query")
    print(f"Slow result: {result}\n")

asyncio.run(main())

# Expected Token Savings: Avoids wasted model calls waiting for stuck tools; fast path unblocked quickly
# Environment: asyncio; adjust TIMEOUT_SCHEDULE to your tool's expected latency distribution
```

## Option 2: Timeout Escalation with Jitter and Back-off

```python
import anthropic
import asyncio
import random
import time

client = anthropic.AsyncAnthropic()

def escalating_timeouts(
    base: float = 3.0,
    multiplier: float = 2.5,
    max_timeout: float = 60.0,
    jitter: float = 0.2,
    attempts: int = 4,
) -> list[float]:
    timeouts = []
    t = base
    for _ in range(attempts):
        jittered = t * (1 + random.uniform(-jitter, jitter))
        timeouts.append(min(jittered, max_timeout))
        t *= multiplier
    return timeouts

async def tool_with_latency(name: str, latency: float) -> dict:
    await asyncio.sleep(latency)
    return {"tool": name, "status": "ok", "latency": latency}

async def resilient_tool_call(
    tool_fn,
    *args,
    base_timeout: float = 3.0,
    attempts: int = 4,
    **kwargs,
) -> dict:
    timeouts = escalating_timeouts(base=base_timeout, attempts=attempts)
    for i, timeout in enumerate(timeouts, 1):
        start = time.monotonic()
        try:
            result = await asyncio.wait_for(tool_fn(*args, **kwargs), timeout=timeout)
            elapsed = time.monotonic() - start
            print(f"  Attempt {i}: success in {elapsed:.2f}s (budget={timeout:.1f}s)")
            return result
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start
            print(f"  Attempt {i}: timeout after {elapsed:.2f}s (budget={timeout:.1f}s)")
            if i == len(timeouts):
                raise TimeoutError(f"Tool failed after {attempts} attempts")
            # Brief pause before retry
            await asyncio.sleep(0.5)

async def main():
    # Simulate tool that's slow on first call, fast on retry
    call_count = [0]
    async def flaky_tool(name):
        call_count[0] += 1
        latency = 8.0 if call_count[0] == 1 else 1.0  # slow first, fast retry
        return await tool_with_latency(name, latency)

    print("Testing flaky tool (slow first call):")
    result = await resilient_tool_call(flaky_tool, "search_api", base_timeout=3.0, attempts=4)
    print(f"Result: {result}\n")

asyncio.run(main())

# Expected Token Savings: Jitter prevents retry storms; base_timeout tunable per tool SLA
# Environment: asyncio; multiplier=2.5 doubles timeout ~every other attempt
```

## Option 3: Per-Tool Timeout Policy Registry

```python
import anthropic
import asyncio
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

@dataclass
class TimeoutPolicy:
    name: str
    schedule: list[float]  # timeout per attempt in seconds
    critical: bool = False  # if True, raise immediately on all timeouts

POLICIES: dict[str, TimeoutPolicy] = {
    "web_search":   TimeoutPolicy("web_search",   [5.0, 15.0, 30.0]),
    "db_query":     TimeoutPolicy("db_query",     [1.0, 3.0, 10.0]),
    "file_read":    TimeoutPolicy("file_read",    [0.5, 1.0, 2.0]),
    "llm_call":     TimeoutPolicy("llm_call",     [30.0, 60.0, 120.0]),
    "payment_api":  TimeoutPolicy("payment_api",  [10.0, 20.0], critical=True),
}

async def call_tool_with_policy(
    tool_name: str,
    tool_fn,
    *args,
    **kwargs,
) -> tuple[str, dict]:
    policy = POLICIES.get(tool_name, TimeoutPolicy("default", [5.0, 15.0, 30.0]))
    errors = []

    for attempt, timeout in enumerate(policy.schedule, 1):
        try:
            result = await asyncio.wait_for(tool_fn(*args, **kwargs), timeout=timeout)
            print(f"[{tool_name}] attempt={attempt} timeout={timeout}s -> OK")
            return "success", result
        except asyncio.TimeoutError as e:
            errors.append(f"attempt {attempt}: timeout after {timeout}s")
            print(f"[{tool_name}] attempt={attempt} timeout={timeout}s -> TIMEOUT")
            if policy.critical:
                raise TimeoutError(f"Critical tool '{tool_name}' timed out: {errors}")

    return "failed", {"errors": errors, "tool": tool_name}

async def fake_tool(delay: float) -> dict:
    await asyncio.sleep(delay)
    return {"data": "result"}

async def main():
    # DB query — fast policy
    status, result = await call_tool_with_policy(
        "db_query", fake_tool, delay=0.8
    )
    print(f"db_query: {status} {result}\n")

    # Web search — medium policy, slow first attempt
    calls = [0]
    async def slow_first(delay_map):
        calls[0] += 1
        await asyncio.sleep(delay_map.get(calls[0], 1.0))
        return {"hits": []}

    status, result = await call_tool_with_policy(
        "web_search", slow_first, {1: 8, 2: 3, 3: 1}
    )
    print(f"web_search: {status} {result}\n")

asyncio.run(main())

# Expected Token Savings: Per-tool policies prevent over-waiting on fast tools; tight budgets for DB/file
# Environment: extend POLICIES dict for each tool in your agent; critical=True for payment/critical ops
```

## Option 4: Deadline-Aware Timeout Escalation

```python
import anthropic
import asyncio
import time

client = anthropic.AsyncAnthropic()

async def tool_call(query: str, latency: float = 1.0) -> str:
    await asyncio.sleep(latency)
    return f"Answer: {query}"

async def call_within_deadline(
    tool_fn,
    *args,
    overall_deadline: float,       # absolute time (monotonic)
    min_attempt_timeout: float = 1.0,
    max_attempts: int = 4,
    **kwargs,
) -> str:
    """Distribute remaining deadline budget across retries with escalation."""
    for attempt in range(1, max_attempts + 1):
        remaining = overall_deadline - time.monotonic()
        if remaining <= min_attempt_timeout:
            raise TimeoutError(f"Insufficient deadline remaining ({remaining:.1f}s) for attempt {attempt}")

        # Give progressively larger share of remaining budget
        fraction = min(0.3 * attempt, 1.0)   # 30%, 60%, 90%, 100%
        timeout = max(remaining * fraction, min_attempt_timeout)
        timeout = min(timeout, remaining - 0.1)  # always leave 100ms safety margin

        print(f"Attempt {attempt}: timeout={timeout:.1f}s remaining={remaining:.1f}s")
        try:
            return await asyncio.wait_for(tool_fn(*args, **kwargs), timeout=timeout)
        except asyncio.TimeoutError:
            print(f"  Timed out, retrying...")

    raise TimeoutError("All attempts exhausted within deadline")

async def agent_respond(user_query: str, max_latency_s: float = 20.0) -> str:
    deadline = time.monotonic() + max_latency_s

    # First tool call — allow some time
    try:
        search_result = await call_within_deadline(
            tool_call, user_query, latency=3.0,
            overall_deadline=deadline,
        )
    except TimeoutError:
        search_result = "Search timed out — using cached knowledge."

    # Model call with remaining deadline
    remaining = deadline - time.monotonic()
    resp = await asyncio.wait_for(
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user",
                "content": f"Context: {search_result}\nAnswer: {user_query}"}],
        ),
        timeout=max(remaining - 0.5, 5.0),
    )
    return resp.content[0].text

async def main():
    result = await agent_respond("What is the speed of light?", max_latency_s=30.0)
    print(f"Result: {result}")

asyncio.run(main())

# Expected Token Savings: Deadline propagation prevents tool from stealing time from model call
# Environment: asyncio; pass overall_deadline from request handler for end-to-end SLA enforcement
```

## Option 5: SQLite-Tracked Timeout Telemetry for Adaptive Tuning

```python
import anthropic
import asyncio
import sqlite3
import time
import statistics

client = anthropic.AsyncAnthropic()
DB = "timeout_telemetry.db"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS timeout_log (
            tool TEXT, attempt INTEGER,
            timeout_used REAL, elapsed REAL,
            outcome TEXT, ts REAL
        )
    """)
    con.commit(); con.close()

def log_outcome(tool: str, attempt: int, timeout: float, elapsed: float, outcome: str):
    con = sqlite3.connect(DB)
    con.execute("INSERT INTO timeout_log VALUES (?,?,?,?,?,?)",
                (tool, attempt, timeout, elapsed, outcome, time.time()))
    con.commit(); con.close()

def recommended_timeout(tool: str, percentile: float = 0.95) -> float:
    """Recommend timeout based on historical p95 latency."""
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT elapsed FROM timeout_log WHERE tool=? AND outcome='success' ORDER BY elapsed",
        (tool,)
    ).fetchall()
    con.close()
    if len(rows) < 5:
        return 10.0  # not enough data — use default
    latencies = sorted(r[0] for r in rows)
    idx = int(len(latencies) * percentile)
    p95 = latencies[min(idx, len(latencies)-1)]
    return round(p95 * 1.5, 1)  # p95 + 50% safety margin

async def timed_tool_call(
    tool_name: str,
    tool_fn,
    *args,
    base_schedule: list[float] = (3.0, 8.0, 20.0),
    **kwargs,
) -> str:
    # Adapt first timeout from telemetry if available
    recommended = recommended_timeout(tool_name)
    schedule = [recommended] + list(base_schedule[1:])
    print(f"[{tool_name}] timeout schedule: {schedule}")

    for attempt, timeout in enumerate(schedule, 1):
        start = time.monotonic()
        try:
            result = await asyncio.wait_for(tool_fn(*args, **kwargs), timeout=timeout)
            elapsed = time.monotonic() - start
            log_outcome(tool_name, attempt, timeout, elapsed, "success")
            print(f"  Attempt {attempt}: success in {elapsed:.2f}s")
            return result
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start
            log_outcome(tool_name, attempt, timeout, elapsed, "timeout")
            print(f"  Attempt {attempt}: timeout after {elapsed:.2f}s")

    raise TimeoutError(f"{tool_name} failed after {len(schedule)} attempts")

async def fake_tool(latency: float) -> str:
    await asyncio.sleep(latency)
    return f"done in {latency}s"

async def main():
    init_db()
    # Simulate 10 historical calls at ~2s
    for _ in range(10):
        await timed_tool_call("search", fake_tool, 2.0)

    print(f"\nRecommended timeout for 'search': {recommended_timeout('search')}s")

    # New call — timeout adapted from telemetry
    result = await timed_tool_call("search", fake_tool, 2.5)
    print(f"Result: {result}")

asyncio.run(main())

# Expected Token Savings: Adaptive timeouts minimize wait time based on real data; no manual tuning
# Environment: SQLite persists telemetry; run recommended_timeout() at startup for warm start
```

## Option 6: Hierarchical Timeout with Context Propagation

```python
import anthropic
import asyncio
import contextvars
import time
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

# Context variable propagates deadline through call stack
request_deadline: contextvars.ContextVar[float] = contextvars.ContextVar(
    "request_deadline", default=float("inf")
)

@dataclass
class TimeoutBudget:
    overall_s: float
    tool_fraction: float = 0.4   # 40% for tool calls
    model_fraction: float = 0.5  # 50% for model call
    buffer_fraction: float = 0.1 # 10% overhead buffer

    @property
    def tool_budget(self) -> float:
        return self.overall_s * self.tool_fraction

    @property
    def model_budget(self) -> float:
        return self.overall_s * self.model_fraction

async def tool_with_deadline(name: str, fn, *args, fallback=None, **kwargs):
    """Call tool; respect request_deadline context variable."""
    deadline = request_deadline.get()
    remaining = deadline - time.monotonic()
    if remaining <= 0.5:
        print(f"[{name}] No time left, using fallback")
        return fallback

    # Use at most half remaining time for this single tool
    timeout = min(remaining * 0.5, 10.0)
    print(f"[{name}] timeout={timeout:.1f}s remaining={remaining:.1f}s")
    try:
        return await asyncio.wait_for(fn(*args, **kwargs), timeout=timeout)
    except asyncio.TimeoutError:
        print(f"[{name}] timed out, using fallback")
        return fallback

async def fake_search(query: str, latency: float = 1.0) -> list[str]:
    await asyncio.sleep(latency)
    return [f"Result for {query}"]

async def handle_request(user_query: str, sla_s: float = 20.0) -> str:
    budget = TimeoutBudget(overall_s=sla_s)
    deadline = time.monotonic() + sla_s
    request_deadline.set(deadline)

    # Tool calls with shared deadline context
    results = await asyncio.gather(
        tool_with_deadline("search_a", fake_search, user_query, latency=2.0, fallback=[]),
        tool_with_deadline("search_b", fake_search, user_query, latency=3.0, fallback=[]),
    )
    context = " ".join(r for batch in results for r in batch)

    # Model call with remaining budget
    remaining = deadline - time.monotonic()
    model_timeout = min(remaining - 0.5, budget.model_budget)
    print(f"Model call: timeout={model_timeout:.1f}s remaining={remaining:.1f}s")

    resp = await asyncio.wait_for(
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user",
                "content": f"Context: {context or 'none'}. Answer: {user_query}"}],
        ),
        timeout=max(model_timeout, 5.0),
    )
    return resp.content[0].text

async def main():
    result = await handle_request("What is asyncio?", sla_s=30.0)
    print(f"\nFinal: {result}")

asyncio.run(main())

# Expected Token Savings: Budget fractions ensure model always gets its share; tools can't starve it
# Environment: contextvars propagate deadline automatically through nested async calls
```

## Comparison

| Option | Escalation Strategy | Deadline Awareness | Best For |
|--------|--------------------|--------------------|----------|
| 1 — Fixed Schedule | Predefined timeout list | None | Simple tools with known latency range |
| 2 — Exponential + Jitter | Multiplier-based growth | None | Unpredictable tools; prevents retry storms |
| 3 — Per-Tool Policy | Registry-driven schedule | None | Multi-tool agents with different SLAs |
| 4 — Deadline-Aware | Fraction of remaining budget | Full end-to-end | SLA-bounded request handlers |
| 5 — Telemetry-Adaptive | p95 latency from SQLite | None | Long-running agents; auto-tunes over time |
| 6 — Hierarchical Context | ContextVar + budget fractions | Full hierarchy | Complex pipelines; model always gets budget |
