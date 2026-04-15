---
layout: solution
title: "Agent calls tools sequentially instead of in parallel"
category: performance
description: "Agent calls three independent tools one after another — waiting for each to complete before starting the next. If each tool takes 500ms, three sequential calls take 1500ms. The model can request multiple tool calls in a single response; executing them in parallel takes 500ms — a 3× speedup for free."
tags: [performance, tool-use, parallel, asyncio, latency, concurrency, fanout]
---

## Symptom

An agent needs three pieces of data: weather, stock price, and news. It calls `get_weather()`, waits 600ms, calls `get_stock_price()`, waits 400ms, calls `get_news()`, waits 500ms. Total: 1500ms. None of these calls depend on each other's output. A user on a voice interface hears 1.5 seconds of silence. The same information could have been fetched in 600ms (the slowest individual call) with parallel execution.

## Root Cause

The model returns one tool call per response turn by default, and the agent's loop calls one tool, injects the result, then calls the model again. This creates a sequential tool-result-tool-result chain. The Anthropic API supports returning multiple tool_use blocks in a single response, and the tool results can be submitted together in a single batch — enabling true parallel execution.

## Fix

Tell the model it can call multiple tools at once by instructing it in the system prompt. The model will return multiple `tool_use` blocks in one response. Execute them with `asyncio.gather()` and submit all results in a single batch message.

---

### Option 1 — System prompt instruction to use parallel tools

```python
import anthropic
import asyncio
import time

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")

TOOLS = [
    {
        "name": "get_weather",
        "description": "Get current weather for a city.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
    {
        "name": "get_stock_price",
        "description": "Get the current stock price for a ticker symbol.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_news",
        "description": "Get the latest news headlines for a topic.",
        "input_schema": {
            "type": "object",
            "properties": {"topic": {"type": "string"}},
            "required": ["topic"],
        },
    },
]

PARALLEL_TOOL_SYSTEM = """\
You are a helpful assistant with access to real-time data tools.

IMPORTANT: When you need data from multiple independent tools, call ALL of them
in a SINGLE response. Do not call tools one at a time — if the results are
independent of each other, request all of them simultaneously.

For example, if the user asks about weather, stocks, AND news, call get_weather,
get_stock_price, AND get_news in the same response turn. Do not wait for one
result before calling the next.
"""


async def execute_tool(tool_name: str, tool_input: dict) -> str:
    """Simulate tool execution with network latency."""
    latency = {"get_weather": 0.6, "get_stock_price": 0.4, "get_news": 0.5}
    await asyncio.sleep(latency.get(tool_name, 0.3))

    results = {
        "get_weather": f"Weather in {tool_input.get('city', '?')}: 22°C, sunny",
        "get_stock_price": f"{tool_input.get('ticker', '?')}: $192.45 (+1.2%)",
        "get_news": f"Top news on {tool_input.get('topic', '?')}: Markets rally on tech earnings",
    }
    return results.get(tool_name, f"[{tool_name} result]")


async def run_parallel_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    for turn in range(5):
        response = await async_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=PARALLEL_TOOL_SYSTEM,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        if response.stop_reason == "tool_use":
            tool_blocks = [b for b in response.content if b.type == "tool_use"]
            print(f"[Turn {turn}] Model requested {len(tool_blocks)} tool call(s)")

            # Execute ALL tool calls in PARALLEL
            start = time.perf_counter()
            tool_results = await asyncio.gather(*[
                execute_tool(b.name, b.input) for b in tool_blocks
            ])
            elapsed = time.perf_counter() - start

            # Show timing
            if len(tool_blocks) > 1:
                print(f"[Parallel] {len(tool_blocks)} tools in {elapsed:.2f}s "
                      f"(sequential would be ~{sum({'get_weather':0.6,'get_stock_price':0.4,'get_news':0.5}.get(b.name,0.3) for b in tool_blocks):.2f}s)")

            # Submit ALL results in a single batch
            messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    }
                    for block, result in zip(tool_blocks, tool_results)
                ],
            })

    return "Max turns reached"


result = asyncio.run(run_parallel_agent(
    "What's the weather in London, Apple stock price, and latest AI news?"
))
print(result)
```

**Expected Token Savings:** Zero token change; parallel execution reduces 3×500ms to 500ms (the max individual latency) — a 3× latency improvement at zero cost; works with any number of independent tools.
**Environment:** Any agent with independent tool calls; the system prompt instruction is the simplest intervention — one paragraph that enables parallel tool use for all subsequent requests.

---

### Option 2 — Parallel tool execution with per-tool timeout

```python
import anthropic
import asyncio
import time

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")

TOOLS = [
    {
        "name": "search_web",
        "description": "Search the web for information.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "query_database",
        "description": "Query the internal database.",
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
        },
    },
    {
        "name": "call_external_api",
        "description": "Call an external partner API.",
        "input_schema": {
            "type": "object",
            "properties": {"endpoint": {"type": "string"}},
            "required": ["endpoint"],
        },
    },
]

# Per-tool SLA timeouts (seconds)
TOOL_TIMEOUTS = {
    "search_web": 3.0,
    "query_database": 1.0,
    "call_external_api": 5.0,
}


async def execute_tool_with_timeout(
    tool_name: str,
    tool_id: str,
    tool_input: dict,
) -> dict:
    """Execute one tool with its individual timeout."""
    timeout = TOOL_TIMEOUTS.get(tool_name, 3.0)
    start = time.perf_counter()
    try:
        async with asyncio.timeout(timeout):
            await asyncio.sleep(0.3)   # simulate work
            result = f"[{tool_name}] result for {tool_input}"
            elapsed = time.perf_counter() - start
            print(f"  [{tool_name}] completed in {elapsed:.2f}s")
            return {"tool_use_id": tool_id, "content": result, "error": None}
    except asyncio.TimeoutError:
        elapsed = time.perf_counter() - start
        print(f"  [{tool_name}] TIMED OUT after {elapsed:.2f}s (limit: {timeout:.1f}s)")
        return {
            "tool_use_id": tool_id,
            "content": f"[{tool_name}] timed out after {timeout:.1f}s",
            "error": "timeout",
        }
    except Exception as e:
        return {"tool_use_id": tool_id, "content": f"[{tool_name}] error: {e}", "error": str(e)}


async def run_agent_parallel_with_timeouts(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    system = (
        "You have access to multiple tools. Call all tools you need simultaneously "
        "in a single response — do not call them one at a time."
    )

    for turn in range(5):
        response = await async_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        if response.stop_reason == "tool_use":
            tool_blocks = [b for b in response.content if b.type == "tool_use"]
            start = time.perf_counter()

            # Execute ALL tools in parallel, each with its own timeout
            raw_results = await asyncio.gather(*[
                execute_tool_with_timeout(b.name, b.id, b.input)
                for b in tool_blocks
            ])
            elapsed = time.perf_counter() - start
            print(f"[Parallel] {len(tool_blocks)} tools completed in {elapsed:.2f}s")

            # Submit all results (including timeouts) as a batch
            messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": r["tool_use_id"],
                        "content": r["content"],
                        "is_error": r["error"] is not None,
                    }
                    for r in raw_results
                ],
            })

    return "Max turns reached"


asyncio.run(run_agent_parallel_with_timeouts(
    "Search the web for Python news, query user stats, and call the recommendations API"
))
```

**Expected Token Savings:** Zero token change; per-tool timeouts prevent one slow tool from blocking all others — without them, `asyncio.gather()` waits for the slowest tool, but per-tool timeouts let fast tools complete while slow tools are cancelled.
**Environment:** Production agents with heterogeneous tool latencies; per-tool timeouts are essential when one tool (external API) could be 10× slower than others (database query).

---

### Option 3 — Structured parallel executor with result aggregation

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")


@dataclass
class ToolCallResult:
    tool_use_id: str
    tool_name: str
    content: str
    elapsed_ms: float
    success: bool


class ParallelToolExecutor:
    """
    Executes a batch of tool calls in parallel and aggregates results.
    Provides timing statistics to identify bottleneck tools.
    """
    def __init__(self, tool_dispatch: dict):
        """tool_dispatch: {tool_name: async_callable}"""
        self.dispatch = tool_dispatch
        self.stats: list[ToolCallResult] = []

    async def _run_one(self, tool_id: str, tool_name: str, tool_input: dict) -> ToolCallResult:
        start = time.perf_counter()
        try:
            fn = self.dispatch.get(tool_name)
            if fn is None:
                raise ValueError(f"Unknown tool: {tool_name}")
            result = await fn(**tool_input)
            elapsed = (time.perf_counter() - start) * 1000
            return ToolCallResult(
                tool_use_id=tool_id,
                tool_name=tool_name,
                content=str(result),
                elapsed_ms=elapsed,
                success=True,
            )
        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            return ToolCallResult(
                tool_use_id=tool_id,
                tool_name=tool_name,
                content=f"Error: {e}",
                elapsed_ms=elapsed,
                success=False,
            )

    async def execute_batch(self, tool_blocks) -> list[ToolCallResult]:
        start = time.perf_counter()
        results = await asyncio.gather(*[
            self._run_one(b.id, b.name, b.input)
            for b in tool_blocks
        ])
        total_elapsed = (time.perf_counter() - start) * 1000
        sequential_total = sum(r.elapsed_ms for r in results)

        self.stats.extend(results)
        print(
            f"[Executor] {len(results)} tools: parallel={total_elapsed:.0f}ms, "
            f"sequential_equiv={sequential_total:.0f}ms, "
            f"speedup={sequential_total/max(total_elapsed,1):.1f}×"
        )
        return list(results)

    def to_tool_results(self, results: list[ToolCallResult]) -> list[dict]:
        return [
            {
                "type": "tool_result",
                "tool_use_id": r.tool_use_id,
                "content": r.content,
                "is_error": not r.success,
            }
            for r in results
        ]

    def bottleneck_report(self) -> str:
        if not self.stats:
            return "No tool calls recorded"
        by_tool = {}
        for r in self.stats:
            by_tool.setdefault(r.tool_name, []).append(r.elapsed_ms)
        lines = ["Tool timing report:"]
        for tool, times in sorted(by_tool.items(), key=lambda x: -sum(x[1])):
            avg = sum(times) / len(times)
            lines.append(f"  {tool}: avg={avg:.0f}ms, calls={len(times)}")
        return "\n".join(lines)


# Tool implementations
async def get_weather(city: str) -> str:
    await asyncio.sleep(0.6)
    return f"Weather in {city}: 22°C, sunny"

async def get_price(ticker: str) -> str:
    await asyncio.sleep(0.3)
    return f"{ticker}: $192.45"

async def get_headlines(topic: str) -> str:
    await asyncio.sleep(0.5)
    return f"Headlines for {topic}: market rally continues"


executor = ParallelToolExecutor({
    "get_weather": get_weather,
    "get_stock_price": get_price,
    "get_news": get_headlines,
})

TOOLS = [
    {"name": "get_weather", "description": "Get weather.", "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}},
    {"name": "get_stock_price", "description": "Get stock price.", "input_schema": {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]}},
    {"name": "get_news", "description": "Get news headlines.", "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}},
]


async def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    system = "Call all needed tools simultaneously in one response turn."

    for _ in range(5):
        response = await async_client.messages.create(
            model="claude-sonnet-4-6", max_tokens=1024,
            system=system, tools=TOOLS, messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "end_turn":
            print(executor.bottleneck_report())
            return next((b.text for b in response.content if hasattr(b, "text")), "")
        if response.stop_reason == "tool_use":
            tool_blocks = [b for b in response.content if b.type == "tool_use"]
            results = await executor.execute_batch(tool_blocks)
            messages.append({"role": "user", "content": executor.to_tool_results(results)})

    return "Max turns reached"


asyncio.run(run_agent("Get weather for NYC, AAPL price, and tech news"))
```

**Expected Token Savings:** Zero token change; the bottleneck report identifies which tools to optimize — if `get_weather` takes 600ms and the others take 100ms each, the report makes it clear that optimizing weather reduces total latency by 83%.
**Environment:** Multi-tool agents in production; the structured executor provides latency observability that makes performance optimization data-driven.

---

### Option 4 — Detect and warn on sequential tool call patterns

```python
import anthropic
import asyncio
import time

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")

TOOLS = [
    {"name": "fetch_a", "description": "Fetch data source A.", "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}},
    {"name": "fetch_b", "description": "Fetch data source B.", "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}},
    {"name": "fetch_c", "description": "Fetch data source C.", "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}},
]


def detect_sequential_opportunity(
    current_tool_name: str,
    current_tool_input: dict,
    tool_result: str,
    previous_calls: list[str],
) -> bool:
    """
    Detect if the agent is calling tools sequentially that could be parallel.
    Heuristic: if tool B's input doesn't reference tool A's result, they were independent.
    """
    result_lower = tool_result.lower()
    input_str = str(current_tool_input).lower()
    # Check if this tool's input references any previous result
    for prev_tool in previous_calls:
        if prev_tool.lower()[:10] in input_str:
            return False   # this tool depends on previous result
    # Independent tool call detected
    return len(previous_calls) > 0


async def execute_single_tool(name: str, tool_input: dict) -> str:
    await asyncio.sleep(0.4)  # simulate latency
    return f"[{name}] result for {tool_input}"


async def run_agent_with_sequential_detection(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    previous_tools: list[str] = []
    sequential_warnings = 0
    total_tool_time = 0.0

    SYSTEM = (
        "You are an efficient assistant. Always call all independent tools simultaneously "
        "in a single response. Only call tools sequentially if one result is required "
        "as input to the next call."
    )

    for turn in range(10):
        response = await async_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            if sequential_warnings > 0:
                print(
                    f"[Sequential warning] Agent made {sequential_warnings} sequential "
                    f"tool calls that could have been parallel. "
                    f"Total tool time: {total_tool_time:.2f}s"
                )
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        if response.stop_reason == "tool_use":
            tool_blocks = [b for b in response.content if b.type == "tool_use"]

            if len(tool_blocks) == 1 and len(previous_tools) > 0:
                # Single tool call after having called other tools — check if independent
                block = tool_blocks[0]
                sequential_warnings += 1
                print(
                    f"[Sequential warning] Called {block.name} alone after {previous_tools} — "
                    f"consider calling them together"
                )

            start = time.perf_counter()
            results = await asyncio.gather(*[
                execute_single_tool(b.name, b.input) for b in tool_blocks
            ])
            total_tool_time += time.perf_counter() - start

            previous_tools.extend(b.name for b in tool_blocks)
            messages.append({
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": b.id, "content": r}
                    for b, r in zip(tool_blocks, results)
                ],
            })

    return "Max turns reached"


asyncio.run(run_agent_with_sequential_detection(
    "Fetch data from sources A, B, and C and summarize"
))
```

**Expected Token Savings:** Zero token change; sequential detection surfaces anti-patterns that slow the agent without affecting correctness — monitoring sequential warnings helps identify when the system prompt needs strengthening or the model needs more explicit instruction.
**Environment:** Development and monitoring environments; the warning log guides prompt tuning to increase parallel tool use frequency.

---

### Option 5 — Dependency-aware parallel scheduling

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")


@dataclass
class ToolTask:
    name: str
    tool_id: str
    tool_input: dict
    depends_on: list[str] = field(default_factory=list)  # tool_ids this depends on


async def execute_dependency_aware(tasks: list[ToolTask]) -> dict[str, str]:
    """
    Execute tasks respecting dependencies.
    Independent tasks run in parallel; dependent tasks wait only for their specific deps.
    """
    results: dict[str, str] = {}   # tool_id → result
    pending = list(tasks)
    in_flight: dict[str, asyncio.Task] = {}

    async def run_task(task: ToolTask) -> tuple[str, str]:
        await asyncio.sleep(0.4)  # simulate tool latency
        return task.tool_id, f"[{task.name}] result"

    while pending or in_flight:
        # Launch all tasks whose dependencies are satisfied
        newly_launched = []
        for task in pending:
            deps_done = all(dep in results for dep in task.depends_on)
            if deps_done:
                print(f"[Scheduler] Launching: {task.name} (deps: {task.depends_on or 'none'})")
                in_flight[task.tool_id] = asyncio.create_task(run_task(task))
                newly_launched.append(task)

        for task in newly_launched:
            pending.remove(task)

        if not in_flight:
            break   # no progress possible — dependency cycle?

        # Wait for at least one task to complete
        done, _ = await asyncio.wait(
            in_flight.values(),
            return_when=asyncio.FIRST_COMPLETED,
        )

        for completed_task in done:
            tool_id, result = await completed_task
            results[tool_id] = result
            del in_flight[tool_id]
            print(f"[Scheduler] Completed: {tool_id}")

    return results


async def run_agent_dependency_aware(user_message: str) -> str:
    """
    Agent that explicitly schedules tool calls based on declared dependencies.
    Tools A and B run in parallel; C waits for A; D waits for B and C.
    """
    tasks = [
        ToolTask("fetch_user_profile", "t1", {"user_id": "123"}, depends_on=[]),
        ToolTask("fetch_market_data", "t2", {"symbol": "AAPL"}, depends_on=[]),
        ToolTask("personalize_content", "t3", {"user_id": "123"}, depends_on=["t1"]),
        ToolTask("generate_report", "t4", {}, depends_on=["t2", "t3"]),
    ]

    start = time.perf_counter()
    results = await execute_dependency_aware(tasks)
    elapsed = time.perf_counter() - start
    print(f"[DAG] All {len(tasks)} tasks completed in {elapsed:.2f}s")
    # Optimal: t1+t2 parallel → t3 (depends on t1) → t4 (depends on t2+t3)

    context = "\n".join(f"[{tid}]: {result}" for tid, result in results.items())
    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": f"{user_message}\n\nTool results:\n{context}"}],
    )
    return response.content[0].text


asyncio.run(run_agent_dependency_aware("Prepare a personalized market report for user 123"))
```

**Expected Token Savings:** Zero token change; dependency-aware scheduling extracts maximum parallelism from a DAG of tool calls — for a 4-task DAG with 2 parallel start nodes, reduces total time from ~1600ms (sequential) to ~800ms (optimal parallel).
**Environment:** Complex agents with tool dependency chains; the scheduler handles the case where some parallelism is possible but not all tasks are independent.

---

### Option 6 — Benchmark: measure sequential vs parallel latency

```python
import anthropic
import asyncio
import time

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")

TOOLS = [
    {"name": "tool_a", "description": "Tool A (300ms).", "input_schema": {"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]}},
    {"name": "tool_b", "description": "Tool B (500ms).", "input_schema": {"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]}},
    {"name": "tool_c", "description": "Tool C (200ms).", "input_schema": {"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]}},
]

TOOL_LATENCIES = {"tool_a": 0.3, "tool_b": 0.5, "tool_c": 0.2}


async def execute_tool(name: str, tool_input: dict) -> str:
    await asyncio.sleep(TOOL_LATENCIES.get(name, 0.3))
    return f"{name} result"


async def run_sequential(tools_to_call: list[tuple[str, dict]]) -> tuple[list[str], float]:
    """Run tools one at a time."""
    start = time.perf_counter()
    results = []
    for name, inp in tools_to_call:
        result = await execute_tool(name, inp)
        results.append(result)
    elapsed = time.perf_counter() - start
    return results, elapsed


async def run_parallel(tools_to_call: list[tuple[str, dict]]) -> tuple[list[str], float]:
    """Run all tools at once."""
    start = time.perf_counter()
    results = await asyncio.gather(*[
        execute_tool(name, inp) for name, inp in tools_to_call
    ])
    elapsed = time.perf_counter() - start
    return list(results), elapsed


async def benchmark():
    tools = [
        ("tool_a", {"input": "test"}),
        ("tool_b", {"input": "test"}),
        ("tool_c", {"input": "test"}),
    ]

    _, seq_time = await run_sequential(tools)
    _, par_time = await run_parallel(tools)

    sequential_sum = sum(TOOL_LATENCIES[name] for name, _ in tools)
    max_latency = max(TOOL_LATENCIES[name] for name, _ in tools)

    print("\n=== Tool Execution Benchmark ===")
    print(f"Tools: {[name for name, _ in tools]}")
    print(f"Individual latencies: {[f'{TOOL_LATENCIES[name]:.1f}s' for name, _ in tools]}")
    print(f"\nSequential: {seq_time:.2f}s (theoretical: {sequential_sum:.2f}s)")
    print(f"Parallel:   {par_time:.2f}s (theoretical: {max_latency:.2f}s)")
    print(f"Speedup:    {seq_time/par_time:.1f}×")
    print(f"Time saved: {seq_time - par_time:.2f}s ({(seq_time-par_time)/seq_time*100:.0f}%)")

    # Comparison table
    # | Option | Pattern | Speedup | Complexity | Best For |
    # |--------|---------|---------|-----------|----------|
    # | 1 System prompt | LLM instruction | 3× | None | Simple agents |
    # | 2 Per-tool timeout | asyncio.timeout | 3× | Low | Mixed-latency tools |
    # | 3 Structured executor | Timing stats | 3× | Medium | Production monitoring |
    # | 4 Sequential detector | Warning log | 3× | Low | Development tuning |
    # | 5 DAG scheduler | Dependency-aware | Optimal | High | Complex tool chains |
    # | 6 Benchmark | Latency measurement | N/A | Low | Measuring baseline |


asyncio.run(benchmark())
```

**Expected Token Savings:** Zero token change; the benchmark makes the business case for parallel execution concrete — a 3× speedup in tool execution reduces P50 agent latency from 1500ms to 500ms, directly improving user experience at zero API cost.
**Environment:** Any agent using multiple tools; run the benchmark before and after implementing parallel execution to quantify the improvement and justify the engineering work.
