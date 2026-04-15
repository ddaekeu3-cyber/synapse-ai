---
layout: solution
title: "Agent Includes Redundant Tool Definitions in Every Call"
category: token-cost
description: "Agent re-sends the full tool schema array on every API call even though tools never change, burning input tokens proportional to the number and complexity of tool definitions."
tags: [tools, token-cost, prompt-caching, optimization, tool-use]
---

## Symptom

Token usage reports show a large, constant input token overhead on every API call regardless of message content. Increasing the number of tools makes each call more expensive. Prompt caching shows low cache hit rates. The cost per conversation turn grows linearly with the number of tools defined.

## Root Cause

Every `client.messages.create()` call includes the full `tools` array. Each tool definition contains a name, description, and JSON schema — often 100–500 tokens per tool. An agent with 10 tools adds 1,000–5,000 tokens of overhead to every single call. Since tools rarely change within a session, this is pure waste. Claude's prompt caching can eliminate this cost, but only if the tools array is placed correctly in the cache-eligible portion of the request.

## Fix

### Option 1: Enable prompt caching on the tools array

```python
import anthropic

client = anthropic.Anthropic()

# Define tools once — these rarely change during a session
TOOLS = [
    {
        "name": "search_web",
        "description": "Search the web for current information on a topic",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "num_results": {"type": "integer", "description": "Number of results", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_file",
        "description": "Read the contents of a file by path",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute file path"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file, creating it if it doesn't exist",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_python",
        "description": "Execute a Python code snippet and return stdout",
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute"},
            },
            "required": ["code"],
        },
    },
]

# Add cache_control to the LAST tool in the array.
# Claude caches everything up to and including the last cache breakpoint.
CACHED_TOOLS = [*TOOLS[:-1], {**TOOLS[-1], "cache_control": {"type": "ephemeral"}}]


def run_agent_turn(messages: list[dict], user_input: str) -> str:
    messages.append({"role": "user", "content": user_input})

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        tools=CACHED_TOOLS,  # Tools are cached after first call
        messages=messages,
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )

    # Report cache performance
    usage = response.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0)
    cache_write = getattr(usage, "cache_creation_input_tokens", 0)
    print(f"Tokens — input: {usage.input_tokens}, cache_read: {cache_read}, cache_write: {cache_write}")

    assistant_msg = {"role": "assistant", "content": response.content}
    messages.append(assistant_msg)
    return response.content[0].text if response.content[0].type == "text" else ""


# Multi-turn conversation — tools are only paid for once (first call writes cache)
conversation: list[dict] = []
run_agent_turn(conversation, "What files are in the /tmp directory?")
run_agent_turn(conversation, "Read the first file you find.")
run_agent_turn(conversation, "Summarize its contents.")
```

**Expected Token Savings:** 50–90% reduction in tool definition tokens after the first call (paid once as cache write, then free as cache read).
**Environment:** Python 3.9+; requires prompt-caching beta header; most effective with 3+ tools.

---

### Option 2: Measure tool definition token cost before optimizing

```python
import json
import anthropic

client = anthropic.Anthropic()


def count_tool_tokens(tools: list[dict]) -> int:
    """
    Estimate token count for a tools array by serializing it.
    Actual count may differ slightly due to API formatting overhead.
    """
    serialized = json.dumps(tools)
    # Rough estimate: ~4 characters per token for JSON
    return len(serialized) // 4


def audit_tools(tools: list[dict]) -> None:
    """Print a cost breakdown per tool."""
    print(f"\n{'Tool':<30} {'Est. Tokens':>12} {'Est. Cost/1M calls':>20}")
    print("-" * 65)

    for tool in tools:
        tool_tokens = count_tool_tokens([tool])
        # claude-sonnet-4-6 input: $3/M tokens
        cost_per_million = (tool_tokens / 1_000_000) * 3.00 * 1_000_000
        print(f"{tool['name']:<30} {tool_tokens:>12} ${cost_per_million:>18.2f}")

    total = count_tool_tokens(tools)
    total_cost = (total / 1_000_000) * 3.00 * 1_000_000
    print("-" * 65)
    print(f"{'TOTAL':<30} {total:>12} ${total_cost:>18.2f}")
    print(f"\nWith caching, after first call: ~$0.30/1M calls (90% reduction)")


TOOLS = [
    {
        "name": "search_knowledge_base",
        "description": "Search the internal knowledge base for relevant documents. Returns the top matching documents with their titles, snippets, and relevance scores.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language search query"},
                "max_results": {"type": "integer", "description": "Maximum number of results to return", "default": 5},
                "filter_tags": {"type": "array", "items": {"type": "string"}, "description": "Optional list of tags to filter by"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "create_ticket",
        "description": "Create a support ticket in the ticketing system",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Ticket title (max 100 chars)"},
                "description": {"type": "string", "description": "Detailed problem description"},
                "priority": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                "assignee": {"type": "string", "description": "Email of assignee (optional)"},
            },
            "required": ["title", "description", "priority"],
        },
    },
    {
        "name": "send_email",
        "description": "Send an email to one or more recipients",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "array", "items": {"type": "string"}, "description": "Recipient email addresses"},
                "subject": {"type": "string"},
                "body": {"type": "string", "description": "Email body in plain text or HTML"},
                "cc": {"type": "array", "items": {"type": "string"}, "description": "CC recipients"},
            },
            "required": ["to", "subject", "body"],
        },
    },
]

audit_tools(TOOLS)

# Add cache_control to last tool
cached_tools = [*TOOLS[:-1], {**TOOLS[-1], "cache_control": {"type": "ephemeral"}}]

response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=512,
    tools=cached_tools,
    messages=[{"role": "user", "content": "Search for information about onboarding."}],
    extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
)
print(f"\nActual usage: {response.usage}")
```

**Expected Token Savings:** Baseline measurement enables informed optimization; caching then cuts 90% of tool token cost.
**Environment:** Python 3.9+; useful before deploying to understand real overhead.

---

### Option 3: Tool registry with lazy loading and caching

```python
import anthropic
from dataclasses import dataclass, field
from typing import Any, Callable

client = anthropic.Anthropic()


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict
    handler: Callable
    enabled: bool = True
    cache: bool = True  # Include in cached tool prefix


class ToolRegistry:
    """Manages tool definitions and their cache-aware serialization."""

    def __init__(self):
        self._tools: list[ToolSpec] = []
        self._cached_api_tools: list[dict] | None = None  # Invalidated on change

    def register(self, spec: ToolSpec) -> None:
        self._tools.append(spec)
        self._cached_api_tools = None  # Invalidate cache

    def get_api_tools(self) -> list[dict]:
        """Return tools array formatted for API, with cache_control on last entry."""
        if self._cached_api_tools is not None:
            return self._cached_api_tools

        active = [t for t in self._tools if t.enabled]
        if not active:
            return []

        api_tools = []
        for i, tool in enumerate(active):
            entry: dict[str, Any] = {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.schema,
            }
            # Mark last tool as cache breakpoint
            if i == len(active) - 1 and tool.cache:
                entry["cache_control"] = {"type": "ephemeral"}
            api_tools.append(entry)

        self._cached_api_tools = api_tools
        return api_tools

    def dispatch(self, tool_name: str, tool_input: dict) -> str:
        for tool in self._tools:
            if tool.name == tool_name:
                return tool.handler(tool_input)
        raise ValueError(f"Unknown tool: {tool_name}")


# Build registry
registry = ToolRegistry()

registry.register(ToolSpec(
    name="get_weather",
    description="Get current weather for a city",
    schema={
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
    handler=lambda inp: f"Weather in {inp['city']}: 22°C, partly cloudy",
))

registry.register(ToolSpec(
    name="calculate",
    description="Evaluate a mathematical expression",
    schema={
        "type": "object",
        "properties": {"expression": {"type": "string", "description": "Math expression to evaluate"}},
        "required": ["expression"],
    },
    handler=lambda inp: str(eval(inp["expression"])),  # noqa: S307 — demo only
))


def agent_loop(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=registry.get_api_tools(),  # Same object reused — stable cache key
            messages=messages,
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = registry.dispatch(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": tool_results})


print(agent_loop("What's the weather in Tokyo and what is 42 * 17?"))
```

**Expected Token Savings:** 85–95% tool token reduction after first call through stable cache key.
**Environment:** Python 3.10+; registry pattern scales to 20+ tools.

---

### Option 4: Dynamic tool selection — only send relevant tools

```python
import anthropic

client = anthropic.Anthropic()

# Full tool library — never send all of these at once
ALL_TOOLS = {
    "search_web": {
        "name": "search_web",
        "description": "Search the internet for current information",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    "read_file": {
        "name": "read_file",
        "description": "Read a file from the filesystem",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
    "write_file": {
        "name": "write_file",
        "description": "Write content to a file",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
    },
    "query_database": {
        "name": "query_database",
        "description": "Run a SQL query against the database",
        "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}, "params": {"type": "array"}}, "required": ["sql"]},
    },
    "send_notification": {
        "name": "send_notification",
        "description": "Send a push notification to a user",
        "input_schema": {"type": "object", "properties": {"user_id": {"type": "string"}, "message": {"type": "string"}}, "required": ["user_id", "message"]},
    },
    "analyze_image": {
        "name": "analyze_image",
        "description": "Analyze the contents of an image",
        "input_schema": {"type": "object", "properties": {"image_url": {"type": "string"}, "question": {"type": "string"}}, "required": ["image_url"]},
    },
}

# Intent → relevant tool subsets
TOOL_PROFILES = {
    "research": ["search_web", "read_file"],
    "file_ops": ["read_file", "write_file"],
    "data": ["query_database", "read_file"],
    "notify": ["send_notification"],
    "vision": ["analyze_image"],
    "general": ["search_web", "query_database"],  # Minimal default
}


def classify_intent(user_message: str) -> str:
    """Use Haiku to classify intent — much cheaper than sending all tools."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
        system="Classify the user's intent. Reply with exactly one word: research, file_ops, data, notify, vision, or general.",
        messages=[{"role": "user", "content": user_message}],
    )
    intent = response.content[0].text.strip().lower()
    return intent if intent in TOOL_PROFILES else "general"


def get_tools_for_intent(intent: str) -> list[dict]:
    tool_names = TOOL_PROFILES[intent]
    tools = [ALL_TOOLS[name] for name in tool_names if name in ALL_TOOLS]
    # Cache the last tool
    if tools:
        tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    return tools


def run_agent(user_message: str) -> str:
    intent = classify_intent(user_message)
    tools = get_tools_for_intent(intent)
    print(f"Intent: {intent} → {len(tools)} tools (vs {len(ALL_TOOLS)} total)")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        tools=tools,
        messages=[{"role": "user", "content": user_message}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    return response.content[0].text if response.content[0].type == "text" else ""


print(run_agent("Search the web for recent AI agent benchmarks"))
print(run_agent("Read the file at /var/log/app.log"))
```

**Expected Token Savings:** 40–80% reduction from tool selection alone (sends 2–3 tools instead of 10+), plus caching benefit.
**Environment:** Python 3.9+; Haiku classification costs ~10 tokens — trivial vs. savings from reduced tool payload.

---

### Option 5: Compress tool descriptions to reduce token footprint

```python
import anthropic

client = anthropic.Anthropic()


def compress_tool_description(tool: dict, verbose: bool = False) -> dict:
    """
    Return a token-efficient version of a tool definition.
    verbose=True restores full descriptions (for development/testing).
    """
    if verbose:
        return tool

    compressed = dict(tool)

    # Shorten description to first sentence
    desc = tool.get("description", "")
    first_sentence = desc.split(".")[0].strip()
    if len(first_sentence) > 10:
        compressed["description"] = first_sentence

    # Remove 'description' from optional schema properties
    if "input_schema" in tool and "properties" in tool["input_schema"]:
        compressed_props = {}
        for prop_name, prop_def in tool["input_schema"]["properties"].items():
            # Keep type and enum but drop description for non-required fields
            required = tool["input_schema"].get("required", [])
            if prop_name in required:
                compressed_props[prop_name] = prop_def  # Keep full for required
            else:
                compressed_props[prop_name] = {
                    k: v for k, v in prop_def.items() if k != "description"
                }
        compressed = {
            **compressed,
            "input_schema": {**tool["input_schema"], "properties": compressed_props},
        }

    return compressed


VERBOSE_TOOLS = [
    {
        "name": "search_documents",
        "description": "Search through the document repository to find relevant files and content matching a query. Returns ranked results with titles, snippets, and metadata about each document.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Full text search query to execute against document index"},
                "limit": {"type": "integer", "description": "Maximum number of results to return (default: 10, max: 50)", "default": 10},
                "doc_type": {"type": "string", "description": "Filter by document type: pdf, docx, txt, or all", "enum": ["pdf", "docx", "txt", "all"], "default": "all"},
                "date_from": {"type": "string", "description": "Filter to documents created after this ISO 8601 date"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "summarize_document",
        "description": "Generate a structured summary of a document given its ID, including key points, entities, and action items.",
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string", "description": "Unique document identifier from search results"},
                "focus": {"type": "string", "description": "Optional focus area for the summary (e.g., 'financial data', 'action items')"},
                "max_length": {"type": "integer", "description": "Approximate maximum length of summary in words", "default": 200},
            },
            "required": ["doc_id"],
        },
    },
]

production_mode = True  # Set False during development
compressed_tools = [compress_tool_description(t, verbose=not production_mode) for t in VERBOSE_TOOLS]

import json
verbose_tokens = len(json.dumps(VERBOSE_TOOLS)) // 4
compressed_tokens = len(json.dumps(compressed_tools)) // 4
print(f"Tool tokens: {verbose_tokens} verbose → {compressed_tokens} compressed ({100*(1-compressed_tokens/verbose_tokens):.0f}% reduction)")

# Add cache_control
compressed_tools[-1] = {**compressed_tools[-1], "cache_control": {"type": "ephemeral"}}

response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=512,
    tools=compressed_tools,
    messages=[{"role": "user", "content": "Find documents about Q4 budget planning"}],
    extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
)
print(response.content[0].text)
```

**Expected Token Savings:** 20–40% from description compression + 85–95% from caching = compounding savings.
**Environment:** Python 3.9+; compression is lossless for required fields, lossy only on optional field descriptions.

---

### Option 6: Tool definition versioning with cache invalidation

```python
import hashlib
import json
import anthropic

client = anthropic.Anthropic()


class CachedToolSet:
    """
    Versioned tool set. Cache is only invalidated when tools actually change.
    Tracks cache hits/misses for cost reporting.
    """

    def __init__(self, tools: list[dict]):
        self._tools = tools
        self._version = self._compute_hash(tools)
        self._api_tools = self._build_api_tools(tools)
        self._cache_writes = 0
        self._cache_reads = 0

    @staticmethod
    def _compute_hash(tools: list[dict]) -> str:
        serialized = json.dumps(tools, sort_keys=True)
        return hashlib.sha256(serialized.encode()).hexdigest()[:8]

    @staticmethod
    def _build_api_tools(tools: list[dict]) -> list[dict]:
        if not tools:
            return []
        result = list(tools)
        result[-1] = {**result[-1], "cache_control": {"type": "ephemeral"}}
        return result

    def update(self, new_tools: list[dict]) -> bool:
        """Update tools if changed. Returns True if cache was invalidated."""
        new_hash = self._compute_hash(new_tools)
        if new_hash == self._version:
            return False
        self._tools = new_tools
        self._version = new_hash
        self._api_tools = self._build_api_tools(new_tools)
        print(f"Tool cache invalidated (version: {self._version})")
        return True

    def get_api_tools(self) -> list[dict]:
        return self._api_tools

    def record_usage(self, usage) -> None:
        writes = getattr(usage, "cache_creation_input_tokens", 0)
        reads = getattr(usage, "cache_read_input_tokens", 0)
        self._cache_writes += writes
        self._cache_reads += reads

    def report(self) -> str:
        total = self._cache_writes + self._cache_reads
        if total == 0:
            return "No cache data yet"
        hit_rate = self._cache_reads / total * 100
        # Cached reads cost 10% of normal input price
        saved = self._cache_reads * 0.9  # tokens saved vs full re-read
        return (
            f"Tool cache v{self._version}: "
            f"{self._cache_reads:,} reads, {self._cache_writes:,} writes, "
            f"{hit_rate:.0f}% hit rate, ~{saved:,.0f} tokens saved"
        )


TOOLS = [
    {
        "name": "fetch_data",
        "description": "Fetch data from an external API endpoint",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "method": {"type": "string", "enum": ["GET", "POST"], "default": "GET"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "store_result",
        "description": "Store a result to the persistent key-value store",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {"type": "string"},
            },
            "required": ["key", "value"],
        },
    },
]

tool_set = CachedToolSet(TOOLS)
messages = []

for turn, user_msg in enumerate([
    "Fetch data from https://api.example.com/status",
    "Store the result under key 'status_check'",
    "Fetch data from https://api.example.com/users",
]):
    messages.append({"role": "user", "content": user_msg})
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        tools=tool_set.get_api_tools(),
        messages=messages,
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    tool_set.record_usage(response.usage)
    messages.append({"role": "assistant", "content": response.content})
    print(f"Turn {turn+1}: {response.usage}")

print(f"\n{tool_set.report()}")
```

**Expected Token Savings:** 85–95% tool token reduction after turn 1, with version tracking to ensure correctness.
**Environment:** Python 3.9+; hash-based invalidation prevents stale caches when tools legitimately change.

---

| Option | Approach | Savings Source | Best For |
|--------|----------|---------------|----------|
| 1 | cache_control on last tool | Prompt caching | All multi-turn agents |
| 2 | Audit + cache | Measurement first | Cost diagnosis |
| 3 | Tool registry | Stable cache key | Large tool libraries |
| 4 | Dynamic selection | Fewer tools sent | 10+ tool libraries |
| 5 | Compress descriptions | Smaller payload | Verbose schemas |
| 6 | Versioned tool set | Cache + tracking | Production cost monitoring |
