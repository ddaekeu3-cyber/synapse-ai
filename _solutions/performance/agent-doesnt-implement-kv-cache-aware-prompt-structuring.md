---
title: "Agent Doesn't Implement KV-Cache-Aware Prompt Structuring"
description: "Structure prompts so that stable prefixes are cached by the model's KV cache, dramatically reducing latency and cost for repeated requests sharing the same context."
category: performance
difficulty: intermediate
tags: [performance, kv-cache, prompt-caching, latency, token-cost, efficiency]
---

# Agent Doesn't Implement KV-Cache-Aware Prompt Structuring

## Problem

Every API call reprocesses the entire prompt from scratch by default. When multiple requests share a long stable prefix (system prompt, tool definitions, RAG documents), reprocessing this prefix on each call wastes compute, increases latency, and raises costs. KV-cache-aware structuring places stable content first, variable content last, and explicitly marks cacheable blocks — turning repeated prefix processing into fast cache lookups.

---

## Option 1: Explicit cache_control on System Prompt

```python
import asyncio
import anthropic
import time

client = anthropic.AsyncAnthropic()

# Large stable system prompt that doesn't change between requests
STABLE_SYSTEM = """You are an expert Python developer assistant.

## Core Principles
- Write idiomatic, Pythonic code
- Prefer readability over cleverness
- Always include type hints
- Follow PEP 8 conventions
- Consider edge cases and error handling
- Optimize for maintainability

## Python Best Practices
- Use dataclasses for simple data containers
- Prefer composition over inheritance
- Use context managers for resource management
- Leverage standard library before third-party packages
- Write self-documenting code

## Code Review Checklist
- No bare except clauses
- No mutable default arguments
- Proper exception handling with specific exception types
- Meaningful variable names
- Functions do one thing well

""" + "# Extended Documentation\n" + "\n".join([f"## Section {i}\nDetailed guidance on Python pattern {i}." for i in range(1, 50)])

async def cached_call(user_question: str) -> tuple[str, dict]:
    """Uses cache_control to cache the stable system prompt."""
    t0 = time.time()
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=[
            {
                "type": "text",
                "text": STABLE_SYSTEM,
                "cache_control": {"type": "ephemeral"}  # Cache this block
            }
        ],
        messages=[{"role": "user", "content": user_question}]
    )
    latency_ms = (time.time() - t0) * 1000
    cache_stats = {
        "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0),
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "latency_ms": round(latency_ms, 1),
    }
    return resp.content[0].text, cache_stats

async def main():
    questions = [
        "How do I read a file safely in Python?",
        "What's the best way to handle JSON in Python?",
        "How should I structure a Python CLI application?",
        "What are Python dataclasses useful for?",
    ]
    print(f"System prompt size: ~{len(STABLE_SYSTEM.split())} words\n")
    for i, q in enumerate(questions):
        answer, stats = await cached_call(q)
        hit = stats["cache_read_input_tokens"] > 0
        print(f"[Q{i+1}] {'CACHE HIT' if hit else 'CACHE MISS'} | latency={stats['latency_ms']}ms | "
              f"cache_read={stats['cache_read_input_tokens']} | created={stats['cache_creation_input_tokens']}")
        print(f"  A: {answer[:80]}\n")

asyncio.run(main())
```

---

## Option 2: Multi-Block Caching (System + Documents + Tools)

```python
import asyncio
import anthropic
import time

client = anthropic.AsyncAnthropic()

SYSTEM_PROMPT = "You are a helpful research assistant. Answer questions based on the provided documents."

# Simulate a large RAG document set that stays stable across queries
RAG_DOCUMENTS = "\n\n".join([
    f"""## Document {i}: Topic {i}
{'Lorem ipsum content about topic ' + str(i) + '. ' * 30}
Key facts: fact_a_{i}, fact_b_{i}, fact_c_{i}.
Source: docs.example.com/topic-{i}
"""
    for i in range(1, 20)
])

TOOL_DEFINITIONS = [
    {
        "name": "search_documents",
        "description": "Search through the provided documents for relevant information.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "description": "Maximum results to return"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_document",
        "description": "Retrieve a specific document by ID.",
        "input_schema": {
            "type": "object",
            "properties": {"doc_id": {"type": "integer"}},
            "required": ["doc_id"]
        }
    }
]

async def multi_block_cached_call(user_question: str) -> tuple[str, dict]:
    t0 = time.time()
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        # Cache both system prompt and RAG documents
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                # Don't cache the short system prompt alone — combine with docs
            },
            {
                "type": "text",
                "text": f"## Reference Documents\n{RAG_DOCUMENTS}",
                "cache_control": {"type": "ephemeral"}  # Cache the large document set
            }
        ],
        # Cache tool definitions on the last tool
        tools=[
            *TOOL_DEFINITIONS[:-1],
            {**TOOL_DEFINITIONS[-1], "cache_control": {"type": "ephemeral"}}
        ],
        messages=[{"role": "user", "content": user_question}]
    )
    latency_ms = (time.time() - t0) * 1000
    return resp.content[0].text, {
        "cache_read": getattr(resp.usage, "cache_read_input_tokens", 0),
        "cache_created": getattr(resp.usage, "cache_creation_input_tokens", 0),
        "latency_ms": round(latency_ms, 1),
    }

async def main():
    questions = [
        "What is in Document 5?",
        "Summarize Document 10.",
        "Compare Documents 3 and 7.",
        "What are the key facts in Document 15?",
    ]
    for i, q in enumerate(questions):
        answer, stats = await multi_block_cached_call(q)
        hit = stats["cache_read"] > 0
        print(f"[Q{i+1}] {'HIT' if hit else 'MISS'} | latency={stats['latency_ms']}ms | read={stats['cache_read']} created={stats['cache_created']}")
        print(f"  A: {answer[:80]}\n")

asyncio.run(main())
```

---

## Option 3: Prompt Structure Optimizer (Stable-First Ordering)

```python
import asyncio
import anthropic
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class PromptBlock:
    content: str
    stability: str  # "static", "session", "request"
    cache: bool = False

    def token_estimate(self) -> int:
        return max(1, int(len(self.content.split()) * 1.3))

def structure_for_cache(blocks: list[PromptBlock]) -> list[dict]:
    """
    Order blocks: static → session → request.
    Mark the last non-request block for caching (cache everything stable).
    """
    static = [b for b in blocks if b.stability == "static"]
    session = [b for b in blocks if b.stability == "session"]
    request = [b for b in blocks if b.stability == "request"]

    ordered = static + session + request

    # Find the last stable block (session or static) to mark for caching
    last_cacheable_idx = -1
    for i, b in enumerate(ordered):
        if b.stability in ("static", "session"):
            last_cacheable_idx = i

    result = []
    for i, block in enumerate(ordered):
        block_dict: dict = {"type": "text", "text": block.content}
        if i == last_cacheable_idx:
            block_dict["cache_control"] = {"type": "ephemeral"}
        result.append(block_dict)

    stable_tokens = sum(b.token_estimate() for b in static + session)
    total_tokens = sum(b.token_estimate() for b in blocks)
    print(f"[CACHE STRUCTURE] Cacheable: ~{stable_tokens} tokens ({stable_tokens/max(total_tokens,1):.0%} of total)")
    return result

async def structured_call(
    static_blocks: list[str],
    session_blocks: list[str],
    user_message: str,
) -> str:
    blocks = (
        [PromptBlock(content=c, stability="static") for c in static_blocks] +
        [PromptBlock(content=c, stability="session") for c in session_blocks] +
        [PromptBlock(content=user_message, stability="request")]
    )
    system_content = structure_for_cache(blocks[:-1])  # exclude user message from system
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system_content,
        messages=[{"role": "user", "content": user_message}]
    )
    return resp.content[0].text

async def main():
    static = [
        "You are an expert software architect.",
        "## Core Principles\n" + "\n".join([f"- Principle {i}: Always consider scalability, maintainability, and reliability." for i in range(1, 20)])
    ]
    session = [
        "## Current Project Context\nWe are building a distributed microservices system in Python using FastAPI and PostgreSQL.",
        "## Constraints\n- Must support 10k RPS\n- 99.9% uptime requirement\n- Budget: $5k/month infrastructure"
    ]
    for question in ["How should we structure our API gateway?", "What database sharding strategy do you recommend?"]:
        result = await structured_call(static, session, question)
        print(f"Q: {question}\nA: {result[:150]}\n")

asyncio.run(main())
```

---

## Option 4: Cache Warm-Up Strategy

```python
import asyncio
import anthropic
import time

client = anthropic.AsyncAnthropic()

LARGE_SYSTEM = "You are a knowledgeable assistant.\n\n" + "\n".join([
    f"## Domain Knowledge {i}\n{'Expert knowledge in domain ' + str(i) + '. ' * 20}"
    for i in range(1, 30)
])

_cache_warmed = False
_warm_time: float = 0

async def warm_cache() -> dict:
    """Pre-warm the cache with a minimal request to prime the KV cache."""
    global _cache_warmed, _warm_time
    t0 = time.time()
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=5,  # Minimal tokens — just enough to create the cache
        system=[{"type": "text", "text": LARGE_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": "ready"}]
    )
    _cache_warmed = True
    _warm_time = time.time() - t0
    return {
        "warm_latency_ms": round(_warm_time * 1000, 1),
        "cache_created": getattr(resp.usage, "cache_creation_input_tokens", 0),
    }

async def call_after_warmup(user_question: str) -> tuple[str, float]:
    t0 = time.time()
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=[{"type": "text", "text": LARGE_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_question}]
    )
    latency = (time.time() - t0) * 1000
    cache_read = getattr(resp.usage, "cache_read_input_tokens", 0)
    return resp.content[0].text, latency

async def main():
    print("Warming cache...")
    warm_stats = await warm_cache()
    print(f"Cache warmed in {warm_stats['warm_latency_ms']}ms, created {warm_stats['cache_created']} cached tokens\n")

    questions = ["What is your expertise?", "How can you help with Python?", "What domains do you cover?"]
    for q in questions:
        answer, latency = await call_after_warmup(q)
        print(f"[{latency:.0f}ms] Q: {q}\n  A: {answer[:80]}\n")

asyncio.run(main())
```

---

## Option 5: Session-Scoped Shared Prefix Pool

```python
import asyncio
import anthropic
import time
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class SharedPrefixSession:
    """Manages a shared cached prefix for all requests in a session."""
    session_id: str
    system_content: list[dict]
    created_at: float = field(default_factory=time.time)
    request_count: int = 0
    total_cache_read_tokens: int = 0
    total_input_tokens: int = 0

    async def call(self, user_message: str, conversation: list[dict] | None = None) -> str:
        self.request_count += 1
        msgs = list(conversation or []) + [{"role": "user", "content": user_message}]
        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=self.system_content,
            messages=msgs
        )
        self.total_cache_read_tokens += getattr(resp.usage, "cache_read_input_tokens", 0)
        self.total_input_tokens += resp.usage.input_tokens
        return resp.content[0].text

    def cache_hit_rate_tokens(self) -> float:
        total = self.total_cache_read_tokens + self.total_input_tokens
        return self.total_cache_read_tokens / max(total, 1)

    def session_stats(self) -> dict:
        return {
            "session_id": self.session_id,
            "requests": self.request_count,
            "cache_read_tokens": self.total_cache_read_tokens,
            "regular_input_tokens": self.total_input_tokens,
            "estimated_cache_hit_rate": f"{self.cache_hit_rate_tokens():.1%}",
        }

def create_session(session_id: str, base_system: str, documents: str = "") -> SharedPrefixSession:
    content: list[dict] = [{"type": "text", "text": base_system}]
    if documents:
        content.append({"type": "text", "text": documents, "cache_control": {"type": "ephemeral"}})
    else:
        content[-1]["cache_control"] = {"type": "ephemeral"}
    return SharedPrefixSession(session_id=session_id, system_content=content)

async def main():
    big_docs = "## Knowledge Base\n" + "\n".join([f"### Article {i}\n{'Content ' * 40}" for i in range(1, 25)])
    session = create_session("sess-001", "You are a helpful assistant.", big_docs)

    questions = [
        "Summarize the knowledge base.",
        "What is in article 5?",
        "Compare articles 10 and 15.",
        "What are the main themes?",
    ]
    for q in questions:
        answer = await session.call(q)
        print(f"Q: {q}\nA: {answer[:80]}\n")

    print(f"Session stats: {session.session_stats()}")

asyncio.run(main())
```

---

## Option 6: Dynamic Cache Invalidation on Content Change

```python
import asyncio
import anthropic
import hashlib
import time
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class CacheablePrompt:
    content: str
    content_hash: str = field(init=False)
    last_cached_at: float | None = None
    cache_hit_count: int = 0
    cache_miss_count: int = 0

    def __post_init__(self):
        self.content_hash = hashlib.sha256(self.content.encode()).hexdigest()[:16]

    def has_changed(self, new_content: str) -> bool:
        new_hash = hashlib.sha256(new_content.encode()).hexdigest()[:16]
        return new_hash != self.content_hash

    def update(self, new_content: str):
        self.content = new_content
        self.content_hash = hashlib.sha256(new_content.encode()).hexdigest()[:16]
        self.last_cached_at = None  # Invalidate cache tracking

class DynamicCacheManager:
    def __init__(self):
        self._prompts: dict[str, CacheablePrompt] = {}

    def register(self, name: str, content: str) -> CacheablePrompt:
        prompt = CacheablePrompt(content=content)
        self._prompts[name] = prompt
        return prompt

    def update(self, name: str, new_content: str) -> bool:
        """Returns True if content changed (cache invalidated)."""
        if name not in self._prompts:
            self.register(name, new_content)
            return True
        prompt = self._prompts[name]
        if prompt.has_changed(new_content):
            print(f"[CACHE INVALIDATE] '{name}' content changed — will recreate cache")
            prompt.update(new_content)
            return True
        return False

    def build_system(self, *names: str) -> list[dict]:
        """Build system content with cache_control on the last stable block."""
        blocks: list[dict] = []
        for name in names:
            if name in self._prompts:
                prompt = self._prompts[name]
                blocks.append({"type": "text", "text": prompt.content})
        if blocks:
            blocks[-1]["cache_control"] = {"type": "ephemeral"}
        return blocks

    async def call(self, system_names: list[str], user_message: str) -> str:
        system = self.build_system(*system_names)
        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=system,
            messages=[{"role": "user", "content": user_message}]
        )
        # Track cache stats
        cache_read = getattr(resp.usage, "cache_read_input_tokens", 0)
        for name in system_names:
            if name in self._prompts:
                if cache_read > 0:
                    self._prompts[name].cache_hit_count += 1
                else:
                    self._prompts[name].cache_miss_count += 1
        return resp.content[0].text

mgr = DynamicCacheManager()

async def main():
    base_prompt = "You are a helpful assistant with deep Python expertise.\n\n" + "## Python Docs\n" + "Python knowledge. " * 200
    docs = "## Project Docs\nCurrent project uses FastAPI.\n" + "Project detail. " * 100

    mgr.register("base", base_prompt)
    mgr.register("docs", docs)

    # First few calls — build cache
    for q in ["What is Python?", "Explain async/await.", "How do I use FastAPI?"]:
        result = await mgr.call(["base", "docs"], q)
        print(f"Q: {q}\nA: {result[:60]}\n")

    # Simulate content change — docs updated
    new_docs = "## Project Docs\nCurrent project uses Django now.\n" + "Project detail. " * 100
    changed = mgr.update("docs", new_docs)
    print(f"Docs changed: {changed} — cache will be recreated\n")

    result = await mgr.call(["base", "docs"], "What framework are we using?")
    print(f"After update: {result[:100]}")

asyncio.run(main())
```

---

## Comparison

| Option | What's Cached | Cache Scope | Overhead | Best For |
|--------|-------------|------------|---------|----------|
| 1 – Single System Block | System prompt only | Per-request (shared prefix) | None | Large fixed system prompts |
| 2 – Multi-Block (System+Docs+Tools) | System + RAG + tools | Per-request | None | RAG agents with tool use |
| 3 – Stable-First Ordering | Stable blocks auto-detected | Per-session | None | Mixed stability content |
| 4 – Cache Warm-Up | System prompt | Pre-warmed | One cold call | Latency-critical agents |
| 5 – Session Prefix Pool | All stable content | Per-session | None | Multi-turn conversation agents |
| 6 – Dynamic Invalidation | Named content blocks | Per-content-hash | None | Agents with updatable knowledge |

**Recommendation:** Start with Option 1 — add `cache_control: {"type": "ephemeral"}` to your system prompt if it exceeds ~1,000 tokens. Upgrade to Option 2 when you add RAG documents or tool definitions that repeat across calls. Use Option 4 (warm-up) for latency-sensitive applications where the first request's cache miss is unacceptable. Note: prompt caching is supported on claude-sonnet and claude-opus models; minimum cacheable block size is 1,024 tokens.
