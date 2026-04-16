---
layout: solution
title: "Agent Doesn't Implement Token Usage Attribution by Tool Call"
category: observability
description: "Track and attribute token consumption to individual tool calls, agent steps, and pipeline stages so you can identify cost hotspots and optimize where it matters."
tags: [observability, token-cost, attribution, tracing, analytics, monitoring]
---

Agents report total token usage per API call, but that number hides the real cost driver. Is it the system prompt? The tool schema definitions? A single tool that returns 50KB of raw output? Without per-tool attribution, cost optimization is guesswork. Instrumenting each tool call to track its token contribution exposes the actual hotspots.

## Option 1: Before/After Token Delta Attribution

Estimate each tool's token contribution by capturing context token count before and after injecting its result into the conversation. The difference is attributed to that tool.

```python
import anthropic
import json
from dataclasses import dataclass, field

@dataclass
class ToolTokenRecord:
    tool_name: str
    input_size_chars: int
    output_size_chars: int
    estimated_input_tokens: int    # chars injected into next prompt
    estimated_output_tokens: int   # chars in tool result
    call_index: int

def estimate_tokens(text: str) -> int:
    """Rough approximation: ~4 chars per token."""
    return max(1, len(text) // 4)

class AttributingAgent:
    def __init__(self):
        self.client = anthropic.Anthropic()
        self.tool_records: list[ToolTokenRecord] = []
        self.call_index = 0
        self._total_api_tokens_in = 0
        self._total_api_tokens_out = 0

    def record_tool_call(self, name: str, tool_input: dict, tool_result: str) -> None:
        input_json = json.dumps(tool_input)
        self.tool_records.append(ToolTokenRecord(
            tool_name=name,
            input_size_chars=len(input_json),
            output_size_chars=len(tool_result),
            estimated_input_tokens=estimate_tokens(input_json),
            estimated_output_tokens=estimate_tokens(tool_result),
            call_index=self.call_index,
        ))
        self.call_index += 1

    def run(self, user_message: str) -> str:
        tools = [
            {
                "name": "get_weather",
                "description": "Get current weather for a location",
                "input_schema": {
                    "type": "object",
                    "properties": {"location": {"type": "string"}},
                    "required": ["location"],
                },
            },
            {
                "name": "search_database",
                "description": "Search a large database and return matching records",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                    "required": ["query"],
                },
            },
        ]

        messages = [{"role": "user", "content": user_message}]

        while True:
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                tools=tools,
                messages=messages,
            )
            self._total_api_tokens_in += response.usage.input_tokens
            self._total_api_tokens_out += response.usage.output_tokens

            if response.stop_reason != "tool_use":
                return response.content[0].text if response.content else ""

            # Process tool calls
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    # Simulate tool execution
                    if block.name == "get_weather":
                        result = json.dumps({"temp": 72, "condition": "sunny", "humidity": 45, "wind_mph": 8})
                    elif block.name == "search_database":
                        result = json.dumps([{"id": i, "name": f"Record {i}", "value": i * 10} for i in range(block.input.get("limit", 5))])
                    else:
                        result = "{}"

                    self.record_tool_call(block.name, block.input, result)
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

    def print_attribution_report(self) -> None:
        print("\n=== Token Attribution Report ===")
        print(f"Total API tokens: {self._total_api_tokens_in} in / {self._total_api_tokens_out} out")
        print(f"\nPer-Tool Breakdown:")

        by_tool: dict[str, list[ToolTokenRecord]] = {}
        for r in self.tool_records:
            by_tool.setdefault(r.tool_name, []).append(r)

        total_output_est = sum(r.estimated_output_tokens for r in self.tool_records)
        for tool_name, records in sorted(by_tool.items(), key=lambda x: sum(r.estimated_output_tokens for r in x[1]), reverse=True):
            calls = len(records)
            total_out = sum(r.estimated_output_tokens for r in records)
            avg_out = total_out // calls if calls else 0
            pct = (total_out / total_output_est * 100) if total_output_est else 0
            print(f"  {tool_name}: {calls} calls | ~{total_out} output tokens | avg {avg_out}/call | {pct:.1f}% of tool output")

if __name__ == "__main__":
    agent = AttributingAgent()
    result = agent.run("What's the weather in NYC and show me 10 database records about weather data?")
    print(result)
    agent.print_attribution_report()

# Expected Token Savings: Identifies which tools to optimize first (typically 20% of tools = 80% of cost)
# Environment: pip install anthropic
```

## Option 2: Middleware Wrapper with Per-Step Accounting

Wrap the Anthropic client to intercept every API call. Log the token delta between consecutive calls and attribute it to the step label set by the agent. Steps are named (e.g. "tool:search", "step:summarize") so the report maps cost to agent phases.

```python
import anthropic
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from collections import defaultdict

@dataclass
class StepTokenUsage:
    step_label: str
    input_tokens: int
    output_tokens: int
    duration_ms: float
    call_count: int = 1

class InstrumentedClient:
    def __init__(self):
        self._client = anthropic.Anthropic()
        self._current_step = "unnamed"
        self._step_usage: dict[str, StepTokenUsage] = {}
        self._call_log: list[dict] = []

    @contextmanager
    def step(self, label: str):
        prev = self._current_step
        self._current_step = label
        try:
            yield
        finally:
            self._current_step = prev

    def messages_create(self, **kwargs) -> anthropic.types.Message:
        start = time.monotonic()
        response = self._client.messages.create(**kwargs)
        elapsed_ms = (time.monotonic() - start) * 1000

        label = self._current_step
        usage = response.usage

        self._call_log.append({
            "step": label,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "model": kwargs.get("model"),
            "duration_ms": elapsed_ms,
        })

        if label in self._step_usage:
            existing = self._step_usage[label]
            self._step_usage[label] = StepTokenUsage(
                step_label=label,
                input_tokens=existing.input_tokens + usage.input_tokens,
                output_tokens=existing.output_tokens + usage.output_tokens,
                duration_ms=existing.duration_ms + elapsed_ms,
                call_count=existing.call_count + 1,
            )
        else:
            self._step_usage[label] = StepTokenUsage(
                step_label=label,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                duration_ms=elapsed_ms,
            )
        return response

    def attribution_report(self) -> dict:
        total_in = sum(s.input_tokens for s in self._step_usage.values())
        total_out = sum(s.output_tokens for s in self._step_usage.values())
        report = {
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "total_tokens": total_in + total_out,
            "steps": [],
        }
        for step in sorted(self._step_usage.values(), key=lambda s: s.input_tokens + s.output_tokens, reverse=True):
            total_step = step.input_tokens + step.output_tokens
            report["steps"].append({
                "step": step.step_label,
                "input_tokens": step.input_tokens,
                "output_tokens": step.output_tokens,
                "total_tokens": total_step,
                "pct_of_total": f"{total_step / (total_in + total_out) * 100:.1f}%" if (total_in + total_out) else "0%",
                "calls": step.call_count,
                "avg_ms": f"{step.duration_ms / step.call_count:.0f}ms",
            })
        return report

def run_pipeline(client: InstrumentedClient, task: str) -> str:
    # Step 1: Plan
    with client.step("plan"):
        plan_resp = client.messages_create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": f"Create a 3-step plan to: {task}"}],
        )
        plan = plan_resp.content[0].text

    # Step 2: Tool simulation (search)
    with client.step("tool:search"):
        search_resp = client.messages_create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": f"Summarize key findings for: {task}"}],
        )
        findings = search_resp.content[0].text

    # Step 3: Synthesize
    with client.step("synthesize"):
        synth_resp = client.messages_create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": f"Plan:\n{plan}\n\nFindings:\n{findings}\n\nSynthesize into a final answer for: {task}",
            }],
        )
        return synth_resp.content[0].text

if __name__ == "__main__":
    client = InstrumentedClient()
    result = run_pipeline(client, "research best practices for Python async code")
    print(result[:200])

    import json
    report = client.attribution_report()
    print("\n=== Step Attribution Report ===")
    print(f"Total tokens: {report['total_tokens']} ({report['total_input_tokens']} in / {report['total_output_tokens']} out)")
    for step in report["steps"]:
        print(f"  [{step['step']}] {step['total_tokens']} tokens ({step['pct_of_total']}) — {step['calls']} calls, avg {step['avg_ms']}")

# Expected Token Savings: Pinpoints step-level hotspots; typically 1-2 steps consume 60%+ of tokens
# Environment: pip install anthropic
```

## Option 3: Async Attribution with Concurrent Tool Tracking

In async agents that run tools concurrently, attribute token costs to each tool using asyncio task names. Track concurrent tool executions independently and merge results into a unified cost report after all tasks complete.

```python
import anthropic
import asyncio
import json
import time
from dataclasses import dataclass, field

@dataclass
class AsyncToolRecord:
    tool_name: str
    start_time: float
    end_time: float
    input_tokens: int
    output_tokens: int
    result_chars: int

_async_records: list[AsyncToolRecord] = []
_records_lock = asyncio.Lock()

async def tracked_tool_call(
    client: anthropic.AsyncAnthropic,
    tool_name: str,
    tool_prompt: str,
    model: str = "claude-haiku-4-5-20251001",
) -> tuple[str, AsyncToolRecord]:
    start = time.monotonic()
    response = await client.messages.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": tool_prompt}],
    )
    end = time.monotonic()
    result = response.content[0].text

    record = AsyncToolRecord(
        tool_name=tool_name,
        start_time=start,
        end_time=end,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        result_chars=len(result),
    )
    async with _records_lock:
        _async_records.append(record)
    return result, record

async def run_parallel_tools(user_query: str) -> str:
    client = anthropic.AsyncAnthropic()

    # Simulate parallel tool calls
    tool_tasks = [
        tracked_tool_call(client, "web_search", f"Search the web for: {user_query}"),
        tracked_tool_call(client, "database_lookup", f"Query internal database for: {user_query}"),
        tracked_tool_call(client, "api_call", f"Call external API to get data about: {user_query}"),
    ]

    tool_results = await asyncio.gather(*tool_tasks)
    combined_context = "\n\n".join(f"[{name}]: {result[:300]}" for (result, record), name in
                                   zip(tool_results, ["web_search", "database_lookup", "api_call"]))

    # Final synthesis
    synthesis_start = time.monotonic()
    synthesis = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"Context:\n{combined_context}\n\nAnswer: {user_query}",
        }],
    )
    _async_records.append(AsyncToolRecord(
        tool_name="synthesis",
        start_time=synthesis_start,
        end_time=time.monotonic(),
        input_tokens=synthesis.usage.input_tokens,
        output_tokens=synthesis.usage.output_tokens,
        result_chars=len(synthesis.content[0].text),
    ))
    return synthesis.content[0].text

def print_async_attribution():
    total_tokens = sum(r.input_tokens + r.output_tokens for r in _async_records)
    print("\n=== Async Token Attribution ===")
    for r in sorted(_async_records, key=lambda x: x.input_tokens + x.output_tokens, reverse=True):
        total = r.input_tokens + r.output_tokens
        pct = total / total_tokens * 100 if total_tokens else 0
        duration_ms = (r.end_time - r.start_time) * 1000
        print(f"  [{r.tool_name:20s}] {r.input_tokens:4d}in + {r.output_tokens:4d}out = {total:5d} total ({pct:5.1f}%) | {duration_ms:5.0f}ms | {r.result_chars} result chars")
    print(f"  {'TOTAL':20s} {total_tokens:5d} tokens")

async def main():
    result = await run_parallel_tools("best practices for distributed system observability")
    print(f"Result: {result[:150]}...")
    print_async_attribution()

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Identifies which concurrent tools to parallelize vs. short-circuit
# Environment: pip install anthropic
```

## Option 4: SQLite-Backed Attribution Store with Query Interface

Persist all token attribution data to SQLite. Query it after runs to find the most expensive tools across many agent executions, spot trends, and generate cost reports by tool, session, or time window.

```python
import anthropic
import sqlite3
import json
import time
import uuid
from contextlib import contextmanager

DB_PATH = "/tmp/token_attribution.db"

def init_db(path: str = DB_PATH) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tool_usage (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                step_label TEXT,
                model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                duration_ms REAL,
                tool_input_chars INTEGER,
                tool_result_chars INTEGER,
                created_at REAL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_session ON tool_usage(session_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tool ON tool_usage(tool_name)")

def record_usage(
    session_id: str,
    tool_name: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    duration_ms: float,
    tool_input_chars: int = 0,
    tool_result_chars: int = 0,
    step_label: str = "",
    path: str = DB_PATH,
) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("""
            INSERT INTO tool_usage (id, session_id, tool_name, step_label, model,
                input_tokens, output_tokens, duration_ms, tool_input_chars, tool_result_chars, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (str(uuid.uuid4()), session_id, tool_name, step_label, model,
              input_tokens, output_tokens, duration_ms, tool_input_chars, tool_result_chars, time.time()))

def query_attribution_report(session_id: str = None, path: str = DB_PATH) -> list[dict]:
    where = "WHERE session_id = ?" if session_id else ""
    params = (session_id,) if session_id else ()
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"""
            SELECT tool_name,
                   COUNT(*) as calls,
                   SUM(input_tokens) as total_input,
                   SUM(output_tokens) as total_output,
                   SUM(input_tokens + output_tokens) as total_tokens,
                   AVG(duration_ms) as avg_ms,
                   AVG(tool_result_chars) as avg_result_chars
            FROM tool_usage {where}
            GROUP BY tool_name
            ORDER BY total_tokens DESC
        """, params).fetchall()
        return [dict(r) for r in rows]

def run_instrumented_agent(session_id: str, task: str) -> str:
    client = anthropic.Anthropic()
    tool_defs = [
        {"name": "search", "description": "Search for information", "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
        {"name": "calculate", "description": "Perform calculations", "input_schema": {"type": "object", "properties": {"expr": {"type": "string"}}, "required": ["expr"]}},
    ]
    messages = [{"role": "user", "content": task}]

    while True:
        start = time.monotonic()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tool_defs,
            messages=messages,
        )
        duration_ms = (time.monotonic() - start) * 1000

        if response.stop_reason != "tool_use":
            record_usage(session_id, "final_response", "claude-haiku-4-5-20251001",
                        response.usage.input_tokens, response.usage.output_tokens, duration_ms, step_label="final")
            return response.content[0].text if response.content else ""

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = json.dumps({"result": f"simulated {block.name} output for: {block.input}"})
                record_usage(session_id, f"tool:{block.name}", "claude-haiku-4-5-20251001",
                            response.usage.input_tokens // max(1, len([b for b in response.content if b.type == "tool_use"])),
                            response.usage.output_tokens // max(1, len([b for b in response.content if b.type == "tool_use"])),
                            duration_ms, tool_input_chars=len(json.dumps(block.input)),
                            tool_result_chars=len(result), step_label=block.name)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

if __name__ == "__main__":
    init_db()
    sid = str(uuid.uuid4())
    result = run_instrumented_agent(sid, "Search for Python best practices and calculate the ROI of code reviews.")
    print(f"Result: {result[:200]}")

    report = query_attribution_report(sid)
    print("\n=== SQLite Attribution Report ===")
    for row in report:
        print(f"  [{row['tool_name']}] {row['total_tokens']} tokens | {row['calls']} calls | avg {row['avg_ms']:.0f}ms")

# Expected Token Savings: Historical data reveals recurring expensive tools to cache or optimize
# Environment: pip install anthropic; uses stdlib sqlite3
```

## Option 5: Real-Time Token Budget Enforcement with Attribution

Set per-tool token budgets. Track actual usage against the budget in real time and cut off expensive tools mid-run before they exhaust the session budget. Report which tools hit their caps.

```python
import anthropic
import time
from dataclasses import dataclass, field

@dataclass
class TokenBudget:
    total: int
    per_tool_limits: dict[str, int] = field(default_factory=dict)
    _used: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _total_used: int = field(default=0, init=False, repr=False)

    def check_and_reserve(self, tool_name: str, estimated_tokens: int) -> bool:
        limit = self.per_tool_limits.get(tool_name, self.total)
        tool_used = self._used.get(tool_name, 0)
        if tool_used + estimated_tokens > limit:
            print(f"[Budget] {tool_name} would exceed per-tool limit ({tool_used}/{limit}). Skipping.")
            return False
        if self._total_used + estimated_tokens > self.total:
            print(f"[Budget] Total budget would be exceeded ({self._total_used}/{self.total}). Skipping {tool_name}.")
            return False
        return True

    def record(self, tool_name: str, actual_tokens: int) -> None:
        self._used[tool_name] = self._used.get(tool_name, 0) + actual_tokens
        self._total_used += actual_tokens

    def report(self) -> None:
        print(f"\n=== Budget Report ===")
        print(f"Total used: {self._total_used}/{self.total} ({self._total_used/self.total*100:.1f}%)")
        for tool, used in sorted(self._used.items(), key=lambda x: x[1], reverse=True):
            limit = self.per_tool_limits.get(tool, self.total)
            print(f"  {tool}: {used}/{limit} ({used/limit*100:.1f}%)")

def run_budget_aware_agent(task: str, budget: TokenBudget) -> str:
    client = anthropic.Anthropic()
    tools_to_run = ["web_search", "database_query", "api_lookup", "summarize"]
    results = {}

    for tool in tools_to_run:
        estimated = 300  # pre-flight estimate
        if not budget.check_and_reserve(tool, estimated):
            results[tool] = "[SKIPPED: budget exceeded]"
            continue

        start = time.monotonic()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": f"Execute {tool} for task: {task}"}],
        )
        actual = response.usage.input_tokens + response.usage.output_tokens
        budget.record(tool, actual)
        results[tool] = response.content[0].text
        print(f"[Budget] {tool}: used {actual} tokens (estimated {estimated})")

    # Final synthesis with remaining budget
    remaining = budget.total - budget._total_used
    if remaining < 100:
        return "Budget exhausted before synthesis."

    synthesis = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=min(256, remaining),
        messages=[{
            "role": "user",
            "content": f"Synthesize:\n" + "\n".join(f"[{k}]: {v[:100]}" for k, v in results.items() if "[SKIPPED" not in v) + f"\n\nAnswer: {task}",
        }],
    )
    budget.record("synthesis", synthesis.usage.input_tokens + synthesis.usage.output_tokens)
    return synthesis.content[0].text

if __name__ == "__main__":
    budget = TokenBudget(
        total=3000,
        per_tool_limits={
            "web_search": 800,
            "database_query": 600,
            "api_lookup": 400,
            "summarize": 500,
            "synthesis": 600,
        },
    )
    result = run_budget_aware_agent("explain token attribution in AI agents", budget)
    print(f"\nResult: {result[:200]}")
    budget.report()

# Expected Token Savings: Hard caps prevent runaway tools from consuming entire session budget
# Environment: pip install anthropic
```

## Option 6: Streaming Token Attribution with Live Dashboard

Stream responses and track token usage in real time, updating a live cost dashboard per tool as chunks arrive. Enables immediate intervention when a tool generates unexpectedly large output.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

@dataclass
class LiveToolMetrics:
    tool_name: str
    tokens_streamed: int = 0
    chunks_received: int = 0
    start_time: float = field(default_factory=time.monotonic)
    finished: bool = False

    @property
    def tokens_per_second(self) -> float:
        elapsed = time.monotonic() - self.start_time
        return self.tokens_streamed / elapsed if elapsed > 0 else 0

    @property
    def elapsed_ms(self) -> float:
        return (time.monotonic() - self.start_time) * 1000

async def stream_with_attribution(
    client: anthropic.AsyncAnthropic,
    tool_name: str,
    prompt: str,
    metrics_store: dict[str, LiveToolMetrics],
    token_limit: int = 500,
) -> str:
    metrics = LiveToolMetrics(tool_name=tool_name)
    metrics_store[tool_name] = metrics
    full_text = ""

    async with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=token_limit,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for text in stream.text_stream:
            chunk_tokens = max(1, len(text) // 4)
            metrics.tokens_streamed += chunk_tokens
            metrics.chunks_received += 1
            full_text += text

            # Live warning if a tool is burning tokens fast
            if metrics.tokens_streamed > token_limit * 0.8:
                print(f"  ⚠ [{tool_name}] approaching limit: {metrics.tokens_streamed}/{token_limit} tokens")

        final = await stream.get_final_message()
        metrics.tokens_streamed = final.usage.output_tokens  # use actual count
        metrics.finished = True

    print(f"  ✓ [{tool_name}] {metrics.tokens_streamed} tokens in {metrics.elapsed_ms:.0f}ms ({metrics.tokens_per_second:.1f} tok/s)")
    return full_text

async def run_streaming_pipeline(task: str) -> str:
    client = anthropic.AsyncAnthropic()
    metrics_store: dict[str, LiveToolMetrics] = {}

    # Concurrent streaming tool calls
    results = await asyncio.gather(
        stream_with_attribution(client, "research", f"Research: {task}", metrics_store),
        stream_with_attribution(client, "analyze", f"Analyze implications of: {task}", metrics_store),
        stream_with_attribution(client, "examples", f"Give 3 concrete examples for: {task}", metrics_store, token_limit=300),
    )

    print("\n[Live Dashboard] All tools complete:")
    total = sum(m.tokens_streamed for m in metrics_store.values())
    for name, m in sorted(metrics_store.items(), key=lambda x: x[1].tokens_streamed, reverse=True):
        pct = m.tokens_streamed / total * 100 if total else 0
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        print(f"  {name:12s} [{bar}] {m.tokens_streamed:4d} tok ({pct:5.1f}%)")

    synthesis = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Combine:\n1. {results[0][:150]}\n2. {results[1][:150]}\n3. {results[2][:150]}\n\nFinal answer for: {task}",
        }],
    )
    return synthesis.content[0].text

if __name__ == "__main__":
    result = asyncio.run(run_streaming_pipeline("token attribution in AI agents"))
    print(f"\nFinal: {result[:200]}")

# Expected Token Savings: Real-time visibility enables early termination of unexpectedly verbose tools
# Environment: pip install anthropic
```

## Comparison

| Option | Storage | Query Interface | Real-Time | Best For |
|--------|---------|----------------|-----------|----------|
| 1. Token Delta | Memory | Print report | No | Single-run cost analysis |
| 2. Middleware Wrapper | Memory | Dict query | No | Step-by-step pipeline tracing |
| 3. Async Attribution | Memory | Print report | Concurrent | Parallel tool cost tracking |
| 4. SQLite Store | SQLite | SQL queries | No | Multi-run historical analysis |
| 5. Budget Enforcement | Memory | Print report | Enforced | Cost-cap sensitive workloads |
| 6. Streaming Dashboard | Memory | Live display | Yes | Real-time monitoring |
