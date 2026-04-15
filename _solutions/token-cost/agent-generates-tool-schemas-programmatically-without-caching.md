---
layout: solution
title: "Agent Generates Tool Schemas Programmatically Without Caching"
category: token-cost
description: "Agent rebuilds tool schema dictionaries from Python code on every API call, missing the opportunity to cache the stable tool payload and paying full input token price for identical schemas each turn."
tags: [token-cost, tools, prompt-caching, schema, optimization]
---

## Symptom

Token usage reports show tool definition tokens billed at the full input rate on every API call. Profiling reveals time spent executing schema-building functions before each request. Adding the prompt caching beta header has no effect because the tools array is reconstructed as a new Python object each time, preventing cache reuse. Cost per conversation scales linearly with number of turns even when tools never change.

## Root Cause

The agent calls a `build_tools()` function or constructs tool schema dicts inside the request loop. Even though the content is identical every call, Python creates a new list object each time. The Anthropic caching layer works at the content level, but the cache_control marker must be present and the object must be constructed once and reused — not rebuilt. Rebuilding also wastes CPU time on JSON serialization and dict construction for schemas that are always the same.

## Fix

### Option 1: Build tools once at module level with cache_control marker

```python
import anthropic

client = anthropic.Anthropic()

# Build schemas ONCE at module load time — never inside the request loop
def _build_tools() -> list[dict]:
    """Called once at startup. The result is frozen for the process lifetime."""
    tools = [
        {
            "name": "search_knowledge_base",
            "description": "Search the knowledge base for relevant articles and documentation.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {"type": "integer", "default": 5},
                    "filter_tag": {"type": "string"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "create_ticket",
            "description": "Create a support ticket in the ticketing system.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "priority": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["title", "description", "priority"],
            },
        },
        {
            "name": "get_account_info",
            "description": "Retrieve account details for a given user ID.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                },
                "required": ["user_id"],
            },
        },
    ]

    # Add cache_control to the last tool — Claude caches everything up to this point
    tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    return tools


# Module-level constant — built once, reused on every API call
TOOLS = _build_tools()


def run_agent(messages: list[dict]) -> str:
    # TOOLS is the same Python object on every call — cache key is stable
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        tools=TOOLS,  # Never rebuilt
        messages=messages,
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )

    usage = response.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0)
    cache_write = getattr(usage, "cache_creation_input_tokens", 0)
    print(f"tokens: in={usage.input_tokens} cache_read={cache_read} cache_write={cache_write}")

    return next((b.text for b in response.content if b.type == "text"), "")


# Multi-turn: after turn 1, tool tokens are read from cache
conversation = []
for user_msg in ["Search for onboarding docs", "Create a ticket for missing docs", "Get info for user-123"]:
    conversation.append({"role": "user", "content": user_msg})
    reply = run_agent(conversation)
    conversation.append({"role": "assistant", "content": reply})
    print(f"Reply: {reply[:80]}\n")
```

**Expected Token Savings:** 85–95% reduction in tool schema token cost after turn 1; tools cached on first write, read from cache on every subsequent turn.
**Environment:** Python 3.9+; module-level `TOOLS` constant is the simplest and most reliable caching strategy.

---

### Option 2: Frozen schema class with lazy initialization

```python
import anthropic
from functools import lru_cache
from typing import Any

client = anthropic.Anthropic()


class ToolSchemaRegistry:
    """
    Registry that builds tool schemas once and serves the same objects on every call.
    Uses lru_cache to ensure the built list is identical across calls.
    """

    _instance: "ToolSchemaRegistry | None" = None

    def __new__(cls) -> "ToolSchemaRegistry":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._built = False
        return cls._instance

    @lru_cache(maxsize=1)
    def get_tools(self) -> tuple[dict, ...]:
        """
        Returns a tuple (hashable, stable identity) of tool definitions.
        lru_cache ensures this runs exactly once.
        """
        tools: list[dict[str, Any]] = [
            {
                "name": "web_search",
                "description": "Search the web for up-to-date information.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "num_results": {"type": "integer", "default": 5},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "read_file",
                "description": "Read contents of a file by path.",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
            {
                "name": "write_file",
                "description": "Write content to a file.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
            {
                "name": "run_command",
                "description": "Run a shell command and return stdout.",
                "input_schema": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            },
        ]

        # Mark last tool as cache breakpoint
        tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
        print(f"[Schema build] Built {len(tools)} tool schemas (runs once per process)")
        return tuple(tools)

    def as_list(self) -> list[dict]:
        """Convert to list for API call — same underlying objects."""
        return list(self.get_tools())


registry = ToolSchemaRegistry()


def call_agent(prompt: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        tools=registry.as_list(),  # Same dict objects every call
        messages=[{"role": "user", "content": prompt}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    return next((b.text for b in response.content if b.type == "text"), "")


# Schema built exactly once regardless of how many calls are made
for i in range(3):
    result = call_agent(f"Search for Python asyncio tutorial (call {i+1})")
    print(f"Call {i+1}: {result[:60]}")
```

**Expected Token Savings:** `lru_cache` guarantees single build; stable object identity maximizes cache hit rate.
**Environment:** Python 3.8+; singleton pattern; `lru_cache` is stdlib, no dependencies.

---

### Option 3: Pydantic model → tool schema with compile-time generation

```python
import anthropic
from pydantic import BaseModel, Field
from typing import Any

client = anthropic.Anthropic()


def pydantic_to_tool_schema(model: type[BaseModel], name: str, description: str) -> dict[str, Any]:
    """Convert a Pydantic model to an Anthropic tool schema."""
    schema = model.model_json_schema()
    # Pydantic includes $defs for nested models — flatten if needed
    schema.pop("title", None)
    schema.pop("$defs", None)
    return {
        "name": name,
        "description": description,
        "input_schema": schema,
    }


# Define tool inputs as Pydantic models — validated at call time
class SearchInput(BaseModel):
    query: str = Field(..., description="Search query string")
    max_results: int = Field(5, ge=1, le=20, description="Number of results")
    category: str | None = Field(None, description="Optional category filter")


class CreateTicketInput(BaseModel):
    title: str = Field(..., max_length=100)
    description: str = Field(..., description="Detailed description")
    priority: str = Field(..., pattern="^(low|medium|high|critical)$")
    assignee_email: str | None = None


class GetUserInput(BaseModel):
    user_id: str = Field(..., description="User UUID")
    include_preferences: bool = Field(False)


# Build schemas at module level — ONE TIME
def _make_cached_tools() -> list[dict]:
    tools = [
        pydantic_to_tool_schema(SearchInput, "search", "Search the knowledge base"),
        pydantic_to_tool_schema(CreateTicketInput, "create_ticket", "Create a support ticket"),
        pydantic_to_tool_schema(GetUserInput, "get_user", "Get user account details"),
    ]
    tools[-1]["cache_control"] = {"type": "ephemeral"}
    return tools


TOOLS = _make_cached_tools()

# Tool dispatch with Pydantic validation
TOOL_MODELS = {
    "search": SearchInput,
    "create_ticket": CreateTicketInput,
    "get_user": GetUserInput,
}


def dispatch_tool(name: str, raw_input: dict) -> str:
    model_class = TOOL_MODELS.get(name)
    if not model_class:
        return f"Unknown tool: {name}"
    try:
        validated = model_class(**raw_input)
        return f"Tool {name} executed with validated input: {validated.model_dump()}"
    except Exception as e:
        return f"Validation error for {name}: {e}"


messages = [{"role": "user", "content": "Search for Python best practices"}]

while True:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        tools=TOOLS,
        messages=messages,
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    messages.append({"role": "assistant", "content": response.content})

    if response.stop_reason == "end_turn":
        print(next(b.text for b in response.content if b.type == "text"))
        break

    results = []
    for block in response.content:
        if block.type == "tool_use":
            result = dispatch_tool(block.name, block.input)
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
    messages.append({"role": "user", "content": results})
```

**Expected Token Savings:** Pydantic-derived schemas are generated at import time, never in the hot path; same cache benefits as Option 1.
**Environment:** Python 3.10+; requires `pydantic>=2.0` (`pip install pydantic`).

---

### Option 4: Schema version hash to detect when rebuild is actually needed

```python
import hashlib
import json
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic()


@dataclass
class VersionedToolSchema:
    tools: list[dict]
    version_hash: str
    build_count: int = 0


def compute_schema_hash(tools: list[dict]) -> str:
    """Hash tool schemas to detect content changes."""
    # Exclude cache_control from hash (it's metadata, not schema content)
    normalized = [{k: v for k, v in t.items() if k != "cache_control"} for t in tools]
    serialized = json.dumps(normalized, sort_keys=True)
    return hashlib.sha256(serialized.encode()).hexdigest()[:12]


def build_raw_tools() -> list[dict]:
    """The actual schema construction logic — only called when schemas change."""
    return [
        {
            "name": "analyze_sentiment",
            "description": "Analyze the sentiment of a text passage.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "language": {"type": "string", "default": "en"},
                },
                "required": ["text"],
            },
        },
        {
            "name": "translate_text",
            "description": "Translate text to a target language.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "target_language": {"type": "string", "description": "ISO 639-1 language code"},
                },
                "required": ["text", "target_language"],
            },
        },
        {
            "name": "summarize",
            "description": "Generate a summary of the provided text.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "max_sentences": {"type": "integer", "default": 3},
                    "style": {"type": "string", "enum": ["bullet", "paragraph", "headline"]},
                },
                "required": ["text"],
            },
        },
    ]


class CachingSchemaManager:
    """Rebuilds tool schemas only when content actually changes."""

    def __init__(self):
        self._current: VersionedToolSchema | None = None
        self._build_count = 0

    def get_tools(self, force_rebuild: bool = False) -> list[dict]:
        raw = build_raw_tools()
        new_hash = compute_schema_hash(raw)

        if (
            self._current is None
            or force_rebuild
            or self._current.version_hash != new_hash
        ):
            # Only rebuild when hash changes
            raw[-1] = {**raw[-1], "cache_control": {"type": "ephemeral"}}
            self._build_count += 1
            self._current = VersionedToolSchema(
                tools=raw,
                version_hash=new_hash,
                build_count=self._build_count,
            )
            print(f"[Schema] Built version {new_hash} (build #{self._build_count})")
        else:
            print(f"[Schema] Reusing version {new_hash} (no rebuild needed)")

        return self._current.tools

    @property
    def rebuild_count(self) -> int:
        return self._build_count


schema_manager = CachingSchemaManager()


def call_agent(prompt: str) -> str:
    tools = schema_manager.get_tools()  # Returns cached version if schema unchanged
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        tools=tools,
        messages=[{"role": "user", "content": prompt}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    return next((b.text for b in response.content if b.type == "text"), "")


for i in range(4):
    call_agent(f"Analyze sentiment of turn {i}")

print(f"\nTotal schema rebuilds: {schema_manager.rebuild_count} (expected: 1)")
```

**Expected Token Savings:** Hash-based caching ensures schemas are rebuilt only when they actually change (e.g., after a deploy), not on every call.
**Environment:** Python 3.9+; hash comparison costs microseconds; reliable for dynamic tool sets that change rarely.

---

### Option 5: Async tool registry with warm-up call

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

_tools_cache: list[dict] | None = None
_cache_lock = asyncio.Lock()
_cache_warm = False


def _build_tools_raw() -> list[dict]:
    tools = [
        {
            "name": "fetch_data",
            "description": "Fetch data from an API endpoint.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "method": {"type": "string", "enum": ["GET", "POST"], "default": "GET"},
                    "timeout": {"type": "integer", "default": 30},
                },
                "required": ["url"],
            },
        },
        {
            "name": "store_result",
            "description": "Store a result to the key-value store.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                    "ttl_seconds": {"type": "integer", "default": 3600},
                },
                "required": ["key", "value"],
            },
        },
        {
            "name": "send_notification",
            "description": "Send a notification to a user.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "recipient": {"type": "string"},
                    "message": {"type": "string"},
                    "channel": {"type": "string", "enum": ["email", "slack", "sms"]},
                },
                "required": ["recipient", "message", "channel"],
            },
        },
    ]
    tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    return tools


async def get_tools() -> list[dict]:
    """Return cached tool schemas, building once under a lock."""
    global _tools_cache
    if _tools_cache is not None:
        return _tools_cache
    async with _cache_lock:
        if _tools_cache is None:  # Double-check after acquiring lock
            _tools_cache = _build_tools_raw()
            print(f"[Tools] Initialized {len(_tools_cache)} schemas")
    return _tools_cache


async def warm_up_tool_cache() -> None:
    """
    Make a minimal API call at startup to write the tool schema to Claude's cache.
    Subsequent calls pay the cache read price (10% of normal).
    """
    global _cache_warm
    if _cache_warm:
        return

    tools = await get_tools()
    print("[Warmup] Writing tool schema to prompt cache...")
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1,
        tools=tools,
        messages=[{"role": "user", "content": "ping"}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    cache_write = getattr(response.usage, "cache_creation_input_tokens", 0)
    print(f"[Warmup] Cache written: {cache_write} tokens cached")
    _cache_warm = True


async def handle_request(user_message: str) -> str:
    tools = await get_tools()
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        tools=tools,
        messages=[{"role": "user", "content": user_message}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    cache_read = getattr(response.usage, "cache_read_input_tokens", 0)
    print(f"[Request] cache_read={cache_read} tokens")
    return next((b.text for b in response.content if b.type == "text"), "")


async def main():
    # Warm up cache at startup (before serving real traffic)
    await warm_up_tool_cache()

    # All real requests read from cache
    results = await asyncio.gather(
        handle_request("Fetch data from https://api.example.com/users"),
        handle_request("Store 'result_1' with key 'job-42'"),
        handle_request("Send a Slack notification to alice@example.com"),
    )
    for r in results:
        print(f"Result: {r[:80]}")


asyncio.run(main())
```

**Expected Token Savings:** Warm-up call front-loads the cache write; all real requests pay only cache read price (10% of normal input cost).
**Environment:** Python 3.11+; warm-up pattern suits FastAPI `lifespan` startup or container initialization.

---

### Option 6: Cost comparison — rebuilding vs. caching tool schemas

```python
import json
import time
import anthropic

client = anthropic.Anthropic()

# Pricing constants (claude-sonnet-4-6)
INPUT_PRICE_PER_TOKEN = 3.00 / 1_000_000        # $3 per 1M input tokens
CACHE_WRITE_PRICE = 3.75 / 1_000_000            # $3.75 per 1M tokens written to cache
CACHE_READ_PRICE = 0.30 / 1_000_000             # $0.30 per 1M tokens read from cache (10%)


def count_tokens_estimate(tools: list[dict]) -> int:
    return len(json.dumps(tools)) // 4


def cost_analysis(num_turns: int, tool_token_count: int) -> None:
    """Compare cost of rebuilding vs. caching tool schemas over N turns."""

    # Without caching: pay full input price every turn
    no_cache_cost = num_turns * tool_token_count * INPUT_PRICE_PER_TOKEN

    # With caching: pay write price once, read price for remaining turns
    cache_write_cost = tool_token_count * CACHE_WRITE_PRICE
    cache_read_cost = (num_turns - 1) * tool_token_count * CACHE_READ_PRICE
    cache_total = cache_write_cost + cache_read_cost

    savings = no_cache_cost - cache_total
    savings_pct = (savings / no_cache_cost) * 100 if no_cache_cost > 0 else 0

    print(f"\n{'='*50}")
    print(f"Tool schema tokens: {tool_token_count:,}")
    print(f"Conversation turns: {num_turns}")
    print(f"\nWithout caching: ${no_cache_cost:.4f}")
    print(f"With caching:    ${cache_total:.4f}")
    print(f"  └ Cache write:  ${cache_write_cost:.4f} (turn 1)")
    print(f"  └ Cache reads:  ${cache_read_cost:.4f} (turns 2–{num_turns})")
    print(f"Savings:         ${savings:.4f} ({savings_pct:.1f}%)")


# Demonstrate with realistic tool sets
SMALL_TOOLS = [
    {"name": "search", "description": "Search docs", "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}},
    {"name": "fetch", "description": "Fetch URL", "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
]

LARGE_TOOLS = SMALL_TOOLS * 8  # Simulate 16 tools with longer descriptions

small_tokens = count_tokens_estimate(SMALL_TOOLS)
large_tokens = count_tokens_estimate(LARGE_TOOLS)

print("=== Small tool set (2 tools) ===")
cost_analysis(num_turns=10, tool_token_count=small_tokens)
cost_analysis(num_turns=50, tool_token_count=small_tokens)

print("\n=== Large tool set (16 tools) ===")
cost_analysis(num_turns=10, tool_token_count=large_tokens)
cost_analysis(num_turns=50, tool_token_count=large_tokens)

# Now demonstrate the correct cached setup
CACHED_LARGE_TOOLS = [*LARGE_TOOLS[:-1], {**LARGE_TOOLS[-1], "cache_control": {"type": "ephemeral"}}]

print(f"\nLarge tool set size: ~{large_tokens} tokens")
print(f"Cached version has cache_control on last tool: {bool(CACHED_LARGE_TOOLS[-1].get('cache_control'))}")
```

**Expected Token Savings:** 85–95% tool schema cost reduction after turn 1; large tool libraries (10+ tools, 2,000+ schema tokens) save $0.05–0.50 per 1,000 conversation turns.
**Environment:** Python 3.9+; cost analysis runs without API calls; adjust pricing constants as Anthropic updates rates.

---

| Option | Approach | Build Frequency | Best For |
|--------|----------|----------------|----------|
| 1 | Module-level constant | Once per process | Simplest; most reliable |
| 2 | `lru_cache` singleton | Once per process | Library code |
| 3 | Pydantic-derived at import | Once per process | Type-safe tool inputs |
| 4 | Hash-based conditional rebuild | Only on content change | Dynamic tool sets |
| 5 | Async lock + warm-up call | Once per startup | Async services |
| 6 | Cost analysis comparison | N/A (diagnostic) | Understanding ROI |
