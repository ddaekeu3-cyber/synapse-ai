---
layout: solution
title: "Agent Doesn't Implement Adaptive Timeout Based on Request Complexity"
category: performance
description: "Agent applies the same fixed timeout to all requests — causing simple queries to wait unnecessarily long on hung calls, and complex multi-step tasks to be killed prematurely before they can complete."
tags: [performance, timeout, adaptive, reliability, asyncio, complexity-estimation]
---

# Agent Doesn't Implement Adaptive Timeout Based on Request Complexity

## Problem

A fixed timeout (e.g., 30 seconds) is simultaneously too long for a simple lookup and too short for a complex multi-step research task. Agents using fixed timeouts suffer from:

- **False failures**: complex tasks killed mid-execution at 30s when they need 90s
- **Hung resources**: simple lookups that get stuck keep a connection open for the full timeout duration
- **Poor user experience**: users wait for the full timeout on a failed simple request
- **Wasted compute**: partially-complete complex tasks timeout and restart from scratch

**Root cause:** The timeout is a static constant rather than a value estimated from the nature of the request.

---

## Option 1: Complexity Classifier — Estimate Timeout Before Execution

Use a cheap model call to classify request complexity and assign a timeout tier.

```python
import anthropic
import asyncio
import json
import time
from enum import Enum

client = anthropic.AsyncAnthropic()

class ComplexityTier(Enum):
    SIMPLE = "simple"       # Single lookup, factual Q&A
    MODERATE = "moderate"   # Multi-step reasoning, 2-3 tool calls
    COMPLEX = "complex"     # Research, analysis, 5+ tool calls
    DEEP = "deep"           # Code generation, long documents, 10+ steps

TIMEOUT_MAP = {
    ComplexityTier.SIMPLE: 15.0,
    ComplexityTier.MODERATE: 45.0,
    ComplexityTier.COMPLEX: 120.0,
    ComplexityTier.DEEP: 300.0,
}

CLASSIFIER_SYSTEM = """Classify the complexity of an AI agent request into one of: simple, moderate, complex, deep.

simple: single factual lookup, yes/no question, one-step task (< 15s)
moderate: multi-step reasoning, 2-3 tool calls required (~45s)
complex: research task, 5+ tool calls, analysis across multiple sources (~120s)
deep: code generation, document writing, 10+ steps (~300s)

Reply with ONLY one word: simple, moderate, complex, or deep"""

async def classify_complexity(query: str) -> ComplexityTier:
    """Use a cheap model to estimate request complexity."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        system=CLASSIFIER_SYSTEM,
        messages=[{"role": "user", "content": query[:200]}]
    )
    label = response.content[0].text.strip().lower()
    mapping = {
        "simple": ComplexityTier.SIMPLE,
        "moderate": ComplexityTier.MODERATE,
        "complex": ComplexityTier.COMPLEX,
        "deep": ComplexityTier.DEEP,
    }
    return mapping.get(label, ComplexityTier.MODERATE)

async def run_agent_task(query: str) -> str:
    """Simulate an agent task of variable duration."""
    await asyncio.sleep(0.1)  # Simulate work
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": query}]
    )
    return response.content[0].text

async def run_with_adaptive_timeout(query: str) -> str:
    # Step 1: Classify (fast, cheap)
    tier = await classify_complexity(query)
    timeout = TIMEOUT_MAP[tier]
    print(f"[adaptive] Complexity: {tier.value} → timeout: {timeout}s")

    # Step 2: Execute with adaptive timeout
    start = time.time()
    try:
        result = await asyncio.wait_for(run_agent_task(query), timeout=timeout)
        elapsed = time.time() - start
        print(f"[adaptive] Completed in {elapsed:.2f}s (budget: {timeout}s)")
        return result
    except asyncio.TimeoutError:
        elapsed = time.time() - start
        print(f"[adaptive] Timed out after {elapsed:.2f}s (budget was {timeout}s)")
        return f"Task timed out after {timeout}s. The request was classified as '{tier.value}'."

# Test with different complexities
queries = [
    "What is 2 + 2?",
    "Explain the trade-offs between SQL and NoSQL databases",
    "Research the history of distributed systems and summarize key milestones",
]

async def main():
    for q in queries:
        print(f"\nQuery: {q[:60]}...")
        result = await run_with_adaptive_timeout(q)
        print(f"Result: {result[:100]}...")

asyncio.run(main())

# Expected Token Savings: ~5% (classifier uses haiku; avoids expensive retries on prematurely timed-out complex tasks)
# Environment: Public-facing agents with diverse request complexity; API gateways with SLA tiers
```

---

## Option 2: Heuristic Complexity Scoring — No Extra Model Call

Score complexity from the query itself using heuristics (word count, tool mentions, question count).

```python
import anthropic
import asyncio
import re
import time

client = anthropic.AsyncAnthropic()

TOOL_KEYWORDS = [
    "search", "find", "look up", "fetch", "get", "retrieve", "check",
    "calculate", "compute", "analyze", "compare", "list", "enumerate"
]

COMPLEXITY_INDICATORS = {
    "simple": ["what is", "define", "who is", "when did", "yes or no", "true or false"],
    "deep": ["write a", "generate", "create a full", "implement", "build", "design the entire",
             "comprehensive", "detailed report", "step by step", "complete guide"]
}

def estimate_timeout_heuristic(query: str, base_timeout: float = 30.0) -> tuple[float, dict]:
    """Estimate timeout from query features without a model call."""
    q = query.lower()
    factors = {}

    # Word count
    word_count = len(query.split())
    factors["word_count"] = word_count
    word_multiplier = 1.0 + min(word_count / 50, 3.0)

    # Tool keyword count
    tool_hits = sum(1 for kw in TOOL_KEYWORDS if kw in q)
    factors["tool_keyword_hits"] = tool_hits
    tool_multiplier = 1.0 + tool_hits * 0.5

    # Question count (multiple questions = more work)
    question_count = q.count("?") + q.count(" and ") + q.count(", then")
    factors["question_count"] = question_count
    question_multiplier = 1.0 + question_count * 0.3

    # Simple/deep indicators
    is_simple = any(q.startswith(ind) for ind in COMPLEXITY_INDICATORS["simple"])
    is_deep = any(ind in q for ind in COMPLEXITY_INDICATORS["deep"])
    factors["is_simple"] = is_simple
    factors["is_deep"] = is_deep

    if is_simple:
        timeout = base_timeout * 0.4
    elif is_deep:
        timeout = base_timeout * 6.0
    else:
        timeout = base_timeout * word_multiplier * tool_multiplier * question_multiplier

    # Clamp to [5s, 300s]
    timeout = max(5.0, min(300.0, timeout))
    return timeout, factors

async def run_heuristic_timeout_agent(query: str) -> str:
    timeout, factors = estimate_timeout_heuristic(query)
    print(f"[heuristic] Timeout: {timeout:.1f}s | Factors: {factors}")

    async def agent_task() -> str:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": query}]
        )
        return response.content[0].text

    try:
        start = time.time()
        result = await asyncio.wait_for(agent_task(), timeout=timeout)
        print(f"[heuristic] Done in {time.time()-start:.2f}s")
        return result
    except asyncio.TimeoutError:
        return f"Timed out (estimated {timeout:.0f}s for this query complexity)"

TEST_QUERIES = [
    "What is the capital of France?",
    "Search for Python async patterns, find examples, and compare asyncio vs trio",
    "Write a comprehensive guide to building a production-ready microservices architecture",
]

async def main():
    for q in TEST_QUERIES:
        print(f"\n{'='*60}")
        print(f"Query: {q[:70]}...")
        result = await run_heuristic_timeout_agent(q)
        print(f"Result: {result[:80]}...")

asyncio.run(main())

# Expected Token Savings: ~0% (heuristic is pure local computation; no extra model calls)
# Environment: Low-latency agents where a pre-flight classification call is too expensive
```

---

## Option 3: Progressive Timeout with Mid-Task Extension

Start with a conservative timeout; if the task is still making progress at the deadline, extend it.

```python
import anthropic
import asyncio
import json
import time
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class ProgressTracker:
    last_activity: float = field(default_factory=time.time)
    tool_calls_made: int = 0
    turns_completed: int = 0
    last_tool_name: str = ""

    def record_activity(self, event: str = ""):
        self.last_activity = time.time()
        if event.startswith("tool:"):
            self.tool_calls_made += 1
            self.last_tool_name = event[5:]
        elif event == "turn":
            self.turns_completed += 1

    @property
    def seconds_since_activity(self) -> float:
        return time.time() - self.last_activity

    def is_making_progress(self, stall_threshold: float = 10.0) -> bool:
        return self.seconds_since_activity < stall_threshold

async def run_with_progressive_timeout(
    query: str,
    initial_timeout: float = 20.0,
    max_extensions: int = 3,
    extension_per_progress: float = 15.0,
    stall_timeout: float = 8.0
) -> str:
    tracker = ProgressTracker()
    total_budget = initial_timeout
    extensions_used = 0

    tools = [
        {
            "name": "research",
            "description": "Research a topic",
            "input_schema": {
                "type": "object",
                "properties": {"topic": {"type": "string"}},
                "required": ["topic"]
            }
        }
    ]

    messages = [{"role": "user", "content": query}]
    deadline = time.time() + initial_timeout
    result_text = ""

    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            # Check if we can extend
            if extensions_used < max_extensions and tracker.is_making_progress(stall_timeout):
                extension = extension_per_progress * (0.8 ** extensions_used)  # Diminishing extensions
                deadline = time.time() + extension
                extensions_used += 1
                print(f"[progressive] Extending timeout by {extension:.1f}s "
                      f"(extension {extensions_used}/{max_extensions}). "
                      f"Last activity: {tracker.last_tool_name or 'turn'}")
                remaining = extension
            else:
                stall_info = f"stalled for {tracker.seconds_since_activity:.1f}s" \
                    if not tracker.is_making_progress(stall_timeout) else "max extensions reached"
                print(f"[progressive] Hard stop: {stall_info}")
                return f"Task stopped after {total_budget + extensions_used * extension_per_progress:.0f}s budget. " \
                       f"Completed {tracker.turns_completed} turns, {tracker.tool_calls_made} tool calls."

        try:
            response = await asyncio.wait_for(
                client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=256,
                    tools=tools,
                    messages=messages
                ),
                timeout=min(remaining, 30.0)  # Per-call timeout
            )
        except asyncio.TimeoutError:
            # No activity from model — check if we should extend or stop
            if tracker.is_making_progress(stall_timeout):
                continue
            return f"Model call timed out. {tracker.turns_completed} turns completed."

        tracker.record_activity("turn")

        if response.stop_reason == "end_turn":
            result_text = next(b.text for b in response.content if hasattr(b, "text"))
            print(f"[progressive] Completed: {tracker.turns_completed} turns, "
                  f"{tracker.tool_calls_made} tools, {extensions_used} extensions")
            return result_text

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            tracker.record_activity(f"tool:{block.name}")
            await asyncio.sleep(0.1)  # Simulate tool work
            result = {"topic": block.input.get("topic"), "data": "mock research result"}
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result)
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return result_text or "Completed"

async def main():
    result = await run_with_progressive_timeout(
        "Research Python async frameworks: asyncio, trio, and curio. Compare them.",
        initial_timeout=5.0,  # Low for demo; would crash without extensions
        max_extensions=3,
        extension_per_progress=10.0
    )
    print(f"\nResult: {result[:200]}...")

asyncio.run(main())

# Expected Token Savings: ~20% (prevents premature termination of complex tasks; avoids expensive restarts)
# Environment: Agents with unpredictable execution time; research/analysis agents
```

---

## Option 4: Per-Step Timeout with Cumulative Budget

Apply a per-step timeout for each tool call and model turn; track cumulative budget across all steps.

```python
import anthropic
import asyncio
import json
import time

client = anthropic.AsyncAnthropic()

class BudgetedExecutor:
    def __init__(
        self,
        total_budget_s: float,
        per_model_call_s: float = 20.0,
        per_tool_call_s: float = 10.0
    ):
        self.total_budget = total_budget_s
        self.per_model_call = per_model_call_s
        self.per_tool_call = per_tool_call_s
        self.spent = 0.0
        self.log: list[dict] = []

    @property
    def remaining(self) -> float:
        return max(0, self.total_budget - self.spent)

    @property
    def is_exhausted(self) -> bool:
        return self.spent >= self.total_budget

    async def timed_model_call(self, **kwargs) -> object:
        if self.is_exhausted:
            raise asyncio.TimeoutError("Budget exhausted before model call")

        timeout = min(self.per_model_call, self.remaining)
        start = time.time()
        try:
            result = await asyncio.wait_for(
                client.messages.create(**kwargs),
                timeout=timeout
            )
            elapsed = time.time() - start
            self.spent += elapsed
            self.log.append({"type": "model", "duration_s": round(elapsed, 2), "budget_left": round(self.remaining, 2)})
            return result
        except asyncio.TimeoutError:
            self.spent += timeout
            raise asyncio.TimeoutError(f"Model call exceeded per-call limit ({timeout:.1f}s)")

    async def timed_tool_call(self, tool_name: str, coro) -> dict:
        if self.is_exhausted:
            return {"error": "Budget exhausted", "tool": tool_name}

        timeout = min(self.per_tool_call, self.remaining)
        start = time.time()
        try:
            result = await asyncio.wait_for(coro, timeout=timeout)
            elapsed = time.time() - start
            self.spent += elapsed
            self.log.append({"type": "tool", "tool": tool_name, "duration_s": round(elapsed, 2)})
            return result
        except asyncio.TimeoutError:
            self.spent += timeout
            self.log.append({"type": "tool_timeout", "tool": tool_name, "timeout_s": timeout})
            return {"error": f"Tool {tool_name} timed out after {timeout:.1f}s"}

async def mock_tool(tool_name: str, tool_input: dict) -> dict:
    # Simulate variable tool latency
    await asyncio.sleep(0.1)
    return {"tool": tool_name, "result": f"data_for_{tool_input}"}

async def run_budgeted_agent(query: str, total_budget: float = 60.0) -> str:
    executor = BudgetedExecutor(
        total_budget_s=total_budget,
        per_model_call_s=15.0,
        per_tool_call_s=8.0
    )

    tools = [
        {
            "name": "fetch",
            "description": "Fetch data",
            "input_schema": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"]
            }
        }
    ]

    messages = [{"role": "user", "content": query}]

    while not executor.is_exhausted:
        print(f"[budget] Remaining: {executor.remaining:.1f}s")
        try:
            response = await executor.timed_model_call(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                tools=tools,
                messages=messages
            )
        except asyncio.TimeoutError as e:
            return f"Stopped: {e}. Budget log: {executor.log}"

        if response.stop_reason == "end_turn":
            print(f"[budget] Done. Spent {executor.spent:.2f}s of {total_budget}s budget")
            print(f"[budget] Log: {executor.log}")
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result = await executor.timed_tool_call(
                block.name,
                mock_tool(block.name, block.input)
            )
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result)
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return f"Budget exhausted after {executor.spent:.1f}s. Log: {executor.log}"

result = asyncio.run(run_budgeted_agent(
    "Fetch data for keys: alpha, beta, gamma, delta. Then summarize.",
    total_budget=30.0
))
print(f"\nResult: {result[:200]}...")

# Expected Token Savings: ~15% (per-step budgets prevent runaway multi-tool tasks)
# Environment: Cost-sensitive agents; serverless agents with hard wall-clock limits (Lambda timeout)
```

---

## Option 5: SLA-Driven Timeout — Different Tiers for Different Customers

Apply timeout tiers based on customer SLA (free vs. pro vs. enterprise).

```python
import anthropic
import asyncio
import json
import time
from dataclasses import dataclass
from enum import Enum

client = anthropic.AsyncAnthropic()

class SLATier(Enum):
    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"

@dataclass
class SLAConfig:
    tier: SLATier
    max_timeout_s: float
    max_tool_calls: int
    max_turns: int
    priority: int  # Higher = processed first in queue

SLA_CONFIGS = {
    SLATier.FREE: SLAConfig(SLATier.FREE, max_timeout_s=15.0, max_tool_calls=3, max_turns=5, priority=1),
    SLATier.PRO: SLAConfig(SLATier.PRO, max_timeout_s=60.0, max_tool_calls=10, max_turns=15, priority=5),
    SLATier.ENTERPRISE: SLAConfig(SLATier.ENTERPRISE, max_timeout_s=300.0, max_tool_calls=50, max_turns=50, priority=10),
}

def get_sla(user_id: str) -> SLATier:
    """Lookup user SLA tier (simplified)."""
    if user_id.startswith("ent-"):
        return SLATier.ENTERPRISE
    if user_id.startswith("pro-"):
        return SLATier.PRO
    return SLATier.FREE

async def run_sla_constrained_agent(query: str, user_id: str) -> str:
    tier = get_sla(user_id)
    config = SLA_CONFIGS[tier]
    print(f"[sla] User {user_id} → {tier.value}: "
          f"timeout={config.max_timeout_s}s, "
          f"max_tools={config.max_tool_calls}, "
          f"max_turns={config.max_turns}")

    tools = [
        {
            "name": "lookup",
            "description": "Look up data",
            "input_schema": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"]
            }
        }
    ]

    messages = [{"role": "user", "content": query}]
    tool_call_count = 0
    turn_count = 0
    start = time.time()

    async def agent_loop() -> str:
        nonlocal tool_call_count, turn_count

        while True:
            if turn_count >= config.max_turns:
                return f"[SLA limit] Max turns ({config.max_turns}) reached for {tier.value} tier"

            turn_count += 1
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                tools=tools,
                messages=messages
            )

            if response.stop_reason == "end_turn":
                return next(b.text for b in response.content if hasattr(b, "text"))

            if response.stop_reason != "tool_use":
                break

            tool_blocks = [b for b in response.content if b.type == "tool_use"]
            if tool_call_count + len(tool_blocks) > config.max_tool_calls:
                remaining = config.max_tool_calls - tool_call_count
                tool_blocks = tool_blocks[:remaining]
                if not tool_blocks:
                    return f"[SLA limit] Max tool calls ({config.max_tool_calls}) reached for {tier.value} tier"

            tool_results = []
            for block in tool_blocks:
                tool_call_count += 1
                await asyncio.sleep(0.05)
                result = {"key": block.input.get("key"), "data": "mock"}
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result)
                })

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

        return "Done"

    try:
        result = await asyncio.wait_for(agent_loop(), timeout=config.max_timeout_s)
        elapsed = time.time() - start
        print(f"[sla] Completed: {turn_count} turns, {tool_call_count} tools, {elapsed:.2f}s")
        return result
    except asyncio.TimeoutError:
        return f"[SLA timeout] {tier.value} tier limit ({config.max_timeout_s}s) exceeded. Upgrade for longer tasks."

async def main():
    queries = [
        ("free-123", "Look up alpha, beta, gamma, delta, epsilon and summarize"),
        ("pro-456", "Look up alpha, beta, gamma, delta, epsilon and summarize"),
        ("ent-789", "Look up alpha, beta, gamma, delta, epsilon and summarize"),
    ]
    for user_id, q in queries:
        print(f"\n{'='*50}")
        result = await run_sla_constrained_agent(q, user_id)
        print(f"Result: {result[:100]}...")

asyncio.run(main())

# Expected Token Savings: ~30% for free tier (hard limits prevent free users from consuming enterprise-level resources)
# Environment: Multi-tenant SaaS agents with tiered pricing; public APIs with rate limits per plan
```

---

## Option 6: Learned Timeout from Historical Execution Data

Track actual execution times per query pattern; predict timeout from historical P95 latency.

```python
import anthropic
import asyncio
import json
import sqlite3
import time
import math
from pathlib import Path

client = anthropic.AsyncAnthropic()
LATENCY_DB = Path("/tmp/agent_latency_history.db")

def init_latency_db() -> sqlite3.Connection:
    conn = sqlite3.connect(LATENCY_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS executions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query_hash TEXT NOT NULL,
            query_length INTEGER,
            tool_call_count INTEGER,
            duration_s REAL NOT NULL,
            status TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hash ON executions(query_hash)")
    conn.commit()
    return conn

def query_fingerprint(query: str) -> str:
    """Create a coarse fingerprint for grouping similar queries."""
    words = query.lower().split()
    length_bucket = len(words) // 10  # Bucket by tens of words
    has_code = any(w in query.lower() for w in ["code", "script", "implement", "write"])
    has_search = any(w in query.lower() for w in ["search", "find", "look"])
    has_compare = any(w in query.lower() for w in ["compare", "versus", "vs", "difference"])
    return f"len{length_bucket}_code{int(has_code)}_search{int(has_search)}_compare{int(has_compare)}"

def estimate_timeout_from_history(conn: sqlite3.Connection, query: str) -> float:
    fingerprint = query_fingerprint(query)
    rows = conn.execute("""
        SELECT duration_s FROM executions
        WHERE query_hash = ? AND status = 'ok'
        ORDER BY created_at DESC LIMIT 20
    """, (fingerprint,)).fetchall()

    if len(rows) < 3:
        # Not enough history — use length-based heuristic
        word_count = len(query.split())
        fallback = max(15.0, min(120.0, word_count * 1.5))
        print(f"[learned] No history for {fingerprint}, using fallback: {fallback:.0f}s")
        return fallback

    durations = sorted(r[0] for r in rows)
    # Use P95 + 20% buffer as the timeout
    p95_idx = int(len(durations) * 0.95)
    p95 = durations[min(p95_idx, len(durations) - 1)]
    timeout = p95 * 1.2
    timeout = max(10.0, min(300.0, timeout))
    print(f"[learned] History for {fingerprint}: P95={p95:.2f}s → timeout={timeout:.1f}s ({len(rows)} samples)")
    return timeout

def record_execution(conn: sqlite3.Connection, query: str, tool_calls: int, duration: float, status: str):
    conn.execute(
        "INSERT INTO executions (query_hash, query_length, tool_call_count, duration_s, status) VALUES (?, ?, ?, ?, ?)",
        (query_fingerprint(query), len(query.split()), tool_calls, duration, status)
    )
    conn.commit()

conn = init_latency_db()

# Seed some historical data
for _ in range(5):
    conn.execute(
        "INSERT INTO executions (query_hash, query_length, tool_call_count, duration_s, status) VALUES (?, ?, ?, ?, ?)",
        ("len0_code0_search1_compare0", 5, 2, 3.5 + math.sin(_ * 0.7), "ok")
    )
conn.commit()

async def run_learned_timeout_agent(query: str) -> str:
    timeout = estimate_timeout_from_history(conn, query)

    tools = [
        {
            "name": "search",
            "description": "Search for information",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"]
            }
        }
    ]

    messages = [{"role": "user", "content": query}]
    tool_call_count = 0
    start = time.time()

    async def agent_loop() -> str:
        nonlocal tool_call_count
        while True:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                tools=tools,
                messages=messages
            )
            if response.stop_reason == "end_turn":
                return next(b.text for b in response.content if hasattr(b, "text"))
            if response.stop_reason != "tool_use":
                return "Done"

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                tool_call_count += 1
                await asyncio.sleep(0.08)
                result = {"query": block.input.get("query"), "result": "mock search result"}
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result)
                })

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

    try:
        result = await asyncio.wait_for(agent_loop(), timeout=timeout)
        duration = time.time() - start
        record_execution(conn, query, tool_call_count, duration, "ok")
        print(f"[learned] Recorded: {duration:.2f}s, {tool_call_count} tools")
        return result
    except asyncio.TimeoutError:
        duration = time.time() - start
        record_execution(conn, query, tool_call_count, duration, "timeout")
        print(f"[learned] Timeout recorded for future calibration")
        return f"Timed out after {timeout:.1f}s"

async def main():
    queries = [
        "Search for Python async best practices",
        "Find and compare the top 5 Python web frameworks",
    ]
    for q in queries:
        print(f"\nQuery: {q}")
        result = await run_learned_timeout_agent(q)
        print(f"Result: {result[:100]}...")

asyncio.run(main())

# Expected Token Savings: ~25% (learned timeouts prevent both premature kills and runaway waits)
# Environment: Mature production agents with weeks of execution data; agents with consistent query patterns
```

---

## Comparison

| Option | Timeout Source | Extra Cost | Learns Over Time | SLA-Aware | Best For |
|--------|---------------|------------|-----------------|-----------|----------|
| 1. Complexity Classifier | LLM classification | ~1 haiku call | No | No | Diverse query types |
| 2. Heuristic Scoring | Query features | None | No | No | Low-latency, no pre-flight cost |
| 3. Progressive Extension | Activity monitoring | None | No | No | Unpredictable-length tasks |
| 4. Per-Step Budget | Cumulative time | None | No | No | Serverless/Lambda with hard limits |
| 5. SLA-Driven | Customer tier | None | No | Yes | Multi-tenant SaaS with pricing tiers |
| 6. Learned from History | SQLite P95 | SQLite write | Yes | No | Mature production with stable patterns |
