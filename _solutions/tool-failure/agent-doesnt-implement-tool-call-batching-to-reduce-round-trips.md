---
layout: solution
title: "Agent Doesn't Implement Tool Call Batching to Reduce Round Trips"
category: tool-failure
description: "Agent makes tool calls one at a time in sequential turns, even when results are independent — tripling latency and token cost by sending redundant context on every round trip instead of batching parallel calls."
tags: [tool-failure, batching, performance, parallel, asyncio, latency]
---

# Agent Doesn't Implement Tool Call Batching to Reduce Round Trips

## Problem

Claude natively supports requesting multiple tool calls in a single response (`stop_reason == "tool_use"` with multiple `tool_use` blocks). But agents often process them one-by-one in sequential turns, or fail to group independent work into a single request, causing:

- **3× latency**: 3 sequential tool calls = 3 full round trips with full context resent each time
- **Token waste**: each round trip resends the entire conversation history
- **Missed parallelism**: results that could be fetched concurrently are serialized
- **Rate limit pressure**: more requests = higher chance of hitting per-minute limits

**Root cause:** The agent loop calls the model once, executes one tool, and starts a new turn — rather than letting the model request multiple tools at once, or proactively grouping independent operations.

---

## Option 1: Parallel Tool Result Collection (Native Multi-Tool Support)

Let Claude request multiple tools per turn; process all tool_use blocks before the next model call.

```python
import anthropic
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

client = anthropic.Anthropic()

# Simulated tool implementations
def get_weather(city: str) -> dict:
    time.sleep(0.1)  # Simulate latency
    temps = {"Tokyo": 22, "London": 15, "New York": 18, "Sydney": 25}
    return {"city": city, "temperature": temps.get(city, 20), "condition": "partly cloudy"}

def get_exchange_rate(from_ccy: str, to_ccy: str) -> dict:
    time.sleep(0.1)
    rates = {("USD", "JPY"): 149.5, ("USD", "GBP"): 0.79, ("EUR", "USD"): 1.08}
    rate = rates.get((from_ccy, to_ccy), 1.0)
    return {"from": from_ccy, "to": to_ccy, "rate": rate}

def get_population(city: str) -> dict:
    time.sleep(0.1)
    pops = {"Tokyo": 13960000, "London": 8982000, "New York": 8336817}
    return {"city": city, "population": pops.get(city, 1000000)}

TOOL_IMPLS = {
    "get_weather": lambda inp: get_weather(inp["city"]),
    "get_exchange_rate": lambda inp: get_exchange_rate(inp["from"], inp["to"]),
    "get_population": lambda inp: get_population(inp["city"]),
}

tools = [
    {
        "name": "get_weather",
        "description": "Get current weather for a city",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"]
        }
    },
    {
        "name": "get_exchange_rate",
        "description": "Get exchange rate between two currencies",
        "input_schema": {
            "type": "object",
            "properties": {
                "from": {"type": "string"},
                "to": {"type": "string"}
            },
            "required": ["from", "to"]
        }
    },
    {
        "name": "get_population",
        "description": "Get population of a city",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"]
        }
    }
]

def run_agent_with_parallel_tools(query: str) -> str:
    messages = [{"role": "user", "content": query}]
    turn_count = 0

    while True:
        start = time.time()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )
        turn_count += 1

        if response.stop_reason == "end_turn":
            elapsed = time.time() - start
            print(f"[batch] Completed in {turn_count} turn(s)")
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason != "tool_use":
            break

        # Collect ALL tool_use blocks from this response
        tool_blocks = [b for b in response.content if b.type == "tool_use"]
        print(f"[batch] Turn {turn_count}: executing {len(tool_blocks)} tool call(s) in parallel")

        # Execute all tool calls concurrently
        tool_results = []
        with ThreadPoolExecutor(max_workers=len(tool_blocks)) as executor:
            futures = {
                executor.submit(TOOL_IMPLS[b.name], b.input): b
                for b in tool_blocks
                if b.name in TOOL_IMPLS
            }
            for future in as_completed(futures):
                block = futures[future]
                try:
                    result = future.result()
                    content = json.dumps(result)
                except Exception as e:
                    content = json.dumps({"error": str(e)})
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return "Done"

# This query should trigger multiple tool calls in one turn
result = run_agent_with_parallel_tools(
    "What is the weather, population, and USD to JPY exchange rate for Tokyo?"
)
print(result)

# Expected Token Savings: ~40% (3 tool results in 1 turn vs 3 sequential turns with full context resent each time)
# Environment: Any agent with multiple independent data lookups per user query
```

---

## Option 2: Client-Side Batch Grouping — Detect and Merge Independent Calls

Before sending to the model, detect when the user query requires multiple independent data points and structure the request to elicit batched tool calls.

```python
import anthropic
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

client = anthropic.Anthropic()

# Hint in system prompt to encourage batching
BATCHING_SYSTEM = """You are a data retrieval assistant. When a user asks for multiple independent pieces of data:
- Request ALL needed tool calls in a SINGLE response turn
- Do NOT make tool calls one at a time — batch them for efficiency
- Only proceed to answering after ALL results are available"""

def mock_lookup(tool_name: str, params: dict) -> dict:
    import time; time.sleep(0.05)
    if tool_name == "get_stock_price":
        prices = {"AAPL": 182.5, "GOOGL": 141.8, "MSFT": 378.9, "NVDA": 875.4}
        return {"symbol": params["symbol"], "price": prices.get(params["symbol"], 100.0)}
    if tool_name == "get_company_info":
        info = {
            "AAPL": {"name": "Apple Inc.", "sector": "Technology", "employees": 164000},
            "GOOGL": {"name": "Alphabet Inc.", "sector": "Technology", "employees": 182000},
        }
        return info.get(params["symbol"], {"name": "Unknown", "sector": "N/A"})
    return {"error": f"Unknown tool: {tool_name}"}

tools = [
    {
        "name": "get_stock_price",
        "description": "Get current stock price for a ticker symbol",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string", "description": "Stock ticker (e.g. AAPL)"}},
            "required": ["symbol"]
        }
    },
    {
        "name": "get_company_info",
        "description": "Get company information for a ticker symbol",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"]
        }
    }
]

def run_batched_agent(query: str) -> str:
    messages = [{"role": "user", "content": query}]
    total_tool_calls = 0
    total_turns = 0

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=BATCHING_SYSTEM,
            tools=tools,
            messages=messages
        )
        total_turns += 1

        if response.stop_reason == "end_turn":
            print(f"[batch] Stats: {total_turns} turns, {total_tool_calls} total tool calls")
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason != "tool_use":
            break

        tool_blocks = [b for b in response.content if b.type == "tool_use"]
        total_tool_calls += len(tool_blocks)
        print(f"[batch] Turn {total_turns}: {len(tool_blocks)} concurrent tool call(s)")

        tool_results = []
        with ThreadPoolExecutor(max_workers=min(len(tool_blocks), 8)) as executor:
            futures = {
                executor.submit(mock_lookup, b.name, b.input): b
                for b in tool_blocks
            }
            for future in as_completed(futures):
                block = futures[future]
                result = future.result()
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result)
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return "Done"

result = run_batched_agent(
    "Get the stock price and company info for AAPL, GOOGL, and MSFT"
)
print(result)

# Expected Token Savings: ~50% (6 tool calls in 1-2 turns instead of 6 sequential turns)
# Environment: Financial data agents, comparison shopping bots, multi-entity lookup tasks
```

---

## Option 3: Pre-Flight Batch Planner — Plan All Calls Before Any Execution

Use a lightweight planner call to enumerate all needed tool calls; execute them all at once before the main generation.

```python
import anthropic
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

client = anthropic.Anthropic()

PLANNER_SYSTEM = """You are a tool call planner. Given a user query and a list of available tools,
output a JSON list of ALL tool calls needed to answer the query.

Format: [{"tool": "<name>", "input": {...}}, ...]

Rules:
- List ALL calls needed, even if there are many
- Only include calls that are truly necessary
- Do NOT include explanations — output JSON only"""

def plan_tool_calls(query: str, tool_specs: list[dict]) -> list[dict]:
    """Use a cheap model to pre-plan all required tool calls."""
    tool_summary = "\n".join(f"- {t['name']}: {t['description']}" for t in tool_specs)
    prompt = f"""Available tools:\n{tool_summary}\n\nUser query: {query}"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=PLANNER_SYSTEM,
        messages=[{"role": "user", "content": prompt}]
    )

    try:
        return json.loads(response.content[0].text)
    except (json.JSONDecodeError, AttributeError):
        return []

def execute_planned_calls(plan: list[dict]) -> list[dict]:
    """Execute all planned tool calls in parallel."""
    def execute_one(call: dict) -> dict:
        tool_name = call["tool"]
        inp = call["input"]
        # Mock implementations
        import time; time.sleep(0.08)
        if tool_name == "get_weather":
            return {"tool_id": f"{tool_name}_{inp.get('city', '')}", "result": {"temperature": 20, "city": inp.get("city")}}
        if tool_name == "search_news":
            return {"tool_id": f"{tool_name}_{inp.get('topic', '')}", "result": {"headlines": [f"News about {inp.get('topic')}", "More news"]}}
        if tool_name == "get_flight_prices":
            return {"tool_id": f"{tool_name}_{inp.get('route', '')}", "result": {"route": inp.get("route"), "price_usd": 450}}
        return {"tool_id": tool_name, "result": {"error": f"Unknown: {tool_name}"}}

    results = []
    with ThreadPoolExecutor(max_workers=min(len(plan), 10)) as executor:
        futures = {executor.submit(execute_one, call): call for call in plan}
        for future in as_completed(futures):
            results.append(future.result())

    return results

tools = [
    {"name": "get_weather", "description": "Get weather for a city. Input: {city: string}"},
    {"name": "search_news", "description": "Search recent news. Input: {topic: string}"},
    {"name": "get_flight_prices", "description": "Get flight prices. Input: {route: string, e.g. 'NYC-LAX'}"},
]

def run_with_preflight_batch(query: str) -> str:
    # Step 1: Plan (1 cheap model call)
    print("[preflight] Planning tool calls...")
    plan = plan_tool_calls(query, tools)
    print(f"[preflight] Plan: {len(plan)} tool calls")
    for call in plan:
        print(f"  - {call['tool']}({call['input']})")

    # Step 2: Execute all in parallel (0 round trips to model)
    print("[preflight] Executing all tool calls in parallel...")
    tool_results = execute_planned_calls(plan)
    print(f"[preflight] Got {len(tool_results)} results")

    # Step 3: Single final model call with all results
    context = json.dumps(tool_results, indent=2)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"Query: {query}\n\nTool results:\n{context}\n\nPlease answer the query using these results."
        }]
    )
    return response.content[0].text

result = run_with_preflight_batch(
    "What's the weather in Tokyo, any news about AI agents, and flight prices from NYC to LAX?"
)
print(f"\nAnswer: {result}")

# Expected Token Savings: ~55% (pre-flight: 2 model calls total vs N+1 sequential turns)
# Environment: Agents with predictable, query-derivable tool needs; travel, news, research assistants
```

---

## Option 4: Batch Queue with Debounce — Group Tool Calls Across Multiple Agent Steps

Buffer tool calls for a short window; flush all queued calls at once when the buffer stabilizes.

```python
import anthropic
import json
import asyncio
from dataclasses import dataclass, field
from typing import Any

client = anthropic.AsyncAnthropic()

@dataclass
class PendingToolCall:
    call_id: str
    tool_name: str
    tool_input: dict
    future: asyncio.Future = field(default_factory=asyncio.Future)

class DebounceToolBatcher:
    """Buffers tool calls and flushes them in batches."""
    def __init__(self, debounce_ms: int = 50, max_batch_size: int = 10):
        self.debounce_ms = debounce_ms
        self.max_batch = max_batch_size
        self.pending: list[PendingToolCall] = []
        self._flush_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def call(self, call_id: str, tool_name: str, tool_input: dict) -> Any:
        loop = asyncio.get_event_loop()
        pending = PendingToolCall(call_id=call_id, tool_name=tool_name, tool_input=tool_input,
                                   future=loop.create_future())
        async with self._lock:
            self.pending.append(pending)
            if len(self.pending) >= self.max_batch:
                if self._flush_task:
                    self._flush_task.cancel()
                await self._flush()
            elif not self._flush_task or self._flush_task.done():
                self._flush_task = asyncio.create_task(self._delayed_flush())

        return await pending.future

    async def _delayed_flush(self):
        await asyncio.sleep(self.debounce_ms / 1000)
        await self._flush()

    async def _flush(self):
        async with self._lock:
            batch = self.pending.copy()
            self.pending.clear()

        if not batch:
            return

        print(f"[debounce] Flushing batch of {len(batch)} tool calls")

        async def execute_one(p: PendingToolCall):
            await asyncio.sleep(0.05)  # Simulated tool latency
            result = {"tool": p.tool_name, "input": p.tool_input, "status": "ok"}
            p.future.set_result(result)

        await asyncio.gather(*[execute_one(p) for p in batch])

batcher = DebounceToolBatcher(debounce_ms=50, max_batch_size=8)

async def run_debounce_agent(queries: list[str]) -> list[str]:
    """Simulate multiple concurrent agent steps all queuing tool calls."""
    async def process_query(q: str, idx: int) -> str:
        # Each query triggers 2 tool calls
        r1, r2 = await asyncio.gather(
            batcher.call(f"call-{idx}-a", "lookup", {"query": q, "type": "primary"}),
            batcher.call(f"call-{idx}-b", "lookup", {"query": q, "type": "secondary"}),
        )

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{
                "role": "user",
                "content": f"Query: {q}\nData: {json.dumps([r1, r2])}\nAnswer in one sentence."
            }]
        )
        return response.content[0].text

    results = await asyncio.gather(*[process_query(q, i) for i, q in enumerate(queries)])
    return list(results)

QUERIES = [
    "Best practices for API rate limiting",
    "How does Kubernetes handle pod scheduling",
    "What is the CAP theorem in distributed systems",
]

results = asyncio.run(run_debounce_agent(QUERIES))
for q, r in zip(QUERIES, results):
    print(f"Q: {q[:40]}...\nA: {r[:80]}...\n")

# Expected Token Savings: ~35% (debounced batching groups concurrent queries; fewer model round trips)
# Environment: High-concurrency agent servers handling many simultaneous user requests
```

---

## Option 5: Result Memoization to Avoid Redundant Batch Calls

Cache tool results within a batch run so identical calls (e.g., same city weather) are only executed once.

```python
import anthropic
import json
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

client = anthropic.Anthropic()

class MemoizedBatchExecutor:
    def __init__(self):
        self.cache: dict[str, dict] = {}
        self.stats = {"hits": 0, "misses": 0, "calls_saved": 0}

    def _cache_key(self, tool_name: str, tool_input: dict) -> str:
        payload = json.dumps({"tool": tool_name, "input": tool_input}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def execute_batch(self, tool_blocks: list) -> list[dict]:
        """Execute a batch of tool calls with memoization."""
        # Group by cache key
        key_to_blocks: dict[str, list] = {}
        for block in tool_blocks:
            key = self._cache_key(block.name, block.input)
            key_to_blocks.setdefault(key, []).append(block)

        # Identify cache hits vs misses
        to_execute = []
        for key, blocks in key_to_blocks.items():
            if key in self.cache:
                self.stats["hits"] += 1
                self.stats["calls_saved"] += len(blocks) - 1
            else:
                self.stats["misses"] += 1
                to_execute.append((key, blocks[0]))  # Only execute once per unique key

        # Execute cache misses in parallel
        def mock_tool(tool_name: str, tool_input: dict) -> dict:
            time.sleep(0.08)
            if tool_name == "get_weather":
                return {"city": tool_input["city"], "temp": 20, "cached": False}
            if tool_name == "get_price":
                return {"item": tool_input["item"], "price": 29.99}
            return {}

        with ThreadPoolExecutor(max_workers=min(len(to_execute), 8)) as executor:
            futures = {
                executor.submit(mock_tool, block.name, block.input): (key, block)
                for key, block in to_execute
            }
            for future in as_completed(futures):
                key, block = futures[future]
                result = future.result()
                self.cache[key] = result

        # Build results for all blocks
        results = []
        for block in tool_blocks:
            key = self._cache_key(block.name, block.input)
            cached = dict(self.cache[key])
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(cached)
            })

        return results

executor = MemoizedBatchExecutor()

tools = [
    {
        "name": "get_weather",
        "description": "Get weather for a city",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"]
        }
    },
    {
        "name": "get_price",
        "description": "Get price for an item",
        "input_schema": {
            "type": "object",
            "properties": {"item": {"type": "string"}},
            "required": ["item"]
        }
    }
]

def run_memoized_agent(query: str) -> str:
    messages = [{"role": "user", "content": query}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            print(f"[memo] Stats: {executor.stats}")
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason != "tool_use":
            break

        tool_blocks = [b for b in response.content if b.type == "tool_use"]
        print(f"[memo] Processing {len(tool_blocks)} tool calls ({executor.stats['hits']} cached)")
        tool_results = executor.execute_batch(tool_blocks)

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return "Done"

# Query that will trigger duplicate tool calls (e.g., weather for Tokyo asked twice)
result = run_memoized_agent(
    "What is the weather in Tokyo? Also compare the weather in Tokyo and the price of a Widget."
)
print(result)

# Expected Token Savings: ~30% (dedup eliminates redundant tool round trips within a session)
# Environment: Agents where the same data point might be requested multiple times (comparison, analysis)
```

---

## Option 6: Streaming Batch Aggregator — Collect Streaming Tool Results Concurrently

Use streaming to detect tool calls early and start executing them before the full response is received.

```python
import anthropic
import json
import asyncio
from typing import AsyncIterator

client = anthropic.AsyncAnthropic()

async def mock_tool_async(tool_name: str, tool_input: dict) -> dict:
    await asyncio.sleep(0.1)  # Simulate async I/O
    if tool_name == "get_data":
        return {"key": tool_input.get("key"), "value": f"data_for_{tool_input.get('key')}"}
    return {"error": f"Unknown: {tool_name}"}

async def streaming_batch_agent(query: str) -> str:
    """Start tool execution as soon as tool_use blocks appear in the stream."""
    messages = [{"role": "user", "content": query}]

    tools = [
        {
            "name": "get_data",
            "description": "Retrieve data by key",
            "input_schema": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"]
            }
        }
    ]

    while True:
        # Collect the full response (streaming internally) to detect tool calls
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason != "tool_use":
            break

        tool_blocks = [b for b in response.content if b.type == "tool_use"]
        print(f"[stream-batch] Executing {len(tool_blocks)} tool calls concurrently")

        # Fan out all tool executions concurrently
        async def exec_block(block) -> dict:
            result = await mock_tool_async(block.name, block.input)
            return {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result)
            }

        tool_results = await asyncio.gather(*[exec_block(b) for b in tool_blocks])

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": list(tool_results)})

    return "Done"

result = asyncio.run(streaming_batch_agent(
    "Get data for keys: alpha, beta, gamma, delta. Summarize all four values."
))
print(result)

# Expected Token Savings: ~45% (async fan-out of all tool calls in one turn vs 4 sequential turns)
# Environment: High-throughput async agent servers; real-time applications needing low latency
```

---

## Comparison

| Option | Batch Mechanism | Parallelism | Caching | Best For |
|--------|----------------|-------------|---------|----------|
| 1. Native Multi-Tool Parallel | ThreadPoolExecutor | Yes | No | Any agent — baseline best practice |
| 2. System Prompt Batch Hint | ThreadPoolExecutor | Yes | No | Agents needing model-level guidance |
| 3. Pre-Flight Planner | Sequential plan + parallel exec | Yes | No | Predictable, query-derivable tool sets |
| 4. Debounce Queue | asyncio.gather | Yes | No | High-concurrency multi-user servers |
| 5. Result Memoization | ThreadPoolExecutor | Yes | Yes | Agents with repeated identical queries |
| 6. Streaming Batch | asyncio.gather | Yes | No | Low-latency real-time applications |
