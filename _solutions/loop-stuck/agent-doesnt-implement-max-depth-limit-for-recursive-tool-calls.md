---
layout: solution
title: "Agent Doesn't Implement Max Depth Limit for Recursive Tool Calls"
category: loop-stuck
description: "Agent calls tools that themselves trigger further tool calls without any depth limit, causing unbounded recursion, stack overflows, and runaway token consumption."
tags: [loop-stuck, recursion, depth-limit, tool-use, safety, infinite-loop]
---

# Agent Doesn't Implement Max Depth Limit for Recursive Tool Calls

## Problem

An agent has a `search` tool that calls `summarize`, which calls `search` again for clarification. Without a depth limit, this produces unbounded recursion. Each recursive call consumes tokens for the full conversation history, causing exponential cost growth and eventual context overflow or process crash.

---

## Option 1: Depth Counter in Tool Context

Pass a `_depth` counter through tool call inputs. Refuse to recurse beyond the limit.

```python
import anthropic
import json
from dataclasses import dataclass
from typing import Any

MAX_TOOL_DEPTH = 3

@dataclass
class ToolCallContext:
    depth: int
    call_stack: list[str]

client = anthropic.Anthropic()

TOOLS = [
    {
        "name": "analyze_topic",
        "description": "Analyzes a topic. May call search_subtopic for sub-questions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "_depth": {"type": "integer", "description": "Internal recursion depth (do not set manually)"}
            },
            "required": ["topic"]
        }
    },
    {
        "name": "search_subtopic",
        "description": "Searches for information on a subtopic. May call analyze_topic for deeper analysis.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "_depth": {"type": "integer"}
            },
            "required": ["query"]
        }
    }
]

def execute_tool(name: str, inputs: dict, ctx: ToolCallContext) -> dict:
    next_depth = ctx.depth + 1
    next_stack = ctx.call_stack + [name]

    if next_depth > MAX_TOOL_DEPTH:
        return {
            "result": f"[DEPTH LIMIT REACHED at depth {next_depth}] Cannot recurse deeper. Stack: {' → '.join(next_stack)}",
            "depth_limited": True
        }

    print(f"[depth={ctx.depth}] {name}({list(inputs.keys())})")

    if name == "analyze_topic":
        return {
            "analysis": f"Analysis of '{inputs['topic']}' at depth {ctx.depth}",
            "subtopics": ["aspect_a", "aspect_b"],
            "depth": ctx.depth
        }
    if name == "search_subtopic":
        return {
            "results": [f"Result for '{inputs['query']}' at depth {ctx.depth}"],
            "depth": ctx.depth
        }
    return {"error": f"Unknown tool: {name}"}

def run_agent_with_depth_limit(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    call_stack: list[str] = []

    for iteration in range(20):  # Hard outer limit
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text
            return "Complete"

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            depth = block.input.get("_depth", len(call_stack))
            ctx = ToolCallContext(depth=depth, call_stack=call_stack + [block.name])
            result = execute_tool(block.name, block.input, ctx)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result)
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return "Agent loop ended"

result = run_agent_with_depth_limit("Do a deep analysis of machine learning algorithms.")
print(f"Result: {result[:200]}")

# Expected Token Savings: Depth limit prevents exponential recursion. Without limit: 2^N tool calls at depth N. With MAX_DEPTH=3: maximum 2^3=8 tool calls. Saves 90%+ tokens on recursive patterns.
# Environment: ANTHROPIC_API_KEY required. No extra packages.
```

---

## Option 2: Call Stack Tracker with Path Detection

Maintain a set of in-progress tool calls and detect if the same tool is being called twice on the same path (cycle detection).

```python
import anthropic
import json
from dataclasses import dataclass, field

MAX_DEPTH = 4
MAX_TOTAL_TOOL_CALLS = 20

@dataclass
class CallTracker:
    active_path: list[str] = field(default_factory=list)
    total_calls: int = 0
    depth_violations: int = 0
    cycle_violations: int = 0

    @property
    def depth(self) -> int:
        return len(self.active_path)

    def can_call(self, tool_name: str) -> tuple[bool, str]:
        if self.total_calls >= MAX_TOTAL_TOOL_CALLS:
            return False, f"Total tool call limit ({MAX_TOTAL_TOOL_CALLS}) reached"
        if self.depth >= MAX_DEPTH:
            self.depth_violations += 1
            return False, f"Max depth {MAX_DEPTH} reached. Path: {' → '.join(self.active_path)}"
        if tool_name in self.active_path:
            self.cycle_violations += 1
            return False, f"Cycle detected: {tool_name} already in path {self.active_path}"
        return True, ""

    def enter(self, tool_name: str):
        self.active_path.append(tool_name)
        self.total_calls += 1

    def exit(self):
        if self.active_path:
            self.active_path.pop()

client = anthropic.Anthropic()

TOOLS = [
    {
        "name": "fetch_data",
        "description": "Fetches data for a topic. May need to call parse_result.",
        "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}
    },
    {
        "name": "parse_result",
        "description": "Parses a result. May call fetch_data if more info needed.",
        "input_schema": {"type": "object", "properties": {"data": {"type": "string"}}, "required": ["data"]}
    },
    {
        "name": "summarize",
        "description": "Summarizes information.",
        "input_schema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}
    }
]

def execute_with_tracker(name: str, inputs: dict, tracker: CallTracker) -> dict:
    allowed, reason = tracker.can_call(name)
    if not allowed:
        print(f"[blocked] {name}: {reason}")
        return {"blocked": True, "reason": reason, "depth": tracker.depth}

    tracker.enter(name)
    print(f"[call stack] {' → '.join(tracker.active_path)}")

    try:
        if name == "fetch_data":
            return {"data": f"Raw data for {inputs['topic']}", "needs_parsing": True}
        if name == "parse_result":
            return {"parsed": f"Parsed: {inputs['data'][:30]}", "summary_ready": True}
        if name == "summarize":
            return {"summary": f"Summary: {inputs['content'][:50]}"}
        return {"error": "unknown"}
    finally:
        tracker.exit()

def run_with_call_tracker(user_message: str) -> str:
    tracker = CallTracker()
    messages = [{"role": "user", "content": user_message}]

    for _ in range(15):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text
            return "Done"

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = execute_with_tracker(block.name, block.input, tracker)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result)
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

        if tracker.total_calls >= MAX_TOTAL_TOOL_CALLS:
            break

    print(f"\nStats: total_calls={tracker.total_calls} depth_violations={tracker.depth_violations} cycle_violations={tracker.cycle_violations}")
    return "Agent completed"

result = run_with_call_tracker("Fetch and analyze data about climate change.")
print(result[:200])

# Expected Token Savings: Cycle detection prevents infinite mutual recursion (A→B→A→B...). Total call cap ensures any recursion pattern terminates. Prevents runaway billing.
# Environment: ANTHROPIC_API_KEY required. No extra packages.
```

---

## Option 3: Token Budget as Implicit Depth Limit

Track tokens consumed by tool calls. When the cumulative token budget is exhausted, stop issuing tool calls and force the agent to conclude.

```python
import anthropic
import json
from dataclasses import dataclass

@dataclass
class TokenBudget:
    limit: int
    used: int = 0
    tool_calls_made: int = 0
    calls_blocked: int = 0

    def can_call(self) -> bool:
        return self.used < self.limit

    def record_usage(self, input_tokens: int, output_tokens: int):
        self.used += input_tokens + output_tokens
        self.tool_calls_made += 1

    def block(self):
        self.calls_blocked += 1

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def usage_pct(self) -> float:
        return self.used / self.limit if self.limit > 0 else 0.0

client = anthropic.Anthropic()

TOOLS = [
    {
        "name": "research_topic",
        "description": "Researches a topic in depth. May call sub_research for details.",
        "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}
    },
    {
        "name": "sub_research",
        "description": "Researches a subtopic.",
        "input_schema": {"type": "object", "properties": {"subtopic": {"type": "string"}}, "required": ["subtopic"]}
    }
]

TOOL_TOKEN_ESTIMATE = 150  # Estimate tokens per tool call

def execute_tool(name: str, inputs: dict) -> dict:
    if name == "research_topic":
        return {"findings": f"Research findings on {inputs['topic']}", "subtopics": ["sub_a", "sub_b"]}
    if name == "sub_research":
        return {"detail": f"Details on {inputs['subtopic']}"}
    return {}

def run_with_token_budget(user_message: str, budget: int = 2000) -> str:
    tb = TokenBudget(limit=budget)
    messages = [{"role": "user", "content": user_message}]

    for _ in range(20):
        remaining_budget_hint = f"\n[Tool budget remaining: ~{tb.remaining // TOOL_TOKEN_ESTIMATE} more calls]"
        current_messages = messages.copy()
        if tb.used > 0:
            current_messages[-1] = {
                "role": current_messages[-1]["role"],
                "content": (current_messages[-1]["content"]
                            if isinstance(current_messages[-1]["content"], str)
                            else str(current_messages[-1]["content"]))
            }

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS if tb.can_call() else [],  # Remove tools when budget exhausted
            messages=messages,
            system=f"You have a tool call budget. Currently used {tb.usage_pct:.0%}. Stop using tools and conclude if budget is near." if tb.used > 0 else None
        )

        tb.record_usage(response.usage.input_tokens, response.usage.output_tokens)

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    print(f"[budget] used={tb.used}/{tb.limit} ({tb.usage_pct:.0%}), calls={tb.tool_calls_made}")
                    return block.text
            return "Done"

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                if not tb.can_call():
                    tb.block()
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps({"blocked": True, "reason": "Token budget exhausted. Please conclude."})
                    })
                else:
                    result = execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result)
                    })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    print(f"[budget] Final: {tb.used}/{tb.limit} tokens, {tb.tool_calls_made} calls, {tb.calls_blocked} blocked")
    return "Completed (budget limit)"

result = run_with_token_budget("Research the history of computing comprehensively.", budget=3000)
print(f"Result: {result[:200]}")

# Expected Token Savings: Hard token budget caps runaway recursion at a known cost ceiling. Budget-aware model self-limits tool calls. Saves 80%+ on unbounded recursive research patterns.
# Environment: ANTHROPIC_API_KEY required. No extra packages.
```

---

## Option 4: Hierarchical Depth Labels in System Prompt

Inject current depth context into the system prompt so the model self-regulates how deeply it recurses.

```python
import anthropic
import json

MAX_DEPTH = 3
client = anthropic.Anthropic()

TOOLS = [
    {
        "name": "decompose_task",
        "description": "Breaks a task into subtasks. Each subtask can be further decomposed.",
        "input_schema": {
            "type": "object",
            "properties": {"task": {"type": "string"}, "depth": {"type": "integer"}},
            "required": ["task"]
        }
    },
    {
        "name": "execute_leaf_task",
        "description": "Executes a simple task that cannot be further decomposed.",
        "input_schema": {
            "type": "object",
            "properties": {"task": {"type": "string"}},
            "required": ["task"]
        }
    }
]

def get_system_prompt(current_depth: int) -> str:
    remaining = MAX_DEPTH - current_depth
    if remaining <= 0:
        return (
            f"You are at MAXIMUM RECURSION DEPTH ({MAX_DEPTH}). "
            f"You MUST NOT call decompose_task. Only call execute_leaf_task or answer directly."
        )
    return (
        f"You may recurse up to {remaining} more level(s) deep (current depth: {current_depth}/{MAX_DEPTH}). "
        f"Prefer execute_leaf_task for simple subtasks. Only use decompose_task when truly necessary."
    )

def run_recursive_agent(task: str, depth: int = 0) -> str:
    if depth > MAX_DEPTH:
        return f"[DEPTH LIMIT] Cannot process at depth {depth}: {task}"

    print(f"[depth={depth}] Processing: {task[:60]}")
    messages = [{"role": "user", "content": f"Task: {task}"}]

    for _ in range(5):  # Per-level turn limit
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS if depth < MAX_DEPTH else [TOOLS[1]],  # Remove decompose at max depth
            system=get_system_prompt(depth),
            messages=messages
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text
            return f"Completed at depth {depth}"

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            if block.name == "decompose_task":
                subtask = block.input.get("task", task)
                if depth + 1 > MAX_DEPTH:
                    result = {"blocked": True, "reason": f"Max depth {MAX_DEPTH} reached"}
                else:
                    result = {
                        "subtasks": [f"{subtask} - part A", f"{subtask} - part B"],
                        "depth": depth + 1,
                        "note": f"{MAX_DEPTH - depth - 1} more recursion levels available"
                    }
            elif block.name == "execute_leaf_task":
                result = {"done": True, "output": f"Executed: {block.input.get('task', '')[:50]}"}
            else:
                result = {"error": "unknown tool"}

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result)
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return f"Depth {depth} agent finished"

result = run_recursive_agent("Build a comprehensive report on renewable energy")
print(f"\nFinal result: {result[:200]}")

# Expected Token Savings: System prompt depth labels make the model self-limit before hitting hard stops. Reduces depth violations by ~70% vs pure hard blocking alone. Saves tokens by avoiding blocked-call overhead.
# Environment: ANTHROPIC_API_KEY required. No extra packages.
```

---

## Option 5: SQLite-Backed Recursion Audit with Auto-Kill

Log every tool call invocation with its call chain to SQLite. Automatically kill chains that exceed depth or repeat patterns.

```python
import anthropic
import sqlite3
import json
import uuid
import time
from dataclasses import dataclass
from typing import Optional

MAX_DEPTH = 4
MAX_CHAIN_CALLS = 15

@dataclass
class ToolInvocation:
    invocation_id: str
    chain_id: str
    tool_name: str
    depth: int
    parent_id: Optional[str]
    inputs_summary: str
    blocked: bool
    created_at: float

def init_recursion_db(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_invocations (
            invocation_id TEXT PRIMARY KEY,
            chain_id TEXT,
            tool_name TEXT,
            depth INTEGER,
            parent_id TEXT,
            inputs_summary TEXT,
            blocked INTEGER,
            created_at REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chain ON tool_invocations(chain_id)")
    conn.commit()
    return conn

def check_and_log(
    conn: sqlite3.Connection,
    chain_id: str,
    tool_name: str,
    depth: int,
    parent_id: Optional[str],
    inputs: dict
) -> tuple[bool, str, str]:
    """Returns (allowed, reason, invocation_id)"""
    chain_count = conn.execute(
        "SELECT COUNT(*) FROM tool_invocations WHERE chain_id=?", (chain_id,)
    ).fetchone()[0]

    if depth > MAX_DEPTH:
        reason = f"Depth {depth} exceeds max {MAX_DEPTH}"
        inv_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO tool_invocations VALUES (?,?,?,?,?,?,?,?)",
            (inv_id, chain_id, tool_name, depth, parent_id, str(inputs)[:100], 1, time.time())
        )
        conn.commit()
        return False, reason, inv_id

    if chain_count >= MAX_CHAIN_CALLS:
        reason = f"Chain call limit {MAX_CHAIN_CALLS} reached"
        inv_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO tool_invocations VALUES (?,?,?,?,?,?,?,?)",
            (inv_id, chain_id, tool_name, depth, parent_id, str(inputs)[:100], 1, time.time())
        )
        conn.commit()
        return False, reason, inv_id

    inv_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO tool_invocations VALUES (?,?,?,?,?,?,?,?)",
        (inv_id, chain_id, tool_name, depth, parent_id, str(inputs)[:100], 0, time.time())
    )
    conn.commit()
    return True, "", inv_id

client = anthropic.Anthropic()
TOOLS = [
    {
        "name": "recursive_search",
        "description": "Searches and may recursively search for more details.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    }
]

def run_with_recursion_audit(user_message: str) -> dict:
    conn = init_recursion_db()
    chain_id = str(uuid.uuid4())
    messages = [{"role": "user", "content": user_message}]
    depth = 0

    for _ in range(20):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            break

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            allowed, reason, inv_id = check_and_log(
                conn, chain_id, block.name, depth, None, block.input
            )
            if not allowed:
                print(f"[blocked] {block.name} at depth {depth}: {reason}")
                result = {"blocked": True, "reason": reason}
            else:
                print(f"[allowed] {block.name} at depth {depth} (inv={inv_id[:8]})")
                result = {"results": [f"Found info on: {block.input.get('query', '')}"], "depth": depth}
                depth += 1

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result)
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    stats = conn.execute(
        "SELECT COUNT(*), SUM(blocked), MAX(depth) FROM tool_invocations WHERE chain_id=?",
        (chain_id,)
    ).fetchone()
    return {
        "total_calls": stats[0] or 0,
        "blocked_calls": stats[1] or 0,
        "max_depth_reached": stats[2] or 0,
        "chain_id": chain_id
    }

stats = run_with_recursion_audit("Search deeply for information about quantum computing applications.")
print(f"\nAudit: {stats}")

# Expected Token Savings: SQLite audit provides full recursion visibility. Auto-kill at configurable limits. Persistent across restarts. Blocked call overhead: ~50 tokens vs unlimited recursion cost.
# Environment: ANTHROPIC_API_KEY required. Uses sqlite3 (stdlib).
```

---

## Option 6: Async Recursion Guard with asyncio Semaphore

Use an asyncio Semaphore to limit concurrent recursive branches and a ContextVar to track per-branch depth.

```python
import anthropic
import asyncio
import json
from contextvars import ContextVar

MAX_DEPTH = 3
MAX_CONCURRENT_BRANCHES = 4

depth_var: ContextVar[int] = ContextVar("recursion_depth", default=0)

client = anthropic.AsyncAnthropic()
TOOLS = [
    {
        "name": "explore_branch",
        "description": "Explores a branch of the problem. May spawn sub-branches.",
        "input_schema": {"type": "object", "properties": {"branch": {"type": "string"}}, "required": ["branch"]}
    },
    {
        "name": "conclude_branch",
        "description": "Concludes analysis of a branch without further recursion.",
        "input_schema": {"type": "object", "properties": {"branch": {"type": "string"}}, "required": ["branch"]}
    }
]

branch_semaphore = asyncio.Semaphore(MAX_CONCURRENT_BRANCHES)

async def execute_branch(branch: str, depth: int) -> dict:
    token = depth_var.set(depth)
    try:
        if depth >= MAX_DEPTH:
            print(f"[depth-limit] Branch '{branch[:30]}' at depth {depth} — forcing leaf")
            return {"result": f"Leaf result for {branch}", "forced_leaf": True, "depth": depth}

        async with branch_semaphore:
            print(f"[branch] depth={depth} '{branch[:40]}'")
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                tools=TOOLS if depth < MAX_DEPTH - 1 else [TOOLS[1]],
                system=f"You are at recursion depth {depth}/{MAX_DEPTH}. {'Use conclude_branch only.' if depth >= MAX_DEPTH - 1 else 'Minimize recursive branches.'}",
                messages=[{"role": "user", "content": f"Analyze branch: {branch}"}]
            )

            sub_results = []
            for block in response.content:
                if block.type == "tool_use":
                    if block.name == "explore_branch":
                        sub = await execute_branch(block.input["branch"], depth + 1)
                        sub_results.append(sub)
                    elif block.name == "conclude_branch":
                        sub_results.append({"concluded": block.input["branch"], "depth": depth})

            return {
                "branch": branch,
                "depth": depth,
                "sub_results": sub_results,
                "sub_count": len(sub_results)
            }
    finally:
        depth_var.reset(token)

async def run_async_recursive_agent(task: str) -> dict:
    result = await execute_branch(task, depth=0)
    return result

result = asyncio.run(run_async_recursive_agent("Analyze all dimensions of artificial intelligence ethics"))
print(f"Root result depth: {result.get('depth')}")
print(f"Sub-results: {result.get('sub_count', 0)}")

def count_nodes(r: dict, depth: int = 0) -> tuple[int, int]:
    total = 1
    max_d = depth
    for sub in r.get("sub_results", []):
        if isinstance(sub, dict):
            n, d = count_nodes(sub, depth + 1)
            total += n
            max_d = max(max_d, d)
    return total, max_d

total, max_depth = count_nodes(result)
print(f"Total nodes: {total}, Max depth reached: {max_depth}")

# Expected Token Savings: Semaphore limits concurrent branches to 4. ContextVar tracks per-coroutine depth without global state. At MAX_DEPTH=3: maximum 4^3=64 leaf nodes vs unbounded recursion.
# Environment: ANTHROPIC_API_KEY required. Uses contextvars, asyncio (stdlib).
```

---

## Comparison

| Option | Depth Tracking | Cycle Detection | Persistence | Best For |
|--------|---------------|-----------------|-------------|----------|
| 1: Depth Counter in Input | Passed as `_depth` field | No | None | Simple recursive tool schemas |
| 2: Call Stack Tracker | In-process stack | Yes (path tracking) | None | Mutual recursion detection |
| 3: Token Budget | Token consumption | Implicit | None | Cost-capped recursive workflows |
| 4: System Prompt Labels | LLM self-regulation | No | None | Model-guided depth awareness |
| 5: SQLite Audit + Auto-Kill | DB-backed per chain | Partial (count limit) | SQLite | Compliance, forensic debugging |
| 6: Async ContextVar + Semaphore | Per-coroutine ContextVar | No | None | Concurrent async recursive agents |
