---
layout: solution
title: "Agent Doesn't Implement Loop Budget with Forced Exit"
category: loop-stuck
description: "Prevent infinite agent loops by enforcing a hard iteration budget, wall-clock time limit, and cost cap — with a forced graceful exit that returns partial results instead of hanging forever."
tags: [loop, budget, forced-exit, infinite-loop, iteration-limit, timeout]
---

# Agent Doesn't Implement Loop Budget with Forced Exit

An agent without an iteration budget runs indefinitely when it gets stuck in a reasoning loop, retry cycle, or tool-call spiral. The process hangs, burns API credits, and never returns to the user. A loop budget enforces a hard cap on iterations, wall-clock time, and API cost — and when any limit is hit, the agent exits gracefully with whatever partial result it has accumulated.

## Option 1: Simple Iteration Counter with Forced Exit

```python
import anthropic

client = anthropic.Anthropic()
MAX_ITERATIONS = 10


def run_agent_loop(goal: str) -> str:
    messages = [{"role": "user", "content": goal}]
    partial_result = ""

    for iteration in range(MAX_ITERATIONS):
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system="You are a task execution agent. When the task is complete, start your response with DONE:",
            messages=messages,
        )
        reply = r.content[0].text
        partial_result = reply

        print(f"[ITER {iteration+1}/{MAX_ITERATIONS}] {reply[:60]}...")

        if reply.strip().startswith("DONE:"):
            print(f"[LOOP] Completed at iteration {iteration+1}")
            return reply[5:].strip()

        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user", "content": "Continue."})

    # Budget exhausted — forced exit with partial result
    print(f"[LOOP] Budget exhausted after {MAX_ITERATIONS} iterations — forcing exit")
    r_final = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[
            {"role": "user", "content": f"Original goal: {goal}\n\nPartial progress:\n{partial_result}\n\nSummarize what was accomplished so far in one paragraph."},
        ],
    )
    return f"[PARTIAL] {r_final.content[0].text}"


if __name__ == "__main__":
    result = run_agent_loop("Analyze the pros and cons of 5 different Python async libraries.")
    print("\n=== Final Result ===\n", result[:400])

# Expected Token Savings: Hard limit prevents runaway loops; partial exit beats hanging indefinitely
# Environment: Python 3.9+; tune MAX_ITERATIONS based on your task complexity
```

## Option 2: Wall-Clock Time Budget with asyncio.wait_for

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()

WALL_CLOCK_LIMIT = 30.0  # seconds
MAX_ITERATIONS = 20


async def agent_iteration(messages: list[dict]) -> str:
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system="Complete tasks step by step. When done, write COMPLETE: <summary>",
        messages=messages,
    )
    return r.content[0].text


async def timed_agent_loop(goal: str) -> str:
    messages = [{"role": "user", "content": goal}]
    results: list[str] = []
    start = time.monotonic()

    for iteration in range(MAX_ITERATIONS):
        elapsed = time.monotonic() - start
        remaining = WALL_CLOCK_LIMIT - elapsed

        if remaining <= 0:
            print(f"[LOOP] Wall-clock limit reached at iteration {iteration+1}")
            break

        try:
            reply = await asyncio.wait_for(
                agent_iteration(messages),
                timeout=min(remaining, 10.0),
            )
        except asyncio.TimeoutError:
            print(f"[LOOP] Single iteration timed out at {elapsed:.1f}s")
            break

        results.append(reply)
        print(f"[ITER {iteration+1}] elapsed={elapsed:.1f}s: {reply[:50]}...")

        if "COMPLETE:" in reply:
            return reply.split("COMPLETE:", 1)[1].strip()

        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user", "content": "Continue to next step."})

    total_elapsed = time.monotonic() - start
    print(f"[LOOP] Forced exit after {total_elapsed:.1f}s, {len(results)} iterations")
    return f"[PARTIAL after {total_elapsed:.1f}s] " + (results[-1][:200] if results else "No result")


async def main() -> None:
    result = await timed_agent_loop("List and briefly describe 10 Python design patterns.")
    print("\n=== Result ===\n", result)


asyncio.run(main())

# Expected Token Savings: Time budget prevents cost overruns from slow API responses
# Environment: Python 3.11+; WALL_CLOCK_LIMIT should be 2-3x your expected task duration
```

## Option 3: Triple Budget (Iterations + Time + Cost) with SQLite Audit

```python
import asyncio
import sqlite3
import time
import anthropic
from dataclasses import dataclass, field

DB_PATH = "loop_budget.db"
client = anthropic.AsyncAnthropic()


@dataclass
class BudgetConfig:
    max_iterations: int = 15
    max_seconds: float = 60.0
    max_cost_usd: float = 0.10
    haiku_input_cost: float = 0.80 / 1_000_000    # per token
    haiku_output_cost: float = 4.00 / 1_000_000


@dataclass
class LoopState:
    config: BudgetConfig
    iterations: int = 0
    start_time: float = field(default_factory=time.monotonic)
    total_input_tokens: int = 0
    total_output_tokens: int = 0

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.start_time

    @property
    def estimated_cost(self) -> float:
        return (self.total_input_tokens * self.config.haiku_input_cost +
                self.total_output_tokens * self.config.haiku_output_cost)

    def check_budget(self) -> tuple[bool, str]:
        if self.iterations >= self.config.max_iterations:
            return False, f"iteration limit ({self.config.max_iterations})"
        if self.elapsed >= self.config.max_seconds:
            return False, f"time limit ({self.config.max_seconds:.0f}s)"
        if self.estimated_cost >= self.config.max_cost_usd:
            return False, f"cost limit (${self.config.max_cost_usd:.2f})"
        return True, "ok"


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS loop_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            goal TEXT, iterations INTEGER, elapsed REAL,
            cost_usd REAL, exit_reason TEXT, partial_result TEXT, ts REAL
        )
    """)
    conn.commit()
    return conn


async def budgeted_loop(goal: str, config: BudgetConfig | None = None) -> str:
    if config is None:
        config = BudgetConfig()

    state = LoopState(config=config)
    messages = [{"role": "user", "content": goal}]
    last_reply = ""
    conn = init_db()
    exit_reason = "complete"

    while True:
        ok, reason = state.check_budget()
        if not ok:
            print(f"[BUDGET] Forced exit: {reason} (cost=${state.estimated_cost:.4f})")
            exit_reason = reason
            break

        state.iterations += 1
        try:
            r = await asyncio.wait_for(
                client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=256,
                    system="Execute the task step by step. Write DONE: <result> when finished.",
                    messages=messages,
                ),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            exit_reason = "per-call timeout"
            break

        reply = r.content[0].text
        state.total_input_tokens += r.usage.input_tokens
        state.total_output_tokens += r.usage.output_tokens
        last_reply = reply

        print(f"[ITER {state.iterations}] cost=${state.estimated_cost:.4f} elapsed={state.elapsed:.1f}s")

        if "DONE:" in reply:
            exit_reason = "complete"
            last_reply = reply.split("DONE:", 1)[1].strip()
            break

        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user", "content": "Continue."})

    # Audit log
    conn.execute(
        "INSERT INTO loop_runs VALUES (NULL,?,?,?,?,?,?,?)",
        (goal[:100], state.iterations, state.elapsed, state.estimated_cost,
         exit_reason, last_reply[:200], time.time()),
    )
    conn.commit()
    conn.close()

    prefix = "" if exit_reason == "complete" else f"[PARTIAL — {exit_reason}] "
    return prefix + last_reply


async def main() -> None:
    result = await budgeted_loop(
        "Summarize the top 5 Python async patterns with code examples.",
        config=BudgetConfig(max_iterations=8, max_seconds=30.0, max_cost_usd=0.05),
    )
    print("\n=== Result ===\n", result[:400])


asyncio.run(main())

# Expected Token Savings: Cost cap prevents unexpected charges; SQLite audit enables post-mortem analysis
# Environment: Python 3.11+, SQLite3; update haiku_input_cost/haiku_output_cost if pricing changes
```

## Option 4: Progress-Gated Loop with Stall Detection

```python
import re
import asyncio
import hashlib
import anthropic

client = anthropic.AsyncAnthropic()

MAX_ITERATIONS = 12
STALL_WINDOW = 3   # if last N replies are too similar, force exit


def content_hash(text: str) -> str:
    # Normalize whitespace before hashing
    normalized = re.sub(r'\s+', ' ', text.lower().strip())
    return hashlib.md5(normalized.encode()).hexdigest()[:8]


def is_stalled(recent_replies: list[str], similarity_threshold: float = 0.85) -> bool:
    if len(recent_replies) < STALL_WINDOW:
        return False
    hashes = [content_hash(r) for r in recent_replies[-STALL_WINDOW:]]
    unique = len(set(hashes))
    return unique <= 1  # all identical


def measure_progress(prev: str, current: str) -> float:
    """Simple progress metric: new unique words added."""
    prev_words = set(re.findall(r'\w+', prev.lower()))
    curr_words = set(re.findall(r'\w+', current.lower()))
    new_words = curr_words - prev_words
    return len(new_words) / max(len(curr_words), 1)


async def progress_gated_loop(goal: str) -> str:
    messages = [{"role": "user", "content": goal}]
    replies: list[str] = []
    cumulative_output = ""

    for iteration in range(MAX_ITERATIONS):
        # Stall check
        if is_stalled(replies):
            print(f"[STALL] Detected stall at iteration {iteration+1} — forcing exit")
            break

        r = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system="Work through the task. Write FINISHED: <summary> when complete.",
            messages=messages,
        )
        reply = r.content[0].text
        replies.append(reply)

        # Measure progress
        progress = measure_progress(cumulative_output, reply)
        cumulative_output += " " + reply
        print(f"[ITER {iteration+1}] progress={progress:.1%} stall_check={[content_hash(r) for r in replies[-STALL_WINDOW:]]}")

        if "FINISHED:" in reply:
            return reply.split("FINISHED:", 1)[1].strip()

        # No progress for 2 consecutive turns — break early
        if iteration >= 2 and progress < 0.05:
            print(f"[STALL] Progress < 5% — breaking early at iteration {iteration+1}")
            break

        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user", "content": "Continue working on the task."})

    return f"[PARTIAL] {replies[-1][:200]}" if replies else "[NO OUTPUT]"


async def main() -> None:
    result = await progress_gated_loop(
        "Explain asyncio event loops, tasks, and coroutines in Python."
    )
    print("\n=== Result ===\n", result)


asyncio.run(main())

# Expected Token Savings: Stall detection exits early on repetition; saves 2-5 redundant iterations
# Environment: Python 3.11+; tune STALL_WINDOW and progress threshold for your task type
```

## Option 5: Tool-Call Loop Budget with Per-Tool Limits

```python
import asyncio
import anthropic
from collections import defaultdict

client = anthropic.AsyncAnthropic()

GLOBAL_TOOL_CALL_LIMIT = 20
PER_TOOL_LIMIT = 5  # max calls per individual tool


def make_tools() -> list[dict]:
    return [
        {"name": "search", "description": "Search the web",
         "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
        {"name": "calculate", "description": "Do math",
         "input_schema": {"type": "object", "properties": {"expr": {"type": "string"}}, "required": ["expr"]}},
        {"name": "finish", "description": "Complete the task with a final answer",
         "input_schema": {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}},
    ]


def simulate_tool(name: str, args: dict) -> str:
    if name == "search":
        return f"Search results for '{args['query']}': [result 1, result 2, result 3]"
    if name == "calculate":
        try:
            return str(eval(args["expr"], {"__builtins__": {}}, {}))
        except Exception:
            return "Calculation error"
    return "done"


async def tool_budgeted_loop(goal: str) -> str:
    messages = [{"role": "user", "content": goal}]
    tools = make_tools()
    total_calls = 0
    per_tool_calls: dict[str, int] = defaultdict(int)

    while total_calls < GLOBAL_TOOL_CALL_LIMIT:
        r = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )

        if r.stop_reason == "end_turn":
            text = next((b.text for b in r.content if hasattr(b, "text")), "")
            return text

        tool_uses = [b for b in r.content if b.type == "tool_use"]
        if not tool_uses:
            text = next((b.text for b in r.content if hasattr(b, "text")), "")
            return text

        messages.append({"role": "assistant", "content": r.content})
        tool_results = []

        for tool_use in tool_uses:
            name = tool_use.name
            total_calls += 1
            per_tool_calls[name] += 1

            print(f"[TOOL] {name} call #{per_tool_calls[name]} (total={total_calls}/{GLOBAL_TOOL_CALL_LIMIT})")

            # Per-tool limit check
            if per_tool_calls[name] > PER_TOOL_LIMIT:
                result = f"[BUDGET] Tool '{name}' call limit ({PER_TOOL_LIMIT}) exceeded — stopping this tool"
                print(f"[BUDGET] {result}")
            elif name == "finish":
                return tool_use.input.get("answer", "Task complete.")
            else:
                result = simulate_tool(name, tool_use.input)

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_use.id,
                "content": result,
            })

        messages.append({"role": "user", "content": tool_results})

    # Global limit hit
    print(f"[BUDGET] Global tool call limit ({GLOBAL_TOOL_CALL_LIMIT}) reached — forcing exit")
    r_final = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"Summarize what you found so far about: {goal}"}],
    )
    return f"[PARTIAL] {r_final.content[0].text}"


async def main() -> None:
    result = await tool_budgeted_loop("Research Python async frameworks and compare asyncio vs trio.")
    print("\n=== Result ===\n", result[:400])


asyncio.run(main())

# Expected Token Savings: Per-tool limits prevent single-tool spirals; global cap is final backstop
# Environment: Python 3.11+; set PER_TOOL_LIMIT based on your tool's expected call frequency
```

## Option 6: Hierarchical Budget Manager with Escalation

```python
import asyncio
import time
import anthropic
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.AsyncAnthropic()


class ExitReason(Enum):
    COMPLETE    = "complete"
    ITER_LIMIT  = "iteration_limit"
    TIME_LIMIT  = "time_limit"
    COST_LIMIT  = "cost_limit"
    STALL       = "stall_detected"
    ERROR       = "error"


@dataclass
class LoopResult:
    output: str
    exit_reason: ExitReason
    iterations: int
    elapsed: float
    cost_usd: float


@dataclass
class BudgetManager:
    max_iters: int = 10
    max_secs: float = 45.0
    max_cost: float = 0.05
    input_rate: float = 0.80 / 1_000_000
    output_rate: float = 4.00 / 1_000_000

    _iters: int = 0
    _start: float = field(default_factory=time.monotonic)
    _input_tokens: int = 0
    _output_tokens: int = 0

    def record(self, input_tok: int, output_tok: int) -> None:
        self._iters += 1
        self._input_tokens += input_tok
        self._output_tokens += output_tok

    @property
    def cost(self) -> float:
        return self._input_tokens * self.input_rate + self._output_tokens * self.output_rate

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._start

    def check(self) -> ExitReason | None:
        if self._iters >= self.max_iters:
            return ExitReason.ITER_LIMIT
        if self.elapsed >= self.max_secs:
            return ExitReason.TIME_LIMIT
        if self.cost >= self.max_cost:
            return ExitReason.COST_LIMIT
        return None

    def summary(self) -> str:
        return f"iters={self._iters}/{self.max_iters} elapsed={self.elapsed:.1f}s cost=${self.cost:.4f}"


async def budgeted_agent(goal: str, budget: BudgetManager | None = None) -> LoopResult:
    if budget is None:
        budget = BudgetManager()

    messages = [{"role": "user", "content": goal}]
    last_output = ""
    prev_reply = ""

    while True:
        exit_r = budget.check()
        if exit_r:
            print(f"[BUDGET] {exit_r.value}: {budget.summary()}")
            return LoopResult(output=f"[{exit_r.value}] {last_output[:200]}",
                              exit_reason=exit_r, iterations=budget._iters,
                              elapsed=budget.elapsed, cost_usd=budget.cost)

        try:
            r = await asyncio.wait_for(
                client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=256,
                    system="Work step by step. Write COMPLETE: <answer> when done.",
                    messages=messages,
                ),
                timeout=8.0,
            )
        except asyncio.TimeoutError:
            return LoopResult(output=f"[timeout] {last_output[:200]}",
                              exit_reason=ExitReason.ERROR, iterations=budget._iters,
                              elapsed=budget.elapsed, cost_usd=budget.cost)

        reply = r.content[0].text
        budget.record(r.usage.input_tokens, r.usage.output_tokens)
        last_output = reply

        print(f"[LOOP] {budget.summary()} reply={reply[:50]}...")

        if "COMPLETE:" in reply:
            answer = reply.split("COMPLETE:", 1)[1].strip()
            return LoopResult(output=answer, exit_reason=ExitReason.COMPLETE,
                              iterations=budget._iters, elapsed=budget.elapsed,
                              cost_usd=budget.cost)

        if reply.strip() == prev_reply.strip():
            return LoopResult(output=f"[stall] {last_output[:200]}",
                              exit_reason=ExitReason.STALL, iterations=budget._iters,
                              elapsed=budget.elapsed, cost_usd=budget.cost)

        prev_reply = reply
        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user", "content": "Continue."})


async def main() -> None:
    result = await budgeted_agent(
        "List and explain 5 key principles of async Python programming.",
        budget=BudgetManager(max_iters=6, max_secs=25.0, max_cost=0.02),
    )
    print(f"\nExit: {result.exit_reason.value} | iters={result.iterations} | cost=${result.cost_usd:.4f}")
    print("Output:\n", result.output[:400])


asyncio.run(main())

# Expected Token Savings: Hierarchical budget catches all failure modes; LoopResult enables retry decisions
# Environment: Python 3.11+; use ExitReason to decide whether to retry with a larger budget
```

## Comparison

| Option | Iteration Limit | Time Limit | Cost Limit | Stall Detection | Audit Log | Best For |
|--------|----------------|-----------|-----------|----------------|-----------|----------|
| 1. Simple Counter | Yes | No | No | No | No | Minimal setup |
| 2. Wall-Clock | Yes | Yes | No | No | No | Latency-sensitive tasks |
| 3. Triple Budget | Yes | Yes | Yes | No | SQLite | Production cost control |
| 4. Progress-Gated | Yes | No | No | Yes | No | Repetition detection |
| 5. Tool-Call Budget | Per-tool + global | No | No | No | No | Tool-heavy agents |
| 6. Hierarchical Manager | Yes | Yes | Yes | Yes | Structured | Full production hardening |
