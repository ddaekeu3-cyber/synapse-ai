---
layout: solution
title: "Agent Doesn't Implement Prompt Prefix Deduplication"
category: token-cost
description: "Eliminate redundant prompt prefixes sent across parallel or repeated API calls by canonicalizing shared context and leveraging Anthropic's prompt caching."
tags: [prompt-caching, deduplication, prefix, token-savings, parallel-calls, cache-control]
---

# Agent Doesn't Implement Prompt Prefix Deduplication

When an agent fans out to N parallel calls — all sharing the same system prompt and tool definitions — it sends the identical prefix N times and pays full input token cost each time. Prefix deduplication identifies the shared head of each prompt family, caches it once with `cache_control`, and eliminates redundant billing on every subsequent call in that batch.

## Option 1: Shared System Prompt with Cache Control

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

# Long system prompt that would otherwise be re-sent on every parallel call
SYSTEM_PROMPT = """You are an expert code reviewer with 15 years of experience.
You specialize in Python, async patterns, distributed systems, and AI agent architecture.
When reviewing code, always check for: correctness, performance, security, maintainability.
Format your review as: ISSUES (list), SUGGESTIONS (list), VERDICT (one sentence).
Be concise and actionable. Do not repeat the code back.""" * 3  # simulate a longer prompt


async def review_code(snippet: str, language: str) -> str:
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},  # Cache the shared prefix
            }
        ],
        messages=[{"role": "user", "content": f"Review this {language} code:\n```{language}\n{snippet}\n```"}],
    )
    usage = r.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0)
    cache_write = getattr(usage, "cache_creation_input_tokens", 0)
    print(f"[CACHE] read={cache_read} write={cache_write} new_input={usage.input_tokens}")
    return r.content[0].text


async def main() -> None:
    snippets = [
        ("def add(a, b): return a+b", "python"),
        ("async def fetch(): await asyncio.sleep(1)", "python"),
        ("SELECT * FROM users WHERE id=1", "sql"),
    ]
    # All 3 calls share the system prompt — only the first pays cache_creation tokens
    results = await asyncio.gather(*[review_code(s, lang) for s, lang in snippets])
    for i, r in enumerate(results):
        print(f"\n--- Review {i+1} ---\n{r[:200]}")


asyncio.run(main())

# Expected Token Savings: ~90% reduction on system prompt tokens for calls 2+ in each batch
# Environment: Python 3.11+; cache_control requires claude-haiku-4-5/sonnet-4-6/opus-4-6 models
```

## Option 2: Tool Schema Prefix Caching for Multi-Tool Agents

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

# Large tool set shared across all agent calls
TOOLS = [
    {
        "name": "search_web",
        "description": "Search the web for current information on any topic",
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
        "name": "execute_python",
        "description": "Execute Python code in a sandboxed environment and return stdout",
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute"},
                "timeout": {"type": "integer", "description": "Execution timeout in seconds", "default": 10},
            },
            "required": ["code"],
        },
    },
    {
        "name": "read_file",
        "description": "Read the contents of a file from the workspace",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File path to read"}},
            "required": ["path"],
        },
    },
]

CACHED_SYSTEM = [
    {
        "type": "text",
        "text": "You are a helpful AI assistant with access to tools. Use tools when needed to answer questions accurately.",
        "cache_control": {"type": "ephemeral"},
    }
]


async def agent_call(task: str) -> str:
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=CACHED_SYSTEM,
        tools=TOOLS,
        messages=[{"role": "user", "content": task}],
    )
    usage = r.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0)
    print(f"[CACHE] task='{task[:40]}' cache_read={cache_read} input={usage.input_tokens}")
    return r.content[0].text if r.content else str(r.stop_reason)


async def main() -> None:
    tasks = [
        "What tools do you have available?",
        "Can you search the web for me?",
        "How would you execute Python code?",
    ]
    results = await asyncio.gather(*[agent_call(t) for t in tasks])
    for t, r in zip(tasks, results):
        print(f"\nTask: {t}\nAnswer: {r[:150]}")


asyncio.run(main())

# Expected Token Savings: Tool schemas often 200-500 tokens; caching saves 90% on repeated calls
# Environment: Python 3.11+; tools list itself is not directly cacheable via cache_control (use system)
```

## Option 3: Conversation History Prefix Cache for Multi-Turn Agents

```python
import anthropic

client = anthropic.Anthropic()


def build_cached_history(turns: list[dict]) -> list[dict]:
    """
    Mark the stable prefix of a conversation for caching.
    Only the last turn (the new user message) should be uncached.
    """
    if len(turns) < 2:
        return turns

    cached = []
    for i, turn in enumerate(turns[:-1]):
        if turn["role"] == "user":
            content = turn["content"]
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            # Mark the last content block of the last stable turn as the cache boundary
            if i == len(turns) - 2:
                content = list(content)
                content[-1] = dict(content[-1], cache_control={"type": "ephemeral"})
            cached.append({"role": turn["role"], "content": content})
        else:
            cached.append(turn)

    cached.append(turns[-1])  # Latest turn — not cached
    return cached


def multi_turn_agent(history: list[dict], new_message: str) -> tuple[str, dict]:
    """Add new message and call with cached history prefix."""
    turns = history + [{"role": "user", "content": new_message}]
    cached_turns = build_cached_history(turns)

    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=[{"type": "text", "text": "You are a helpful assistant with perfect memory of this conversation.", "cache_control": {"type": "ephemeral"}}],
        messages=cached_turns,
    )
    usage = r.usage
    stats = {
        "input": usage.input_tokens,
        "output": usage.output_tokens,
        "cache_read": getattr(usage, "cache_read_input_tokens", 0),
        "cache_write": getattr(usage, "cache_creation_input_tokens", 0),
    }
    return r.content[0].text, stats


if __name__ == "__main__":
    history: list[dict] = []
    questions = [
        "I'm building a Python agent that handles 1000 requests/hour.",
        "What concurrency pattern would you recommend?",
        "How should I handle backpressure in that pattern?",
        "And what about health checks for the workers?",
    ]

    for q in questions:
        answer, stats = multi_turn_agent(history, q)
        history.append({"role": "user", "content": q})
        history.append({"role": "assistant", "content": answer})
        print(f"Q: {q}")
        print(f"A: {answer[:120]}")
        print(f"   [tokens] input={stats['input']} cache_read={stats['cache_read']} cache_write={stats['cache_write']}\n")

# Expected Token Savings: Growing history reuses cached prefix; savings increase with conversation length
# Environment: Python 3.9+; cache boundary should be placed at the last stable turn
```

## Option 4: Batch Deduplication — Detect and Collapse Identical Prefixes

```python
import hashlib
import asyncio
import anthropic
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()


@dataclass
class PromptRequest:
    task_id: str
    system: str
    user_message: str


def prefix_hash(system: str) -> str:
    return hashlib.sha256(system.encode()).hexdigest()[:12]


async def batch_with_dedup(requests: list[PromptRequest]) -> dict[str, str]:
    """
    Group requests by system prompt hash.
    Within each group, first call creates the cache; subsequent calls read it.
    """
    from collections import defaultdict
    groups: dict[str, list[PromptRequest]] = defaultdict(list)
    for req in requests:
        groups[prefix_hash(req.system)].append(req)

    results: dict[str, str] = {}

    async def run_group(group: list[PromptRequest]) -> None:
        system_text = group[0].system
        cached_system = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]

        async def call_one(req: PromptRequest) -> None:
            r = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                system=cached_system,
                messages=[{"role": "user", "content": req.user_message}],
            )
            cache_read = getattr(r.usage, "cache_read_input_tokens", 0)
            print(f"[BATCH] {req.task_id}: input={r.usage.input_tokens} cache_read={cache_read}")
            results[req.task_id] = r.content[0].text

        await asyncio.gather(*[call_one(req) for req in group])

    await asyncio.gather(*[run_group(group) for group in groups.values()])
    return results


async def main() -> None:
    EXPERT_SYSTEM = "You are a Python expert. Answer concisely in one sentence." * 10

    requests = [
        PromptRequest("q1", EXPERT_SYSTEM, "What is a generator?"),
        PromptRequest("q2", EXPERT_SYSTEM, "What is a context manager?"),
        PromptRequest("q3", EXPERT_SYSTEM, "What is the GIL?"),
        PromptRequest("q4", "You are a SQL expert. Answer in one sentence.", "What is an index?"),
    ]

    results = await batch_with_dedup(requests)
    for tid, answer in results.items():
        print(f"{tid}: {answer[:100]}")


asyncio.run(main())

# Expected Token Savings: 3 Python-expert calls share one cache write; 2nd and 3rd pay only cache_read
# Environment: Python 3.11+; group by prefix_hash before fanning out parallel calls
```

## Option 5: Static Prefix Registry with Lazy Cache Warming

```python
import asyncio
import anthropic
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

# Registry of named system prompts
PREFIX_REGISTRY: dict[str, str] = {
    "code_reviewer": "You are an expert code reviewer. Check for bugs, performance, and security. Be concise." * 8,
    "sql_expert": "You are a senior DBA. Answer SQL questions accurately and briefly." * 8,
    "devops_advisor": "You are a DevOps expert specializing in Docker, Kubernetes, and CI/CD pipelines." * 8,
}

_cache_warmed: set[str] = set()
_warm_lock = asyncio.Lock()


async def warm_cache(prefix_name: str) -> None:
    """Send a minimal message to prime the cache for a given prefix."""
    async with _warm_lock:
        if prefix_name in _cache_warmed:
            return
        system_text = PREFIX_REGISTRY[prefix_name]
        await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            system=[{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": "ready"}],
        )
        _cache_warmed.add(prefix_name)
        print(f"[WARM] Prefix '{prefix_name}' cache warmed")


async def call_with_prefix(prefix_name: str, user_message: str) -> str:
    await warm_cache(prefix_name)
    system_text = PREFIX_REGISTRY[prefix_name]
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=[{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_message}],
    )
    cache_read = getattr(r.usage, "cache_read_input_tokens", 0)
    print(f"[CALL] prefix='{prefix_name}' cache_read={cache_read} input={r.usage.input_tokens}")
    return r.content[0].text


async def main() -> None:
    # Warm all caches in parallel at startup
    await asyncio.gather(*[warm_cache(name) for name in PREFIX_REGISTRY])

    tasks = [
        call_with_prefix("code_reviewer", "Review: def foo(): pass"),
        call_with_prefix("code_reviewer", "Review: x = lambda: None"),
        call_with_prefix("sql_expert", "Is SELECT * ever acceptable?"),
        call_with_prefix("devops_advisor", "When should I use multi-stage Docker builds?"),
    ]
    results = await asyncio.gather(*tasks)
    for r in results:
        print(f"> {r[:120]}\n")


asyncio.run(main())

# Expected Token Savings: Pre-warming ensures all real calls hit cache_read; 90% savings on prefix
# Environment: Python 3.11+; call warm_cache() at agent startup before serving requests
```

## Option 6: Dynamic Prefix Extraction and Dedup Pipeline

```python
import asyncio
import anthropic
from collections import Counter

client = anthropic.AsyncAnthropic()


def extract_common_prefix(messages_list: list[list[dict]]) -> tuple[str, list[list[dict]]]:
    """
    Find the longest common prefix across all message lists.
    Returns (prefix_as_system_text, list_of_unique_suffixes).
    """
    if not messages_list:
        return "", []

    # Simple heuristic: if all first messages are identical user messages, extract as prefix
    first_contents = [msgs[0]["content"] for msgs in messages_list if msgs]
    counter = Counter(first_contents)
    common, count = counter.most_common(1)[0]

    if count < len(messages_list) * 0.8:
        return "", messages_list  # No dominant prefix

    prefix = common
    suffixes = []
    for msgs in messages_list:
        if msgs and msgs[0]["content"] == prefix:
            suffixes.append(msgs[1:] if len(msgs) > 1 else [{"role": "user", "content": "continue"}])
        else:
            suffixes.append(msgs)

    return prefix, suffixes


async def dedup_batch_call(
    shared_context: str,
    tasks: list[str],
    model: str = "claude-haiku-4-5-20251001",
) -> list[str]:
    """
    Send a batch of tasks that all share the same context prefix.
    Uses cache_control on the shared context to dedup prefix tokens.
    """
    system = [
        {
            "type": "text",
            "text": shared_context,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    async def one_call(task: str) -> str:
        r = await client.messages.create(
            model=model,
            max_tokens=128,
            system=system,
            messages=[{"role": "user", "content": task}],
        )
        cache_read = getattr(r.usage, "cache_read_input_tokens", 0)
        cache_write = getattr(r.usage, "cache_creation_input_tokens", 0)
        if cache_write:
            print(f"[DEDUP] Cache written ({cache_write} tokens)")
        elif cache_read:
            print(f"[DEDUP] Cache hit ({cache_read} tokens saved)")
        return r.content[0].text

    return list(await asyncio.gather(*[one_call(t) for t in tasks]))


async def main() -> None:
    # Long shared document context — sent once, cached, referenced by all tasks
    shared_doc = """
    Architecture Document: SynapseAI Platform v2.0

    Components:
    - Gateway: handles authentication, routing, rate limiting
    - Agent Runtime: executes tools, manages context, handles retries
    - Memory Store: SQLite + vector index for episodic and semantic memory
    - Task Queue: SQLite-backed persistent queue with lease-based claiming
    - Observability: structured JSON logs, Prometheus metrics, distributed traces

    Constraints:
    - Max concurrent agents: 50
    - Max context window: 200k tokens
    - Tool call timeout: 30s
    - Memory TTL: 7 days
    """ * 5  # simulate a longer document

    tasks = [
        "What is the role of the Gateway component?",
        "How does the Memory Store work?",
        "What is the max concurrent agents limit?",
        "How are tool calls timed out?",
    ]

    results = await dedup_batch_call(shared_doc, tasks)
    for task, result in zip(tasks, results):
        print(f"\nQ: {task}\nA: {result[:150]}")


asyncio.run(main())

# Expected Token Savings: 4 tasks sharing a 500-token doc: saves 1500 tokens (3 cache hits)
# Environment: Python 3.11+; effective when fanning out analysis of a shared document/codebase
```

## Comparison

| Option | Dedup Strategy | Cache Scope | Parallel | Warm-Up | Best For |
|--------|---------------|-------------|----------|---------|----------|
| 1. Shared System Prompt | `cache_control` on system | System prompt | Yes | No | Repeated parallel calls |
| 2. Tool Schema Cache | `cache_control` on system | System + tools | Yes | No | Multi-tool agents |
| 3. History Prefix Cache | Cache last stable turn | Conversation | No | No | Multi-turn agents |
| 4. Batch Dedup | Group by prefix hash | System | Yes | No | Mixed-prefix batches |
| 5. Registry + Warming | Pre-warm on startup | Named prefixes | Yes | Yes | High-frequency roles |
| 6. Dynamic Extraction | Auto-extract common prefix | Shared document | Yes | No | Document Q&A fanout |
