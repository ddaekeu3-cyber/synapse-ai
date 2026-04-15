---
layout: solution
title: "Agent invokes tools for questions it can answer directly"
category: token-cost
description: "Agent calls external tools, databases, or APIs for factual questions it could answer from its training knowledge. Each unnecessary tool call adds a full round-trip: tool invocation, tool execution, tool result injection — costing tokens and latency with no accuracy gain."
tags: [token-cost, tool-use, routing, classification, performance, prompt-engineering]
---

## Symptom

A user asks "What is the capital of France?" and the agent calls `search_web(query="capital of France")` before answering "Paris". Or the user asks for a simple calculation and the agent calls `calculator(expression="2+2")`. The final answer is correct, but it cost 3× the tokens and added 200–500ms of unnecessary latency.

## Root Cause

The system prompt always includes tools and the model defaults to using them when available — even for questions where its parametric knowledge is sufficient and authoritative. Without guidance on when to use tools versus when to answer directly, the model errs on the side of using the tools it was given.

## Fix

Classify whether a question requires external data or can be answered from training knowledge. Either instruct the model explicitly, use a pre-call classifier to route requests, or define tool-use guardrails in the system prompt.

---

### Option 1 — System prompt guardrails for when NOT to use tools

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

TOOLS = [
    {
        "name": "search_web",
        "description": "Search the internet for current or real-time information.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "query_database",
        "description": "Query the company's internal database for user data.",
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
        },
    },
]

SYSTEM = """\
You are a helpful assistant with access to web search and a database.

Tool-use guidelines:
- Answer from your own knowledge for: general facts, definitions, math, logic, geography,
  history (before 2024), coding questions, language questions
- Use search_web ONLY for: current events, prices, weather, recent news, real-time data
- Use query_database ONLY for: user-specific data, order history, account information
- Never call a tool when you already know the answer with high confidence
- If you're unsure whether to use a tool, answer from knowledge first and note your uncertainty

These rules prevent unnecessary tool calls that add cost and latency.
"""


def run_agent(user_message: str) -> str:
    messages: list[dict] = [{"role": "user", "content": user_message}]

    for _ in range(5):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    # Tool dispatch would go here
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"[Result for {block.name}({block.input})]",
                    })
            messages.append({"role": "user", "content": results})

    return ""
```

**Expected Token Savings:** Tool calls that the model skips save ~200–500 tokens each (tool invocation overhead + result injection); for 50 % unnecessary calls, this halves tool-related costs.
**Environment:** Any agent with tools; the system prompt rule is the lowest-cost intervention requiring no code changes.

---

### Option 2 — Pre-call classifier: route to tool-free or tool-enabled path

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

CLASSIFIER_SYSTEM = (
    "Classify whether this user question requires external tools to answer correctly. "
    "Reply with exactly one word:\n"
    "  direct — can be answered from general knowledge without tools\n"
    "  realtime — needs current/live data (news, prices, weather, user data)\n"
    "  database — needs internal company data (user accounts, orders, inventory)"
)

TOOLS = [
    {
        "name": "search_web",
        "description": "Search for current information.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "query_database",
        "description": "Query internal database.",
        "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
    },
]


def classify_question(user_message: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        system=CLASSIFIER_SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    label = response.content[0].text.strip().lower()
    return label if label in ("direct", "realtime", "database") else "direct"


def run_agent(user_message: str) -> str:
    route = classify_question(user_message)
    print(f"Route: {route}")

    if route == "direct":
        # Answer without tools — save the tool round-trips
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": user_message}],
        )
        return response.content[0].text

    else:
        # Use tools only for requests that genuinely need them
        messages: list[dict] = [{"role": "user", "content": user_message}]
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )
        return response.content[0].text if response.stop_reason == "end_turn" else "[tool call needed]"


# Test
questions = [
    "What is the speed of light?",       # direct
    "What's the weather in Tokyo today?",  # realtime
    "Show me Alice's last 3 orders.",      # database
]
for q in questions:
    print(f"Q: {q}")
    print(f"A: {run_agent(q)}\n")
```

**Expected Token Savings:** Direct-path calls skip tool injection (saves ~200–800 tokens of tool schema); classifier costs ~20 Haiku tokens; net positive after ~25 requests.
**Environment:** Mixed-intent agents receiving a variety of question types; the three-way classifier handles most cases with simple labeling.

---

### Option 3 — `tool_choice: none` for low-confidence requests

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

TOOLS = [
    {
        "name": "search_web",
        "description": "Search the web for current information.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    }
]

# Keywords that suggest the answer is likely in the model's training data
KNOWLEDGE_PATTERNS = [
    "what is", "define", "how does", "explain", "who was", "when did",
    "why is", "what are", "describe", "tell me about", "what year",
    "calculate", "how many", "convert", "what does", "what's the difference",
]

# Keywords that suggest real-time data is needed
REALTIME_PATTERNS = [
    "today", "current", "now", "latest", "recent", "right now",
    "this week", "this month", "price of", "stock", "weather",
    "breaking", "yesterday", "last night",
]


def needs_realtime(user_message: str) -> bool:
    lower = user_message.lower()
    has_realtime = any(p in lower for p in REALTIME_PATTERNS)
    has_knowledge = any(p in lower for p in KNOWLEDGE_PATTERNS)
    # Only use tools if realtime signal is present AND knowledge signal is absent
    return has_realtime and not has_knowledge


def run_agent(user_message: str) -> str:
    use_tools = needs_realtime(user_message)

    kwargs: dict = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": user_message}],
    }

    if use_tools:
        kwargs["tools"] = TOOLS
        # Let the model decide whether to use the tool
    else:
        kwargs["tools"] = TOOLS
        kwargs["tool_choice"] = {"type": "none"}  # suppress tool use

    response = client.messages.create(**kwargs)
    return response.content[0].text
```

**Expected Token Savings:** `tool_choice: none` prevents tool invocation while still sending the schemas; combine with not sending schemas at all for maximum savings.
**Environment:** Agents where it's easier to detect "definitely needs real-time" than to detect "definitely doesn't need tools"; tool_choice suppression is a hard override.

---

### Option 4 — Async parallel: check knowledge first, fetch in background only if needed

```python
import anthropic
import asyncio

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")

CONFIDENCE_SYSTEM = (
    "Answer the user's question from your training knowledge. "
    "At the END of your response, add exactly one line: "
    "'CONFIDENCE: high' if you're certain, or 'CONFIDENCE: low' if the answer might be outdated or unknown."
)


async def answer_from_knowledge(user_message: str) -> tuple[str, bool]:
    """Returns (answer, is_high_confidence)."""
    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=CONFIDENCE_SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    text = response.content[0].text
    high_confidence = "CONFIDENCE: high" in text
    # Strip the confidence line from the answer
    answer = text.replace("CONFIDENCE: high", "").replace("CONFIDENCE: low", "").strip()
    return answer, high_confidence


async def answer_with_search(user_message: str) -> str:
    """Fallback: answer using web search tool."""
    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        tools=[{
            "name": "search_web",
            "description": "Search for current information.",
            "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        }],
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


async def run_agent_async(user_message: str) -> str:
    answer, high_confidence = await answer_from_knowledge(user_message)

    if high_confidence:
        return answer   # no tool call needed

    # Low confidence: do a search to verify / supplement
    print("Low confidence — falling back to search")
    return await answer_with_search(user_message)


asyncio.run(run_agent_async("What is the boiling point of water at sea level?"))
```

**Expected Token Savings:** High-confidence questions never invoke tools; ~60–80 % of general-knowledge questions resolve on the first call with no tool overhead.
**Environment:** Knowledge-heavy agents where most questions can be answered from training; the confidence signal allows graceful fallback for edge cases.

---

### Option 5 — Tool schema injection only for tool-eligible requests

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

ALL_TOOLS = [
    {
        "name": "search_web",
        "description": "Search for current events, prices, or real-time data.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "get_user_data",
        "description": "Fetch user account information from the database.",
        "input_schema": {"type": "object", "properties": {"user_id": {"type": "string"}}, "required": ["user_id"]},
    },
    {
        "name": "create_ticket",
        "description": "Create a support ticket.",
        "input_schema": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "description": {"type": "string"}},
            "required": ["title", "description"],
        },
    },
]

TOOL_TRIGGER_KEYWORDS = {
    "search_web": ["current", "today", "news", "price", "weather", "latest", "now"],
    "get_user_data": ["account", "order", "subscription", "user", "purchase", "history"],
    "create_ticket": ["report", "issue", "problem", "ticket", "bug", "complaint", "support"],
}


def select_tools(user_message: str) -> list[dict]:
    lower = user_message.lower()
    eligible = []
    for tool in ALL_TOOLS:
        keywords = TOOL_TRIGGER_KEYWORDS.get(tool["name"], [])
        if any(kw in lower for kw in keywords):
            eligible.append(tool)
    return eligible


def run_agent(user_message: str) -> str:
    tools = select_tools(user_message)
    print(f"Injecting {len(tools)}/{len(ALL_TOOLS)} tools")

    kwargs: dict = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": user_message}],
    }
    if tools:
        kwargs["tools"] = tools

    response = client.messages.create(**kwargs)
    return response.content[0].text
```

**Expected Token Savings:** Questions with no tool triggers save the full tool schema injection (~300–800 tokens for 3 tools); schema injection is the largest avoidable token cost for tool-enabled agents.
**Environment:** Agents with 3+ tools; skipping schema injection entirely for non-tool requests is more effective than `tool_choice: none`.

---

### Option 6 — Token-cost accounting: log when tools are used unnecessarily

```python
import anthropic
import json
from dataclasses import dataclass, field

client = anthropic.Anthropic(api_key="sk-live-...")

TRIVIAL_QUERIES = {
    "capital of france": "Paris",
    "2 + 2": "4",
    "boiling point of water": "100°C (212°F) at sea level",
    "speed of light": "approximately 299,792,458 metres per second",
}


@dataclass
class ToolUsageStats:
    total_calls: int = 0
    tool_calls_made: int = 0
    tool_calls_avoidable: int = 0
    tokens_wasted: int = 0
    examples: list[str] = field(default_factory=list)

    def report(self) -> str:
        avoidable_pct = round(self.tool_calls_avoidable / max(self.tool_calls_made, 1) * 100)
        return (
            f"Tool call stats: {self.tool_calls_made}/{self.total_calls} turns used tools, "
            f"{avoidable_pct}% potentially avoidable, "
            f"~{self.tokens_wasted} tokens wasted\n"
            f"Examples of avoidable calls: {self.examples[:3]}"
        )


stats = ToolUsageStats()

TOOLS = [
    {
        "name": "search_web",
        "description": "Search the internet.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    }
]


def audit_tool_call(tool_name: str, tool_input: dict, user_message: str) -> None:
    """Check if this tool call was necessary — log if it wasn't."""
    query = tool_input.get("query", "").lower().strip()
    for known_query in TRIVIAL_QUERIES:
        if known_query in query or known_query in user_message.lower():
            stats.tool_calls_avoidable += 1
            stats.tokens_wasted += 350  # estimated overhead per unnecessary tool call
            stats.examples.append(f"{tool_name}({json.dumps(tool_input)}) for: {user_message[:60]}")
            print(f"[AUDIT] Avoidable tool call: {tool_name}({tool_input}) — answer is '{TRIVIAL_QUERIES[known_query]}'")
            break


def run_agent(user_message: str) -> str:
    stats.total_calls += 1
    messages: list[dict] = [{"role": "user", "content": user_message}]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        tools=TOOLS,
        messages=messages,
    )

    if response.stop_reason == "tool_use":
        stats.tool_calls_made += 1
        for block in response.content:
            if block.type == "tool_use":
                audit_tool_call(block.name, block.input, user_message)

    return response.content[0].text if response.stop_reason == "end_turn" else "[tool call]"


# Run a few queries and check the audit report
for q in ["What is the capital of France?", "Latest AI news", "What is 2+2?"]:
    run_agent(q)

print(stats.report())


# Comparison table
# | Option | Prevention | Extra Cost | Best For |
# |--------|-----------|------------|---------|
# | 1 System rules | Prompt | ~80 tok | All agents |
# | 2 Pre-call classifier | Haiku route | ~20 tok | Mixed intent |
# | 3 tool_choice: none | API parameter | None | Clear no-tool cases |
# | 4 Confidence check | Self-assessment | ~100 tok | Knowledge-heavy |
# | 5 Schema suppression | No injection | None | 3+ tool agents |
# | 6 Audit logger | Post-call analysis | None | Measuring waste |
```

**Expected Token Savings:** The audit logger quantifies the problem — use it to measure unnecessary tool call frequency, then apply Option 1–5 to eliminate the waste.
**Environment:** Any agent in development or production; the audit data informs which prevention strategy has the highest ROI for your specific traffic pattern.
