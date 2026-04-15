---
layout: solution
title: "Agent Misses Prompt Caching — Resends the Same Large Context Every Turn"
category: token-cost
description: "The agent loads a 50,000-token system prompt or knowledge base on every API call. It processes the same long document for each of 100 questions. The same tool definitions are re-encoded and billed as input tokens on every request. Anthropic's prompt caching feature would cache this content and charge 10% of the normal input token cost on cache hits — but the agent isn't using it."
tags: [token-cost, prompt-caching, cache, cost-optimization, system-prompt, anthropic-api, input-tokens, context]
---

# Agent Misses Prompt Caching — Resends the Same Large Context Every Turn

## Symptom
- Large system prompt (>1,024 tokens) is billed as full input tokens on every call
- Processing 100 questions against the same document costs 100× the document's token count
- Tool definitions (often 2,000–5,000 tokens) are re-encoded on every API call
- Input token costs dominate even though output is short
- Batch document QA is far more expensive than expected
- `cache_read_input_tokens` is always 0 in usage statistics — cache never hits

## Root Cause
Anthropic's prompt caching writes frequently-used content to a server-side cache for 5 minutes (extendable). Cache hits cost 10% of normal input token price. Cache writes cost 125% for the first call but pay off immediately on the second. Agents miss this by not marking stable content with `cache_control: {"type": "ephemeral"}`. The cache key is the exact content — even one character difference misses. Content must appear in a consistent position (system prompt, or early in messages) with the `cache_control` marker on the last token of the cacheable block.

## Fix

### Option 1: Cache the system prompt — largest single cache opportunity

```python
import anthropic

client = anthropic.Anthropic()

# WRONG — large system prompt billed in full every call:
def ask_without_caching(question: str, knowledge_base: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=f"You are a helpful assistant.\n\nKnowledge Base:\n{knowledge_base}",
        messages=[{"role": "user", "content": question}]
    )
    return response.content[0].text
# If knowledge_base = 50,000 tokens, each of 100 questions costs 50,000 input tokens
# Total: 5,000,000 input tokens just for the knowledge base

# RIGHT — cache the knowledge base with cache_control:
def ask_with_system_cache(question: str, knowledge_base: str) -> tuple[str, dict]:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": "You are a helpful assistant.\n\nKnowledge Base:\n" + knowledge_base,
                "cache_control": {"type": "ephemeral"}  # Cache this block
            }
        ],
        messages=[{"role": "user", "content": question}]
    )
    usage = {
        "input_tokens": response.usage.input_tokens,
        "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0),
        "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
    }
    return response.content[0].text, usage

# First call: cache WRITE (125% cost for the knowledge base portion)
answer1, usage1 = ask_with_system_cache("What is the return policy?", knowledge_base)
print(f"Call 1: input={usage1['input_tokens']}, cache_write={usage1['cache_creation_input_tokens']}")

# Second call: cache HIT (10% cost for the knowledge base portion)
answer2, usage2 = ask_with_system_cache("What is the warranty period?", knowledge_base)
print(f"Call 2: input={usage2['input_tokens']}, cache_read={usage2['cache_read_input_tokens']}")
# cache_read_input_tokens should equal the knowledge base token count
# Those tokens cost 10% instead of 100% — 90% savings on the large block
```

### Option 2: Cache large documents for multi-question QA

```python
import anthropic
from typing import Iterator

client = anthropic.Anthropic()

def batch_qa_with_caching(
    document: str,
    questions: list[str],
    model: str = "claude-sonnet-4-6"
) -> list[dict]:
    """
    Answer multiple questions about the same document.
    The document is cached after the first call — subsequent calls pay 10% for the document.

    Cost comparison (50,000 token document, 20 questions):
    Without caching: 20 × 50,000 = 1,000,000 input tokens
    With caching: 50,000 (write, 125%) + 19 × 50,000 (read, 10%) = 50,000×2.05 = 102,500 effective tokens
    Savings: ~90% reduction in document token costs
    """
    results = []
    total_usage = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}

    for i, question in enumerate(questions):
        response = client.messages.create(
            model=model,
            max_tokens=512,
            system=[
                {
                    "type": "text",
                    "text": (
                        "You are a document analysis assistant. "
                        "Answer questions based solely on the provided document.\n\n"
                        f"Document:\n{document}"
                    ),
                    "cache_control": {"type": "ephemeral"}
                }
            ],
            messages=[{"role": "user", "content": question}]
        )

        cache_write = getattr(response.usage, "cache_creation_input_tokens", 0)
        cache_read = getattr(response.usage, "cache_read_input_tokens", 0)
        total_usage["input"] += response.usage.input_tokens
        total_usage["cache_write"] += cache_write
        total_usage["cache_read"] += cache_read
        total_usage["output"] += response.usage.output_tokens

        results.append({
            "question": question,
            "answer": response.content[0].text,
            "cache_hit": cache_read > 0,
            "call_index": i
        })

    print(f"\nTotal usage across {len(questions)} questions:")
    print(f"  Input tokens: {total_usage['input']:,}")
    print(f"  Cache writes: {total_usage['cache_write']:,} (at 125% cost)")
    print(f"  Cache reads:  {total_usage['cache_read']:,} (at 10% cost)")
    print(f"  Output tokens: {total_usage['output']:,}")

    return results

# Usage:
document = open("large_document.txt").read()  # 50,000 tokens
questions = [
    "What is the main argument?",
    "What evidence is provided?",
    "What are the limitations?",
    # ... 17 more questions
]
answers = batch_qa_with_caching(document, questions)
# Questions 2-20 get ~90% discount on document tokens
```

### Option 3: Cache tool definitions — tools are large and constant

```python
import anthropic

client = anthropic.Anthropic()

# Tool definitions are often 2,000–5,000 tokens and identical across every call.
# Cache them to avoid paying full price every request.

AGENT_TOOLS = [
    {
        "name": "search_database",
        "description": "Search the product database for items matching a query. Returns up to 10 results with name, price, SKU, and availability.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "category": {"type": "string", "enum": ["electronics", "clothing", "food", "other"]},
                "max_price": {"type": "number"},
                "in_stock_only": {"type": "boolean"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_order_status",
        "description": "Retrieve the current status and tracking information for an order by order ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "The order ID (format: ORD-XXXXXXXX)"},
                "include_tracking": {"type": "boolean", "default": True}
            },
            "required": ["order_id"]
        }
    },
    # ... more tools (total ~3,000 tokens of tool definitions)
]

def call_with_cached_tools(
    user_message: str,
    conversation_history: list[dict] | None = None
) -> str:
    """
    Call Claude with tool definitions marked for caching.
    Tool definitions are stable — cache them to pay 10% on subsequent calls.
    """
    messages = conversation_history or []
    messages = messages + [{"role": "user", "content": user_message}]

    # Mark the last tool for caching — all tools up to and including
    # the last one with cache_control will be cached together:
    tools_with_cache = [tool.copy() for tool in AGENT_TOOLS]
    tools_with_cache[-1]["cache_control"] = {"type": "ephemeral"}

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        tools=tools_with_cache,
        messages=messages
    )
    return response.content[0].text

# First call: pays 125% for tool definitions (cache write)
# All subsequent calls: pay 10% for tool definitions (cache hit)
# Across 1,000 daily calls with 3,000-token tools: saves 2,700,000 tokens/day
```

### Option 4: Multi-turn conversation caching — cache growing history

```python
import anthropic
from typing import Optional

client = anthropic.Anthropic()

class CachedConversation:
    """
    Multi-turn conversation that caches the stable prefix of the conversation.
    As conversation grows, marks older turns for caching.
    """

    def __init__(self, system_prompt: str, model: str = "claude-sonnet-4-6"):
        self._model = model
        self._system = system_prompt
        self._messages: list[dict] = []
        self._cached_up_to: int = 0  # Index of last cached message

    def _mark_cache_checkpoint(self):
        """
        Mark a cache checkpoint at the current end of conversation.
        Up to 4 cache breakpoints are allowed per request.
        """
        if not self._messages:
            return

        # Remove old cache_control markers
        for msg in self._messages:
            if isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    if isinstance(block, dict):
                        block.pop("cache_control", None)
            elif isinstance(msg.get("content"), dict):
                msg["content"].pop("cache_control", None)

        # Mark the last assistant message as a cache point
        # (cache the prefix of the conversation)
        last_assistant_idx = None
        for i in range(len(self._messages) - 1, -1, -1):
            if self._messages[i]["role"] == "assistant":
                last_assistant_idx = i
                break

        if last_assistant_idx is not None:
            content = self._messages[last_assistant_idx]["content"]
            if isinstance(content, str):
                self._messages[last_assistant_idx]["content"] = [
                    {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
                ]
            elif isinstance(content, list) and content:
                # Add cache_control to the last block
                if isinstance(content[-1], dict):
                    content[-1]["cache_control"] = {"type": "ephemeral"}

    def send(self, user_message: str) -> str:
        self._messages.append({"role": "user", "content": user_message})

        # Mark cache checkpoint every 4 turns
        if len(self._messages) % 8 == 0:  # Every 4 complete exchanges
            self._mark_cache_checkpoint()

        response = client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=[{
                "type": "text",
                "text": self._system,
                "cache_control": {"type": "ephemeral"}
            }],
            messages=self._messages
        )

        assistant_reply = response.content[0].text
        self._messages.append({"role": "assistant", "content": assistant_reply})

        cache_read = getattr(response.usage, "cache_read_input_tokens", 0)
        if cache_read > 0:
            print(f"Cache hit: {cache_read:,} tokens at 10% cost")

        return assistant_reply

# Usage:
conv = CachedConversation(
    system_prompt="You are an expert Python tutor with deep knowledge of...\n" + LONG_CURRICULUM
)
reply1 = conv.send("What is a generator in Python?")
reply2 = conv.send("Can you show me an example?")
reply3 = conv.send("How do I use yield from?")
# Earlier turns are cached — the growing history doesn't cost full price each time
```

### Option 5: Explicit cache warming — prime the cache before burst traffic

```python
import anthropic
import asyncio
import logging

logger = logging.getLogger(__name__)
client = anthropic.AsyncAnthropic()

async def warm_cache(
    content_blocks: list[dict],
    model: str = "claude-sonnet-4-6"
) -> dict:
    """
    Prime the cache with a minimal request.
    Send a cheap dummy question that forces the cache to be written.
    All subsequent real calls within 5 minutes will get cache hits.
    """
    response = await client.messages.create(
        model=model,
        max_tokens=1,  # Minimal output — we only want to write the cache
        system=content_blocks,
        messages=[{"role": "user", "content": "Ready."}]
    )
    cache_written = getattr(response.usage, "cache_creation_input_tokens", 0)
    logger.info(f"Cache warmed: {cache_written:,} tokens written to cache")
    return {
        "cache_tokens_written": cache_written,
        "cost_multiplier": "125% (write cost, amortized across subsequent hits)"
    }

async def scheduled_cache_warmer(
    system_content: list[dict],
    warm_interval_seconds: int = 240,  # Re-warm every 4 minutes (cache TTL is 5 min)
    model: str = "claude-sonnet-4-6"
):
    """
    Background task that keeps the cache warm by re-warming every 4 minutes.
    Ensures cache hits for high-traffic endpoints without cold starts.
    """
    while True:
        try:
            await warm_cache(system_content, model)
        except Exception as exc:
            logger.warning(f"Cache warm failed: {exc}")
        await asyncio.sleep(warm_interval_seconds)

# Usage — start warming before the first real user request:
SYSTEM_CONTENT = [
    {
        "type": "text",
        "text": "You are a customer service assistant.\n\n" + LARGE_FAQ_DOCUMENT,
        "cache_control": {"type": "ephemeral"}
    }
]

# Start background warmer:
asyncio.create_task(scheduled_cache_warmer(SYSTEM_CONTENT))

# All user requests within 5 minutes of the last warm will hit the cache:
async def handle_user_question(question: str) -> str:
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM_CONTENT,
        messages=[{"role": "user", "content": question}]
    )
    return response.content[0].text
```

### Option 6: Cache analytics — measure cache hit rate and savings

```python
import anthropic
from dataclasses import dataclass, field
from typing import Any

@dataclass
class CacheStats:
    calls: int = 0
    cache_writes: int = 0
    cache_reads: int = 0
    regular_input: int = 0
    output_tokens: int = 0

    @property
    def cache_hit_rate(self) -> float:
        if self.calls <= 1:
            return 0.0
        return self.cache_reads / max(self.cache_reads + self.regular_input, 1)

    @property
    def estimated_savings_pct(self) -> float:
        """Estimate token cost savings vs. no caching."""
        # Write cost: 1.25×, Read cost: 0.10×, Regular: 1.0×
        cost_with_cache = self.cache_writes * 1.25 + self.cache_reads * 0.10 + self.regular_input
        cost_without_cache = self.cache_writes + self.cache_reads + self.regular_input
        if cost_without_cache == 0:
            return 0.0
        return (1 - cost_with_cache / cost_without_cache) * 100

    def report(self):
        print(f"Cache Stats ({self.calls} calls):")
        print(f"  Cache writes: {self.cache_writes:,} tokens (at 125%)")
        print(f"  Cache reads:  {self.cache_reads:,} tokens (at 10%)")
        print(f"  Regular input: {self.regular_input:,} tokens (at 100%)")
        print(f"  Output: {self.output_tokens:,} tokens")
        print(f"  Cache hit rate: {self.cache_hit_rate:.1%}")
        print(f"  Estimated savings: {self.estimated_savings_pct:.1f}%")

stats = CacheStats()
client = anthropic.Anthropic()

def tracked_call(system: list[dict], messages: list[dict], max_tokens: int = 1024) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        system=system,
        messages=messages
    )
    stats.calls += 1
    stats.cache_writes += getattr(response.usage, "cache_creation_input_tokens", 0)
    stats.cache_reads += getattr(response.usage, "cache_read_input_tokens", 0)
    stats.regular_input += response.usage.input_tokens
    stats.output_tokens += response.usage.output_tokens
    return response.content[0].text

# After processing: stats.report() to see actual savings
```

## When Prompt Caching Pays Off

| Scenario | Cache Content | Break-even | Typical Savings |
|---|---|---|---|
| Large system prompt (>5K tokens) | System prompt | 2nd call | 80–90% on system prompt |
| Document QA (same doc, many questions) | Full document | 2nd question | 85–90% on document |
| Agent with many tools (>10 tools) | Tool definitions | 2nd call | 70–80% on tool tokens |
| Multi-turn conversation | Conversation prefix | 5th+ turn | 40–60% on history |
| Batch processing same instructions | Instructions block | 2nd item | 85–90% on instructions |

## Cache Constraints (Anthropic, as of 2026)
- Minimum cacheable block: **1,024 tokens** (smaller blocks are not cached)
- Cache TTL: **5 minutes** (resets on each cache hit)
- Max cache breakpoints per request: **4**
- Supported models: claude-sonnet-4-6, claude-opus-4-6, claude-haiku-4-5 (check docs for latest)
- Cache write cost: **125%** of normal input token price
- Cache read cost: **10%** of normal input token price
- Break-even: **2 calls** (write on call 1 + read on call 2 = 1.25 + 0.10 = 1.35 vs. 2.0 without cache)

## Expected Token Savings
50,000-token knowledge base, 100 questions/day, no caching: 5,000,000 input tokens/day
With prompt caching: 50,000 (write) + 99 × 5,000 (reads at 10%) = 50,000 + 495,000 effective = 545,000
Savings: 89% reduction in knowledge base token costs = ~$13.35/day at $3/M tokens (Sonnet)

## Environment
- Any agent with a large, stable system prompt (>1,024 tokens), document QA workloads, or agents with rich tool definitions; prompt caching is the single cheapest cost optimization for knowledge-intensive agents — it requires only adding `cache_control` markers, no architecture changes
- Source: direct experience; prompt caching reduces input token costs by 80–90% for document QA agents and is typically the highest-ROI optimization available, often cutting total API costs by 40–60%
