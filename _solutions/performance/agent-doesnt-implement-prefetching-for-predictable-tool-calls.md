---
layout: solution
title: "Agent Doesn't Implement Prefetching for Predictable Tool Calls"
category: performance
description: "Agent fetches tool results sequentially — waiting for each before starting the next — even when the full set of required tools is predictable from context. Latency accumulates unnecessarily."
tags: [performance, latency, parallelism, tool-use, prefetching]
---

## Symptom

An agent processes a user request like "summarise the last 3 pull requests and their CI status". It calls:

```
→ get_pull_request(123)   [300 ms]
→ get_ci_status(123)      [200 ms]
→ get_pull_request(124)   [300 ms]
→ get_ci_status(124)      [200 ms]
→ get_pull_request(125)   [300 ms]
→ get_ci_status(125)      [200 ms]
                           ─────────
                           1 500 ms total
```

The same results could have been fetched in parallel in ~300 ms. The sequential pattern emerged because the model issued one tool call at a time.

## Root Cause

By default, the model emits tool calls one at a time and waits for each result before deciding on the next. When the downstream tools are independent and the full set is inferable from the request, sequential fetching is pure waste:

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# Anti-pattern: the model calls tools one at a time
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    tools=[get_pr_tool, get_ci_tool],
    messages=[{"role": "user", "content": "Summarise PRs 123, 124, 125 with CI status"}]
)
# Model: call get_pull_request(123) → wait → call get_ci_status(123) → wait → ...
```

---

## Fix

### Option 1 — System prompt instructs parallel tool batching

Tell the model to emit all independent tool calls in the same response turn, then process results together.

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")

PARALLEL_TOOLS_SYSTEM = """When a user asks for information that requires multiple independent tool calls,
emit ALL of them in the same response (as parallel tool_use blocks).
Do NOT wait for one result before requesting the next when the calls are independent.

Parallel calls are appropriate when:
- The inputs to each call are known from the user message (not dependent on prior results)
- The calls target different resources or IDs
- The results will be combined in the final answer

Sequential calls are required only when a later call depends on the output of an earlier one."""

tools = [
    {
        "name": "get_pull_request",
        "description": "Fetch pull request details by ID",
        "input_schema": {
            "type": "object",
            "properties": {"pr_id": {"type": "integer"}},
            "required": ["pr_id"]
        }
    },
    {
        "name": "get_ci_status",
        "description": "Fetch CI pipeline status for a PR",
        "input_schema": {
            "type": "object",
            "properties": {"pr_id": {"type": "integer"}},
            "required": ["pr_id"]
        }
    }
]


def fake_tool(name: str, input_data: dict) -> str:
    return json.dumps({"tool": name, "input": input_data, "result": "ok"})


def run_with_parallel_tools(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=PARALLEL_TOOLS_SYSTEM,
            tools=tools,
            messages=messages
        )

        tool_uses = [b for b in response.content if b.type == "tool_use"]

        if not tool_uses:
            # Final text response
            return next(b.text for b in response.content if b.type == "text")

        print(f"[parallel] Model issued {len(tool_uses)} tool call(s) in one turn")

        # Execute all tool calls (could be parallelised — see Option 2)
        tool_results = []
        for tu in tool_uses:
            result = fake_tool(tu.name, tu.input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": result
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})


result = run_with_parallel_tools("Summarise pull requests 123, 124, and 125 with their CI status")
print(result)

# Expected Token Savings: fewer turns → fewer input tokens re-sent; faster response frees user sooner
# Environment: any agent making multiple independent tool calls (GitHub, Jira, DB lookups, etc.)
```

---

### Option 2 — Executor runs parallel tool calls with asyncio.gather

After the model emits multiple tool_use blocks in one turn, execute them concurrently rather than sequentially.

```python
import anthropic
import asyncio
import json
import time

client = anthropic.AsyncAnthropic(api_key="sk-live-...")


async def execute_tool(name: str, input_data: dict) -> str:
    """Simulate async tool execution (replace with real API calls)."""
    await asyncio.sleep(0.3)  # Simulated 300 ms latency
    return json.dumps({"tool": name, "pr_id": input_data.get("pr_id"), "status": "success"})


async def run_agent(user_message: str) -> str:
    tools = [
        {
            "name": "get_pull_request",
            "description": "Fetch pull request by ID",
            "input_schema": {"type": "object", "properties": {"pr_id": {"type": "integer"}}, "required": ["pr_id"]}
        },
        {
            "name": "get_ci_status",
            "description": "Fetch CI status for a PR",
            "input_schema": {"type": "object", "properties": {"pr_id": {"type": "integer"}}, "required": ["pr_id"]}
        }
    ]

    messages = [{"role": "user", "content": user_message}]
    system = "Emit all independent tool calls in the same turn. Do not call them one by one."

    while True:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system,
            tools=tools,
            messages=messages
        )

        tool_uses = [b for b in response.content if b.type == "tool_use"]

        if not tool_uses:
            return next(b.text for b in response.content if b.type == "text")

        t0 = time.monotonic()

        # Execute all tool calls CONCURRENTLY
        results = await asyncio.gather(*[
            execute_tool(tu.name, tu.input) for tu in tool_uses
        ])

        elapsed = time.monotonic() - t0
        print(f"[async] {len(tool_uses)} tool calls completed in {elapsed:.2f}s (parallel)")

        tool_results = [
            {"type": "tool_result", "tool_use_id": tu.id, "content": result}
            for tu, result in zip(tool_uses, results)
        ]

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})


result = asyncio.run(run_agent("Get PRs 10, 20, 30 and their CI status"))
print(result)

# Expected Token Savings: 6 tool calls in 300ms vs 1800ms → faster completion, shorter sessions
# Environment: async agents calling external REST APIs, databases, or microservices
```

---

### Option 3 — Pre-extract IDs and prefetch before the LLM turn

Parse the user's request to extract known IDs, prefetch all tool results before starting the model turn, then inject results as context.

```python
import anthropic
import asyncio
import json
import re

client = anthropic.AsyncAnthropic(api_key="sk-live-...")


async def fetch_pr(pr_id: int) -> dict:
    """Simulated PR fetch."""
    await asyncio.sleep(0.2)
    return {"id": pr_id, "title": f"PR {pr_id}: fix issue", "author": "alice", "merged": False}


async def fetch_ci(pr_id: int) -> dict:
    """Simulated CI status fetch."""
    await asyncio.sleep(0.15)
    return {"pr_id": pr_id, "status": "passing", "coverage": 87}


def extract_pr_ids(message: str) -> list[int]:
    """Extract PR numbers from user message before calling the model."""
    return [int(m) for m in re.findall(r'\b(?:PR|pull request|#)\s*(\d+)\b', message, re.IGNORECASE)]


async def prefetch_context(message: str) -> str:
    """Prefetch all predictable data and return as context string."""
    pr_ids = extract_pr_ids(message)
    if not pr_ids:
        return ""

    # Prefetch everything in parallel
    pr_results, ci_results = await asyncio.gather(
        asyncio.gather(*[fetch_pr(pid) for pid in pr_ids]),
        asyncio.gather(*[fetch_ci(pid) for pid in pr_ids])
    )

    context_lines = ["## Prefetched data (use this — do not call tools for these IDs):"]
    for pr, ci in zip(pr_results, ci_results):
        context_lines.append(
            f"- PR {pr['id']}: title='{pr['title']}', merged={pr['merged']}, "
            f"CI={ci['status']}, coverage={ci['coverage']}%"
        )
    return "\n".join(context_lines)


async def run_with_prefetch(user_message: str) -> str:
    # Prefetch in parallel with (or before) model call
    context = await prefetch_context(user_message)

    augmented_message = user_message
    if context:
        augmented_message = f"{user_message}\n\n{context}"
        print(f"[prefetch] Injected context for {len(extract_pr_ids(user_message))} PRs")

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": augmented_message}]
    )
    return response.content[0].text


result = asyncio.run(run_with_prefetch("Summarise PR 101, PR 102, and PR 103 with CI status"))
print(result)

# Expected Token Savings: zero tool-use turns → no tool_use/tool_result token overhead at all
# Environment: agents where request patterns are structured and IDs can be extracted via regex/NLP
```

---

### Option 4 — Speculative prefetch while streaming model output

Start fetching tool results as soon as tool_use blocks appear in the stream, before the model finishes its full response.

```python
import anthropic
import asyncio
import json

sync_client = anthropic.Anthropic(api_key="sk-live-...")
async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")

PREFETCH_CACHE: dict[tuple, str] = {}


async def speculative_fetch(tool_name: str, tool_input: dict) -> str:
    """Execute a tool speculatively and cache the result."""
    cache_key = (tool_name, json.dumps(tool_input, sort_keys=True))
    if cache_key in PREFETCH_CACHE:
        print(f"[speculative] Cache hit: {tool_name}({tool_input})")
        return PREFETCH_CACHE[cache_key]

    # Simulated async tool call
    await asyncio.sleep(0.25)
    result = json.dumps({"tool": tool_name, "input": tool_input, "result": "fetched"})
    PREFETCH_CACHE[cache_key] = result
    print(f"[speculative] Prefetched: {tool_name}({tool_input})")
    return result


async def run_speculative_agent(user_message: str) -> str:
    tools = [
        {
            "name": "get_file",
            "description": "Read a file by path",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
        },
        {
            "name": "get_metric",
            "description": "Fetch a named metric",
            "input_schema": {"type": "object", "properties": {"metric": {"type": "string"}}, "required": ["metric"]}
        }
    ]

    messages = [{"role": "user", "content": user_message}]
    prefetch_tasks: dict[str, asyncio.Task] = {}

    while True:
        response = await async_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=tools,
            messages=messages
        )

        tool_uses = [b for b in response.content if b.type == "tool_use"]

        if not tool_uses:
            # Cancel any outstanding speculative fetches for unused tools
            for t in prefetch_tasks.values():
                t.cancel()
            return next(b.text for b in response.content if b.type == "text")

        # Kick off speculative fetches for all tool calls immediately
        fetch_tasks = []
        for tu in tool_uses:
            task = asyncio.create_task(speculative_fetch(tu.name, tu.input))
            fetch_tasks.append((tu, task))

        # Await all results (already running in background)
        tool_results = []
        for tu, task in fetch_tasks:
            result = await task
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": result
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})


result = asyncio.run(run_speculative_agent("Read config.yaml and metrics/latency, then summarise"))
print(result)

# Expected Token Savings: speculative fetch eliminates wait time between turns; cache prevents re-fetch
# Environment: agents with predictable tool sequences; long-running sessions where same tools recur
```

---

### Option 5 — Dependency graph: topological sort for tool call ordering

Analyse tool call dependencies explicitly and execute independent tools in parallel, dependent tools sequentially.

```python
import anthropic
import asyncio
import json
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic(api_key="sk-live-...")


@dataclass
class ToolNode:
    id: str
    name: str
    input: dict
    depends_on: list[str] = field(default_factory=list)  # IDs this node depends on
    result: str | None = None


async def execute_tool_node(node: ToolNode, results: dict[str, str]) -> str:
    """Execute a tool, substituting dependency results into inputs if needed."""
    # Resolve any $ref placeholders in input from dependency results
    resolved_input = {}
    for k, v in node.input.items():
        if isinstance(v, str) and v.startswith("$ref:"):
            dep_id = v[5:]
            dep_result = json.loads(results.get(dep_id, "{}"))
            resolved_input[k] = dep_result.get("id", v)
        else:
            resolved_input[k] = v

    await asyncio.sleep(0.2)  # Simulate API call
    return json.dumps({"node": node.id, "name": node.name, "input": resolved_input, "status": "ok"})


async def execute_dag(nodes: list[ToolNode]) -> dict[str, str]:
    """Execute a DAG of tool calls respecting dependencies."""
    results: dict[str, str] = {}
    pending = {n.id: n for n in nodes}
    completed: set[str] = set()

    while pending:
        # Find all nodes whose dependencies are satisfied
        ready = [
            n for n in pending.values()
            if all(dep in completed for dep in n.depends_on)
        ]

        if not ready:
            raise RuntimeError("Circular dependency detected in tool call graph")

        print(f"[dag] Executing {len(ready)} node(s) in parallel: {[n.id for n in ready]}")

        # Execute all ready nodes in parallel
        exec_results = await asyncio.gather(*[
            execute_tool_node(n, results) for n in ready
        ])

        for node, result in zip(ready, exec_results):
            results[node.id] = result
            completed.add(node.id)
            del pending[node.id]

    return results


async def run_dag_agent() -> None:
    # Example: fetch user → fetch their repos → fetch CI for each repo
    nodes = [
        ToolNode(id="n1", name="get_user", input={"user": "alice"}, depends_on=[]),
        ToolNode(id="n2", name="get_repos", input={"user": "alice"}, depends_on=[]),
        ToolNode(id="n3", name="get_file", input={"path": "README.md"}, depends_on=[]),
        ToolNode(id="n4", name="get_ci_status", input={"repo": "$ref:n2"}, depends_on=["n2"]),
        ToolNode(id="n5", name="summarise", input={"user": "$ref:n1", "ci": "$ref:n4"}, depends_on=["n1", "n4"]),
    ]

    results = await execute_dag(nodes)
    for node_id, result in results.items():
        print(f"{node_id}: {result[:60]}...")


asyncio.run(run_dag_agent())

# Expected Token Savings: DAG maximises parallelism at each level → minimum total wall time
# Environment: complex multi-step agents where some tools depend on earlier results
```

---

### Option 6 — Warm prefetch pool: background worker pre-populates likely results

A background task pool pre-fetches common/predictable resources (e.g., recent PRs, active configs) before requests arrive.

```python
import anthropic
import asyncio
import json
import time
from collections import OrderedDict

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

PREFETCH_TTL = 60.0  # Cached results valid for 60 seconds


class PrefetchPool:
    def __init__(self, max_size: int = 100):
        self._cache: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._max_size = max_size
        self._in_flight: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    def _cache_key(self, name: str, input_data: dict) -> str:
        return f"{name}:{json.dumps(input_data, sort_keys=True)}"

    async def prefetch(self, name: str, input_data: dict) -> None:
        """Start a background fetch without waiting for the result."""
        key = self._cache_key(name, input_data)
        async with self._lock:
            if key in self._cache or key in self._in_flight:
                return  # Already cached or in-flight

            task = asyncio.create_task(self._do_fetch(name, input_data, key))
            self._in_flight[key] = task

    async def _do_fetch(self, name: str, input_data: dict, key: str) -> None:
        await asyncio.sleep(0.2)  # Simulate API call
        result = json.dumps({"prefetched": True, "name": name, "input": input_data})

        async with self._lock:
            self._cache[key] = (result, time.monotonic())
            self._in_flight.pop(key, None)

            if len(self._cache) > self._max_size:
                self._cache.popitem(last=False)  # LRU eviction

    async def get(self, name: str, input_data: dict) -> str | None:
        """Get a cached result if available and fresh."""
        key = self._cache_key(name, input_data)

        # Wait briefly for in-flight prefetch to complete
        if key in self._in_flight:
            try:
                await asyncio.wait_for(asyncio.shield(self._in_flight[key]), timeout=0.5)
            except asyncio.TimeoutError:
                pass

        async with self._lock:
            if key in self._cache:
                result, fetched_at = self._cache[key]
                if time.monotonic() - fetched_at < PREFETCH_TTL:
                    return result
                del self._cache[key]

        return None

    async def get_or_fetch(self, name: str, input_data: dict) -> str:
        """Return cached result or fetch synchronously if not available."""
        cached = await self.get(name, input_data)
        if cached:
            print(f"[prefetch pool] Cache hit: {name}")
            return cached

        # Cache miss — fetch synchronously
        print(f"[prefetch pool] Cache miss — fetching: {name}")
        await asyncio.sleep(0.2)
        return json.dumps({"prefetched": False, "name": name, "input": input_data})


pool = PrefetchPool()


async def main() -> None:
    # Warm up cache with commonly needed resources
    await asyncio.gather(
        pool.prefetch("get_pull_request", {"pr_id": 200}),
        pool.prefetch("get_pull_request", {"pr_id": 201}),
        pool.prefetch("get_ci_status", {"pr_id": 200}),
        pool.prefetch("get_ci_status", {"pr_id": 201}),
    )
    print("[prefetch pool] Warm-up complete")

    # Simulate agent tool calls arriving after warm-up
    await asyncio.sleep(0.1)

    results = await asyncio.gather(
        pool.get_or_fetch("get_pull_request", {"pr_id": 200}),
        pool.get_or_fetch("get_ci_status", {"pr_id": 200}),
        pool.get_or_fetch("get_pull_request", {"pr_id": 201}),
    )

    for r in results:
        print(r[:80])


asyncio.run(main())

# Expected Token Savings: prefetched cache eliminates tool-fetch latency → response appears immediately
# Environment: agents serving repeat users; dashboards; scheduled pipelines with known data needs
```

---

## Comparison

| Option | Parallelism | Prefetch Strategy | Dependency Aware | Complexity |
|--------|------------|-------------------|-----------------|------------|
| 1 | Prompt-guided | None (model emits batch) | No | Low |
| 2 | asyncio.gather | Post-emit | No | Low |
| 3 | asyncio.gather | Pre-LLM (regex extract) | No | Medium |
| 4 | Speculative | During streaming | No | Medium |
| 5 | DAG topological | None | Yes | High |
| 6 | Background pool | Pre-request warm-up | No | Medium |

**Recommended starting point:** Option 1 (prompt-guided parallel batching) + Option 2 (async executor). Instruct the model to emit all independent tool calls at once, then execute them concurrently with `asyncio.gather`. This cuts multi-tool latency from O(n) sequential to O(1) parallel with a 10-line change. Add Option 3 for structured requests where IDs are extractable ahead of time.
