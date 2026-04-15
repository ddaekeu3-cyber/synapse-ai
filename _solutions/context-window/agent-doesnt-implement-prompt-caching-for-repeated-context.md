---
layout: solution
title: "Agent Doesn't Implement Prompt Caching for Repeated Context"
category: context-window
description: "Agents that send the same large system prompt, document corpus, or tool definitions on every API call pay full input token costs each time. Anthropic's prompt caching can cut repeated-context costs by up to 90% with a single API parameter."
tags: [prompt-caching, cache-control, token-cost, performance, context-window, anthropic]
---

# Agent Doesn't Implement Prompt Caching for Repeated Context

## Problem

Many agents repeat identical or near-identical content on every API call: a long system prompt with persona and rules, a full document corpus injected as context, a large set of tool definitions, or reference material. Without prompt caching, the model processes and charges for every token of this repeated content on every single request — even when nothing has changed.

Anthropic's prompt caching feature lets you mark content blocks with `cache_control: {"type": "ephemeral"}`. The first request that includes a cache-eligible block pays full price; subsequent requests that hit the cache pay only ~10% of the input token cost. For agents with large, stable context, this routinely reduces costs by 70–90% and also lowers latency on cache hits.

## Solutions

### Option 1: Cache a Large System Prompt

Mark the end of your stable system prompt content so the cache breakpoint sits at the right boundary.

```python
import anthropic

client = anthropic.Anthropic()

# Large stable system prompt — could be thousands of tokens
SYSTEM_CONTENT = """
You are an expert Python engineer with 20 years of experience.
You follow PEP 8, write comprehensive docstrings, prefer explicit
over implicit, and always consider edge cases and error handling.

# Core Principles
[... 2000+ tokens of stable persona and rules ...]

# Code Review Guidelines
[... 1000+ tokens of stable guidelines ...]
"""

def ask_with_cached_system(user_question: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": SYSTEM_CONTENT,
                "cache_control": {"type": "ephemeral"},  # Cache this block
            }
        ],
        messages=[{"role": "user", "content": user_question}]
    )

    # Check cache performance
    usage = response.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0)
    cache_created = getattr(usage, "cache_creation_input_tokens", 0)
    regular = usage.input_tokens

    if cache_read > 0:
        print(f"Cache HIT: {cache_read} tokens read from cache (saved ~90% cost)")
    elif cache_created > 0:
        print(f"Cache MISS (created): {cache_created} tokens cached for next time")
    else:
        print(f"No caching: {regular} input tokens charged at full price")

    return response.content[0].text

# First call — cache is created (pays full price)
r1 = ask_with_cached_system("Review this function: def foo(x): return x*2")

# Second call — cache hit (pays ~10% of system prompt cost)
r2 = ask_with_cached_system("What's the best way to handle None inputs?")
# Expected Token Savings: 70-90% on repeated system prompt tokens
# Environment: Any agent with a large (>1024 token) stable system prompt
```

### Option 2: Cache an Injected Document Corpus

When you inject reference documents that don't change between turns, cache them separately from the user's dynamic question.

```python
import anthropic

client = anthropic.Anthropic()

def load_reference_docs() -> str:
    """In production, load from files/DB. Here we simulate large docs."""
    return "\n\n".join([
        f"# Document {i}\n" + ("Content paragraph. " * 200)
        for i in range(10)  # ~2000 tokens per doc, 20k total
    ])

REFERENCE_DOCS = load_reference_docs()

def answer_with_cached_docs(question: str, conversation_history: list[dict]) -> tuple[str, list[dict]]:
    """
    Structure: system(small) + user-turn-1(docs, cached) + user-turn-2(question)
    The docs are in the first user turn, marked cacheable.
    """
    messages = [
        # Turn 1: inject docs with cache marker (only appears once in history)
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": f"Reference documents for this session:\n\n{REFERENCE_DOCS}",
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        },
        {"role": "assistant", "content": "I have loaded all reference documents. How can I help?"},
    ] + conversation_history + [
        {"role": "user", "content": question}
    ]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system="You are a document analysis assistant. Answer questions using the provided reference docs.",
        messages=messages,
    )

    answer = response.content[0].text
    conversation_history = conversation_history + [
        {"role": "user", "content": question},
        {"role": "assistant", "content": answer},
    ]

    usage = response.usage
    print(f"Cache read: {getattr(usage, 'cache_read_input_tokens', 0)} | "
          f"Cache created: {getattr(usage, 'cache_creation_input_tokens', 0)} | "
          f"Regular: {usage.input_tokens}")

    return answer, conversation_history

history = []
a1, history = answer_with_cached_docs("Summarize document 3.", history)
a2, history = answer_with_cached_docs("Compare documents 1 and 5.", history)
# Docs are cached after turn 1; subsequent turns pay only for new tokens
# Expected Token Savings: 80-90% on document corpus tokens after first request
# Environment: RAG agents, document Q&A, code review bots with large codebases
```

### Option 3: Cache Tool Definitions

Large tool schemas are charged as input tokens. When you have many tools with detailed descriptions, cache them.

```python
import anthropic
import json

client = anthropic.Anthropic()

# Simulate a large set of tools with detailed schemas
def build_tools() -> list[dict]:
    tools = []
    for i in range(20):
        tools.append({
            "name": f"tool_{i}",
            "description": f"Tool {i}: performs operation {i}. " + ("Detailed description. " * 30),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query"},
                    "limit": {"type": "integer", "description": "Max results", "default": 10},
                    "filters": {
                        "type": "object",
                        "properties": {
                            "date_from": {"type": "string"},
                            "date_to": {"type": "string"},
                            "category": {"type": "string"},
                        }
                    }
                },
                "required": ["query"]
            }
        })
    return tools

TOOLS = build_tools()

def call_with_cached_tools(user_message: str) -> str:
    # Add cache_control to the last tool to cache all tools up to that point
    tools_with_cache = TOOLS.copy()
    # Anthropic caches up to the last cache_control breakpoint
    # We add it to the last tool's definition via a wrapper approach:
    # Unfortunately the Tools parameter doesn't support cache_control directly
    # in all SDK versions — use system prompt injection for tool docs instead,
    # or use the beta header approach.

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=[
            {
                "type": "text",
                "text": "You are an agent with access to many tools.",
            },
            {
                "type": "text",
                "text": f"Available tools reference:\n{json.dumps(TOOLS, indent=2)}",
                "cache_control": {"type": "ephemeral"},  # Cache tool schemas here
            }
        ],
        messages=[{"role": "user", "content": user_message}],
    )

    usage = response.usage
    saved = getattr(usage, "cache_read_input_tokens", 0)
    print(f"Tool schema tokens saved from cache: {saved}")
    return response.content[0].text

r = call_with_cached_tools("Which tool should I use to search recent news?")
r = call_with_cached_tools("What does tool_5 do?")
# Expected Token Savings: 60-80% on tool definition tokens for agents with 10+ tools
# Environment: Multi-tool agents, function-calling pipelines, MCP servers
```

### Option 4: Multi-Turn Conversation with Cached Prefix

For long conversations, cache the early turns so you only pay for the growing tail.

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class CachedConversation:
    system: str
    cached_turns: list[dict] = field(default_factory=list)  # stable prefix — cached
    live_turns: list[dict] = field(default_factory=list)    # recent turns — not cached
    cache_after_n_turns: int = 6  # re-cache prefix every N turns

    def add_exchange(self, user_msg: str, assistant_msg: str):
        self.live_turns.append({"role": "user", "content": user_msg})
        self.live_turns.append({"role": "assistant", "content": assistant_msg})

        # Promote old live turns into cached prefix
        if len(self.live_turns) >= self.cache_after_n_turns * 2:
            to_promote = self.live_turns[:self.cache_after_n_turns * 2]
            self.cached_turns.extend(to_promote)
            self.live_turns = self.live_turns[self.cache_after_n_turns * 2:]

    def build_messages(self, new_user_message: str) -> list[dict]:
        messages = []

        # Add cached turns — mark the last cached turn for caching
        for i, turn in enumerate(self.cached_turns):
            if i == len(self.cached_turns) - 1 and turn["role"] == "assistant":
                # Mark cache breakpoint at end of cached prefix
                content = turn["content"]
                messages.append({
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": content if isinstance(content, str) else content[0]["text"],
                            "cache_control": {"type": "ephemeral"},
                        }
                    ]
                })
            else:
                messages.append(turn)

        # Add live (uncached) turns
        messages.extend(self.live_turns)
        messages.append({"role": "user", "content": new_user_message})
        return messages

def chat(conv: CachedConversation, user_message: str) -> str:
    messages = conv.build_messages(user_message)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=conv.system,
        messages=messages,
    )

    answer = response.content[0].text
    conv.add_exchange(user_message, answer)

    usage = response.usage
    cache_hit = getattr(usage, "cache_read_input_tokens", 0)
    print(f"Turn cache read: {cache_hit} tokens | Input: {usage.input_tokens} tokens")
    return answer

conv = CachedConversation(system="You are a helpful coding assistant.")
for i in range(10):
    reply = chat(conv, f"Question {i}: explain Python concept {i}")
    print(f"Q{i}: {reply[:60]}...")
# Expected Token Savings: 40-70% on long conversations by caching stable early turns
# Environment: Long-running chatbots, coding assistants, document editing sessions
```

### Option 5: Cache Warming on Startup

Pre-warm the cache before user traffic arrives so the first real user doesn't pay cache-creation costs.

```python
import anthropic
import asyncio
import time

client = anthropic.Anthropic()

LARGE_SYSTEM = "Expert assistant rules:\n" + ("Rule detail. " * 500)  # ~1000 tokens

async def warm_cache() -> bool:
    """Send a minimal warm-up request to prime the cache."""
    print("Warming prompt cache...")
    start = time.monotonic()

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1,  # Minimal output — we only want to prime the cache
        system=[
            {
                "type": "text",
                "text": LARGE_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": "Ready?"}],
    )

    elapsed = time.monotonic() - start
    created = getattr(response.usage, "cache_creation_input_tokens", 0)
    read = getattr(response.usage, "cache_read_input_tokens", 0)

    if created > 0:
        print(f"Cache created: {created} tokens in {elapsed:.2f}s — ready for traffic")
        return True
    elif read > 0:
        print(f"Cache already warm: {read} tokens available")
        return True
    else:
        print("Warning: cache not created — model may not support caching")
        return False

def serve_request(user_message: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=[
            {
                "type": "text",
                "text": LARGE_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_message}],
    )
    cache_hit = getattr(response.usage, "cache_read_input_tokens", 0)
    print(f"Request cache hit: {cache_hit} tokens")
    return response.content[0].text

async def main():
    # Warm cache at startup
    warmed = await warm_cache()

    if warmed:
        # All subsequent requests hit the cache
        for msg in ["Hello", "How are you?", "Tell me about Python."]:
            reply = serve_request(msg)
            print(f"Reply: {reply[:80]}...")

asyncio.run(main())
# Expected Token Savings: 85-95% on all requests after warm-up
# Environment: High-traffic API servers, batch processing, serverless agents
```

### Option 6: Cache Hit Monitoring and Cost Reporting

Track cache performance across all requests to verify savings and detect cache misses.

```python
import anthropic
from dataclasses import dataclass, field
from collections import defaultdict

client = anthropic.Anthropic()

@dataclass
class CacheMetrics:
    total_requests: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    tokens_from_cache: int = 0
    tokens_cached_created: int = 0
    tokens_regular_input: int = 0
    tokens_output: int = 0
    # Anthropic pricing approximations (per million tokens):
    input_price_per_m: float = 3.0       # Sonnet input
    cache_write_price_per_m: float = 3.75 # cache creation = 1.25x input
    cache_read_price_per_m: float = 0.30  # cache read = 0.1x input

    def record(self, usage):
        self.total_requests += 1
        cache_read = getattr(usage, "cache_read_input_tokens", 0)
        cache_created = getattr(usage, "cache_creation_input_tokens", 0)

        if cache_read > 0:
            self.cache_hits += 1
        else:
            self.cache_misses += 1

        self.tokens_from_cache += cache_read
        self.tokens_cached_created += cache_created
        self.tokens_regular_input += usage.input_tokens
        self.tokens_output += usage.output_tokens

    def report(self) -> str:
        hit_rate = (self.cache_hits / max(self.total_requests, 1)) * 100

        # Cost without caching (all tokens at input price)
        hypothetical_cost = (
            (self.tokens_regular_input + self.tokens_from_cache + self.tokens_cached_created)
            / 1_000_000 * self.input_price_per_m
        )
        # Actual cost with caching
        actual_cost = (
            self.tokens_regular_input / 1_000_000 * self.input_price_per_m
            + self.tokens_cached_created / 1_000_000 * self.cache_write_price_per_m
            + self.tokens_from_cache / 1_000_000 * self.cache_read_price_per_m
            + self.tokens_output / 1_000_000 * 15.0  # output price
        )
        savings = hypothetical_cost - actual_cost
        savings_pct = (savings / max(hypothetical_cost, 0.0001)) * 100

        return (
            f"Cache Performance Report\n"
            f"  Requests: {self.total_requests} | Hit rate: {hit_rate:.1f}%\n"
            f"  Tokens from cache: {self.tokens_from_cache:,}\n"
            f"  Tokens cached (created): {self.tokens_cached_created:,}\n"
            f"  Tokens regular input: {self.tokens_regular_input:,}\n"
            f"  Est. cost without cache: ${hypothetical_cost:.4f}\n"
            f"  Est. actual cost: ${actual_cost:.4f}\n"
            f"  Est. savings: ${savings:.4f} ({savings_pct:.1f}%)"
        )

SYSTEM = [{"type": "text", "text": "Expert assistant. " + ("Detail. " * 400),
           "cache_control": {"type": "ephemeral"}}]

metrics = CacheMetrics()

questions = [
    "What is Python?",
    "Explain list comprehensions.",
    "What is a decorator?",
    "How does asyncio work?",
    "What is a context manager?",
]

for q in questions:
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=100,
        system=SYSTEM,
        messages=[{"role": "user", "content": q}]
    )
    metrics.record(resp.usage)

print(metrics.report())
# Expected Token Savings: Real-time visibility into 60-90% savings from cache hits
# Environment: Production monitoring, cost dashboards, cache ROI analysis
```

## Comparison Table

| Option | Cache Target | When to Use | Setup Effort | Max Savings |
|--------|-------------|-------------|--------------|-------------|
| 1: Cached System Prompt | Large persona/rules | Agent with stable system prompt >1024 tokens | Very Low | ~90% on system prompt |
| 2: Cached Document Corpus | Reference docs | RAG, document Q&A, code context | Low | ~90% on doc tokens |
| 3: Cached Tool Definitions | Tool schemas | 10+ tools with detailed descriptions | Low | ~80% on tool tokens |
| 4: Multi-Turn Prefix Cache | Conversation history | Long conversations 10+ turns | Medium | 40-70% growing |
| 5: Cache Warming on Startup | System prompt | High-traffic servers, cold starts | Low | 95% after warmup |
| 6: Cache Hit Monitoring | All content | Production cost tracking | Low | Visibility only |
