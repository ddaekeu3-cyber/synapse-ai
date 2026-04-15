---
layout: solution
title: "Agent Doesn't Use Prompt Caching for Repeated System Prompts"
category: token-cost
description: "Agent rebuilds and re-sends the same large system prompt on every API call without using Anthropic's prompt caching feature, paying full input token cost every time."
tags: [token-cost, prompt-caching, system-prompt, efficiency, cost-reduction]
---

## Symptom

Agent sends identical large system prompts on every call with no caching:

```python
# System prompt contains: tool definitions (500 tokens) + instructions (800 tokens)
# + few-shot examples (1,200 tokens) + knowledge base (3,000 tokens) = 5,500 tokens

for user_message in message_queue:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,  # 5,500 tokens — billed every single call
        messages=[{"role": "user", "content": user_message}],
    )

# 1,000 messages/day × 5,500 system tokens = 5,500,000 input tokens/day
# At $3/M tokens (Sonnet): $16.50/day just for the system prompt
# With prompt caching: 5,500 cached tokens × $0.30/M = $1.65/day → 90% savings
```

Every API call re-sends content that is byte-for-byte identical to the previous call. Anthropic's prompt caching charges only 10% of the normal rate for cached tokens on subsequent calls.

## Root Cause

Prompt caching is an opt-in feature that requires adding `cache_control` markers to the request. Without explicit configuration, the SDK sends the full system prompt as regular input tokens on every call — even when the content is completely unchanged. The saving is invisible until you explicitly enable and measure it.

## Fix

---

### Option 1: Add cache_control to System Prompt — Minimal Change

Add `"cache_control": {"type": "ephemeral"}` to the system prompt block. This single change enables caching with no other modifications.

```python
import anthropic

client = anthropic.Anthropic()

LARGE_SYSTEM_PROMPT = """You are an expert AI assistant with the following capabilities and knowledge:

## Core Instructions
Always respond in a helpful, accurate, and concise manner. When uncertain, say so explicitly.
For technical questions, provide working code examples. For factual questions, cite confidence levels.

## Tool Usage Guidelines
When using tools:
1. Prefer the most specific tool for the task
2. Validate inputs before submission
3. Handle errors gracefully and report them clearly
4. Never call the same tool twice with identical arguments unless the first failed

## Domain Knowledge
[... imagine 2,000+ tokens of domain-specific knowledge, few-shot examples, etc. ...]
Python best practices, async patterns, security guidelines, API conventions...
""" + "x" * 4000  # Simulate a large system prompt

def call_without_caching(user_message: str) -> dict:
    """Original approach — full system prompt billed every time."""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=LARGE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    usage = response.usage
    return {
        "text": response.content[0].text,
        "input_tokens": usage.input_tokens,
        "cache_read": 0,
        "cache_write": 0,
    }

def call_with_caching(user_message: str) -> dict:
    """With prompt caching — system prompt tokens cached after first call."""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=[
            {
                "type": "text",
                "text": LARGE_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},  # ← The only change needed
            }
        ],
        messages=[{"role": "user", "content": user_message}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    usage = response.usage
    return {
        "text": response.content[0].text,
        "input_tokens": usage.input_tokens,
        "cache_read": getattr(usage, "cache_read_input_tokens", 0),
        "cache_write": getattr(usage, "cache_creation_input_tokens", 0),
    }

# Demonstrate the savings
messages = ["What is asyncio?", "How does Python's GIL work?", "Explain decorators"]

print("=== Without caching ===")
total_without = 0
for msg in messages:
    result = call_without_caching(msg)
    total_without += result["input_tokens"]
    print(f"  '{msg[:30]}': {result['input_tokens']} input tokens")
print(f"  Total: {total_without} input tokens")

print("\n=== With prompt caching ===")
total_with = 0
total_cached = 0
for msg in messages:
    result = call_with_caching(msg)
    total_with += result["input_tokens"]
    total_cached += result["cache_read"]
    print(f"  '{msg[:30]}': {result['input_tokens']} billed + {result['cache_read']} cached")
print(f"  Total billed: {total_with} tokens, cached reads: {total_cached} tokens")
```

**Expected Token Savings:** For a 5,500-token system prompt and 1,000 calls/day: without caching = 5,500,000 input tokens/day. With caching (first call cache-write + 999 cache-reads at 10% cost): 5,500 + 999 × 550 = 554,950 effective tokens. **90% reduction** in system prompt token costs.
**Environment:** Requires `anthropic-beta: prompt-caching-2024-07-31` header. Cache TTL is 5 minutes (ephemeral). The system prompt must be identical across calls to hit the cache — even one character difference creates a new cache entry.

---

### Option 2: Multi-Block Caching — Cache Stable Parts, Leave Dynamic Parts Uncached

Split the system prompt into stable (cacheable) and dynamic (per-request) sections. Cache only the stable part.

```python
import anthropic
from datetime import datetime

client = anthropic.Anthropic()

# Stable section: changes rarely — cache this
STABLE_INSTRUCTIONS = """You are an expert assistant specializing in Python development.

## Capabilities
- Code review and refactoring
- Performance optimisation
- Security analysis
- Architecture design

## Coding Standards
Always follow PEP 8. Prefer explicit over implicit. Use type hints.
Write docstrings for public functions. Prefer composition over inheritance.

## Security Rules
Never suggest hardcoded secrets. Always validate inputs. Use parameterised queries.
""" + "x" * 2000  # Additional stable content

def build_system_blocks(user_context: dict) -> list[dict]:
    """Build system prompt with stable part cached and dynamic part uncached."""
    blocks = [
        # Block 1: Stable — cached across all calls
        {
            "type": "text",
            "text": STABLE_INSTRUCTIONS,
            "cache_control": {"type": "ephemeral"},
        },
        # Block 2: Dynamic per user — NOT cached (changes per request)
        {
            "type": "text",
            "text": (
                f"\n## Current Session Context\n"
                f"User: {user_context.get('user_id', 'anonymous')}\n"
                f"Project: {user_context.get('project', 'unknown')}\n"
                f"Timestamp: {datetime.now().isoformat()}\n"
                f"Permissions: {', '.join(user_context.get('permissions', []))}\n"
            ),
            # No cache_control — billed normally but is small
        },
    ]
    return blocks

def call_with_split_caching(user_message: str, user_context: dict) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=build_system_blocks(user_context),
        messages=[{"role": "user", "content": user_message}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    usage = response.usage
    print(
        f"Tokens — input: {usage.input_tokens}, "
        f"cache_read: {getattr(usage, 'cache_read_input_tokens', 0)}, "
        f"cache_write: {getattr(usage, 'cache_creation_input_tokens', 0)}"
    )
    return response.content[0].text

# Multiple users — each gets personalised context but shares the stable cache
users = [
    {"user_id": "alice", "project": "api-v2", "permissions": ["read", "write"]},
    {"user_id": "bob", "project": "frontend", "permissions": ["read"]},
    {"user_id": "alice", "project": "api-v2", "permissions": ["read", "write"]},  # repeat
]

for ctx in users:
    result = call_with_split_caching(
        f"Review this code for {ctx['project']}",
        ctx,
    )
    print(f"[{ctx['user_id']}] {result[:60]}...\n")
```

**Expected Token Savings:** Stable block (2,200 tokens) cached; dynamic block (~100 tokens) billed normally. For 1,000 calls: cached savings = 999 × 2,200 × 90% = 1,977,900 effective tokens saved. Dynamic part: 1,000 × 100 = 100,000 tokens (can't cache per-user data). Total savings vs no caching: ~89%.
**Environment:** The stable block must be positioned BEFORE the dynamic block in the list. Cache hits require byte-identical stable content — avoid adding timestamps or request IDs inside the cached block.

---

### Option 3: Tool Definition Caching — Cache Large Tool Lists

When your agent has many tools with verbose descriptions, cache the tool definitions separately from the messages.

```python
import anthropic
import json

client = anthropic.Anthropic()

# Large tool set — many verbose tool definitions
TOOLS = [
    {
        "name": f"tool_{i}",
        "description": f"Tool {i} for performing operation {i}. " + "x" * 200,
        "input_schema": {
            "type": "object",
            "properties": {
                "param_a": {"type": "string", "description": f"Parameter A for tool {i}"},
                "param_b": {"type": "integer", "description": f"Parameter B for tool {i}"},
            },
            "required": ["param_a"],
        },
    }
    for i in range(20)  # 20 tools × ~250 tokens each ≈ 5,000 tokens
]

def call_with_cached_tools(user_message: str, messages: list[dict]) -> str:
    """Cache tool definitions — they rarely change."""

    # Encode tool definitions as a text block in the system prompt
    # (Alternative: use tool caching when supported by the API)
    tools_as_text = json.dumps(TOOLS, indent=2)
    tool_token_estimate = len(tools_as_text) // 4

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=[
            {
                "type": "text",
                "text": (
                    "You are a helpful assistant with access to the following tools.\n\n"
                    "IMPORTANT: Use tools by outputting JSON in the format:\n"
                    '{"tool": "tool_name", "params": {...}}\n\n'
                    "Available tools (cached):\n"
                    + tools_as_text[:8000]  # Cap to avoid huge system prompts
                ),
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tools=TOOLS[:10],  # Use subset of tools directly in API
        messages=messages + [{"role": "user", "content": user_message}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )

    usage = response.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0)
    cache_write = getattr(usage, "cache_creation_input_tokens", 0)

    if cache_write > 0:
        print(f"Cache written: {cache_write} tokens (first call or cache expired)")
    elif cache_read > 0:
        print(f"Cache hit: {cache_read} tokens read at 10% cost")

    return response.content[0].text

# Conversation with tool caching
conversation = []
queries = [
    "What tools do you have available?",
    "Use tool_3 to process data",
    "Now use tool_7 with param_a='test'",
]

for query in queries:
    result = call_with_cached_tools(query, conversation)
    conversation.append({"role": "user", "content": query})
    conversation.append({"role": "assistant", "content": result})
    print(f"Q: {query!r}\nA: {result[:80]}...\n")
```

**Expected Token Savings:** 20 tools × ~250 tokens = 5,000 tool definition tokens. Without caching: 5,000 × 1,000 calls = 5M tokens/day. With caching: 5,000 + 999 × 500 = 504,500 effective tokens. **90% reduction** on tool definition costs.
**Environment:** Tool definitions are ideal for caching — they change only during development. Cache expires after 5 minutes; in production, tool definitions typically persist for hours between restarts.

---

### Option 4: Multi-Turn Conversation Caching — Cache the Growing Conversation Prefix

For long multi-turn conversations, cache the accumulated conversation history so only the newest message is billed at full rate.

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class CachingConversation:
    system_prompt: str
    messages: list[dict] = field(default_factory=list)
    cache_after_turn: int = 3  # Cache conversation after N turns

    def add_user(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})

    def add_assistant(self, content: str) -> None:
        self.messages.append({"role": "assistant", "content": content})

    def _build_cacheable_messages(self) -> list[dict]:
        """Mark all but the last 2 messages for caching."""
        if len(self.messages) < self.cache_after_turn * 2:
            return self.messages  # Too short to cache

        msgs = list(self.messages)
        # Cache the second-to-last user message (penultimate turn)
        # The latest user message stays uncached (it's the new question)
        if len(msgs) >= 3:
            # Mark the content before the last turn as cacheable
            target_idx = len(msgs) - 3  # Third from end
            msg = msgs[target_idx]
            content = msg["content"]
            if isinstance(content, str):
                msgs[target_idx] = {
                    **msg,
                    "content": [
                        {
                            "type": "text",
                            "text": content,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
        return msgs

    def send(self, user_message: str) -> str:
        self.add_user(user_message)
        cacheable_msgs = self._build_cacheable_messages()

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=[
                {
                    "type": "text",
                    "text": self.system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=cacheable_msgs,
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )

        reply = response.content[0].text
        self.add_assistant(reply)

        usage = response.usage
        cache_read = getattr(usage, "cache_read_input_tokens", 0)
        billed = usage.input_tokens
        total_tokens = billed + cache_read
        savings_pct = (cache_read * 0.9 / max(total_tokens, 1)) * 100
        print(f"Turn {len(self.messages)//2}: billed={billed}, cached={cache_read}, "
              f"savings={savings_pct:.0f}%")

        return reply

# Simulate a long conversation where history grows
conv = CachingConversation(
    system_prompt="You are a Python expert. Provide detailed, accurate answers." + "x" * 1000
)

questions = [
    "Explain Python's asyncio event loop",
    "How does async/await relate to generators?",
    "What's the difference between asyncio.gather and asyncio.wait?",
    "When should I use asyncio.Queue?",
    "How do I handle cancellation in async code?",
]

for q in questions:
    answer = conv.send(q)
    print(f"Q: {q!r}\nA: {answer[:80]}...\n")
```

**Expected Token Savings:** By turn 5 of a conversation, the accumulated history may be 4,000+ tokens. Caching turns 1-3 saves 90% of those tokens on each subsequent call. For a 10-turn conversation: without caching = 45,000 total history tokens; with caching = ~9,000 billed + 36,000 cached at 10% = ~12,600 effective tokens. **72% reduction** in conversation history costs.
**Environment:** Cache TTL is 5 minutes. For long sessions that pause, the cache may expire between turns — the next call re-writes the cache. Still beneficial for active conversations and burst patterns.

---

### Option 5: Knowledge Base Caching — Cache Large Reference Contexts

When injecting large knowledge bases, FAQs, or document corpora into the prompt, cache the entire knowledge section.

```python
import anthropic

client = anthropic.Anthropic()

# Large knowledge base injected into every call
KNOWLEDGE_BASE = """
# Company Knowledge Base

## Products
Our product lineup includes Widget Pro (premium), Widget Lite (basic), and Widget API (developer).
Widget Pro: $99/month, includes all features, priority support, 99.9% SLA.
Widget Lite: $19/month, core features only, community support, 99% SLA.
Widget API: $0.001/call, pay-as-you-go, full API access, no UI.

## Pricing FAQ
Q: Can I switch plans? A: Yes, anytime. Upgrades take effect immediately; downgrades at billing period end.
Q: Is there a free trial? A: 14-day free trial for Widget Pro.
Q: Volume discounts? A: Available for 10+ seats — contact sales@example.com.

## Technical Specifications
Rate limits: 100 req/min (Lite), 1000 req/min (Pro), 10000 req/min (API).
Supported formats: JSON, CSV, XML. Max file size: 100MB.
Integrations: Salesforce, HubSpot, Slack, Jira, GitHub.

## Support Policies
Response times: Community (48h), Pro (4h), Enterprise (1h).
Escalation: support@example.com → account manager → VP of Support.
""" + "\n## Extended Knowledge\n" + "Additional knowledge... " * 200

def answer_with_kb_cache(question: str, session_context: str = "") -> str:
    """Answer questions using a cached knowledge base."""
    system_blocks = [
        # Large KB — cached across all customer service calls
        {
            "type": "text",
            "text": (
                "You are a customer service agent. Use the following knowledge base:\n\n"
                + KNOWLEDGE_BASE
            ),
            "cache_control": {"type": "ephemeral"},
        },
    ]

    # Add session context if provided (not cached — unique per session)
    if session_context:
        system_blocks.append({
            "type": "text",
            "text": f"\nSession context: {session_context}",
        })

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system_blocks,
        messages=[{"role": "user", "content": question}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )

    usage = response.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0)
    cache_write = getattr(usage, "cache_creation_input_tokens", 0)
    print(f"Usage — billed: {usage.input_tokens}, cache_read: {cache_read}, cache_write: {cache_write}")

    return response.content[0].text

# Simulate customer service calls
questions = [
    "What's the difference between Widget Pro and Widget Lite?",
    "How long is the free trial?",
    "What are the rate limits for the API plan?",
    "Can I get a refund?",
    "How do I integrate with Slack?",
]

for q in questions:
    answer = answer_with_kb_cache(q, session_context="Customer: Enterprise tier, account #98765")
    print(f"Q: {q!r}\nA: {answer[:120]}...\n")
```

**Expected Token Savings:** KB = ~3,000 tokens. Without caching: 3,000 × 500 calls/day = 1.5M input tokens/day. With caching: 3,000 (write) + 499 × 300 (reads at 10%) = 151,500 effective tokens. **90% reduction** for KB costs. At Haiku pricing ($0.25/M): saves ~$3.37/day.
**Environment:** Knowledge base must be identical across calls (no dynamic insertion). Update by restarting the process or bumping a version string in the KB (which invalidates the cache and starts fresh). Use the 5-minute TTL strategically — schedule high-load periods within TTL windows.

---

### Option 6: Cache Warming and TTL Management — Proactive Cache Maintenance

Monitor cache hit rates and proactively warm the cache before it expires to maintain a near-100% hit rate.

```python
import asyncio
import time
import anthropic
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

SYSTEM_PROMPT = "You are a helpful assistant.\n" + "x" * 3000  # 3,000+ token system prompt
CACHE_TTL = 4 * 60  # Refresh 1 minute before 5-minute TTL expires

@dataclass
class CacheStats:
    writes: int = 0
    reads: int = 0
    misses: int = 0

    def hit_rate(self) -> float:
        total = self.reads + self.misses
        return self.reads / max(total, 1)

stats = CacheStats()
_last_cache_write: float = 0.0

async def warm_cache() -> None:
    """Make a minimal call to ensure the cache is fresh."""
    global _last_cache_write
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1,
        system=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": "ping"}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    usage = response.usage
    cache_write = getattr(usage, "cache_creation_input_tokens", 0)
    if cache_write > 0:
        _last_cache_write = time.time()
        stats.writes += 1
        print(f"Cache warmed: {cache_write} tokens written")

async def cache_warmer(interval: float = CACHE_TTL) -> None:
    """Background task: re-warm cache before TTL expires."""
    while True:
        await asyncio.sleep(interval)
        await warm_cache()

async def call_with_warm_cache(user_message: str) -> str:
    global _last_cache_write
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_message}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    usage = response.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0)
    cache_write = getattr(usage, "cache_creation_input_tokens", 0)

    if cache_read > 0:
        stats.reads += 1
    elif cache_write > 0:
        stats.writes += 1
        _last_cache_write = time.time()
    else:
        stats.misses += 1

    return response.content[0].text

# Comparison table
"""
| Approach | Cache Target | Savings | Setup Complexity | TTL Risk |
|---|---|---|---|---|
| Option 1: Basic cache_control | System prompt | 90% | Minimal | Medium |
| Option 2: Multi-block split | Stable + dynamic | 85-90% | Low | Medium |
| Option 3: Tool definition cache | Tool descriptions | 90% | Low | Medium |
| Option 4: Conversation history | Message history | 60-75% | Medium | High |
| Option 5: Knowledge base | Large KB | 90% | Low | Medium |
| Option 6: Cache warming | Any | 95%+ | Medium | Eliminated |
"""

async def main():
    # Warm cache on startup
    await warm_cache()

    # Start background warmer
    warmer = asyncio.create_task(cache_warmer(CACHE_TTL))

    try:
        # Process messages
        messages = [f"Question {i} about Python" for i in range(10)]
        results = await asyncio.gather(*[call_with_warm_cache(m) for m in messages])

        print(f"\nCache stats: writes={stats.writes}, reads={stats.reads}, misses={stats.misses}")
        print(f"Hit rate: {stats.hit_rate():.1%}")
    finally:
        warmer.cancel()

asyncio.run(main())
```

**Expected Token Savings:** Cache warming eliminates TTL-expiry misses. Without warming: cache expires every 5 minutes, causing a cache-write call that costs full price. With warming at 4-minute intervals: near-100% hit rate. For 1,000 calls/day at 3,000-token system prompt, warming prevents ~288 full-cost calls/day (one every 5 min). Net savings vs unwarmed: additional ~8.6% of remaining cost.
**Environment:** Warming call uses `max_tokens=1` to minimise cost (only 1 output token). Run the warmer as a background asyncio task or a separate cron job. For multi-process deployments, have only one instance perform warming to avoid redundant writes.
