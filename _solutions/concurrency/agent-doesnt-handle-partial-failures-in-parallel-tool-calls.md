---
layout: solution
title: "Agent Doesn't Handle Partial Failures in Parallel Tool Calls"
category: concurrency
description: "When running multiple tool calls in parallel, one failure kills the entire batch. Successful results are discarded, the agent retries everything, and costs double."
tags: [concurrency, parallel, tool-use, error-handling, asyncio, partial-failure, gather]
---

## Symptom

Your agent runs 5 tool calls in parallel. Tool 3 raises a network error. `asyncio.gather()` propagates the exception immediately, cancelling the other 4 still-running tasks. All 5 results are lost. The agent retries all 5 calls on the next turn — paying twice for the 4 that had already succeeded. Alternatively, `return_exceptions=True` is used but the agent passes raw `Exception` objects as tool results to the LLM, causing a crash or confusing response.

## Root Cause

Parallel tool execution with `asyncio.gather()` defaults to fail-fast behavior. Even with `return_exceptions=True`, raw exceptions must be explicitly handled before they become tool results. There is no partial-success path: the agent either gets all results or treats the batch as a total failure.

```python
# Anti-pattern: fail-fast gather loses all results on any error
results = await asyncio.gather(
    call_tool_a(), call_tool_b(), call_tool_c()
)  # raises if any fails; all results lost
```

## Fix

---

### Option 1: gather with return_exceptions + Typed ToolResult

Use `return_exceptions=True` and wrap each outcome in a typed result object before returning to the LLM.

```python
import asyncio
import json
import anthropic
from dataclasses import dataclass
from typing import Any

client = anthropic.AsyncAnthropic()


@dataclass
class ToolResult:
    tool_use_id: str
    success: bool
    data: Any = None
    error: str = ""

    def to_api_result(self) -> dict:
        if self.success:
            return {
                "type": "tool_result",
                "tool_use_id": self.tool_use_id,
                "content": json.dumps(self.data),
            }
        else:
            return {
                "type": "tool_result",
                "tool_use_id": self.tool_use_id,
                "content": json.dumps({"error": self.error}),
                "is_error": True,
            }


async def execute_one_tool(tool_use_id: str, name: str, args: dict) -> ToolResult:
    """Execute a single tool call and return a typed ToolResult."""
    try:
        result = await dispatch_tool(name, args)
        return ToolResult(tool_use_id=tool_use_id, success=True, data=result)
    except Exception as e:
        return ToolResult(tool_use_id=tool_use_id, success=False, error=str(e))


async def dispatch_tool(name: str, args: dict) -> dict:
    """Simulate tool dispatch with occasional failures."""
    await asyncio.sleep(0.1)  # simulate network
    if name == "failing_tool":
        raise ConnectionError("Service temporarily unavailable")
    return {"tool": name, "result": f"success with {args}"}


async def execute_tools_parallel(tool_use_blocks: list) -> list[dict]:
    """
    Execute all tool calls in parallel.
    Partial failures become error results, not exceptions.
    Returns list of API-ready tool_result dicts.
    """
    tasks = [
        execute_one_tool(block.id, block.name, block.input)
        for block in tool_use_blocks
    ]

    # return_exceptions=True ensures all tasks complete
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    results = []
    for outcome in outcomes:
        if isinstance(outcome, ToolResult):
            if not outcome.success:
                print(f"  [Tool error] {outcome.tool_use_id}: {outcome.error}")
            results.append(outcome.to_api_result())
        elif isinstance(outcome, Exception):
            # gather itself failed (shouldn't happen with return_exceptions=True)
            print(f"  [Unexpected gather error]: {outcome}")
            # We don't have tool_use_id here — this is a programming error
            raise

    successful = sum(1 for r in results if not r.get("is_error"))
    failed     = len(results) - successful
    print(f"  [Parallel tools] {successful} succeeded, {failed} failed")

    return results


TOOLS = [
    {"name": "get_weather",   "description": "Get weather.",   "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}},
    {"name": "failing_tool",  "description": "Always fails.",  "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_news",      "description": "Get headlines.", "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}}},
]


async def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        tool_blocks = [b for b in response.content if b.type == "tool_use"]
        if not tool_blocks:
            return next(b.text for b in response.content if b.type == "text")

        tool_results = await execute_tools_parallel(tool_blocks)

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})


result = asyncio.run(run_agent("Get weather for London, run the failing tool, and get tech news."))
print(result)
```

**Expected Token Savings**: Partial success prevents full retry. 4/5 tools succeed → only 1 tool retried on next turn instead of all 5.
**Environment**: Async Python. `return_exceptions=True` is key.

---

### Option 2: Retry Failed Tools Individually Without Re-Running Successes

Track which tool calls succeeded. On the next turn, only retry failed ones.

```python
import asyncio
import json
import time
import anthropic
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()


@dataclass
class ToolOutcome:
    tool_use_id: str
    name: str
    args: dict
    success: bool
    data: dict | None = None
    error: str = ""
    attempts: int = 1


class SelectiveRetryRunner:
    """
    Executes tool calls in parallel.
    On failure, retries only the failed tools (up to max_retries times).
    """
    MAX_RETRIES = 2
    RETRY_DELAY = 0.5

    async def run_all(self, tool_blocks: list) -> list[dict]:
        outcomes: dict[str, ToolOutcome] = {
            block.id: ToolOutcome(block.id, block.name, block.input, False)
            for block in tool_blocks
        }

        pending_ids = list(outcomes.keys())
        blocks_by_id = {block.id: block for block in tool_blocks}

        for attempt in range(1, self.MAX_RETRIES + 2):
            if not pending_ids:
                break

            tasks = {
                tid: self._execute(outcomes[tid])
                for tid in pending_ids
            }
            results = await asyncio.gather(*tasks.values(), return_exceptions=True)

            next_pending = []
            for tid, result in zip(tasks.keys(), results):
                if isinstance(result, Exception):
                    outcomes[tid].error = str(result)
                    outcomes[tid].success = False
                elif isinstance(result, dict):
                    outcomes[tid].data = result
                    outcomes[tid].success = True
                    print(f"  [OK] {outcomes[tid].name} (attempt {attempt})")
                else:
                    outcomes[tid].error = f"Unexpected result type: {type(result)}"
                    outcomes[tid].success = False

                if not outcomes[tid].success and attempt <= self.MAX_RETRIES:
                    outcomes[tid].attempts += 1
                    next_pending.append(tid)
                    print(f"  [Retry {attempt}/{self.MAX_RETRIES}] {outcomes[tid].name}: {outcomes[tid].error}")
                    await asyncio.sleep(self.RETRY_DELAY * attempt)

            pending_ids = next_pending

        # Build API results
        return [
            {
                "type": "tool_result",
                "tool_use_id": tid,
                "content": json.dumps(o.data) if o.success else json.dumps({"error": o.error}),
                **({"is_error": True} if not o.success else {}),
            }
            for tid, o in outcomes.items()
        ]

    async def _execute(self, outcome: ToolOutcome) -> dict:
        await asyncio.sleep(0.05)
        # Simulate flaky tool: fails on first attempt, succeeds after
        if outcome.name == "flaky_tool" and outcome.attempts == 1:
            raise ConnectionError("Transient network error")
        return {"tool": outcome.name, "status": "ok", "data": f"result for {outcome.args}"}


TOOLS = [
    {"name": "stable_tool", "description": "Always works.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "flaky_tool",  "description": "Flaky network.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "another_stable", "description": "Also stable.", "input_schema": {"type": "object", "properties": {}}},
]

runner = SelectiveRetryRunner()


async def run_agent(message: str) -> str:
    messages = [{"role": "user", "content": message}]
    while True:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        tool_blocks = [b for b in response.content if b.type == "tool_use"]
        tool_results = await runner.run_all(tool_blocks)

        messages += [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": tool_results},
        ]


print(asyncio.run(run_agent("Run all three tools simultaneously.")))
```

**Expected Token Savings**: Flaky tools retry individually. No redundant re-execution of successful tools.
**Environment**: Async Python. Exponential backoff on retry delays.

---

### Option 3: Timeout Per Tool with Partial Success Reporting

Each tool has its own timeout. Slow tools time out without blocking the others.

```python
import asyncio
import json
import anthropic

client = anthropic.AsyncAnthropic()

# Per-tool timeout configuration (seconds)
TOOL_TIMEOUTS = {
    "fast_lookup":  2.0,
    "slow_search":  5.0,
    "database_query": 3.0,
    "external_api":   4.0,
}
DEFAULT_TIMEOUT = 3.0


async def execute_with_timeout(tool_id: str, name: str, args: dict) -> dict:
    """Execute tool with per-tool timeout. Returns result or error dict."""
    timeout = TOOL_TIMEOUTS.get(name, DEFAULT_TIMEOUT)

    async def _run():
        await asyncio.sleep(0.1)  # simulate work
        if name == "slow_search":
            await asyncio.sleep(10.0)  # will time out
        return {"tool": name, "data": f"result for {args}"}

    try:
        result = await asyncio.wait_for(_run(), timeout=timeout)
        return {"tool_use_id": tool_id, "success": True, "data": result}
    except asyncio.TimeoutError:
        return {
            "tool_use_id": tool_id,
            "success": False,
            "error": f"Tool '{name}' timed out after {timeout}s",
        }
    except Exception as e:
        return {
            "tool_use_id": tool_id,
            "success": False,
            "error": f"Tool '{name}' failed: {e}",
        }


async def run_parallel_tools(tool_blocks: list) -> tuple[list[dict], dict]:
    """
    Run all tools in parallel with individual timeouts.
    Returns (api_results, summary).
    """
    tasks = [
        execute_with_timeout(block.id, block.name, block.input)
        for block in tool_blocks
    ]
    outcomes = await asyncio.gather(*tasks)

    api_results = []
    summary = {"succeeded": [], "failed": [], "timed_out": []}

    for outcome in outcomes:
        tid = outcome["tool_use_id"]
        if outcome["success"]:
            api_results.append({
                "type": "tool_result",
                "tool_use_id": tid,
                "content": json.dumps(outcome["data"]),
            })
            summary["succeeded"].append(tid)
        else:
            api_results.append({
                "type": "tool_result",
                "tool_use_id": tid,
                "content": json.dumps({"error": outcome["error"]}),
                "is_error": True,
            })
            if "timed out" in outcome["error"]:
                summary["timed_out"].append(tid)
            else:
                summary["failed"].append(tid)

    print(f"  [Tools] ✓ {len(summary['succeeded'])} | "
          f"✗ {len(summary['failed'])} | "
          f"⏱ {len(summary['timed_out'])} timed out")

    return api_results, summary


TOOLS = [
    {"name": "fast_lookup",   "description": "Fast DB lookup.", "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "slow_search",   "description": "Full text search (can be slow).", "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}}},
    {"name": "database_query","description": "DB query.", "input_schema": {"type": "object", "properties": {}}},
]


async def run_agent(message: str) -> str:
    messages = [{"role": "user", "content": message}]
    while True:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        tool_blocks = [b for b in response.content if b.type == "tool_use"]
        tool_results, _ = await run_parallel_tools(tool_blocks)

        messages += [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": tool_results},
        ]


print(asyncio.run(run_agent("Look up user 42, run a search for 'python async', and query the database.")))
```

**Expected Token Savings**: Per-tool timeouts prevent one slow tool from blocking all others. Wall-clock time for N parallel tools = max(individual) instead of sum.
**Environment**: `asyncio.wait_for` per tool. Timeouts configurable per tool name.

---

### Option 4: Circuit Breaker for Repeatedly Failing Tools

Track failure rates per tool. Open the circuit after N failures to skip the tool without calling it.

```python
import asyncio
import json
import time
from dataclasses import dataclass, field
import anthropic

client = anthropic.AsyncAnthropic()


@dataclass
class CircuitBreaker:
    failure_threshold: int = 3
    recovery_timeout: float = 30.0   # seconds before trying again

    _failures: int = field(default=0, init=False)
    _opened_at: float = field(default=0.0, init=False)
    _state: str = field(default="closed", init=False)  # closed | open | half-open

    def is_open(self) -> bool:
        if self._state == "open":
            if time.monotonic() - self._opened_at >= self.recovery_timeout:
                self._state = "half-open"
                return False
            return True
        return False

    def record_success(self):
        self._failures = 0
        self._state = "closed"

    def record_failure(self):
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._state = "open"
            self._opened_at = time.monotonic()
            print(f"  [Circuit OPEN] Too many failures ({self._failures})")

    @property
    def state(self) -> str:
        return self._state


# One circuit breaker per tool
breakers: dict[str, CircuitBreaker] = {}


def get_breaker(tool_name: str) -> CircuitBreaker:
    if tool_name not in breakers:
        breakers[tool_name] = CircuitBreaker()
    return breakers[tool_name]


async def circuit_protected_execute(tool_id: str, name: str, args: dict) -> dict:
    breaker = get_breaker(name)

    if breaker.is_open():
        return {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": json.dumps({"error": f"Tool '{name}' circuit open — skipped (too many recent failures)"}),
            "is_error": True,
        }

    try:
        # Simulate tool execution
        await asyncio.sleep(0.05)
        if name == "unreliable_service":
            raise Exception("Service down")

        result = {"tool": name, "data": f"ok: {args}"}
        breaker.record_success()
        return {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": json.dumps(result),
        }
    except Exception as e:
        breaker.record_failure()
        return {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": json.dumps({"error": str(e), "circuit_state": breaker.state}),
            "is_error": True,
        }


TOOLS = [
    {"name": "reliable_tool",      "description": "Always works.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "unreliable_service", "description": "Sometimes fails.", "input_schema": {"type": "object", "properties": {}}},
]


async def run_agent(message: str) -> str:
    messages = [{"role": "user", "content": message}]
    while True:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        tool_blocks = [b for b in response.content if b.type == "tool_use"]
        tasks = [circuit_protected_execute(b.id, b.name, b.input) for b in tool_blocks]
        tool_results = await asyncio.gather(*tasks)

        messages += [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": list(tool_results)},
        ]


# Run multiple times to trigger circuit breaker
for i in range(5):
    print(f"\n--- Turn {i+1} ---")
    print(asyncio.run(run_agent("Run reliable_tool and unreliable_service.")))
```

**Expected Token Savings**: After 3 failures, circuit opens and skips the tool — no API call, no wait, no wasted tokens on known-failing tools.
**Environment**: In-memory circuit breaker. For multi-process, store state in Redis with TTL.

---

### Option 5: Dependency-Aware Parallel Execution

Some tools depend on outputs from other tools. Execute independent tools in parallel; chain dependents sequentially.

```python
import asyncio
import json
import anthropic
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()


@dataclass
class ToolNode:
    id: str
    name: str
    args: dict
    depends_on: list[str] = None  # tool_use_ids this tool depends on

    def __post_init__(self):
        self.depends_on = self.depends_on or []


async def execute_tool(node: ToolNode, resolved: dict[str, dict]) -> dict:
    """Execute a tool, injecting results from dependencies."""
    args = dict(node.args)
    # Inject dependency results into args
    for dep_id in node.depends_on:
        dep_result = resolved.get(dep_id, {})
        args[f"dep_{dep_id[:8]}"] = dep_result

    await asyncio.sleep(0.05)
    return {"tool": node.name, "args_received": args, "result": f"success"}


async def execute_dag(nodes: list[ToolNode]) -> dict[str, dict]:
    """
    Execute tools as a DAG. Independent tools run in parallel;
    dependent tools wait for their dependencies.
    """
    results: dict[str, dict] = {}
    pending = {node.id: node for node in nodes}

    while pending:
        # Find nodes whose dependencies are all resolved
        ready = [
            node for node in pending.values()
            if all(dep in results for dep in node.depends_on)
        ]

        if not ready:
            # Circular dependency or unresolvable — execute remaining sequentially
            print("  [Warning] Circular or unresolvable dependencies detected")
            for node in list(pending.values()):
                results[node.id] = await execute_tool(node, results)
                del pending[node.id]
            break

        print(f"  [Parallel batch] {[n.name for n in ready]}")
        batch_results = await asyncio.gather(
            *[execute_tool(node, results) for node in ready],
            return_exceptions=True,
        )

        for node, result in zip(ready, batch_results):
            if isinstance(result, Exception):
                results[node.id] = {"error": str(result)}
                print(f"  [Failed] {node.name}: {result}")
            else:
                results[node.id] = result
                print(f"  [Done]   {node.name}")
            del pending[node.id]

    return results


# Example: user_lookup → enrichment (depends on user_lookup), weather (independent)
tool_nodes = [
    ToolNode("tid_1", "user_lookup", {"user_id": "42"}),
    ToolNode("tid_2", "user_enrichment", {"source": "crm"}, depends_on=["tid_1"]),
    ToolNode("tid_3", "get_weather", {"city": "Tokyo"}),  # independent
]


async def demo():
    start = asyncio.get_event_loop().time()
    results = await execute_dag(tool_nodes)
    elapsed = asyncio.get_event_loop().time() - start
    print(f"\nAll results in {elapsed*1000:.0f}ms:")
    for tid, result in results.items():
        print(f"  {tid}: {result}")


asyncio.run(demo())
```

**Expected Token Savings**: Correct dependency ordering prevents premature tool calls. Independent tools complete in parallel instead of sequentially.
**Environment**: Pure async Python. Dependency graph built statically or from LLM-specified tool annotations.

---

### Option 6: Partial Success Aggregator with Graceful Degradation

Collect all results, mark partial failures clearly, and instruct the LLM to work with what succeeded.

```python
import asyncio
import json
import anthropic

client = anthropic.AsyncAnthropic()

TOOLS = [
    {"name": "get_stock_price", "description": "Get current stock price.", "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "get_company_news", "description": "Get recent company news.", "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "get_analyst_rating", "description": "Get analyst rating.", "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
]

# Simulate: stock price works, news is flaky, analyst API is down
async def _execute(name: str, args: dict) -> dict:
    await asyncio.sleep(0.1)
    if name == "get_company_news":
        raise ConnectionError("News API rate limited")
    if name == "get_analyst_rating":
        raise Exception("Analyst service unavailable")
    return {"symbol": args["symbol"], "price": 142.50, "change": "+1.2%"}


async def gather_with_graceful_degradation(tool_blocks: list) -> list[dict]:
    """
    Execute all tools. Return partial results clearly labeled.
    LLM is informed which tools succeeded and which failed.
    """
    async def run_one(block) -> tuple[str, str, bool, str]:
        try:
            result = await _execute(block.name, block.input)
            return block.id, json.dumps(result), True, ""
        except Exception as e:
            return block.id, "", False, str(e)

    outcomes = await asyncio.gather(*[run_one(b) for b in tool_blocks])

    api_results = []
    succeeded, failed = [], []

    for tid, data, ok, err in outcomes:
        if ok:
            api_results.append({
                "type": "tool_result",
                "tool_use_id": tid,
                "content": data,
            })
            succeeded.append(tid)
        else:
            api_results.append({
                "type": "tool_result",
                "tool_use_id": tid,
                "content": json.dumps({
                    "error": err,
                    "note": "This tool failed. Please work with the results from tools that succeeded.",
                }),
                "is_error": True,
            })
            failed.append(tid)

    if failed:
        print(f"  [Partial success] {len(succeeded)} succeeded, {len(failed)} failed")
        print(f"  [LLM will receive error context for failed tools]")

    return api_results


async def run_agent(message: str) -> str:
    messages = [{"role": "user", "content": message}]

    # System prompt instructs graceful degradation
    system = (
        "You are a financial assistant. When some data tools fail, "
        "explicitly acknowledge what data is unavailable and provide "
        "the best analysis possible using only successful results."
    )

    while True:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=system,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        tool_blocks = [b for b in response.content if b.type == "tool_use"]
        tool_results = await gather_with_graceful_degradation(tool_blocks)

        messages += [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": tool_results},
        ]


print(asyncio.run(run_agent("Analyze AAPL: get the stock price, news, and analyst rating.")))
```

**Expected Token Savings**: LLM produces a useful partial answer instead of failing completely. Users get value even when 1–2 of 3 tools are down.
**Environment**: Async Python. System prompt instructs graceful degradation behavior.

---

| Option | Strategy | Retry Logic | Use Case |
|--------|---------|------------|---------|
| 1 | return_exceptions + typed results | None | Basic parallel tool safety |
| 2 | Selective retry (failed only) | Per-tool, up to N times | Flaky transient failures |
| 3 | Per-tool timeouts | None | Slow/hanging tools |
| 4 | Circuit breaker | Skip after N failures | Repeatedly failing services |
| 5 | DAG dependency ordering | None | Tools with data dependencies |
| 6 | Graceful degradation + LLM context | None | Partial results with user communication |
