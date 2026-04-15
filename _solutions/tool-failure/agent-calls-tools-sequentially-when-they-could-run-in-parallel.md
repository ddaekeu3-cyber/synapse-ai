---
layout: solution
title: "Agent Calls Tools Sequentially When They Could Run in Parallel"
category: tool-failure
description: "Agent waits for each tool call to complete before issuing the next, even when multiple tools have no dependencies on each other."
tags: [tool-failure, performance, parallelism, latency, asyncio, efficiency]
---

## Symptom

Agent serialises independent tool calls, multiplying latency:

```
Turn 1: call get_weather(city="London")
Wait 800ms...
Turn 2: call get_weather(city="Paris")
Wait 800ms...
Turn 3: call get_weather(city="Berlin")
Wait 800ms...

Total: 2,400ms for 3 independent queries
Optimal: 800ms (all 3 in parallel)

# Agent also does this with multi-tool calls in a single turn:
# It issues one tool_use block then waits, instead of returning
# multiple tool_use blocks simultaneously
```

The agent treats every tool as if the next one depends on the previous result. For IO-bound tools (API calls, database reads, file reads), this compounds latency linearly.

## Root Cause

The default LLM tool-use pattern generates one `tool_use` block per response turn. Without explicit prompting to batch independent calls, the model defaults to sequential: call, wait, see result, call again. The orchestrator compounds this by running each tool call before returning to the model, even when multiple `tool_use` blocks could be executed simultaneously.

## Fix

---

### Option 1: Prompt the Model to Batch Independent Tool Calls

Instruct the model explicitly to return multiple `tool_use` blocks in a single response when tools are independent.

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

PARALLEL_SYSTEM = """You are an efficient assistant. When multiple tool calls are independent
(the result of one does not affect the input of another), return ALL of them in a single
response as multiple tool_use blocks. Do not make them one at a time.

Example of CORRECT parallel batching:
User asks for weather in London, Paris, and Berlin.
You return THREE tool_use blocks in ONE response:
  tool_use: get_weather(city="London")
  tool_use: get_weather(city="Paris")
  tool_use: get_weather(city="Berlin")

Example of WRONG sequential approach:
  Response 1: tool_use: get_weather(city="London")
  [wait for result]
  Response 2: tool_use: get_weather(city="Paris")
  etc."""

async def execute_tools_parallel(tool_uses: list) -> list[dict]:
    """Execute multiple tool_use blocks concurrently."""
    async def run_tool(tool_use) -> dict:
        # Simulate tool execution (replace with real tool dispatch)
        await asyncio.sleep(0.1)  # 100ms per tool
        return {
            "type": "tool_result",
            "tool_use_id": tool_use.id,
            "content": f"Result for {tool_use.name}({tool_use.input})",
        }

    return await asyncio.gather(*[run_tool(tu) for tu in tool_uses])

async def run_parallel_agent(user_query: str) -> str:
    tools = [
        {
            "name": "get_weather",
            "description": "Get current weather for a city",
            "input_schema": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        }
    ]

    messages = [{"role": "user", "content": user_query}]

    for _ in range(10):
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=PARALLEL_SYSTEM,
            tools=tools,
            messages=messages,
        )

        tool_uses = [b for b in response.content if b.type == "tool_use"]

        if not tool_uses:
            return response.content[0].text

        import time
        start = time.perf_counter()
        # Execute all tool_use blocks in this response simultaneously
        results = await execute_tools_parallel(tool_uses)
        elapsed = time.perf_counter() - start
        print(f"Executed {len(tool_uses)} tools in parallel: {elapsed:.3f}s")

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": results})

    return "Max iterations reached"

asyncio.run(run_parallel_agent("What's the weather in London, Paris, and Berlin?"))
```

**Expected Token Savings:** No direct token savings, but 3× latency reduction for 3 parallel calls. Faster responses mean less context accumulation from intermediate turns, saving ~200 tokens per avoided sequential turn.
**Environment:** Model must support multi-block tool_use responses (Claude does). Orchestrator must execute returned tool_uses concurrently — verify your framework does this before assuming parallelism.

---

### Option 2: Pre-Plan Parallel Batches with a DAG

Before execution, ask the model to identify which tools can run in parallel (no dependencies) vs which must be sequential, then execute accordingly.

```python
import asyncio
import json
import anthropic
from dataclasses import dataclass

@dataclass
class ExecutionPlan:
    parallel_groups: list[list[dict]]  # Each group runs in parallel; groups are sequential

async def plan_tool_execution(task: str, available_tools: list[dict]) -> ExecutionPlan:
    """Ask model to produce a parallel execution plan before running any tools."""
    client = anthropic.AsyncAnthropic()

    plan_response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system="""You are a task planner. Given a task and available tools, produce an
execution plan that maximises parallelism. Return a JSON object:
{
  "parallel_groups": [
    [{"tool": "name", "input": {...}}, ...],  // group 1: all run in parallel
    [{"tool": "name", "input": {...}}, ...],  // group 2: runs after group 1
  ]
}
Tools in the same group have NO dependencies on each other.""",
        messages=[{
            "role": "user",
            "content": f"Task: {task}\nAvailable tools: {json.dumps([t['name'] for t in available_tools])}",
        }],
    )

    raw = plan_response.content[0].text
    if "```" in raw:
        raw = raw.split("```")[1].split("```")[0]
        if raw.startswith("json"):
            raw = raw[4:]
    data = json.loads(raw.strip())
    return ExecutionPlan(**data)

async def execute_plan(plan: ExecutionPlan, tool_registry: dict) -> list[list[dict]]:
    """Execute parallel groups sequentially; within each group run tools concurrently."""
    all_results = []
    for group in plan.parallel_groups:
        async def run_tool_call(call: dict) -> dict:
            fn = tool_registry.get(call["tool"])
            if fn:
                result = await fn(**call["input"])
            else:
                result = f"Unknown tool: {call['tool']}"
            return {"tool": call["tool"], "input": call["input"], "result": result}

        group_results = await asyncio.gather(*[run_tool_call(c) for c in group])
        all_results.append(list(group_results))
    return all_results

# Example tool registry
async def get_weather(city: str) -> str:
    await asyncio.sleep(0.2)
    return f"Weather in {city}: 22°C, sunny"

async def get_population(city: str) -> str:
    await asyncio.sleep(0.2)
    return f"Population of {city}: 2.1M"

async def get_news(topic: str) -> str:
    await asyncio.sleep(0.3)
    return f"News about {topic}: market up 2%"

tool_registry = {
    "get_weather": get_weather,
    "get_population": get_population,
    "get_news": get_news,
}

available_tools = [
    {"name": "get_weather", "description": "Get weather for a city"},
    {"name": "get_population", "description": "Get population of a city"},
    {"name": "get_news", "description": "Get news for a topic"},
]

async def main():
    import time
    task = "Get weather and population for London and Paris, then get news about travel"
    plan = await plan_tool_execution(task, available_tools)
    print("Execution plan:")
    for i, group in enumerate(plan.parallel_groups):
        print(f"  Group {i+1} (parallel): {[c['tool'] for c in group]}")

    start = time.perf_counter()
    results = await execute_plan(plan, tool_registry)
    elapsed = time.perf_counter() - start
    print(f"\nExecuted in {elapsed:.2f}s")
    for i, group in enumerate(results):
        print(f"Group {i+1}: {[r['result'][:40] for r in group]}")

asyncio.run(main())
```

**Expected Token Savings:** Planning call costs ~200 tokens but reduces total execution time. For 6 tools where 4 can run in parallel: sequential = 6 × 300ms = 1.8s; parallel = 2 × 300ms = 600ms. Faster execution reduces time-based costs in serverless environments.
**Environment:** Best for complex multi-tool workflows. DAG planning adds one LLM call overhead — only worthwhile when ≥3 tools would otherwise be sequential.

---

### Option 3: Orchestrator-Level Parallel Dispatch — Always Execute Tool Batches Concurrently

Modify the orchestration loop to always run all tool_uses returned in a single response concurrently, regardless of model intent.

```python
import asyncio
import time
from typing import Callable, Any
import anthropic

type ToolFn = Callable[..., Any]

class ParallelToolOrchestrator:
    def __init__(self, tools: list[dict], tool_registry: dict[str, ToolFn]):
        self.tools = tools
        self.registry = tool_registry
        self.client = anthropic.AsyncAnthropic()
        self.parallelism_stats: list[int] = []

    async def _execute_tool(self, tool_use) -> dict:
        fn = self.registry.get(tool_use.name)
        if not fn:
            return {
                "type": "tool_result",
                "tool_use_id": tool_use.id,
                "content": f"Unknown tool: {tool_use.name}",
                "is_error": True,
            }
        result = await fn(**tool_use.input)
        return {
            "type": "tool_result",
            "tool_use_id": tool_use.id,
            "content": str(result),
        }

    async def run(self, user_message: str, system: str = "", max_turns: int = 10) -> str:
        messages = [{"role": "user", "content": user_message}]

        for turn in range(max_turns):
            response = await self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=system,
                tools=self.tools,
                messages=messages,
            )

            tool_uses = [b for b in response.content if b.type == "tool_use"]

            if not tool_uses:
                return next(
                    (b.text for b in response.content if hasattr(b, "text")), ""
                )

            self.parallelism_stats.append(len(tool_uses))
            start = time.perf_counter()

            # Always parallel regardless of model intent
            results = await asyncio.gather(*[self._execute_tool(tu) for tu in tool_uses])
            elapsed = time.perf_counter() - start

            print(f"Turn {turn+1}: {len(tool_uses)} tools in {elapsed:.3f}s (parallel)")

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": list(results)})

        return "Max turns reached"

    def stats_summary(self) -> dict:
        if not self.parallelism_stats:
            return {}
        return {
            "total_tool_batches": len(self.parallelism_stats),
            "avg_tools_per_batch": sum(self.parallelism_stats) / len(self.parallelism_stats),
            "max_parallel": max(self.parallelism_stats),
            "total_tools": sum(self.parallelism_stats),
        }

# Example usage
async def fetch_stock(ticker: str) -> str:
    await asyncio.sleep(0.15)
    prices = {"AAPL": 189.50, "GOOGL": 175.20, "MSFT": 415.30}
    return f"{ticker}: ${prices.get(ticker, 100.0):.2f}"

async def fetch_earnings(ticker: str) -> str:
    await asyncio.sleep(0.2)
    return f"{ticker} Q4 earnings: beat by 5%"

tools = [
    {"name": "fetch_stock", "description": "Get stock price",
     "input_schema": {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]}},
    {"name": "fetch_earnings", "description": "Get earnings data",
     "input_schema": {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]}},
]

async def main():
    orch = ParallelToolOrchestrator(tools, {"fetch_stock": fetch_stock, "fetch_earnings": fetch_earnings})
    result = await orch.run(
        "Get stock prices and latest earnings for AAPL, GOOGL, and MSFT",
        system="When fetching data for multiple tickers, request all of them at once.",
    )
    print(f"\nResult: {result}")
    print(f"Stats: {orch.stats_summary()}")

asyncio.run(main())
```

**Expected Token Savings:** Zero extra tokens — parallelism is free. For 4 concurrent 200ms tools: sequential = 800ms, parallel = 200ms. In streaming contexts, faster completion reduces response buffering overhead.
**Environment:** Drop-in replacement for standard orchestration loops. Works for any tool type. Ensure tools are stateless or thread-safe before parallelising.

---

### Option 4: Speculative Parallel Pre-Fetch — Start Common Tools Before Model Decides

For known patterns (user asks about multiple cities, tickers, documents), speculatively start tool calls before the model even requests them.

```python
import asyncio
import re
import anthropic

async def prefetch_weather(cities: list[str]) -> dict[str, str]:
    """Start fetching before model responds."""
    async def fetch_one(city: str) -> tuple[str, str]:
        await asyncio.sleep(0.15)  # simulated API call
        return city, f"Weather in {city}: 18°C, partly cloudy"

    results = await asyncio.gather(*[fetch_one(c) for c in cities])
    return dict(results)

def extract_cities(text: str) -> list[str]:
    """Simple city extractor from user query."""
    known_cities = ["London", "Paris", "Berlin", "Tokyo", "New York", "Sydney", "Toronto"]
    return [c for c in known_cities if c.lower() in text.lower()]

client = anthropic.AsyncAnthropic()

async def agent_with_prefetch(user_query: str) -> str:
    # Step 1: Speculatively start fetching while model thinks
    cities = extract_cities(user_query)
    prefetch_task = None
    prefetch_cache: dict[str, str] = {}

    if cities:
        print(f"Speculatively prefetching weather for: {cities}")
        prefetch_task = asyncio.create_task(prefetch_weather(cities))

    tools = [
        {
            "name": "get_weather",
            "description": "Get weather for a city",
            "input_schema": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        }
    ]

    messages = [{"role": "user", "content": user_query}]
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        tools=tools,
        messages=messages,
    )

    # Collect prefetch results (likely done by now)
    if prefetch_task:
        prefetch_cache = await prefetch_task

    tool_uses = [b for b in response.content if b.type == "tool_use"]
    if not tool_uses:
        return response.content[0].text

    # Serve from prefetch cache if available
    tool_results = []
    for tu in tool_uses:
        city = tu.input.get("city", "")
        if city in prefetch_cache:
            print(f"Cache hit for {city}!")
            result = prefetch_cache[city]
        else:
            # Fallback: fetch normally
            result = f"Weather in {city}: data unavailable (not prefetched)"
        tool_results.append({
            "type": "tool_result",
            "tool_use_id": tu.id,
            "content": result,
        })

    messages.append({"role": "assistant", "content": response.content})
    messages.append({"role": "user", "content": tool_results})

    final = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        tools=tools,
        messages=messages,
    )
    return final.content[0].text

asyncio.run(agent_with_prefetch("Compare the weather in London, Paris, and Berlin for my trip"))
```

**Expected Token Savings:** Prefetch eliminates the model-wait-tool latency gap entirely for predicted tool calls. Cache hits serve results instantly — no additional API calls. For queries where prefetch accuracy is >80%, reduces tool-call latency by 100%.
**Environment:** Requires a predictable mapping from user query to tool inputs. Works best for search, weather, stock, and entity-lookup patterns. Low risk: prefetch failures fall back to normal execution.

---

### Option 5: Parallel Sub-Query Decomposition

When the user asks a compound question, decompose it into independent sub-queries and answer each in parallel with separate model calls.

```python
import asyncio
import json
import anthropic

client = anthropic.AsyncAnthropic()

async def decompose_query(compound_query: str) -> list[str]:
    """Break a compound query into independent sub-queries."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system="""Decompose the user's question into independent sub-questions that can be
answered separately and in parallel. Return a JSON array of strings.
If the question is atomic (cannot be decomposed), return a single-element array.""",
        messages=[{"role": "user", "content": compound_query}],
    )
    raw = response.content[0].text.strip()
    if "```" in raw:
        raw = raw.split("```")[1].split("```")[0].strip()
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw)

async def answer_subquery(sub_query: str, context: str = "") -> str:
    """Answer a single focused sub-query."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=f"Answer concisely. {context}".strip(),
        messages=[{"role": "user", "content": sub_query}],
    )
    return response.content[0].text

async def synthesise_answers(original_query: str, sub_answers: list[tuple[str, str]]) -> str:
    """Combine parallel sub-answers into a coherent final response."""
    qa_pairs = "\n\n".join(f"Q: {q}\nA: {a}" for q, a in sub_answers)
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system="Synthesise the following Q&A pairs into a single coherent answer to the original question.",
        messages=[{
            "role": "user",
            "content": f"Original question: {original_query}\n\nSub-answers:\n{qa_pairs}",
        }],
    )
    return response.content[0].text

async def parallel_qa(query: str) -> str:
    import time

    # Step 1: Decompose
    sub_queries = await decompose_query(query)
    print(f"Decomposed into {len(sub_queries)} sub-queries: {sub_queries}")

    if len(sub_queries) == 1:
        return await answer_subquery(sub_queries[0])

    # Step 2: Answer all sub-queries in parallel
    start = time.perf_counter()
    sub_answers = await asyncio.gather(*[answer_subquery(sq) for sq in sub_queries])
    elapsed = time.perf_counter() - start
    print(f"Answered {len(sub_queries)} sub-queries in {elapsed:.2f}s (parallel)")

    # Step 3: Synthesise
    return await synthesise_answers(query, list(zip(sub_queries, sub_answers)))

result = asyncio.run(parallel_qa(
    "What are the main differences between Python asyncio and threading, "
    "and when should I use each one?"
))
print(result)
```

**Expected Token Savings:** Parallel sub-queries have lower individual max_tokens needs (focused answers). Total tokens: decompose (~100) + N × focused-answer (~150) + synthesise (~200) vs single complex answer (~600). Break-even at N=2; saves tokens for N≥3.
**Environment:** Decomposition adds one model call overhead. Best for multi-part questions where sub-answers are genuinely independent. Don't decompose questions that require integrated reasoning across parts.

---

### Option 6: Async Tool Registry with Concurrency Limit

Execute parallel tools with a semaphore to prevent overwhelming downstream APIs while still maximising throughput.

```python
import asyncio
import time
from typing import Any
import anthropic

class BoundedParallelToolRunner:
    """Runs tool calls in parallel with a concurrency cap per tool type."""

    def __init__(self, concurrency_limits: dict[str, int] | None = None, default_limit: int = 5):
        self.limits = concurrency_limits or {}
        self.default_limit = default_limit
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    def _get_semaphore(self, tool_name: str) -> asyncio.Semaphore:
        if tool_name not in self._semaphores:
            limit = self.limits.get(tool_name, self.default_limit)
            self._semaphores[tool_name] = asyncio.Semaphore(limit)
        return self._semaphores[tool_name]

    async def run_tool(self, tool_use, registry: dict) -> dict:
        sem = self._get_semaphore(tool_use.name)
        async with sem:
            fn = registry.get(tool_use.name)
            if not fn:
                return {"type": "tool_result", "tool_use_id": tool_use.id,
                        "content": f"Unknown: {tool_use.name}", "is_error": True}
            try:
                result = await fn(**tool_use.input)
                return {"type": "tool_result", "tool_use_id": tool_use.id, "content": str(result)}
            except Exception as e:
                return {"type": "tool_result", "tool_use_id": tool_use.id,
                        "content": str(e), "is_error": True}

    async def run_all(self, tool_uses: list, registry: dict) -> list[dict]:
        start = time.perf_counter()
        results = await asyncio.gather(*[self.run_tool(tu, registry) for tu in tool_uses])
        elapsed = time.perf_counter() - start
        print(f"Ran {len(tool_uses)} tools in {elapsed:.3f}s (bounded parallel, limits={self.limits})")
        return list(results)

# Tool implementations
async def search_db(query: str) -> str:
    await asyncio.sleep(0.1)
    return f"DB result for '{query}'"

async def call_external_api(endpoint: str) -> str:
    await asyncio.sleep(0.3)
    return f"API response from {endpoint}"

async def read_cache(key: str) -> str:
    await asyncio.sleep(0.01)
    return f"Cache value for {key}"

registry = {
    "search_db": search_db,
    "call_external_api": call_external_api,
    "read_cache": read_cache,
}

# Limit external API to 2 concurrent; DB to 3; cache is fast so unlimited
runner = BoundedParallelToolRunner(
    concurrency_limits={"call_external_api": 2, "search_db": 3},
    default_limit=10,
)

client = anthropic.AsyncAnthropic()

async def main():
    tools = [
        {"name": "search_db", "description": "Search database",
         "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
        {"name": "call_external_api", "description": "Call external API",
         "input_schema": {"type": "object", "properties": {"endpoint": {"type": "string"}}, "required": ["endpoint"]}},
        {"name": "read_cache", "description": "Read from cache",
         "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}},
    ]

    messages = [{"role": "user", "content": "Search for users, call the payments API, and read the session cache"}]
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        tools=tools,
        system="Return all needed tool calls at once as multiple tool_use blocks.",
        messages=messages,
    )

    tool_uses = [b for b in response.content if b.type == "tool_use"]
    if tool_uses:
        results = await runner.run_all(tool_uses, registry)
        print(f"Results: {[r['content'] for r in results]}")

# Comparison table
"""
| Approach | Parallelism Source | Overhead | Best For |
|---|---|---|---|
| Option 1: Prompt batching | Model returns multi-block | ~50 tokens | General |
| Option 2: DAG planning | Pre-plan execution order | +1 LLM call | Complex workflows |
| Option 3: Orchestrator parallel | Always run concurrently | None | Default upgrade |
| Option 4: Speculative prefetch | Start before model asks | Pattern detection | Predictable queries |
| Option 5: Sub-query decompose | Split compound questions | +2 LLM calls | Multi-part questions |
| Option 6: Bounded parallel | Semaphore-limited concurrency | Semaphore overhead | Rate-limited tools |
"""

asyncio.run(main())
```

**Expected Token Savings:** Bounded parallelism prevents cascading failures from overwhelming downstream services — avoids error retries that each cost tokens. For 10 tool calls at concurrency=2: runs in 5 batches of 2 vs 10 sequential calls. Wall-clock 5× faster with zero additional tokens.
**Environment:** Set `call_external_api` concurrency to match the downstream service's rate limit. Cache and local DB tools can be unlimited. Monitor `_semaphores` wait times to tune limits empirically.
