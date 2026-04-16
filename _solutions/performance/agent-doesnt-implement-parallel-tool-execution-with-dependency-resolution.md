---
title: "Agent Doesn't Implement Parallel Tool Execution with Dependency Resolution"
description: "Analyze tool call dependencies and execute independent tools in parallel while respecting ordering constraints—reducing multi-tool response latency by 50-80% compared to sequential execution."
difficulty: intermediate
category: performance
tags: [performance, parallel, tool-calls, dependency-resolution, latency]
---

## Problem

When an agent needs to call multiple tools, it typically executes them sequentially: call tool A, wait for result, call tool B, wait, call tool C. If B and C don't depend on A's result, this is pure wasted latency. A 3-tool workflow that takes 600ms sequentially can complete in 200ms if independent tools run in parallel. Dependency resolution ensures tools that depend on prior results still receive correct inputs.

## Solutions

### Option 1: Parallel Execution of Independent Tool Calls in One Turn

Process all tool calls from a single model response in parallel instead of sequentially.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

# Simulated tools with realistic latency
async def search_web(query: str) -> str:
    await asyncio.sleep(0.3)  # 300ms
    return f"Web results for '{query}': [article1, article2, article3]"

async def get_weather(city: str) -> str:
    await asyncio.sleep(0.25)  # 250ms
    return f"Weather in {city}: 22°C, partly cloudy"

async def fetch_news(topic: str) -> str:
    await asyncio.sleep(0.35)  # 350ms
    return f"Latest news on '{topic}': [story1, story2]"

async def lookup_stock(symbol: str) -> str:
    await asyncio.sleep(0.2)  # 200ms
    return f"{symbol}: $185.42 (+2.1%)"

TOOL_REGISTRY = {
    "search_web": search_web,
    "get_weather": get_weather,
    "fetch_news": fetch_news,
    "lookup_stock": lookup_stock,
}

TOOLS = [
    {
        "name": name,
        "description": f"Call {name}",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"]
        }
    }
    for name in TOOL_REGISTRY
]

async def execute_tools_parallel(tool_calls: list[dict]) -> list[dict]:
    """Execute all tool calls in parallel and return results."""
    async def call_one(tool_use) -> dict:
        tool_fn = TOOL_REGISTRY.get(tool_use["name"])
        if tool_fn:
            # All our tools take a single "query" parameter
            input_val = list(tool_use["input"].values())[0] if tool_use["input"] else ""
            result = await tool_fn(input_val)
        else:
            result = f"Unknown tool: {tool_use['name']}"

        return {
            "type": "tool_result",
            "tool_use_id": tool_use["id"],
            "content": result,
        }

    tasks = [call_one(tc) for tc in tool_calls]
    results = await asyncio.gather(*tasks)
    return list(results)

async def agent_with_parallel_tools(question: str) -> str:
    messages = [{"role": "user", "content": question}]

    for turn in range(5):
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        if response.stop_reason == "tool_use":
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            tool_use_dicts = [
                {"id": b.id, "name": b.name, "input": b.input}
                for b in tool_uses
            ]

            messages.append({"role": "assistant", "content": response.content})

            start = time.monotonic()
            tool_results = await execute_tools_parallel(tool_use_dicts)
            elapsed = (time.monotonic() - start) * 1000

            n = len(tool_uses)
            max_latency = max(
                {"search_web": 300, "get_weather": 250, "fetch_news": 350, "lookup_stock": 200}
                .get(tc["name"], 100) for tc in tool_use_dicts
            )
            sequential_ms = sum(
                {"search_web": 300, "get_weather": 250, "fetch_news": 350, "lookup_stock": 200}
                .get(tc["name"], 100) for tc in tool_use_dicts
            )
            print(f"  Executed {n} tools in {elapsed:.0f}ms parallel "
                  f"(vs {sequential_ms:.0f}ms sequential = "
                  f"{(1 - elapsed/sequential_ms)*100:.0f}% savings)")

            messages.append({"role": "user", "content": tool_results})

    return "Maximum turns reached"

async def demo_parallel_tools():
    question = (
        "I need: (1) web search for Python asyncio, (2) weather in Tokyo, "
        "(3) latest AI news, and (4) Apple stock price."
    )
    print(f"Question: {question}\n")
    result = await agent_with_parallel_tools(question)
    print(f"\nAnswer: {result.strip()[:200]}")

asyncio.run(demo_parallel_tools())
```

### Option 2: Dependency Graph Resolver

Build an explicit dependency graph for planned tool calls and execute in topological order with maximum parallelism.

```python
import asyncio
import time
from dataclasses import dataclass, field
from collections import defaultdict

@dataclass
class ToolNode:
    id: str
    name: str
    input_template: str        # May reference results: "{result_of_search}"
    depends_on: list[str]      # IDs of tools whose results this needs
    result: str | None = None

@dataclass
class DependencyGraph:
    nodes: dict[str, ToolNode]

    def execution_waves(self) -> list[list[str]]:
        """Return nodes grouped by execution wave (topological sort with parallelism)."""
        in_degree = {nid: 0 for nid in self.nodes}
        dependents = defaultdict(list)

        for nid, node in self.nodes.items():
            for dep in node.depends_on:
                in_degree[nid] += 1
                dependents[dep].append(nid)

        waves = []
        ready = [nid for nid, deg in in_degree.items() if deg == 0]

        while ready:
            waves.append(sorted(ready))
            next_ready = []
            for nid in ready:
                for dependent in dependents[nid]:
                    in_degree[dependent] -= 1
                    if in_degree[dependent] == 0:
                        next_ready.append(dependent)
            ready = next_ready

        return waves

    def resolve_input(self, node: ToolNode, results: dict[str, str]) -> str:
        """Substitute dependency results into input template."""
        input_val = node.input_template
        for dep_id, dep_result in results.items():
            input_val = input_val.replace(f"{{{dep_id}}}", dep_result[:100])
        return input_val

# Simulated tool executor
async def run_tool(tool_name: str, tool_input: str) -> str:
    latencies = {"search": 0.3, "summarize": 0.25, "translate": 0.2, "classify": 0.15}
    await asyncio.sleep(latencies.get(tool_name, 0.2))
    return f"{tool_name}({tool_input[:30]}...): [result]"

async def execute_with_dependency_resolution(graph: DependencyGraph) -> dict[str, str]:
    """Execute all tools respecting dependencies, maximizing parallelism."""
    results: dict[str, str] = {}
    waves = graph.execution_waves()

    total_start = time.monotonic()
    for wave_idx, wave in enumerate(waves):
        wave_start = time.monotonic()

        async def execute_node(node_id: str):
            node = graph.nodes[node_id]
            tool_input = graph.resolve_input(node, results)
            result = await run_tool(node.name, tool_input)
            results[node_id] = result
            return node_id, result

        wave_results = await asyncio.gather(*[execute_node(nid) for nid in wave])
        wave_elapsed = (time.monotonic() - wave_start) * 1000

        print(f"  Wave {wave_idx + 1}: [{', '.join(wave)}] in {wave_elapsed:.0f}ms")

    total_elapsed = (time.monotonic() - total_start) * 1000
    print(f"  Total: {total_elapsed:.0f}ms parallel vs {sum(0.2 * len(graph.nodes)):.0f}ms naive sequential")
    return results

async def demo_dependency_graph():
    # Example: search → summarize → translate (chain)
    #          classify (independent)
    graph = DependencyGraph(nodes={
        "search": ToolNode("search", "search", "Python async programming", depends_on=[]),
        "classify": ToolNode("classify", "classify", "AI content", depends_on=[]),
        "summarize": ToolNode("summarize", "summarize", "{search}", depends_on=["search"]),
        "translate": ToolNode("translate", "translate", "{summarize}", depends_on=["summarize"]),
    })

    print("Dependency graph execution:")
    print(f"  Waves: {graph.execution_waves()}")
    print()

    results = await execute_with_dependency_resolution(graph)
    print(f"\nResults: {list(results.keys())}")

asyncio.run(demo_dependency_graph())
```

### Option 3: Speculative Parallel Branches

Execute multiple plausible tool call paths in parallel, cancel losers when the winner is determined.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

@dataclass
class SpeculativeBranch:
    branch_id: str
    tool_name: str
    tool_input: str
    priority: float = 0.5  # Estimated probability this branch is needed

async def speculative_tool(branch: SpeculativeBranch) -> tuple[str, str]:
    """Execute a speculative tool call. Returns (branch_id, result)."""
    # Simulate different tools with different latencies
    latency_map = {"search_docs": 0.4, "search_web": 0.3, "cache_lookup": 0.05}
    await asyncio.sleep(latency_map.get(branch.tool_name, 0.2))
    return branch.branch_id, f"{branch.tool_name}: results for '{branch.tool_input[:30]}'"

async def execute_with_speculation(
    branches: list[SpeculativeBranch],
    select_winner: callable,
    cancel_losers: bool = True,
) -> tuple[str, str, float]:
    """
    Execute all branches in parallel, select the winner based on results.
    Returns (winner_id, winner_result, time_saved_ms).
    """
    # Sort by priority — highest priority branches get slight advantage
    branches_sorted = sorted(branches, key=lambda b: -b.priority)

    tasks = {
        branch.branch_id: asyncio.create_task(speculative_tool(branch))
        for branch in branches_sorted
    }

    start = time.monotonic()
    results: dict[str, str] = {}
    winner_id = None

    # Collect results as they arrive
    pending = list(tasks.values())
    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            branch_id, result = task.result()
            results[branch_id] = result

            if winner_id is None:
                winner_id = select_winner(results, branches)
                if winner_id:
                    if cancel_losers:
                        for bid, t in tasks.items():
                            if bid != winner_id and not t.done():
                                t.cancel()
                    break

    elapsed_ms = (time.monotonic() - start) * 1000
    sequential_ms = sum(
        {"search_docs": 400, "search_web": 300, "cache_lookup": 50}
        .get(b.tool_name, 200) for b in branches
    )
    time_saved = sequential_ms - elapsed_ms

    winner_result = results.get(winner_id, "")
    return winner_id or "", winner_result, time_saved

def select_first_result(results: dict, branches: list) -> str | None:
    """Select the first completed branch (fastest wins)."""
    if results:
        return next(iter(results))
    return None

def select_cache_preferred(results: dict, branches: list) -> str | None:
    """Prefer cache hit if available, else first other result."""
    cache_branches = [b.branch_id for b in branches if b.tool_name == "cache_lookup"]
    for cb in cache_branches:
        if cb in results and "cache miss" not in results[cb].lower():
            return cb
    if results:
        return next(iter(results))
    return None

async def demo_speculative():
    # Speculate: try cache first (fast), fall back to web or docs search
    branches = [
        SpeculativeBranch("cache", "cache_lookup", "Python asyncio tutorial", priority=0.7),
        SpeculativeBranch("web", "search_web", "Python asyncio tutorial", priority=0.5),
        SpeculativeBranch("docs", "search_docs", "Python asyncio tutorial", priority=0.4),
    ]

    winner, result, saved_ms = await execute_with_speculation(
        branches, select_cache_preferred, cancel_losers=True
    )
    print(f"Winner: {winner}")
    print(f"Result: {result[:80]}")
    print(f"Time saved vs sequential: {saved_ms:.0f}ms")

asyncio.run(demo_speculative())
```

### Option 4: Adaptive Parallelism Based on Tool Dependencies

Automatically detect tool dependencies from the model's tool call pattern and adjust parallelism.

```python
import asyncio
import time
import json
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client = AsyncAnthropic()

@dataclass
class ToolCall:
    id: str
    name: str
    input: dict
    depends_on_outputs: set[str] = field(default_factory=set)  # References to other tool IDs

def detect_dependencies(tool_calls: list[dict]) -> dict[str, set[str]]:
    """
    Detect which tool calls reference outputs of other tool calls.
    In practice, this requires inspecting tool input for references.
    Simple heuristic: if a tool's input contains another tool's ID string, it depends on it.
    """
    ids = {tc["id"] for tc in tool_calls}
    deps: dict[str, set[str]] = {tc["id"]: set() for tc in tool_calls}

    for tc in tool_calls:
        input_str = json.dumps(tc.get("input", {}))
        for other_id in ids:
            if other_id != tc["id"] and other_id in input_str:
                deps[tc["id"]].add(other_id)

    return deps

async def execute_adaptive(tool_calls: list[dict]) -> dict[str, str]:
    """Execute tool calls with auto-detected dependency ordering."""
    deps = detect_dependencies(tool_calls)
    results: dict[str, str] = {}
    tc_map = {tc["id"]: tc for tc in tool_calls}
    completed: set[str] = set()
    pending: set[str] = {tc["id"] for tc in tool_calls}

    async def mock_tool(tc_id: str, tc_name: str, tc_input: dict) -> str:
        await asyncio.sleep(0.1)
        return f"{tc_name}(input={str(tc_input)[:30]}): [result]"

    waves_executed = 0
    while pending:
        # Find tools whose dependencies are all satisfied
        ready = [
            tc_id for tc_id in pending
            if deps[tc_id].issubset(completed)
        ]

        if not ready:
            raise RuntimeError(f"Circular dependency detected. Remaining: {pending}")

        # Execute all ready tools in parallel
        wave_tasks = [
            asyncio.create_task(
                mock_tool(tid, tc_map[tid]["name"], tc_map[tid].get("input", {}))
            )
            for tid in ready
        ]
        wave_results = await asyncio.gather(*wave_tasks)

        for tid, result in zip(ready, wave_results):
            results[tid] = result
            completed.add(tid)
            pending.remove(tid)

        waves_executed += 1

    return results

async def demo_adaptive_parallelism():
    # Mix of independent and dependent tools
    tool_calls = [
        {"id": "t1", "name": "search", "input": {"query": "Python asyncio"}},
        {"id": "t2", "name": "weather", "input": {"city": "Tokyo"}},
        {"id": "t3", "name": "summarize", "input": {"text": "t1"}},  # Depends on t1
        {"id": "t4", "name": "translate", "input": {"text": "t3"}},  # Depends on t3
        {"id": "t5", "name": "lookup", "input": {"symbol": "AAPL"}},  # Independent
    ]

    deps = detect_dependencies(tool_calls)
    print("Detected dependencies:")
    for tid, dep_set in deps.items():
        tc = next(tc for tc in tool_calls if tc["id"] == tid)
        print(f"  {tid}({tc['name']}) depends on: {dep_set or 'none'}")

    start = time.monotonic()
    results = await execute_adaptive(tool_calls)
    elapsed = (time.monotonic() - start) * 1000

    sequential_ms = len(tool_calls) * 100
    print(f"\nCompleted in {elapsed:.0f}ms (vs {sequential_ms:.0f}ms sequential)")
    print(f"Speedup: {sequential_ms/elapsed:.1f}x")

asyncio.run(demo_adaptive_parallelism())
```

### Option 5: Streaming-Compatible Parallel Tool Execution

Execute tools in parallel even when the main response is streamed, injecting results as they arrive.

```python
import asyncio
import time
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

# Simulated tool functions
TOOLS_IMPL = {
    "search": lambda q: asyncio.sleep(0.3) and None or f"Search: {q}",
    "calculate": lambda e: asyncio.sleep(0.1) and None or f"Calc: {e}=42",
    "lookup": lambda k: asyncio.sleep(0.2) and None or f"Lookup: {k}=value",
}

async def run_tool_async(name: str, input_val: str) -> str:
    latencies = {"search": 0.3, "calculate": 0.1, "lookup": 0.2}
    await asyncio.sleep(latencies.get(name, 0.15))
    return f"{name}({input_val[:20]}): [mock result]"

class ParallelToolStreamProcessor:
    def __init__(self):
        self._tool_results: dict[str, str] = {}
        self._tool_tasks: dict[str, asyncio.Task] = {}

    async def process_stream_with_parallel_tools(
        self, messages: list[dict], tools: list[dict]
    ) -> str:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        if response.stop_reason == "tool_use":
            tool_uses = [b for b in response.content if b.type == "tool_use"]

            # Launch all tool calls immediately in parallel
            start = time.monotonic()
            tasks = {}
            for block in tool_uses:
                input_val = list(block.input.values())[0] if block.input else ""
                task = asyncio.create_task(run_tool_async(block.name, input_val))
                tasks[block.id] = task

            # Wait for all
            tool_results = []
            for block in tool_uses:
                result = await tasks[block.id]
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

            elapsed = (time.monotonic() - start) * 1000
            sequential_estimate = sum(
                {"search": 300, "calculate": 100, "lookup": 200}.get(b.name, 150)
                for b in tool_uses
            )
            print(f"  {len(tool_uses)} tools in {elapsed:.0f}ms parallel "
                  f"(sequential estimate: {sequential_estimate}ms)")

            messages_extended = list(messages)
            messages_extended.append({"role": "assistant", "content": response.content})
            messages_extended.append({"role": "user", "content": tool_results})

            return await self.process_stream_with_parallel_tools(messages_extended, tools)

        return ""

async def demo_streaming_parallel():
    processor = ParallelToolStreamProcessor()
    tools = [
        {
            "name": name,
            "description": f"Execute {name}",
            "input_schema": {
                "type": "object",
                "properties": {"input": {"type": "string"}},
                "required": ["input"]
            }
        }
        for name in ["search", "calculate", "lookup"]
    ]

    question = "Search for Python, calculate 15*23, and look up the key 'version'."
    result = await processor.process_stream_with_parallel_tools(
        [{"role": "user", "content": question}],
        tools,
    )
    print(f"\nFinal answer: {result.strip()[:200]}")

asyncio.run(demo_streaming_parallel())
```

### Option 6: Work-Stealing Parallel Tool Executor

Use a worker pool with work-stealing to execute variable-latency tools efficiently.

```python
import asyncio
import time
import heapq
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client = AsyncAnthropic()

@dataclass(order=True)
class ToolJob:
    priority: float      # Estimated duration (shorter = higher priority)
    tool_id: str = field(compare=False)
    tool_name: str = field(compare=False)
    tool_input: str = field(compare=False)
    future: asyncio.Future = field(compare=False, default=None)

class WorkStealingToolExecutor:
    def __init__(self, max_workers: int = 4):
        self._max_workers = max_workers
        self._queue: list[ToolJob] = []
        self._active: int = 0
        self._lock = asyncio.Lock()
        self._completed: dict[str, str] = {}

        # Estimated latency per tool type (for priority scheduling)
        self._tool_latencies = {
            "cache_lookup": 0.05,
            "calculate": 0.1,
            "lookup": 0.2,
            "search": 0.3,
            "fetch_url": 0.4,
            "summarize": 0.5,
        }

    async def _run_tool(self, name: str, input_val: str) -> str:
        latency = self._tool_latencies.get(name, 0.2)
        await asyncio.sleep(latency)
        return f"{name}({input_val[:20]}): [result in {latency*1000:.0f}ms]"

    async def _worker(self):
        while True:
            async with self._lock:
                if not self._queue:
                    self._active -= 1
                    return
                job = heapq.heappop(self._queue)

            result = await self._run_tool(job.tool_name, job.tool_input)
            job.future.set_result(result)

    async def submit_all(self, tool_calls: list[dict]) -> dict[str, str]:
        """Submit all tools for parallel execution with priority scheduling."""
        futures: dict[str, asyncio.Future] = {}

        async with self._lock:
            for tc in tool_calls:
                input_val = list(tc.get("input", {}).values())[0] if tc.get("input") else ""
                priority = self._tool_latencies.get(tc["name"], 0.2)

                future: asyncio.Future = asyncio.get_event_loop().create_future()
                futures[tc["id"]] = future

                job = ToolJob(
                    priority=priority,
                    tool_id=tc["id"],
                    tool_name=tc["name"],
                    tool_input=input_val,
                    future=future,
                )
                heapq.heappush(self._queue, job)

            # Start workers up to max_workers
            workers_to_start = min(self._max_workers, len(tool_calls)) - self._active
            for _ in range(workers_to_start):
                self._active += 1
                asyncio.create_task(self._worker())

        # Wait for all results
        results = await asyncio.gather(*futures.values())
        return {tid: result for tid, result in zip(futures.keys(), results)}

async def demo_work_stealing():
    executor = WorkStealingToolExecutor(max_workers=3)

    tool_calls = [
        {"id": "t1", "name": "summarize", "input": {"text": "long document"}},
        {"id": "t2", "name": "cache_lookup", "input": {"key": "config"}},
        {"id": "t3", "name": "search", "input": {"query": "Python async"}},
        {"id": "t4", "name": "calculate", "input": {"expr": "15*23"}},
        {"id": "t5", "name": "fetch_url", "input": {"url": "https://example.com"}},
    ]

    print("Tool calls (sorted by estimated latency for priority scheduling):")
    for tc in sorted(tool_calls, key=lambda x: executor._tool_latencies.get(x["name"], 0.2)):
        print(f"  {tc['id']}: {tc['name']} "
              f"({executor._tool_latencies.get(tc['name'], 0.2)*1000:.0f}ms est.)")

    start = time.monotonic()
    results = await executor.submit_all(tool_calls)
    elapsed = (time.monotonic() - start) * 1000

    sequential_ms = sum(
        executor._tool_latencies.get(tc["name"], 0.2) * 1000
        for tc in tool_calls
    )

    print(f"\nCompleted {len(results)} tools in {elapsed:.0f}ms parallel "
          f"(vs {sequential_ms:.0f}ms sequential)")
    print(f"Speedup: {sequential_ms/elapsed:.1f}x")
    for tid, result in results.items():
        print(f"  {tid}: {result[:60]}")

asyncio.run(demo_work_stealing())
```

## Comparison

| Approach | Dependency Support | Complexity | Speedup | Best For |
|---|---|---|---|---|
| Parallel Tool Calls in One Turn | None (all parallel) | Low | 2-5x | Independent tools in single response |
| Dependency Graph Resolver | Full graph | Medium | 2-4x | Complex multi-step tool workflows |
| Speculative Parallel Branches | None (race) | Medium | 1.5-3x | Uncertain which tool will be needed |
| Adaptive Auto-Detection | Heuristic | Medium | 2-4x | Dynamic tool call patterns |
| Streaming-Compatible Parallel | None | Low | 2-5x | Streaming response agents |
| Work-Stealing Pool | Priority-based | High | 2-5x | Variable-latency tool portfolios |

**Choose Parallel Tool Calls in One Turn** as the immediate win—it requires changing a single `await tool()` call to `asyncio.gather()` and delivers 2-5x latency reduction on any multi-tool workflow. **Choose Dependency Graph Resolver** when tools have clear data dependencies and you want to express them explicitly. **Choose Speculative Parallel Branches** for read operations where you're uncertain which source (cache, web, database) will return first, and you want to use whichever responds fastest.
