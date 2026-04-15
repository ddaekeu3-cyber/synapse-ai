---
layout: solution
title: "Agent doesn't lazy-load large tool schemas"
category: performance
description: "Agent sends all tool definitions to the model on every request, even when only one or two tools are actually needed. This bloats input tokens, increases latency, and wastes prompt cache budget."
tags: [performance, tools, token-cost, lazy-loading, prompt-caching]
---

## Symptom

Every API call includes the full set of tool definitions — dozens of large JSON schemas — regardless of what the user asked. Simple requests that only need one tool still pay the token cost for all of them. Cold-start latency is high and input token counts balloon as the tool library grows.

## Root Cause

Tool schemas are assembled once at import time and passed in full to every `client.messages.create()` call. There is no filtering step that matches the current task to the subset of tools it actually needs. As the tool library grows, the cost compounds: 20 tools × 500 tokens each = 10,000 tokens of overhead per call.

## Fix

Select only the tools relevant to the current request. The right approach depends on how much you know at call time: use explicit routing for well-understood task types, keyword/embedding matching for dynamic classification, or a two-phase approach where the model first selects its own tools from a lightweight catalog.

---

### Option 1 — Explicit task-type router selects a tool subset

```python
import anthropic
from typing import Callable

client = anthropic.Anthropic(api_key="sk-live-...")

# Full tool registry — never sent all at once
ALL_TOOLS: dict[str, dict] = {
    "search_web": {
        "name": "search_web",
        "description": "Search the web for current information.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    "read_file": {
        "name": "read_file",
        "description": "Read the contents of a local file.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    "write_file": {
        "name": "write_file",
        "description": "Write content to a local file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    "run_sql": {
        "name": "run_sql",
        "description": "Execute a SQL query against the database.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    "send_email": {
        "name": "send_email",
        "description": "Send an email to a recipient.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
    },
}

# Routing table: task keyword → tool names needed
TASK_TOOL_MAP: dict[str, list[str]] = {
    "search": ["search_web"],
    "file": ["read_file", "write_file"],
    "database": ["run_sql"],
    "email": ["send_email"],
    "research": ["search_web", "read_file"],
}


def select_tools(user_message: str) -> list[dict]:
    msg_lower = user_message.lower()
    for keyword, tool_names in TASK_TOOL_MAP.items():
        if keyword in msg_lower:
            return [ALL_TOOLS[n] for n in tool_names]
    # Default: send only the two most general-purpose tools
    return [ALL_TOOLS["search_web"], ALL_TOOLS["read_file"]]


def run_agent(user_message: str) -> str:
    tools = select_tools(user_message)
    print(f"Sending {len(tools)}/{len(ALL_TOOLS)} tools for: {user_message[:60]}")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        tools=tools,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text if response.stop_reason == "end_turn" else ""
```

**Expected Token Savings:** 60–90 % of tool-schema tokens eliminated for narrow requests; scales linearly with tool library size.
**Environment:** Any agent with a categorizable task space; requires a routing table that reflects actual usage patterns.

---

### Option 2 — Keyword-matching lazy loader

```python
import anthropic
import re

client = anthropic.Anthropic(api_key="sk-live-...")

TOOL_REGISTRY: dict[str, dict] = {
    "calculator": {
        "name": "calculator",
        "description": "Perform arithmetic calculations.",
        "input_schema": {
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
        },
        "_keywords": ["calculat", "math", "add", "subtract", "multiply", "divide", "sum", "total"],
    },
    "weather": {
        "name": "weather",
        "description": "Get current weather for a location.",
        "input_schema": {
            "type": "object",
            "properties": {"location": {"type": "string"}},
            "required": ["location"],
        },
        "_keywords": ["weather", "temperature", "forecast", "rain", "sunny", "climate"],
    },
    "translate": {
        "name": "translate",
        "description": "Translate text between languages.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "target_language": {"type": "string"},
            },
            "required": ["text", "target_language"],
        },
        "_keywords": ["translat", "french", "spanish", "german", "japanese", "chinese", "language"],
    },
    "code_exec": {
        "name": "code_exec",
        "description": "Execute a Python code snippet.",
        "input_schema": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
        "_keywords": ["code", "python", "script", "run", "execute", "snippet", "function"],
    },
}


def lazy_select_tools(user_message: str, max_tools: int = 3) -> list[dict]:
    msg_lower = user_message.lower()
    scored: list[tuple[int, dict]] = []

    for tool_def in TOOL_REGISTRY.values():
        keywords: list[str] = tool_def.get("_keywords", [])
        score = sum(1 for kw in keywords if kw in msg_lower)
        if score > 0:
            scored.append((score, tool_def))

    scored.sort(key=lambda x: -x[0])
    selected = [
        {k: v for k, v in t.items() if not k.startswith("_")}
        for _, t in scored[:max_tools]
    ]
    return selected


def run_agent(user_message: str) -> str:
    tools = lazy_select_tools(user_message)
    kwargs: dict = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 512,
        "messages": [{"role": "user", "content": user_message}],
    }
    if tools:
        kwargs["tools"] = tools

    response = client.messages.create(**kwargs)
    return response.content[0].text if response.stop_reason == "end_turn" else ""
```

**Expected Token Savings:** ~70 % for single-intent queries in a large tool library; zero false negatives for high-keyword-overlap requests.
**Environment:** Medium-size tool libraries (10–50 tools) with keyword-distinguishable functions.

---

### Option 3 — Two-phase: lightweight catalog → full schema on demand

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")

# Phase 1: lightweight catalog — one line per tool, no full schema
TOOL_CATALOG = """Available tools (name | description):
search_web       | Search the web for current information
read_file        | Read a local file by path
write_file       | Write content to a file
run_sql          | Run a SQL query
send_email       | Send an email
calculate        | Evaluate a math expression
translate        | Translate text to another language
"""

# Phase 2: full schemas, loaded on demand
FULL_SCHEMAS: dict[str, dict] = {
    "search_web": {
        "name": "search_web",
        "description": "Search the web for current information.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    "run_sql": {
        "name": "run_sql",
        "description": "Execute a SQL query.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    # … add remaining schemas …
}


def select_tools_via_llm(user_message: str) -> list[str]:
    """Ask a cheap model which tools are needed — returns a list of tool names."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=(
            "You are a tool selector. Given a user request and a tool catalog, "
            "reply with ONLY a JSON array of tool names needed. "
            "Use an empty array if no tools are needed."
        ),
        messages=[
            {
                "role": "user",
                "content": f"User request: {user_message}\n\nTool catalog:\n{TOOL_CATALOG}",
            }
        ],
    )
    text = response.content[0].text.strip()
    try:
        names = json.loads(text)
        return [n for n in names if n in FULL_SCHEMAS]
    except json.JSONDecodeError:
        return []


def run_agent(user_message: str) -> str:
    # Phase 1: select tool names (cheap, fast)
    selected_names = select_tools_via_llm(user_message)
    tools = [FULL_SCHEMAS[n] for n in selected_names]

    print(f"Phase 1 selected: {selected_names}")

    # Phase 2: run main model with only the selected schemas
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        tools=tools or anthropic.NOT_GIVEN,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text if response.stop_reason == "end_turn" else ""
```

**Expected Token Savings:** 50–80 % on Phase 2 tool tokens; Phase 1 costs ~50–100 tokens on Haiku (negligible).
**Environment:** Large tool libraries (50+ tools) where keyword routing is insufficient; accepts one extra round-trip.

---

### Option 4 — Async parallel lazy-load with per-request tool cache

```python
import anthropic
import asyncio
import hashlib
from functools import lru_cache

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")

TOOL_GROUPS: dict[str, list[dict]] = {
    "search": [
        {
            "name": "web_search",
            "description": "Search the web.",
            "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        }
    ],
    "data": [
        {
            "name": "query_db",
            "description": "Query the database.",
            "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
        },
        {
            "name": "read_csv",
            "description": "Read a CSV file.",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        },
    ],
    "communication": [
        {
            "name": "send_slack",
            "description": "Send a Slack message.",
            "input_schema": {
                "type": "object",
                "properties": {"channel": {"type": "string"}, "text": {"type": "string"}},
                "required": ["channel", "text"],
            },
        }
    ],
}

_TOOL_CACHE: dict[str, list[dict]] = {}


def _cache_key(groups: list[str]) -> str:
    return hashlib.md5(":".join(sorted(groups)).encode()).hexdigest()


@lru_cache(maxsize=32)
def _classify_groups(message_lower: str) -> tuple[str, ...]:
    groups = []
    if any(w in message_lower for w in ("search", "look up", "find", "google")):
        groups.append("search")
    if any(w in message_lower for w in ("database", "sql", "csv", "data", "query")):
        groups.append("data")
    if any(w in message_lower for w in ("slack", "message", "notify", "send")):
        groups.append("communication")
    return tuple(groups) if groups else ("search",)


async def get_tools(user_message: str) -> list[dict]:
    groups = _classify_groups(user_message.lower())
    key = _cache_key(list(groups))
    if key not in _TOOL_CACHE:
        merged: list[dict] = []
        for g in groups:
            merged.extend(TOOL_GROUPS.get(g, []))
        _TOOL_CACHE[key] = merged
    return _TOOL_CACHE[key]


async def run_agent_async(user_message: str) -> str:
    tools = await get_tools(user_message)

    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        tools=tools,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text if response.stop_reason == "end_turn" else ""


async def run_batch(messages: list[str]) -> list[str]:
    return await asyncio.gather(*[run_agent_async(m) for m in messages])
```

**Expected Token Savings:** Tool selection is O(1) after first call via `lru_cache`; parallel batches share cached tool sets.
**Environment:** Async agents processing bursts of requests; the `lru_cache` on `_classify_groups` eliminates repeated string matching.

---

### Option 5 — Schema compression: strip descriptions for cached tools

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

FULL_TOOL = {
    "name": "analyze_document",
    "description": (
        "Analyze a document for key themes, sentiment, named entities, "
        "and a structured summary. Accepts plain text or markdown. "
        "Returns a JSON object with fields: themes (list), sentiment (str), "
        "entities (list of {name, type}), and summary (str)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The document text to analyze."},
            "max_summary_words": {
                "type": "integer",
                "description": "Maximum words in the summary.",
                "default": 100,
            },
        },
        "required": ["text"],
    },
}

COMPACT_TOOL = {
    "name": "analyze_document",
    "description": "Analyze document: themes, sentiment, entities, summary.",
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "max_summary_words": {"type": "integer"},
        },
        "required": ["text"],
    },
}


def run_agent(user_message: str, use_compact: bool = True) -> anthropic.types.Message:
    tool = COMPACT_TOOL if use_compact else FULL_TOOL
    token_estimate = len(str(tool)) // 4  # rough chars-to-tokens
    print(f"Tool schema ~{token_estimate} tokens ({'compact' if use_compact else 'full'})")

    return client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        tools=[tool],
        messages=[{"role": "user", "content": user_message}],
    )
```

**Expected Token Savings:** 30–60 % per tool schema; compound across large tool sets; descriptions are the largest contributor to schema size.
**Environment:** Tools whose descriptions were written for human readers, not model consumption; strip only the redundant prose.

---

### Option 6 — Prompt-cache-aligned tool batching (stable schemas stay cached)

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# Stable tools: sent first and marked for caching
STABLE_TOOLS = [
    {
        "name": "get_user_profile",
        "description": "Retrieve a user profile by ID.",
        "input_schema": {
            "type": "object",
            "properties": {"user_id": {"type": "string"}},
            "required": ["user_id"],
        },
    },
    {
        "name": "list_orders",
        "description": "List orders for a user.",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["user_id"],
        },
    },
]

# Dynamic tools: only included when needed for this specific request
OPTIONAL_TOOLS: dict[str, dict] = {
    "process_refund": {
        "name": "process_refund",
        "description": "Issue a refund for an order.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["order_id", "reason"],
        },
    },
    "escalate_ticket": {
        "name": "escalate_ticket",
        "description": "Escalate a support ticket to a human agent.",
        "input_schema": {
            "type": "object",
            "properties": {"ticket_id": {"type": "string"}},
            "required": ["ticket_id"],
        },
    },
}


def build_tool_list(user_message: str) -> list[dict]:
    """Return stable tools + only the optional tools relevant to this message."""
    msg_lower = user_message.lower()
    optional: list[dict] = []

    if any(w in msg_lower for w in ("refund", "return", "money back")):
        optional.append(OPTIONAL_TOOLS["process_refund"])
    if any(w in msg_lower for w in ("escalat", "human", "manager", "urgent")):
        optional.append(OPTIONAL_TOOLS["escalate_ticket"])

    # Stable tools first so Anthropic's prompt cache covers them
    return STABLE_TOOLS + optional


def run_agent(user_message: str) -> str:
    tools = build_tool_list(user_message)
    print(f"Tool count: {len(tools)} (stable={len(STABLE_TOOLS)}, optional={len(tools)-len(STABLE_TOOLS)})")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        tools=tools,
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text if response.stop_reason == "end_turn" else ""


# Comparison table
# | Option | Selection Method | Extra Latency | Best For |
# |--------|-----------------|---------------|---------|
# | 1 Explicit router | Keyword dict | None | Known task taxonomy |
# | 2 Keyword score | Regex matching | None | Medium libraries |
# | 3 Two-phase LLM | Haiku pre-call | +100ms | 50+ tools, dynamic |
# | 4 Async group cache | lru_cache | None | Burst async workloads |
# | 5 Schema compression | Manual trim | None | Verbose descriptions |
# | 6 Cache-aligned split | Prefix stability | None | Prompt caching setups |
```

**Expected Token Savings:** Stable tools hit the prompt cache on subsequent requests, reducing their cost to ~10 % of normal; dynamic tools only added when triggered.
**Environment:** Customer-facing agents with a stable core tool set plus a long tail of situational tools; combine with Option 1 or 2 for the dynamic layer.
