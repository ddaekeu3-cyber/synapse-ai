---
layout: solution
title: "Agent Doesn't Implement Tool Call Deduplication"
category: tool-failure
description: "Agent makes redundant identical tool calls within the same session, wasting API quota, increasing latency, and causing side-effects to fire multiple times."
tags: [tool-failure, deduplication, caching, idempotency, performance]
---

# Agent Doesn't Implement Tool Call Deduplication

## Problem

When an agent calls the same tool with identical arguments multiple times within a session—either by forgetting a prior result or reprocessing the same subtask—it wastes external API quota, re-triggers side effects (emails sent twice, payments charged twice), and inflates latency. Without deduplication, a simple multi-turn conversation can make dozens of redundant network calls.

## Solution Options

### Option 1: Argument Hash Cache (In-Memory Deduplication)

```python
import anthropic
import hashlib
import json
from typing import Any

client = anthropic.Anthropic()

# Simple in-memory dedup cache: {cache_key: result}
_tool_cache: dict[str, Any] = {}

def make_cache_key(tool_name: str, tool_input: dict) -> str:
    payload = json.dumps({"tool": tool_name, "input": tool_input}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()

def execute_tool(tool_name: str, tool_input: dict) -> dict:
    """Execute tool with deduplication."""
    cache_key = make_cache_key(tool_name, tool_input)

    if cache_key in _tool_cache:
        print(f"[DEDUP] Cache hit for {tool_name} — skipping execution")
        return {"result": _tool_cache[cache_key], "from_cache": True}

    # Simulate tool execution
    if tool_name == "fetch_user_profile":
        result = {"id": tool_input["user_id"], "name": "Alice", "plan": "pro"}
    elif tool_name == "lookup_pricing":
        result = {"plan": tool_input["plan"], "price": 29.99}
    else:
        result = {"status": "ok"}

    _tool_cache[cache_key] = result
    print(f"[TOOL] Executed {tool_name} — cached for future calls")
    return {"result": result, "from_cache": False}

def run_agent_with_dedup(user_message: str):
    tools = [
        {
            "name": "fetch_user_profile",
            "description": "Fetch user profile by ID",
            "input_schema": {
                "type": "object",
                "properties": {"user_id": {"type": "string"}},
                "required": ["user_id"],
            },
        },
        {
            "name": "lookup_pricing",
            "description": "Look up pricing for a plan",
            "input_schema": {
                "type": "object",
                "properties": {"plan": {"type": "string"}},
                "required": ["plan"],
            },
        },
    ]

    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    print(f"Assistant: {block.text}")
            break

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                tool_result = execute_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(tool_result),
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

run_agent_with_dedup("What's Alice's (user ID: u123) current plan and how much does it cost?")

# Expected Token Savings: 15-40% on tool-heavy conversations with repeated lookups
# Environment: Single-process agents; cache is per-session in memory
```

### Option 2: TTL-Based Deduplication with Expiry

```python
import anthropic
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

client = anthropic.Anthropic()

@dataclass
class CacheEntry:
    result: Any
    created_at: float
    hit_count: int = 0

class TTLToolCache:
    def __init__(self, ttl_seconds: float = 300.0):
        self.ttl = ttl_seconds
        self._cache: dict[str, CacheEntry] = {}

    def _key(self, tool_name: str, tool_input: dict) -> str:
        payload = json.dumps({"tool": tool_name, "input": tool_input}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def get(self, tool_name: str, tool_input: dict) -> tuple[bool, Any]:
        key = self._key(tool_name, tool_input)
        entry = self._cache.get(key)
        if entry is None:
            return False, None
        if time.time() - entry.created_at > self.ttl:
            del self._cache[key]
            return False, None
        entry.hit_count += 1
        return True, entry.result

    def set(self, tool_name: str, tool_input: dict, result: Any):
        key = self._key(tool_name, tool_input)
        self._cache[key] = CacheEntry(result=result, created_at=time.time())

    def stats(self) -> dict:
        total_hits = sum(e.hit_count for e in self._cache.values())
        return {"entries": len(self._cache), "total_hits": total_hits}

cache = TTLToolCache(ttl_seconds=120)

def simulate_tool(tool_name: str, tool_input: dict) -> dict:
    time.sleep(0.01)  # Simulate network latency
    if tool_name == "get_inventory":
        return {"item": tool_input["item_id"], "stock": 42, "warehouse": "US-WEST"}
    return {"status": "ok"}

def run_with_ttl_dedup(messages: list[dict], tools: list[dict]) -> str:
    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text
            return ""

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                hit, cached = cache.get(block.name, block.input)
                if hit:
                    result = cached
                    source = "CACHE"
                else:
                    result = simulate_tool(block.name, block.input)
                    cache.set(block.name, block.input, result)
                    source = "LIVE"
                print(f"[{source}] {block.name}({block.input})")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

tools = [{
    "name": "get_inventory",
    "description": "Get inventory level for an item",
    "input_schema": {
        "type": "object",
        "properties": {"item_id": {"type": "string"}},
        "required": ["item_id"],
    },
}]

result = run_with_ttl_dedup(
    [{"role": "user", "content": "Check inventory for item A99, then double-check A99 and also check A99 once more."}],
    tools,
)
print(f"Result: {result}")
print(f"Cache stats: {cache.stats()}")

# Expected Token Savings: Eliminates redundant external calls; ~0 additional Claude tokens
# Environment: Short-lived agents; TTL prevents serving stale data for more than N minutes
```

### Option 3: Side-Effect-Safe Deduplication with Mutation Detection

```python
import anthropic
import hashlib
import json
from enum import Enum
from typing import Any

client = anthropic.Anthropic()

class ToolSafety(Enum):
    READONLY = "readonly"    # Safe to deduplicate always
    IDEMPOTENT = "idempotent"  # Safe to deduplicate within session
    MUTABLE = "mutable"     # Never deduplicate — side effects

# Declare tool safety levels
TOOL_SAFETY: dict[str, ToolSafety] = {
    "get_user": ToolSafety.READONLY,
    "search_products": ToolSafety.READONLY,
    "calculate_price": ToolSafety.READONLY,
    "create_order": ToolSafety.MUTABLE,      # Never deduplicate!
    "send_email": ToolSafety.MUTABLE,        # Never deduplicate!
    "update_inventory": ToolSafety.IDEMPOTENT,
}

_session_cache: dict[str, Any] = {}
_call_log: list[dict] = []

def safe_execute_tool(tool_name: str, tool_input: dict) -> dict:
    safety = TOOL_SAFETY.get(tool_name, ToolSafety.MUTABLE)

    if safety == ToolSafety.MUTABLE:
        # Never deduplicate mutable tools
        result = _real_execute(tool_name, tool_input)
        _call_log.append({"tool": tool_name, "input": tool_input, "deduped": False})
        return result

    # For READONLY and IDEMPOTENT: check cache
    cache_key = hashlib.sha256(
        json.dumps({"tool": tool_name, "input": tool_input}, sort_keys=True).encode()
    ).hexdigest()

    if cache_key in _session_cache:
        _call_log.append({"tool": tool_name, "input": tool_input, "deduped": True})
        print(f"[DEDUP:{safety.value}] {tool_name} — returning cached result")
        return _session_cache[cache_key]

    result = _real_execute(tool_name, tool_input)
    _session_cache[cache_key] = result
    _call_log.append({"tool": tool_name, "input": tool_input, "deduped": False})
    return result

def _real_execute(tool_name: str, tool_input: dict) -> dict:
    """Simulate real tool execution."""
    print(f"[EXECUTE] {tool_name}({json.dumps(tool_input)})")
    return {"tool": tool_name, "input": tool_input, "status": "executed"}

tools = [
    {
        "name": "get_user",
        "description": "Get user information",
        "input_schema": {"type": "object", "properties": {"user_id": {"type": "string"}}, "required": ["user_id"]},
    },
    {
        "name": "send_email",
        "description": "Send an email to the user",
        "input_schema": {
            "type": "object",
            "properties": {"to": {"type": "string"}, "subject": {"type": "string"}},
            "required": ["to", "subject"],
        },
    },
]

messages = [{"role": "user", "content": "Get user u42 info and then send them a welcome email."}]

while True:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        tools=tools,
        messages=messages,
    )
    if response.stop_reason == "end_turn":
        break
    tool_results = []
    for block in response.content:
        if block.type == "tool_use":
            result = safe_execute_tool(block.name, block.input)
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
    messages.append({"role": "assistant", "content": response.content})
    messages.append({"role": "user", "content": tool_results})

print(f"\nCall log: {json.dumps(_call_log, indent=2)}")

# Expected Token Savings: Prevents double side-effects; read tool savings of 20-60% on data-heavy flows
# Environment: E-commerce/transactional agents where mutable tools must never be deduplicated
```

### Option 4: Cross-Turn Deduplication with Call Graph

```python
import anthropic
import hashlib
import json
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class ToolCall:
    tool_name: str
    tool_input: dict
    result: dict
    turn: int
    call_index: int

@dataclass
class CallGraph:
    calls: list[ToolCall] = field(default_factory=list)
    _index: dict[str, int] = field(default_factory=dict)  # key -> calls index

    def _key(self, tool_name: str, tool_input: dict) -> str:
        return hashlib.sha256(
            json.dumps({"tool": tool_name, "input": tool_input}, sort_keys=True).encode()
        ).hexdigest()

    def find(self, tool_name: str, tool_input: dict) -> ToolCall | None:
        key = self._key(tool_name, tool_input)
        idx = self._index.get(key)
        return self.calls[idx] if idx is not None else None

    def record(self, tool_name: str, tool_input: dict, result: dict, turn: int) -> ToolCall:
        call = ToolCall(
            tool_name=tool_name,
            tool_input=tool_input,
            result=result,
            turn=turn,
            call_index=len(self.calls),
        )
        self.calls.append(call)
        self._index[self._key(tool_name, tool_input)] = len(self.calls) - 1
        return call

    def summary(self) -> dict:
        deduped = sum(1 for c in self.calls if c.call_index != self.calls.index(c))
        return {
            "total_calls_attempted": len(self.calls),
            "unique_executions": len(self._index),
        }

def run_with_call_graph():
    graph = CallGraph()
    turn = 0

    tools = [
        {
            "name": "database_query",
            "description": "Query the database",
            "input_schema": {
                "type": "object",
                "properties": {
                    "table": {"type": "string"},
                    "filter": {"type": "string"},
                },
                "required": ["table"],
            },
        }
    ]

    messages = [
        {
            "role": "user",
            "content": "Query the users table for active users, then query users again for active users to confirm, then give me a summary.",
        }
    ]

    while True:
        turn += 1
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    print(f"Assistant: {block.text}")
            break

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                existing = graph.find(block.name, block.input)
                if existing:
                    print(f"[DEDUP] Turn {turn}: {block.name} was called on turn {existing.turn} — reusing result")
                    result = existing.result
                else:
                    # Simulate DB query
                    result = {"rows": [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}], "count": 2}
                    graph.record(block.name, block.input, result, turn)
                    print(f"[EXECUTE] Turn {turn}: {block.name}({block.input})")

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    print(f"\nCall graph summary: {graph.summary()}")

run_with_call_graph()

# Expected Token Savings: 30-50% reduction in external API calls in multi-turn sessions
# Environment: Agents with long conversations where the same data is referenced repeatedly
```

### Option 5: Semantic Deduplication for Near-Identical Calls

```python
import anthropic
import json
import re
from difflib import SequenceMatcher

client = anthropic.Anthropic()

def normalize_input(tool_input: dict) -> dict:
    """Normalize tool inputs to catch near-identical calls."""
    normalized = {}
    for k, v in tool_input.items():
        if isinstance(v, str):
            # Strip whitespace, lowercase, remove punctuation for string fields
            normalized[k] = re.sub(r'[^\w\s]', '', v.lower().strip())
        elif isinstance(v, list):
            normalized[k] = sorted([str(i).lower() for i in v])
        else:
            normalized[k] = v
    return normalized

def semantic_similarity(input_a: dict, input_b: dict) -> float:
    """Compute similarity between two tool inputs."""
    str_a = json.dumps(normalize_input(input_a), sort_keys=True)
    str_b = json.dumps(normalize_input(input_b), sort_keys=True)
    return SequenceMatcher(None, str_a, str_b).ratio()

class SemanticDeduplicator:
    def __init__(self, similarity_threshold: float = 0.90):
        self.threshold = similarity_threshold
        self.calls: list[tuple[str, dict, dict]] = []  # (tool_name, input, result)

    def find_similar(self, tool_name: str, tool_input: dict) -> dict | None:
        for stored_name, stored_input, stored_result in self.calls:
            if stored_name != tool_name:
                continue
            sim = semantic_similarity(tool_input, stored_input)
            if sim >= self.threshold:
                print(f"[SEMANTIC-DEDUP] {tool_name}: similarity={sim:.2f} >= {self.threshold} — reusing result")
                return stored_result
        return None

    def record(self, tool_name: str, tool_input: dict, result: dict):
        self.calls.append((tool_name, tool_input, result))

deduplicator = SemanticDeduplicator(similarity_threshold=0.85)

tools = [
    {
        "name": "search_knowledge_base",
        "description": "Search the knowledge base",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }
]

def run_semantic_dedup_agent(user_message: str):
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    print(f"Assistant: {block.text}")
            break

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                cached = deduplicator.find_similar(block.name, block.input)
                if cached is not None:
                    result = cached
                else:
                    result = {
                        "results": [
                            {"title": "Claude API Guide", "score": 0.92},
                            {"title": "Tool Use Patterns", "score": 0.87},
                        ]
                    }
                    deduplicator.record(block.name, block.input, result)
                    print(f"[EXECUTE] {block.name}: query='{block.input.get('query')}'")

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

# Near-duplicate queries will be caught by semantic similarity
run_semantic_dedup_agent(
    "Search for 'Claude API documentation', then search for 'claude api docs', "
    "then look up 'Claude API reference guide'."
)

# Expected Token Savings: 25-50% on agents with fuzzy/paraphrased repeat queries
# Environment: Knowledge base agents, search-heavy workflows with user query variations
```

### Option 6: Distributed Deduplication with SQLite Persistence

```python
import anthropic
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

client = anthropic.Anthropic()

DB_PATH = Path("/tmp/tool_dedup_cache.db")

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tool_cache (
                cache_key TEXT PRIMARY KEY,
                tool_name TEXT NOT NULL,
                tool_input TEXT NOT NULL,
                result TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL,
                hit_count INTEGER DEFAULT 0,
                session_id TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_expires ON tool_cache(expires_at)")
        conn.commit()

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def cache_key(tool_name: str, tool_input: dict) -> str:
    payload = json.dumps({"tool": tool_name, "input": tool_input}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()

def get_cached(tool_name: str, tool_input: dict) -> dict | None:
    key = cache_key(tool_name, tool_input)
    now = time.time()
    with get_db() as conn:
        row = conn.execute(
            "SELECT result, expires_at FROM tool_cache WHERE cache_key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] and row["expires_at"] < now:
            conn.execute("DELETE FROM tool_cache WHERE cache_key = ?", (key,))
            conn.commit()
            return None
        conn.execute("UPDATE tool_cache SET hit_count = hit_count + 1 WHERE cache_key = ?", (key,))
        conn.commit()
        return json.loads(row["result"])

def store_cached(tool_name: str, tool_input: dict, result: dict, ttl: float = 300, session_id: str = ""):
    key = cache_key(tool_name, tool_input)
    now = time.time()
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO tool_cache
               (cache_key, tool_name, tool_input, result, created_at, expires_at, session_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (key, tool_name, json.dumps(tool_input), json.dumps(result),
             now, now + ttl, session_id),
        )
        conn.commit()

def evict_expired():
    with get_db() as conn:
        deleted = conn.execute(
            "DELETE FROM tool_cache WHERE expires_at < ?", (time.time(),)
        ).rowcount
        conn.commit()
    return deleted

def cache_stats() -> dict:
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as total, SUM(hit_count) as total_hits FROM tool_cache"
        ).fetchone()
        return {"entries": row["total"], "total_hits": row["total_hits"] or 0}

init_db()
SESSION_ID = f"session_{int(time.time())}"

tools = [
    {
        "name": "fetch_config",
        "description": "Fetch application configuration",
        "input_schema": {
            "type": "object",
            "properties": {"config_key": {"type": "string"}},
            "required": ["config_key"],
        },
    }
]

def run_persistent_dedup_agent(user_message: str):
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    print(f"Assistant: {block.text}")
            break

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                cached = get_cached(block.name, block.input)
                if cached is not None:
                    result = cached
                    print(f"[DB-CACHE HIT] {block.name}({block.input})")
                else:
                    result = {"key": block.input["config_key"], "value": "production", "version": "v2"}
                    store_cached(block.name, block.input, result, ttl=600, session_id=SESSION_ID)
                    print(f"[DB-EXECUTE] {block.name}({block.input})")

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

run_persistent_dedup_agent("Fetch config for 'env', then fetch config for 'env' again, then fetch config for 'env' one more time.")
evict_expired()
print(f"\nDB cache stats: {cache_stats()}")

# Expected Token Savings: 40-70% on repeated config/data lookups; survives process restarts
# Environment: Long-running services, multi-worker deployments sharing a SQLite sidecar
```

## Comparison

| Option | Dedup Scope | Side-Effect Safe | Persistence | Fuzzy Match | Best For |
|--------|-------------|-----------------|-------------|-------------|---------|
| 1. Argument Hash Cache | Session | No distinction | Memory | No | Simple single-process agents |
| 2. TTL-Based | Session+Time | No distinction | Memory | No | Data with expiry requirements |
| 3. Safety-Aware | Session | Yes (MUTABLE blocked) | Memory | No | Transactional/e-commerce agents |
| 4. Call Graph | Cross-turn | Configurable | Memory | No | Long multi-turn conversations |
| 5. Semantic | Session | No distinction | Memory | Yes | Search/query-heavy agents |
| 6. SQLite Persistent | Cross-session | No distinction | Disk | No | Multi-worker/long-running services |
