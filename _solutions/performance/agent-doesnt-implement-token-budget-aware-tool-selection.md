---
title: "Agent Doesn't Implement Token-Budget-Aware Tool Selection"
description: "Agents that always execute all tools regardless of remaining token budget waste tokens on low-priority context, pushing the conversation toward the context window limit and increasing cost per turn."
difficulty: intermediate
category: performance
tags: [performance, token-budget, tools, cost-optimization, context-window, prioritization]
---

# Agent Doesn't Implement Token-Budget-Aware Tool Selection

## Problem

Every tool call adds tokens to the conversation: the tool result, schema descriptions, and intermediate reasoning. When an agent always invokes all configured tools regardless of context budget, low-priority results (rarely-used metadata, supplementary facts) consume tokens that crowd out the actual user content and force expensive context compression earlier. A token-budget-aware selector skips cheap or low-priority calls when the budget is tight.

**Symptoms:**
- Context window hits 80% capacity routinely on moderate-length conversations
- High-value tool results (search, database) compete with low-value ones (telemetry, extras)
- Same 12-tool configuration used for a simple yes/no question and a complex research task
- No mechanism to defer "nice to have" tools when input tokens are already large
- Cost per conversation grows linearly with number of tools regardless of relevance

---

## Solution 1: Priority-Based Tool Gating by Remaining Budget

Assign each tool a priority and a minimum token budget requirement; skip low-priority tools when budget is tight.

```python
import asyncio
from dataclasses import dataclass, field
from typing import Any, Optional
import anthropic


@dataclass
class ToolSpec:
    name: str
    description: str
    priority: int           # 1 = critical, 5 = optional
    estimated_result_tokens: int  # Typical tokens the result adds to context
    input_schema: dict = field(default_factory=dict)


TOOL_REGISTRY = [
    ToolSpec("search_web",       "Search the web for recent information",   priority=1, estimated_result_tokens=300),
    ToolSpec("query_database",   "Query user records from the database",    priority=1, estimated_result_tokens=200),
    ToolSpec("get_user_profile", "Fetch the user's profile and preferences",priority=2, estimated_result_tokens=150),
    ToolSpec("fetch_analytics",  "Retrieve usage analytics for this user",  priority=4, estimated_result_tokens=400),
    ToolSpec("load_recommendations","Load personalised product recommendations",priority=3, estimated_result_tokens=500),
    ToolSpec("get_telemetry",    "Fetch agent telemetry and debug data",    priority=5, estimated_result_tokens=600),
]

CONTEXT_WINDOW = 200_000   # claude-opus-4-6
BUDGET_THRESHOLDS = {
    1: 0.90,  # Critical tools: use up to 90% of window
    2: 0.75,
    3: 0.60,
    4: 0.40,
    5: 0.20,  # Optional tools: only run if <20% used
}


def select_tools(
    current_input_tokens: int,
    available_tools: list[ToolSpec] = TOOL_REGISTRY,
) -> list[ToolSpec]:
    """Return tools that fit within budget given current token usage."""
    usage_fraction = current_input_tokens / CONTEXT_WINDOW
    selected = []
    running_estimate = current_input_tokens

    for tool in sorted(available_tools, key=lambda t: t.priority):
        threshold = BUDGET_THRESHOLDS.get(tool.priority, 0.5)
        if usage_fraction <= threshold:
            # Check that adding this tool's result won't blow the budget
            if (running_estimate + tool.estimated_result_tokens) / CONTEXT_WINDOW < 0.95:
                selected.append(tool)
                running_estimate += tool.estimated_result_tokens
        else:
            print(
                f"[budget] Skipping '{tool.name}' (priority={tool.priority}) "
                f"— usage={usage_fraction:.0%} > threshold={threshold:.0%}"
            )
    return selected


class BudgetAwareAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    def _make_tool_schema(self, specs: list[ToolSpec]) -> list[dict]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema or {"type": "object", "properties": {}},
            }
            for t in specs
        ]

    async def respond(self, user_message: str, history_tokens: int = 0) -> str:
        # Estimate current input size: history + system + user message
        estimated_input = history_tokens + len(user_message.split()) * 1.3 + 500  # rough

        selected_tools = select_tools(int(estimated_input))
        print(f"[budget] Selected {len(selected_tools)}/{len(TOOL_REGISTRY)} tools "
              f"(estimated_input={int(estimated_input)} tokens)")

        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1024,
            tools=self._make_tool_schema(selected_tools),
            messages=[{"role": "user", "content": user_message}],
        )
        return response.content[0].text if response.content else ""


async def demo():
    agent = BudgetAwareAgent(api_key="sk-...")

    # Short conversation — all tools available
    r1 = await agent.respond("Hello!", history_tokens=500)
    print(f"Short: {r1[:60]}")

    # Long conversation — only high-priority tools
    r2 = await agent.respond("Summarize what we discussed.", history_tokens=160_000)
    print(f"Long: {r2[:60]}")

# asyncio.run(demo())
```

---

## Solution 2: Dynamic Tool Schema Truncation

When budget is tight, include the full schema only for high-priority tools; summarize lower-priority ones into a compact stub.

```python
import asyncio
from dataclasses import dataclass
from typing import Optional
import anthropic


@dataclass
class ToolDefinition:
    name: str
    full_description: str
    stub_description: str   # One-liner used when budget is tight
    priority: int
    full_schema: dict
    stub_schema: dict       # Minimal schema (no examples, no descriptions on fields)
    estimated_schema_tokens: int  # Tokens consumed by the full schema


TOOLS = [
    ToolDefinition(
        name="web_search",
        full_description="Search the web for current information on any topic. Returns titles, snippets, and URLs from top results.",
        stub_description="Web search.",
        priority=1,
        full_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
                "num_results": {"type": "integer", "description": "Number of results to return", "default": 5},
                "safe_search": {"type": "boolean", "description": "Enable safe search filtering", "default": True},
            },
            "required": ["query"],
        },
        stub_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        estimated_schema_tokens=80,
    ),
    ToolDefinition(
        name="code_executor",
        full_description="Execute Python code in a sandboxed environment and return stdout, stderr, and return value. Supports numpy, pandas, matplotlib.",
        stub_description="Execute Python code.",
        priority=2,
        full_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute"},
                "timeout_seconds": {"type": "integer", "description": "Max execution time", "default": 30},
                "capture_plots": {"type": "boolean", "description": "Return matplotlib figures as base64", "default": False},
            },
            "required": ["code"],
        },
        stub_schema={"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
        estimated_schema_tokens=120,
    ),
]


def build_tool_list(input_tokens: int, context_window: int = 200_000) -> list[dict]:
    budget_fraction = input_tokens / context_window
    tools = []
    for t in sorted(TOOLS, key=lambda x: x.priority):
        if budget_fraction < 0.5:
            # Full schema for all tools
            tools.append({
                "name": t.name,
                "description": t.full_description,
                "input_schema": t.full_schema,
            })
        elif budget_fraction < 0.75 or t.priority <= 1:
            # Stub schema for lower-priority tools
            tools.append({
                "name": t.name,
                "description": t.stub_description,
                "input_schema": t.stub_schema,
            })
        else:
            print(f"[schema] Omitting tool '{t.name}' — budget {budget_fraction:.0%}")
    return tools


class SchemaAwareAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def chat(self, message: str, estimated_input_tokens: int = 1000) -> str:
        tools = build_tool_list(estimated_input_tokens)
        print(f"[schema] Including {len(tools)} tool(s) with budget={estimated_input_tokens} tokens")

        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            tools=tools,
            messages=[{"role": "user", "content": message}],
        )
        return response.content[0].text if response.content else ""
```

---

## Solution 3: Token Cost Estimation Before Tool Execution

Before executing a tool call, estimate whether the result will fit in the remaining budget and skip if not.

```python
import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Optional
import anthropic


@dataclass
class ToolResult:
    name: str
    result: Any
    tokens_used: int
    skipped: bool = False
    skip_reason: str = ""


TOOL_TOKEN_ESTIMATES = {
    "search":     600,
    "database":   300,
    "profile":    150,
    "inventory": 1200,  # Large payload
    "summary":    200,
}


class TokenBudgetedExecutor:
    def __init__(
        self,
        context_window: int = 200_000,
        reserve_for_output: int = 4096,
    ):
        self.context_window = context_window
        self.reserve_for_output = reserve_for_output

    def available_tokens(self, current_input_tokens: int) -> int:
        return self.context_window - current_input_tokens - self.reserve_for_output

    async def run_tool(
        self,
        name: str,
        fn: Callable[[], Coroutine],
        current_input_tokens: int,
        force: bool = False,
    ) -> ToolResult:
        estimate = TOOL_TOKEN_ESTIMATES.get(name, 500)
        available = self.available_tokens(current_input_tokens)

        if not force and estimate > available * 0.3:  # Don't let one tool eat >30% of remainder
            reason = f"estimate={estimate} > 30% of available={available}"
            print(f"[budget] Skipping '{name}': {reason}")
            return ToolResult(name=name, result=None, tokens_used=0, skipped=True, skip_reason=reason)

        result = await fn()
        # Count actual tokens in result
        actual_tokens = len(json.dumps(result)) // 4  # rough estimate
        print(f"[budget] '{name}': {actual_tokens} tokens (estimated {estimate})")
        return ToolResult(name=name, result=result, tokens_used=actual_tokens)


class BudgetedToolAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.executor = TokenBudgetedExecutor()

    async def answer(self, question: str, current_tokens: int = 5000) -> str:
        async def search():
            await asyncio.sleep(0.1)
            return [{"title": "Result 1", "snippet": "..."}]

        async def fetch_inventory():
            await asyncio.sleep(0.1)
            return [{"sku": f"SKU{i}", "qty": i * 10} for i in range(50)]  # Large

        async def get_profile():
            await asyncio.sleep(0.05)
            return {"name": "Alice", "plan": "pro"}

        results = await asyncio.gather(
            self.executor.run_tool("search", search, current_tokens),
            self.executor.run_tool("inventory", fetch_inventory, current_tokens),
            self.executor.run_tool("profile", get_profile, current_tokens, force=True),
        )

        context_parts = []
        for r in results:
            if not r.skipped:
                context_parts.append(f"{r.name}: {json.dumps(r.result)[:500]}")
            else:
                context_parts.append(f"{r.name}: [skipped — {r.skip_reason}]")

        context = "\n".join(context_parts)
        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": f"{question}\n\nContext:\n{context}"
            }],
        )
        return response.content[0].text


async def demo():
    agent = BudgetedToolAgent(api_key="sk-...")
    # Simulate a nearly-full context
    result = await agent.answer("What inventory do we have?", current_tokens=185_000)
    print(result[:100])

# asyncio.run(demo())
```

---

## Solution 4: Adaptive Tool Set Derived from Query Classification

Classify the user's query first, then select only the tools relevant to that query class.

```python
import asyncio
from dataclasses import dataclass
from typing import Optional
import anthropic


QUERY_TOOL_MAP = {
    "factual":     ["search_web", "wikipedia"],
    "account":     ["query_database", "get_user_profile", "billing"],
    "code":        ["code_executor", "search_docs"],
    "analytics":   ["fetch_analytics", "query_database"],
    "general":     ["search_web"],
}

ALL_TOOLS = {
    "search_web":       {"description": "Search the web", "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    "query_database":   {"description": "Query user database", "input_schema": {"type": "object", "properties": {"user_id": {"type": "string"}}, "required": ["user_id"]}},
    "get_user_profile": {"description": "Get user profile", "input_schema": {"type": "object", "properties": {"user_id": {"type": "string"}}, "required": ["user_id"]}},
    "code_executor":    {"description": "Execute Python code", "input_schema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}},
    "fetch_analytics":  {"description": "Fetch usage analytics", "input_schema": {"type": "object", "properties": {"period": {"type": "string"}}, "required": ["period"]}},
}


async def classify_query(client: anthropic.AsyncAnthropic, message: str) -> str:
    """Use a cheap, fast model call to classify the query before tool selection."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",  # Cheap classifier
        max_tokens=16,
        system="Classify the user query into ONE word: factual, account, code, analytics, or general.",
        messages=[{"role": "user", "content": message}],
    )
    category = response.content[0].text.strip().lower()
    if category not in QUERY_TOOL_MAP:
        return "general"
    return category


class QueryClassifyingAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def answer(self, user_message: str) -> str:
        # Step 1: Classify query (cheap)
        category = await classify_query(self.client, user_message)
        tool_names = QUERY_TOOL_MAP.get(category, ["search_web"])
        tools = [
            {"name": name, **spec}
            for name, spec in ALL_TOOLS.items()
            if name in tool_names
        ]
        print(f"[classify] category={category} tools={[t['name'] for t in tools]}")

        # Step 2: Answer with targeted tool set (efficient)
        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            tools=tools,
            messages=[{"role": "user", "content": user_message}],
        )
        return response.content[0].text if response.content else ""


async def demo():
    agent = QueryClassifyingAgent(api_key="sk-...")
    questions = [
        "What is the capital of France?",
        "Show me my billing history.",
        "Write a Python function to sort a list.",
    ]
    for q in questions:
        r = await agent.answer(q)
        print(f"Q: {q[:40]!r} → {r[:50]}")

# asyncio.run(demo())
```

---

## Solution 5: Tool Result Size Limiter

Cap the size of each tool's result before it enters the conversation, preventing runaway tool responses from consuming disproportionate budget.

```python
import asyncio
import json
from dataclasses import dataclass
from typing import Any, Callable, Coroutine
import anthropic


@dataclass
class ToolResultPolicy:
    name: str
    max_tokens: int         # Hard cap on result tokens
    truncation_marker: str = "...[truncated]"
    json_array_limit: Optional[int] = None  # If result is list, limit items


from typing import Optional


def limit_result(result: Any, policy: ToolResultPolicy) -> tuple[Any, bool]:
    """Trim result to fit within token budget. Returns (trimmed_result, was_truncated)."""
    # If it's a list and we have an item limit, slice first
    if isinstance(result, list) and policy.json_array_limit is not None:
        if len(result) > policy.json_array_limit:
            result = result[:policy.json_array_limit]
            return result, True

    serialized = json.dumps(result)
    # Rough token estimate: 4 chars per token
    if len(serialized) // 4 <= policy.max_tokens:
        return result, False

    # Truncate the serialized JSON
    max_chars = policy.max_tokens * 4
    truncated = serialized[:max_chars] + policy.truncation_marker
    return truncated, True


POLICIES = {
    "search_web":     ToolResultPolicy("search_web",    max_tokens=400, json_array_limit=5),
    "query_database": ToolResultPolicy("query_database",max_tokens=300),
    "fetch_report":   ToolResultPolicy("fetch_report",  max_tokens=200, json_array_limit=10),
    "get_all_items":  ToolResultPolicy("get_all_items", max_tokens=500, json_array_limit=20),
}


class ResultLimitedAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def _run_and_limit(
        self,
        tool_name: str,
        fn: Callable[[], Coroutine],
    ) -> Any:
        raw = await fn()
        policy = POLICIES.get(tool_name)
        if policy:
            limited, truncated = limit_result(raw, policy)
            if truncated:
                print(f"[limit] '{tool_name}' result truncated to {policy.max_tokens} tokens")
            return limited
        return raw

    async def answer(self, question: str) -> str:
        async def search():
            return [{"title": f"Result {i}", "body": "x" * 200} for i in range(50)]

        limited_results = await self._run_and_limit("search_web", search)

        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": f"{question}\n\nSearch results: {json.dumps(limited_results)}"
            }],
        )
        return response.content[0].text


async def demo():
    agent = ResultLimitedAgent(api_key="sk-...")
    result = await agent.answer("What are the top results?")
    print(result[:100])

# asyncio.run(demo())
```

---

## Solution 6: Token-Aware Tool Call Deduplication

Track which tool results are already in the conversation history and skip re-executing tools whose fresh result would be identical to a cached one.

```python
import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Optional
import anthropic


@dataclass
class CachedToolResult:
    result: Any
    result_hash: str
    fetched_at: float
    tokens: int

    def is_fresh(self, max_age: float) -> bool:
        return (time.time() - self.fetched_at) < max_age


TOOL_CACHE_TTL = {
    "get_user_profile": 300.0,   # Profile: 5 min
    "search_web":       60.0,    # Search: 1 min
    "query_database":   30.0,    # DB: 30s
    "get_static_config":3600.0,  # Config: 1 hour
}


class DeduplicatingToolRunner:
    def __init__(self):
        self._cache: dict[str, CachedToolResult] = {}

    def _key(self, tool_name: str, args: dict) -> str:
        return f"{tool_name}:{hashlib.md5(json.dumps(args, sort_keys=True).encode()).hexdigest()}"

    async def run(
        self,
        tool_name: str,
        args: dict,
        fn: Callable[[], Coroutine],
    ) -> tuple[Any, bool]:
        """Returns (result, from_cache)."""
        key = self._key(tool_name, args)
        ttl = TOOL_CACHE_TTL.get(tool_name, 0.0)

        if key in self._cache and ttl > 0:
            cached = self._cache[key]
            if cached.is_fresh(ttl):
                print(f"[dedup] '{tool_name}' cache hit — saved ~{cached.tokens} tokens")
                return cached.result, True

        result = await fn()
        serialized = json.dumps(result)
        tokens = len(serialized) // 4
        result_hash = hashlib.md5(serialized.encode()).hexdigest()

        self._cache[key] = CachedToolResult(
            result=result,
            result_hash=result_hash,
            fetched_at=time.time(),
            tokens=tokens,
        )
        return result, False

    def saved_tokens(self) -> int:
        return sum(c.tokens for c in self._cache.values())


class DeduplicatingAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.runner = DeduplicatingToolRunner()

    async def multi_turn(self, messages: list[str]) -> list[str]:
        replies = []
        for msg in messages:
            profile, from_cache = await self.runner.run(
                "get_user_profile",
                {"user_id": "u42"},
                lambda: asyncio.sleep(0.1) or {"name": "Alice", "plan": "pro"},  # type: ignore
            )

            response = await self.client.messages.create(
                model="claude-opus-4-6",
                max_tokens=256,
                messages=[{
                    "role": "user",
                    "content": f"{msg}\nProfile: {profile}"
                }],
            )
            replies.append(response.content[0].text)

        print(f"[dedup] Total saved tokens: {self.runner.saved_tokens()}")
        return replies


async def demo():
    agent = DeduplicatingAgent(api_key="sk-...")
    msgs = ["Hello", "What's my plan?", "Can I upgrade?"]
    results = await agent.multi_turn(msgs)
    for r in results:
        print(r[:60])

# asyncio.run(demo())
```

---

## Comparison

| Solution | Selection Strategy | Token Savings | Complexity | Adaptive | Best For |
|---|---|---|---|---|---|
| Priority-based gating | Skip by priority × threshold | High | Low | No | Multi-tool agents |
| Dynamic schema truncation | Stub schemas when tight | Medium | Low | No | Schema-heavy tool sets |
| Pre-execution cost check | Skip by result estimate | High | Medium | No | Variable-size results |
| Query classification | Route to relevant tool subset | High | Medium | Yes | Diverse query types |
| Result size limiter | Truncate after execution | Medium | Low | No | Uncontrolled tool outputs |
| Tool call deduplication | Cache repeated tool calls | Medium | Low | No | Multi-turn conversations |

**Recommendation:** Start with Solution 1 (priority-based gating) — a simple config table and one comparison gates expensive optional tools automatically. Add Solution 5 (result size limiter) for any tool whose output size you don't control. Use Solution 4 (query classification) when your agent handles radically different query types and you want to minimize wasted schema tokens.
