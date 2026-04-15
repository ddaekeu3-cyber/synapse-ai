---
layout: solution
title: "Agent Sends Redundant System Prompts in Multi-Turn Conversations"
category: token-cost
description: "Agent re-sends the full system prompt on every API call in a conversation, paying full input token cost each turn instead of using prompt caching."
tags: [token-cost, prompt-caching, multi-turn, performance, cost]
---

## Symptom

Each turn of a multi-turn conversation bills the same large system prompt as new input tokens. With a 10,000-token system prompt and a 20-turn conversation, the agent pays for 200,000 system prompt tokens when it should pay for ~10,000 (first turn) plus ~200,000 × 0.1 (cache hits). At scale, this is the single largest avoidable cost in most agent deployments.

Observable signs:
- Input token count in API responses is nearly constant each turn (matches system prompt size)
- Cost per conversation grows linearly with turn count instead of sublinearly
- `cache_read_input_tokens` in API response is always 0
- `usage.cache_creation_input_tokens` never appears in logs

```python
# BROKEN: full system prompt sent every single turn
def chat(history: list[dict], user_message: str) -> str:
    history.append({"role": "user", "content": user_message})
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=LARGE_SYSTEM_PROMPT,   # 10,000 tokens re-sent every call
        messages=history,
    )
    reply = response.content[0].text
    history.append({"role": "assistant", "content": reply})
    return reply
# Turn 1: 10,000 system + 20 user = 10,020 input tokens
# Turn 20: 10,000 system + 8,000 history + 50 user = 18,050 input tokens
# Total: ~280,000 input tokens for the system prompt alone
```

---

## Root Cause

The Anthropic API requires `system` to be sent on every request — it is not persisted server-side between calls. Without prompt caching, the full text is tokenized and billed as new input tokens each time.

Prompt caching solves this: mark a system prompt block with `cache_control: {type: ephemeral}` and Anthropic will cache the KV state of that block for 5 minutes (extended to 1 hour on subsequent hits). Cache hits cost 10% of normal input token price.

The catch: caching requires the cached content to appear at consistent positions in the request, with identical byte content. Any mutation — even a trailing space — busts the cache.

---

## Fix

### Option 1 — Single cache_control Block on System Prompt

The minimal change: add `cache_control` to the system prompt so it's cached after the first turn.

```python
import anthropic

client = anthropic.Anthropic()

# Large system prompt — represents tools, instructions, examples, etc.
SYSTEM_PROMPT = """You are an expert software engineering assistant for AcmeCorp.

## Company Context
AcmeCorp builds distributed data infrastructure. Our stack: Python, Go, Kubernetes,
PostgreSQL, Redis, Kafka. We follow trunk-based development with feature flags.
All code must pass mypy strict, ruff, and our custom linter rules.

## Engineering Standards
[... imagine 8,000 more tokens of context: coding standards, architecture docs,
 API references, team conventions, example code patterns, etc. ...]

## Response Format
- Lead with the answer, not the explanation
- Include runnable code examples when relevant
- Reference specific file paths when discussing our codebase
- Flag security implications explicitly
"""

# Structure system as a list with cache_control — NOT as a plain string
CACHED_SYSTEM = [
    {
        "type": "text",
        "text": SYSTEM_PROMPT,
        "cache_control": {"type": "ephemeral"},  # cache this block
    }
]

def chat_with_cache(history: list[dict], user_message: str) -> tuple[str, dict]:
    """Chat with prompt caching enabled. Returns reply and usage stats."""
    history.append({"role": "user", "content": user_message})

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=CACHED_SYSTEM,    # identical structure every call → cache hit after turn 1
        messages=history,
        betas=["prompt-caching-2024-07-31"],  # required for explicit cache control
    )

    reply = response.content[0].text
    history.append({"role": "assistant", "content": reply})

    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_creation_tokens": getattr(response.usage, "cache_creation_input_tokens", 0),
        "cache_read_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
    }

    return reply, usage

# Demonstrate cache behavior across turns
history = []
topics = [
    "What's the best way to handle database migrations in our stack?",
    "How should I structure error handling in our Go services?",
    "What's our approach to feature flags?",
    "How do we handle secrets management?",
]

print("Turn | Input Tokens | Cache Creation | Cache Read | Effective Cost%")
print("-" * 70)

for i, topic in enumerate(topics):
    reply, usage = chat_with_cache(history, topic)
    cache_pct = (usage["cache_read_tokens"] / max(usage["input_tokens"], 1)) * 100
    effective_cost = usage["input_tokens"] - usage["cache_read_tokens"] * 0.9  # 10% of cache hits
    print(
        f"  {i+1:2d} | {usage['input_tokens']:12,d} | {usage['cache_creation_tokens']:14,d} | "
        f"{usage['cache_read_tokens']:10,d} | {cache_pct:.0f}% cached"
    )

# Expected output:
#  1 | 10,020 | 10,000 (cache created) |      0 | 0% cached
#  2 |  8,120 |                     0  | 10,000 | 86% cached  ← massive saving
#  3 |  9,340 |                     0  | 10,000 | 73% cached
#  4 | 10,890 |                     0  | 10,000 | 68% cached
```

**Expected Token Savings:** 85-90% reduction on system prompt tokens from turn 2 onward. A 10,000-token system prompt cached for 20 turns saves ~171,000 input tokens vs. full re-send.

**Environment:** Python 3.9+, `anthropic>=0.40.0`. Prompt caching is available on Claude Haiku 4.5, Sonnet 4.6, and Opus 4.6. The `betas` parameter enables explicit cache control.

---

### Option 2 — Multi-Block Caching for System + Tool Definitions

Cache both the system prompt and tool definitions separately to maximize cache hits when tools change less often than user conversation.

```python
import anthropic
import json

client = anthropic.Anthropic()

BASE_INSTRUCTIONS = """You are a data analysis assistant. Help users query, transform,
and visualize datasets. Always validate inputs before executing queries.
[... 5,000 tokens of base instructions ...]"""

TOOL_REFERENCE = """## Available Tools Reference

### query_database
Executes SQL against the analytics warehouse. Supports SELECT only.
Parameters: sql (string), timeout_seconds (int, default 30)
Returns: {rows: [...], column_names: [...], row_count: int}

### create_visualization
Generates a chart from tabular data.
Parameters: data (array), chart_type (bar|line|pie|scatter), title (string)
Returns: {chart_url: string, alt_text: string}

[... 3,000 tokens of tool docs ...]"""

# Two cached blocks — base instructions cached longer, tools cached separately
MULTI_BLOCK_SYSTEM = [
    {
        "type": "text",
        "text": BASE_INSTRUCTIONS,
        "cache_control": {"type": "ephemeral"},  # 5-min TTL, extended on hits
    },
    {
        "type": "text",
        "text": TOOL_REFERENCE,
        "cache_control": {"type": "ephemeral"},  # separate cache entry
    },
]

TOOLS = [
    {
        "name": "query_database",
        "description": "Execute a SQL SELECT query against the analytics warehouse.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "SELECT statement to execute"},
                "timeout_seconds": {"type": "integer", "default": 30},
            },
            "required": ["sql"]
        }
    },
    {
        "name": "create_visualization",
        "description": "Create a chart from data.",
        "input_schema": {
            "type": "object",
            "properties": {
                "data": {"type": "array"},
                "chart_type": {"type": "string", "enum": ["bar", "line", "pie", "scatter"]},
                "title": {"type": "string"},
            },
            "required": ["data", "chart_type", "title"]
        }
    }
]

def run_analysis_session(questions: list[str]) -> list[dict]:
    """Run multiple analysis questions in one session, caching system + tools."""
    history = []
    session_usage = []

    for question in questions:
        history.append({"role": "user", "content": question})

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=MULTI_BLOCK_SYSTEM,  # both blocks cached from turn 2 onward
            tools=TOOLS,
            messages=history,
            betas=["prompt-caching-2024-07-31"],
        )

        # Handle tool use or text response
        if response.stop_reason == "tool_use":
            # In real code: execute tool and continue conversation
            history.append({"role": "assistant", "content": response.content})
        else:
            history.append({"role": "assistant", "content": response.content[0].text})

        session_usage.append({
            "question": question[:50],
            "input": response.usage.input_tokens,
            "cache_created": getattr(response.usage, "cache_creation_input_tokens", 0),
            "cache_read": getattr(response.usage, "cache_read_input_tokens", 0),
        })

    return session_usage

results = run_analysis_session([
    "Show me total revenue by region for Q4 2024",
    "Which product category had the highest growth?",
    "Create a bar chart of the top 10 customers by spend",
    "What's the week-over-week trend for active users?",
])

total_input = sum(r["input"] for r in results)
total_cache_read = sum(r["cache_read"] for r in results)
print(f"Total input tokens: {total_input:,}")
print(f"Served from cache: {total_cache_read:,} ({total_cache_read/total_input:.0%})")
print(f"Estimated savings: ~{int(total_cache_read * 0.9):,} tokens (90% discount on cache reads)")
```

**Expected Token Savings:** Caching both system (5,000 tokens) and tool reference (3,000 tokens) saves ~14,400 tokens per turn from turn 2 onward. 10-turn session: ~130,000 tokens saved.

**Environment:** Python 3.9+, `anthropic>=0.40.0` with prompt caching beta.

---

### Option 3 — Async Multi-Session Cache Warming

Pre-warm the cache at session start so the first real user turn already hits the cache.

```python
import anthropic
import asyncio

client = anthropic.AsyncAnthropic()

LARGE_SYSTEM_PROMPT = """[System prompt with 8,000+ tokens of context...]"""

CACHED_SYSTEM = [
    {
        "type": "text",
        "text": LARGE_SYSTEM_PROMPT,
        "cache_control": {"type": "ephemeral"},
    }
]

async def warm_cache() -> dict:
    """
    Send a minimal message to prime the cache before the real conversation starts.
    The cache TTL is 5 minutes, extended on each hit — warm on session start.
    """
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1,           # minimal output — we only want to cache the system prompt
        system=CACHED_SYSTEM,
        messages=[{"role": "user", "content": "ready"}],
        betas=["prompt-caching-2024-07-31"],
    )

    usage = {
        "cache_created": getattr(response.usage, "cache_creation_input_tokens", 0),
        "cache_read": getattr(response.usage, "cache_read_input_tokens", 0),
    }
    return usage

async def chat_turn(history: list[dict], user_message: str) -> tuple[str, dict]:
    """Single conversation turn — cache should already be warm."""
    history.append({"role": "user", "content": user_message})

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=CACHED_SYSTEM,
        messages=history,
        betas=["prompt-caching-2024-07-31"],
    )

    reply = response.content[0].text
    history.append({"role": "assistant", "content": reply})

    return reply, {
        "input": response.usage.input_tokens,
        "cache_read": getattr(response.usage, "cache_read_input_tokens", 0),
    }

async def run_warmed_session(user_messages: list[str]) -> None:
    """Warm cache at session start, then process messages."""

    # Warm cache concurrently with session setup (e.g., loading user profile)
    print("Warming prompt cache...")
    warm_task = asyncio.create_task(warm_cache())

    # Simulate concurrent session setup work
    await asyncio.sleep(0.1)

    warm_usage = await warm_task
    print(f"Cache warmed: {warm_usage['cache_created']:,} tokens stored")

    history = []
    for msg in user_messages:
        reply, usage = await chat_turn(history, msg)
        saved = usage["cache_read"]
        print(f"Turn: {usage['input']:,} input tokens, {saved:,} from cache ({saved/max(usage['input'],1):.0%})")

asyncio.run(run_warmed_session([
    "Summarize our Q3 performance",
    "What are the main risks for Q4?",
    "Draft an executive summary",
]))
```

**Expected Token Savings:** Cache warming costs ~8,000 tokens once; saves ~7,200 tokens (90% discount) on every subsequent turn including the first.

**Environment:** Python 3.9+, `asyncio`, `anthropic>=0.40.0` with prompt caching beta.

---

### Option 4 — Cache-Aware Conversation Manager

Track cache efficiency per session and alert when cache is busted (e.g., by accidental system prompt mutation).

```python
import anthropic
import hashlib
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class CacheStats:
    turns: int = 0
    total_input_tokens: int = 0
    total_cache_created: int = 0
    total_cache_read: int = 0
    cache_busts: int = 0
    last_system_hash: str = ""

    @property
    def cache_hit_rate(self) -> float:
        if self.total_input_tokens == 0:
            return 0.0
        return self.total_cache_read / self.total_input_tokens

    @property
    def estimated_savings_tokens(self) -> int:
        """Tokens saved vs. paying full price on every turn."""
        return int(self.total_cache_read * 0.9)  # 90% discount on cache reads

    def report(self) -> str:
        return (
            f"Cache stats: {self.turns} turns | "
            f"{self.total_input_tokens:,} total input tokens | "
            f"{self.cache_hit_rate:.0%} cache hit rate | "
            f"~{self.estimated_savings_tokens:,} tokens saved | "
            f"{self.cache_busts} cache busts"
        )

class CacheAwareAgent:
    """
    Conversation manager that tracks prompt cache efficiency
    and warns when the system prompt is accidentally mutated.
    """

    CACHE_TTL_SECONDS = 300  # 5 minutes

    def __init__(self, system_prompt: str):
        self._base_system = system_prompt
        self._cached_system = [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        self._history: list[dict] = []
        self._stats = CacheStats()
        self._last_call_time: float = 0.0
        self._system_hash = self._hash(system_prompt)

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.md5(text.encode()).hexdigest()[:8]

    def _check_cache_validity(self):
        """Warn if system prompt changed or cache likely expired."""
        current_hash = self._hash(self._base_system)

        if current_hash != self._system_hash:
            print(f"  ⚠️  System prompt changed! Cache busted. ({self._system_hash} → {current_hash})")
            self._stats.cache_busts += 1
            self._system_hash = current_hash

        if self._last_call_time > 0:
            elapsed = time.monotonic() - self._last_call_time
            if elapsed > self.CACHE_TTL_SECONDS:
                print(f"  ⚠️  Cache likely expired ({elapsed:.0f}s since last call > {self.CACHE_TTL_SECONDS}s TTL)")
                self._stats.cache_busts += 1

    def chat(self, user_message: str) -> str:
        self._check_cache_validity()

        self._history.append({"role": "user", "content": user_message})

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=self._cached_system,
            messages=self._history,
            betas=["prompt-caching-2024-07-31"],
        )

        reply = response.content[0].text
        self._history.append({"role": "assistant", "content": reply})

        # Update stats
        self._stats.turns += 1
        self._stats.total_input_tokens += response.usage.input_tokens
        self._stats.total_cache_created += getattr(response.usage, "cache_creation_input_tokens", 0)
        cache_read = getattr(response.usage, "cache_read_input_tokens", 0)
        self._stats.total_cache_read += cache_read
        self._last_call_time = time.monotonic()

        print(f"  Turn {self._stats.turns}: {response.usage.input_tokens} input, {cache_read} from cache")
        return reply

    def print_stats(self):
        print(f"\n{self._stats.report()}")

# Usage
SYSTEM = "You are a helpful assistant.\n" + ("Context: " + "x" * 9000)  # ~10k token system

agent = CacheAwareAgent(SYSTEM)

messages = [
    "Hello, what can you help me with?",
    "Tell me more about option 2",
    "Can you compare all the options?",
    "Which would you recommend?",
]

for msg in messages:
    reply = agent.chat(msg)

agent.print_stats()
# Expected: 75%+ cache hit rate from turn 2 onward
```

**Expected Token Savings:** 80-90% on system prompt from turn 2. Cache bust detection prevents silent cache misses that would otherwise go unnoticed.

**Environment:** Python 3.9+, `anthropic>=0.40.0` with prompt caching beta.

---

### Option 5 — Shared Cache Across Concurrent Sessions

Multiple agent sessions running simultaneously can share a warmed cache if they use the exact same system prompt.

```python
import anthropic
import asyncio
import time
from typing import Optional

client = anthropic.AsyncAnthropic()

SHARED_SYSTEM_PROMPT = """You are a customer support agent for CloudApp.
[... 8,000 tokens of product documentation, FAQ, policies ...]"""

CACHED_SYSTEM = [
    {
        "type": "text",
        "text": SHARED_SYSTEM_PROMPT,
        "cache_control": {"type": "ephemeral"},
    }
]

_cache_warm_time: Optional[float] = None
_cache_warm_lock = asyncio.Lock()

async def ensure_cache_warm() -> bool:
    """
    Ensure the shared cache is warm. Only one coroutine warms at a time.
    Returns True if cache was just created, False if already warm.
    """
    global _cache_warm_time

    async with _cache_warm_lock:
        now = time.monotonic()
        # Re-warm if never warmed or if TTL likely expired
        if _cache_warm_time is None or (now - _cache_warm_time) > 240:
            response = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1,
                system=CACHED_SYSTEM,
                messages=[{"role": "user", "content": "ping"}],
                betas=["prompt-caching-2024-07-31"],
            )
            _cache_warm_time = now
            created = getattr(response.usage, "cache_creation_input_tokens", 0)
            print(f"  Cache warmed: {created:,} tokens")
            return True
        return False

async def handle_session(session_id: int, user_messages: list[str]) -> dict:
    """Handle a single user session, benefiting from shared cache."""
    await ensure_cache_warm()

    history = []
    total_input = 0
    total_cache_read = 0

    for msg in user_messages:
        history.append({"role": "user", "content": msg})

        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            system=CACHED_SYSTEM,
            messages=history,
            betas=["prompt-caching-2024-07-31"],
        )

        reply = response.content[0].text
        history.append({"role": "assistant", "content": reply})
        total_input += response.usage.input_tokens
        total_cache_read += getattr(response.usage, "cache_read_input_tokens", 0)

    return {
        "session_id": session_id,
        "turns": len(user_messages),
        "total_input_tokens": total_input,
        "total_cache_read": total_cache_read,
        "cache_hit_rate": total_cache_read / max(total_input, 1),
    }

async def simulate_concurrent_sessions():
    """Simulate 10 concurrent user sessions sharing a warmed cache."""
    sessions = [
        handle_session(i, [
            "How do I reset my password?",
            "What's included in the Pro plan?",
        ])
        for i in range(10)
    ]

    results = await asyncio.gather(*sessions)

    total_input = sum(r["total_input_tokens"] for r in results)
    total_cache_read = sum(r["total_cache_read"] for r in results)
    print(f"\n10 concurrent sessions:")
    print(f"  Total input tokens: {total_input:,}")
    print(f"  Served from cache: {total_cache_read:,} ({total_cache_read/total_input:.0%})")
    print(f"  Savings: ~{int(total_cache_read * 0.9):,} tokens")

asyncio.run(simulate_concurrent_sessions())
# All 10 sessions share the same cached system prompt
# Expected: 85%+ cache hit rate across all sessions
```

**Expected Token Savings:** With 10 concurrent sessions × 2 turns × 8,000-token system prompt = 160,000 tokens. Cache read discount saves ~144,000 tokens vs. full pricing.

**Environment:** Python 3.9+, `asyncio`, `anthropic>=0.40.0` with prompt caching beta.

---

### Option 6 — Dynamic System Prompt with Static Prefix Caching

When the system prompt has both static (cacheable) and dynamic (per-user) sections, structure them so the static prefix is always cached.

```python
import anthropic

client = anthropic.Anthropic()

STATIC_PREFIX = """You are an expert coding assistant for DevHub.

## Platform Capabilities
[... 7,000 tokens of static documentation: language support, IDE integrations,
 code review standards, security policies, API references ...]

## Core Behavior
- Provide runnable code examples
- Flag security issues explicitly
- Suggest tests for every code change
- Reference documentation when relevant
"""

def build_system_prompt(
    user_name: str,
    user_role: str,
    active_project: str,
    user_preferences: dict,
) -> list[dict]:
    """
    Build a two-part system prompt:
    - Part 1: Large static prefix (cached) — identical across all users
    - Part 2: Small dynamic suffix (not cached) — personalized per user
    """
    dynamic_suffix = f"""
## Current Session Context
User: {user_name} ({user_role})
Active project: {active_project}
Preferred language: {user_preferences.get('language', 'Python')}
Code style: {user_preferences.get('style', 'PEP 8')}
Verbosity: {user_preferences.get('verbosity', 'concise')}
"""

    return [
        {
            "type": "text",
            "text": STATIC_PREFIX,
            "cache_control": {"type": "ephemeral"},  # cached — same for all users
        },
        {
            "type": "text",
            "text": dynamic_suffix,
            # No cache_control — this changes per user, not worth caching
        },
    ]

def code_assistant_chat(
    history: list[dict],
    user_message: str,
    user_context: dict,
) -> tuple[str, dict]:
    """
    Chat with static prefix cached, dynamic suffix freshly sent.
    """
    system = build_system_prompt(
        user_name=user_context["name"],
        user_role=user_context["role"],
        active_project=user_context["project"],
        user_preferences=user_context.get("preferences", {}),
    )

    history.append({"role": "user", "content": user_message})

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=history,
        betas=["prompt-caching-2024-07-31"],
    )

    reply = response.content[0].text
    history.append({"role": "assistant", "content": reply})

    usage = {
        "input": response.usage.input_tokens,
        "cache_created": getattr(response.usage, "cache_creation_input_tokens", 0),
        "cache_read": getattr(response.usage, "cache_read_input_tokens", 0),
        "dynamic_tokens": len(system[1]["text"].split()),  # approximate
    }
    return reply, usage

# Two different users — both benefit from the same cached static prefix
users = [
    {"name": "Alice", "role": "Senior Engineer", "project": "auth-service",
     "preferences": {"language": "Python", "style": "PEP 8", "verbosity": "concise"}},
    {"name": "Bob", "role": "Junior Engineer", "project": "frontend-app",
     "preferences": {"language": "TypeScript", "style": "Airbnb", "verbosity": "detailed"}},
]

for user in users:
    print(f"\nSession for {user['name']}:")
    history = []
    questions = [
        "How do I write a context manager?",
        "What's the best way to handle errors here?",
    ]
    for q in questions:
        reply, usage = code_assistant_chat(history, q, user)
        print(f"  Input: {usage['input']:,} | Cache read: {usage['cache_read']:,} | Created: {usage['cache_created']:,}")

# Key insight: Alice and Bob share the cached static prefix (7,000 tokens)
# Their dynamic suffix (~50 tokens each) is sent fresh — tiny cost
# Result: 7,000-token cache hit for EVERY turn of EVERY user session
```

**Expected Token Savings:** Static prefix (7,000 tokens) cached and shared across all users. 100 users × 5 turns each = 3,500,000 tokens; with caching: 7,000 (creation) + 3,493,000 × 10% = ~356,300 effective tokens. **90% reduction** on the static portion.

**Environment:** Python 3.9+, `anthropic>=0.40.0` with prompt caching beta. Cache is shared across all requests with identical static prefix content.

---

## Comparison

| Option | Approach | Setup Effort | Sessions | Dynamic Content |
|--------|----------|-------------|----------|-----------------|
| 1 — Single Block | Add cache_control to system | Minimal | Single | No |
| 2 — Multi-Block | Cache system + tools separately | Low | Single | No |
| 3 — Cache Warming | Pre-warm before session starts | Low | Single | No |
| 4 — Cache Monitor | Track hit rate, detect busts | Medium | Single | No |
| 5 — Shared Cache | One warm serves N concurrent sessions | Medium | Multi | No |
| 6 — Static Prefix | Cache static part, send dynamic fresh | Medium | Multi | Yes |

**Start with Option 1** for any existing agent — it's a one-line change to the system prompt structure. Add **Option 6** (static prefix) when different users get personalized system prompts. Use **Option 5** (shared cache warming) for high-concurrency deployments to ensure the first user in each 5-minute window doesn't pay full price.

**Key constraint:** Cache busts happen when system prompt content changes even by one character. Never format, sort, or template-fill the cached portion dynamically — treat it as a compile-time constant.
