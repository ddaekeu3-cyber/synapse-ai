---
layout: solution
title: "Tool Calls Running Sequentially Instead of in Parallel — Slow Agent Execution"
category: performance
description: "Agent calls 5 independent tools one after another, taking 25 seconds. All 5 could run simultaneously in 5 seconds. Agent doesn't parallelize independent tool calls."
tags: [performance, parallelization, asyncio, tool-calls, latency, concurrent]
---

# Tool Calls Running Sequentially Instead of in Parallel — Slow Agent Execution

## Symptom
- Agent fetches 5 URLs one at a time, taking 25s instead of 5s
- Reading 10 files sequentially when all are independent
- Multiple API calls chained unnecessarily
- Task completion time scales linearly with number of sub-tasks
- No dependency between calls yet agent runs them in series

## Root Cause
By default, agents call tools sequentially: call → wait → call → wait. Without explicit parallelization instruction or implementation, the agent doesn't know which calls are independent and could run concurrently.

## Fix

### Option 1: Instruct the model to request parallel tool calls

```
System prompt:
"When you need to call multiple tools and the results are independent of each other,
request them all in a single response as parallel tool calls.

For example, if reading 3 files that don't depend on each other, call read_file
three times in the same turn — don't wait for each result before requesting the next.

Parallelize when: operations don't depend on each other's output.
Run sequentially when: operation B needs the result of operation A."
```

### Option 2: Execute parallel tool calls with asyncio.gather

```python
import asyncio
import anthropic

client = anthropic.Anthropic()

async def execute_tool(tool_name: str, tool_input: dict) -> dict:
    """Execute a single tool call"""
    # Implement your tool dispatch here
    if tool_name == "read_file":
        return {"content": open(tool_input["path"]).read()}
    elif tool_name == "fetch_url":
        import httpx
        async with httpx.AsyncClient() as http:
            resp = await http.get(tool_input["url"])
            return {"content": resp.text}
    # ... other tools

async def execute_tool_calls_parallel(tool_calls: list) -> list:
    """Execute all tool calls in parallel when possible"""
    tasks = [
        execute_tool(tc.name, tc.input)
        for tc in tool_calls
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    tool_results = []
    for tc, result in zip(tool_calls, results):
        if isinstance(result, Exception):
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc.id,
                "content": f"Error: {result}",
                "is_error": True
            })
        else:
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc.id,
                "content": str(result)
            })

    return tool_results
```

### Option 3: Agent loop with parallel execution

```python
async def run_agent_with_parallel_tools(initial_message: str):
    messages = [{"role": "user", "content": initial_message}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            tools=TOOLS,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        if response.stop_reason == "tool_use":
            tool_calls = [
                block for block in response.content
                if block.type == "tool_use"
            ]

            # Run ALL tool calls in parallel
            print(f"Running {len(tool_calls)} tool calls in parallel...")
            tool_results = await execute_tool_calls_parallel(tool_calls)

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
```

### Option 4: Dependency graph for complex workflows

```python
from collections import defaultdict

class ToolCallGraph:
    """Execute tools in topological order, parallelizing independent calls"""

    def __init__(self):
        self.nodes = {}  # id -> tool_call
        self.deps = defaultdict(set)  # id -> set of dependency ids

    def add(self, id: str, tool_call, depends_on: list[str] = None):
        self.nodes[id] = tool_call
        if depends_on:
            self.deps[id] = set(depends_on)

    async def execute(self) -> dict:
        results = {}
        completed = set()
        remaining = set(self.nodes.keys())

        while remaining:
            # Find all nodes whose dependencies are satisfied
            ready = {
                id for id in remaining
                if self.deps[id].issubset(completed)
            }

            if not ready:
                raise RuntimeError("Circular dependency detected")

            # Execute ready nodes in parallel
            tasks = {
                id: execute_tool(self.nodes[id].name, self.nodes[id].input)
                for id in ready
            }
            batch_results = await asyncio.gather(*tasks.values())

            for id, result in zip(tasks.keys(), batch_results):
                results[id] = result
                completed.add(id)
                remaining.remove(id)

        return results

# Usage:
graph = ToolCallGraph()
graph.add("fetch_user", fetch_tool("user", user_id))
graph.add("fetch_config", fetch_tool("config", config_id))
# analyze_data depends on both fetches
graph.add("analyze", analyze_tool(), depends_on=["fetch_user", "fetch_config"])
results = await graph.execute()
```

### Option 5: ThreadPoolExecutor for sync code

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

def execute_tools_parallel_sync(tool_calls: list, max_workers=10) -> list:
    """Synchronous parallel tool execution using thread pool"""
    results = [None] * len(tool_calls)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {
            executor.submit(execute_tool_sync, tc.name, tc.input): i
            for i, tc in enumerate(tool_calls)
        }

        for future in as_completed(future_to_index):
            index = future_to_index[future]
            try:
                results[index] = future.result()
            except Exception as e:
                results[index] = {"error": str(e)}

    return results
```

## Sequential vs Parallel: When to Use Each

| Scenario | Sequential | Parallel |
|---|---|---|
| File A read, then parse A | Sequential — B depends on A | — |
| Read files A, B, C for context | — | Parallel — independent |
| Fetch URL, then post result | Sequential | — |
| Fetch 10 URLs for research | — | Parallel |
| Write file, then verify write | Sequential | — |
| Read 5 database records by ID | — | Parallel |

## Latency Comparison

| Tool calls | Sequential | Parallel |
|-----------|-----------|---------|
| 3 × 2s each | 6s | 2s |
| 5 × 3s each | 15s | 3s |
| 10 × 1s each | 10s | 1s |

## Expected Token Savings
No token savings — but wall-clock time reduction of 60–90% for independent tool call batches.

## Environment
- Any multi-tool agent; most impactful for I/O-bound tools (HTTP, file, DB)
- Source: direct measurement, Anthropic parallel tool call documentation
