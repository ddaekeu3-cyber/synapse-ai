---
title: "Agent Doesn't Implement Prompt Caching for Repeated System Prompts"
description: "Six solutions for using Anthropic's prompt caching feature to reduce costs and latency when system prompts or large contexts are repeated across requests."
difficulty: intermediate
category: performance
tags: [prompt-caching, cost, latency, anthropic, caching, performance]
---

# Agent Doesn't Implement Prompt Caching for Repeated System Prompts

Every request re-sends the same large system prompt, tool definitions, and retrieved context — paying full input token cost each time. Anthropic's prompt caching lets you mark static content with `cache_control` so it's processed once and reused across requests. This can cut input costs by 90% and reduce latency by up to 85% for cache hits.

## Solution 1: Basic System Prompt Caching

Mark the system prompt with `cache_control` to cache it server-side across requests.

```python
import asyncio
from anthropic import AsyncAnthropic


class CachedSystemPromptAgent:
    """
    Adds cache_control to the system prompt so it's cached after the first request.
    All subsequent requests with the same system prompt pay ~10% of normal input cost.
    """

    SYSTEM_PROMPT = """You are an expert software engineering assistant with deep knowledge of:
- Python, TypeScript, Go, Rust, and Java
- Distributed systems, microservices, and API design
- Database design (SQL and NoSQL), caching strategies, and data modeling
- Cloud platforms (AWS, GCP, Azure) and infrastructure as code
- Security best practices, authentication, and authorization
- Performance optimization, profiling, and scalability
- Testing strategies: unit, integration, e2e, property-based, and chaos testing
- Observability: logging, metrics, tracing, and alerting
- CI/CD pipelines, GitOps, and deployment strategies
- Agent systems, LLM APIs, and AI application architecture

Always provide accurate, idiomatic code examples. Explain trade-offs clearly.
When reviewing code, check for security vulnerabilities, performance issues, and maintainability.
""" * 5  # Repeat to make it large enough to benefit from caching (>1024 tokens)

    def __init__(self):
        self.client = AsyncAnthropic()

    async def chat(self, message: str) -> dict:
        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": self.SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},  # Mark for caching
                }
            ],
            messages=[{"role": "user", "content": message}],
        )
        # cache_creation_input_tokens: tokens written to cache (first request)
        # cache_read_input_tokens: tokens read from cache (subsequent requests)
        usage = response.usage
        return {
            "text": response.content[0].text,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_creation_tokens": getattr(usage, "cache_creation_input_tokens", 0),
            "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0),
        }


async def demo_basic_caching():
    agent = CachedSystemPromptAgent()
    questions = [
        "What is the difference between a mutex and a semaphore?",
        "How do I implement rate limiting in Python?",
        "Explain the CAP theorem.",
    ]
    total_cost = 0.0
    for i, q in enumerate(questions):
        result = await agent.chat(q)
        # Cache read tokens cost ~10% of normal input price
        cache_hit = result["cache_read_tokens"] > 0
        print(
            f"Request {i+1}: "
            f"input={result['input_tokens']} "
            f"cache_create={result['cache_creation_tokens']} "
            f"cache_read={result['cache_read_tokens']} "
            f"{'[CACHE HIT]' if cache_hit else '[CACHE MISS]'}"
        )
```

## Solution 2: Multi-Block Caching with Tool Definitions

Cache both the system prompt and tool definitions separately; tool schemas are often large and static.

```python
import asyncio
import json
from anthropic import AsyncAnthropic


# Large static tool definitions
TOOLS = [
    {
        "name": "search_database",
        "description": "Search the product database for items matching the query. Returns up to 20 results with product ID, name, price, stock level, and category.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "category": {"type": "string", "enum": ["electronics", "clothing", "books", "home", "sports"]},
                "max_price": {"type": "number"},
                "in_stock_only": {"type": "boolean"},
                "sort_by": {"type": "string", "enum": ["relevance", "price_asc", "price_desc", "rating"]},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_product_details",
        "description": "Retrieve detailed information about a specific product including reviews, specifications, shipping options, and related products.",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string"},
                "include_reviews": {"type": "boolean", "default": True},
                "include_related": {"type": "boolean", "default": False},
            },
            "required": ["product_id"],
        },
    },
    {
        "name": "place_order",
        "description": "Place an order for a product on behalf of the customer. Validates stock, applies discounts, and initiates fulfillment.",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string"},
                "quantity": {"type": "integer", "minimum": 1, "maximum": 100},
                "shipping_address": {
                    "type": "object",
                    "properties": {
                        "street": {"type": "string"},
                        "city": {"type": "string"},
                        "country": {"type": "string"},
                        "postal_code": {"type": "string"},
                    },
                    "required": ["street", "city", "country"],
                },
                "coupon_code": {"type": "string"},
            },
            "required": ["product_id", "quantity", "shipping_address"],
        },
    },
]


class MultiBlockCachedAgent:
    SYSTEM = (
        "You are a helpful e-commerce assistant. Help customers find products, "
        "check details, and place orders. Always confirm order details before placing. "
        "Be concise and friendly."
    )

    def __init__(self):
        self.client = AsyncAnthropic()

    async def chat(self, message: str, conversation_history: list[dict] | None = None) -> dict:
        # Cache the tool definitions by adding cache_control to the last tool
        cacheable_tools = []
        for i, tool in enumerate(TOOLS):
            t = dict(tool)
            if i == len(TOOLS) - 1:  # Mark last tool to cache all preceding
                t["cache_control"] = {"type": "ephemeral"}
            cacheable_tools.append(t)

        messages = (conversation_history or []) + [{"role": "user", "content": message}]

        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": self.SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=cacheable_tools,
            messages=messages,
        )
        usage = response.usage
        return {
            "content": response.content,
            "stop_reason": response.stop_reason,
            "cache_creation_tokens": getattr(usage, "cache_creation_input_tokens", 0),
            "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0),
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
        }


async def demo_multi_block():
    agent = MultiBlockCachedAgent()
    queries = [
        "Find me a laptop under $1000.",
        "Show me details for product P123.",
        "What are the most popular electronics?",
    ]
    for i, q in enumerate(queries):
        result = await agent.chat(q)
        print(
            f"Q{i+1}: cache_create={result['cache_creation_tokens']} "
            f"cache_read={result['cache_read_tokens']} "
            f"input={result['input_tokens']}"
        )
```

## Solution 3: Conversation History Caching for Long Multi-Turn Chats

Cache the growing conversation history so only the latest user turn is processed fresh each request.

```python
import asyncio
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class CachedConversation:
    """
    Maintains conversation history with cache_control on the last assistant turn.
    This caches everything up to and including the last exchange.
    """
    messages: list[dict] = field(default_factory=list)
    system_prompt: str = "You are a helpful assistant."
    _cache_stats: dict = field(default_factory=lambda: {
        "hits": 0, "misses": 0, "creation_tokens": 0, "read_tokens": 0
    })

    def add_user(self, content: str):
        self.messages.append({"role": "user", "content": content})

    def add_assistant(self, content: str, cache: bool = True):
        """Add assistant message, optionally marking it as cacheable."""
        if cache and self.messages:
            msg = {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": content,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        else:
            msg = {"role": "assistant", "content": content}
        self.messages.append(msg)

    def to_api_messages(self) -> list[dict]:
        """Return messages suitable for the API, with cache on the last assistant turn."""
        if not self.messages:
            return []
        messages = list(self.messages)
        # Ensure the most recent assistant message has cache_control
        for i in range(len(messages) - 1, -1, -1):
            if messages[i]["role"] == "assistant":
                content = messages[i]["content"]
                if isinstance(content, str):
                    messages[i] = {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "text",
                                "text": content,
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                    }
                break
        return messages

    def record_usage(self, usage):
        create = getattr(usage, "cache_creation_input_tokens", 0)
        read = getattr(usage, "cache_read_input_tokens", 0)
        self._cache_stats["creation_tokens"] += create
        self._cache_stats["read_tokens"] += read
        if read > 0:
            self._cache_stats["hits"] += 1
        else:
            self._cache_stats["misses"] += 1

    @property
    def cache_hit_rate(self) -> float:
        total = self._cache_stats["hits"] + self._cache_stats["misses"]
        return self._cache_stats["hits"] / max(total, 1)


class CachedMultiTurnAgent:
    def __init__(self, system_prompt: str = "You are a helpful assistant."):
        self.client = AsyncAnthropic()
        self.conversation = CachedConversation(system_prompt=system_prompt)

    async def chat(self, message: str) -> str:
        self.conversation.add_user(message)

        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": self.conversation.system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=self.conversation.to_api_messages(),
        )
        self.conversation.record_usage(response.usage)
        text = response.content[0].text
        self.conversation.add_assistant(text, cache=True)
        return text

    def stats(self) -> dict:
        s = self.conversation._cache_stats
        return {
            "cache_hit_rate": round(self.conversation.cache_hit_rate, 3),
            "total_creation_tokens": s["creation_tokens"],
            "total_read_tokens": s["read_tokens"],
            "hits": s["hits"],
            "misses": s["misses"],
        }


async def demo_conversation_cache():
    agent = CachedMultiTurnAgent(
        system_prompt="You are an expert Python tutor. Explain concepts clearly with examples."
    )
    turns = [
        "What is a Python generator?",
        "Can you show me a more complex example with send()?",
        "How does this compare to async generators?",
        "What are the memory benefits of generators?",
    ]
    for turn in turns:
        response = await agent.chat(turn)
        print(f"Q: {turn[:50]}")
        print(f"A: {response[:80]}...")
    print(f"\nCache stats: {agent.stats()}")
```

## Solution 4: Retrieved Context Caching (RAG Cache)

When using RAG, cache the retrieved documents so they're only processed once per query set.

```python
import asyncio
import hashlib
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class RetrievedContext:
    query: str
    documents: list[str]
    context_hash: str = ""

    def __post_init__(self):
        combined = "\n".join(self.documents)
        self.context_hash = hashlib.sha256(combined.encode()).hexdigest()[:16]

    def to_cached_block(self) -> dict:
        """Format retrieved docs as a cacheable content block."""
        docs_text = "\n\n---\n\n".join(
            f"Document {i+1}:\n{doc}" for i, doc in enumerate(self.documents)
        )
        return {
            "type": "text",
            "text": f"<retrieved_context>\n{docs_text}\n</retrieved_context>",
            "cache_control": {"type": "ephemeral"},
        }


class RAGCachedAgent:
    """
    Retrieves documents once per unique context; caches the context block
    so follow-up questions on the same documents are cheap.
    """

    def __init__(self):
        self.client = AsyncAnthropic()
        self._context_cache: dict[str, RetrievedContext] = {}
        self._cache_stats = {"hits": 0, "misses": 0}

    async def _retrieve_documents(self, query: str) -> list[str]:
        """Stub: in production, call your vector DB here."""
        # Simulate retrieval — return same docs for same query
        return [
            f"Document about '{query}': This is comprehensive information about {query}. "
            f"Key concepts include: A, B, C. Important details: X, Y, Z. " * 20,
            f"Secondary document on '{query}': Additional context and examples. " * 15,
            f"Reference material for '{query}': Technical specifications and data. " * 10,
        ]

    async def answer(
        self,
        question: str,
        context_key: str | None = None,
        reuse_context: str | None = None,
    ) -> dict:
        """
        context_key: Use this key to retrieve and cache documents.
        reuse_context: Reuse a previously retrieved context by hash.
        """
        if reuse_context and reuse_context in self._context_cache:
            ctx = self._context_cache[reuse_context]
            self._cache_stats["hits"] += 1
        else:
            docs = await self._retrieve_documents(context_key or question)
            ctx = RetrievedContext(query=context_key or question, documents=docs)
            self._context_cache[ctx.context_hash] = ctx
            self._cache_stats["misses"] += 1

        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": "Answer questions based on the provided context. Be precise and cite relevant parts.",
                    "cache_control": {"type": "ephemeral"},
                },
                ctx.to_cached_block(),  # Cached retrieved docs
            ],
            messages=[{"role": "user", "content": question}],
        )
        usage = response.usage
        return {
            "answer": response.content[0].text,
            "context_hash": ctx.context_hash,
            "cache_creation_tokens": getattr(usage, "cache_creation_input_tokens", 0),
            "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0),
        }

    def stats(self) -> dict:
        return self._cache_stats


async def demo_rag_cache():
    agent = RAGCachedAgent()

    # First question: retrieves docs, caches context
    r1 = await agent.answer("What are the key concepts?", context_key="machine learning")
    print(f"Q1 cache_create={r1['cache_creation_tokens']} cache_read={r1['cache_read_tokens']}")
    ctx_hash = r1["context_hash"]

    # Follow-up questions: reuse cached context
    follow_ups = [
        "Give me more details about concept A.",
        "What are the technical specifications?",
        "Summarize the key points.",
    ]
    for q in follow_ups:
        r = await agent.answer(q, reuse_context=ctx_hash)
        print(f"Follow-up cache_read={r['cache_read_tokens']} (cached={'yes' if r['cache_read_tokens'] > 0 else 'no'})")

    print(f"\nRAG cache stats: {agent.stats()}")
```

## Solution 5: Automatic Cache Warming for Predictable Workflows

Pre-warm the cache by sending a dummy request before peak traffic so the first real request gets a cache hit.

```python
import asyncio
import time
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class CacheWarmingConfig:
    warm_interval_seconds: float = 240.0  # Re-warm before 5-minute TTL expires
    warm_on_startup: bool = True


class CacheWarmingAgent:
    """
    Maintains a warm cache by periodically refreshing it.
    Anthropic's ephemeral cache TTL is 5 minutes; we re-warm at 4 minutes.
    """

    SYSTEM_PROMPT = (
        "You are an expert assistant specializing in technical documentation. "
        "Provide accurate, well-structured responses. "
        "Always include code examples when explaining technical concepts. "
    ) * 30  # Make it large enough to benefit from caching

    def __init__(self, config: CacheWarmingConfig | None = None):
        self.client = AsyncAnthropic()
        self.config = config or CacheWarmingConfig()
        self._last_warmed_at: float = 0.0
        self._warm_task: asyncio.Task | None = None
        self._warm_count = 0

    async def start(self):
        if self.config.warm_on_startup:
            await self._warm_cache()
        self._warm_task = asyncio.create_task(self._warming_loop())

    async def stop(self):
        if self._warm_task:
            self._warm_task.cancel()

    async def _warm_cache(self):
        """Send a minimal request to populate the cache."""
        try:
            await self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1,  # Minimal output — just warm the cache
                system=[
                    {
                        "type": "text",
                        "text": self.SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": "warmup"}],
            )
            self._last_warmed_at = time.time()
            self._warm_count += 1
            print(f"[CACHE WARM] Cache warmed (count={self._warm_count})")
        except Exception as e:
            print(f"[CACHE WARM] Failed: {e}")

    async def _warming_loop(self):
        while True:
            await asyncio.sleep(self.config.warm_interval_seconds)
            await self._warm_cache()

    async def chat(self, message: str) -> dict:
        """All requests benefit from the pre-warmed cache."""
        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": self.SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": message}],
        )
        usage = response.usage
        cache_read = getattr(usage, "cache_read_input_tokens", 0)
        return {
            "text": response.content[0].text,
            "cache_hit": cache_read > 0,
            "cache_read_tokens": cache_read,
            "input_tokens": usage.input_tokens,
        }


async def demo_warming():
    agent = CacheWarmingAgent(CacheWarmingConfig(warm_on_startup=True))
    await agent.start()

    questions = ["Explain async/await.", "What is a context manager?", "How does GIL work?"]
    for q in questions:
        result = await agent.chat(q)
        print(f"Cache hit: {result['cache_hit']} | {q}")

    await agent.stop()
    print(f"Total cache warm cycles: {agent._warm_count}")
```

## Solution 6: Cost Tracking with and without Caching

Measure actual cost savings from caching by comparing cache-hit vs. cache-miss token costs.

```python
import asyncio
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic

# Anthropic pricing (USD per 1M tokens) for claude-haiku-4-5-20251001
PRICING = {
    "input": 0.80,
    "output": 4.00,
    "cache_write": 1.00,   # Cache creation: 125% of input price
    "cache_read": 0.08,    # Cache read: 10% of input price
}


@dataclass
class CostTracker:
    baseline_cost: float = 0.0    # Cost without caching
    actual_cost: float = 0.0      # Cost with caching
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    request_count: int = 0

    def record(self, usage, output_tokens: int):
        self.request_count += 1
        inp = usage.input_tokens
        out = output_tokens
        create = getattr(usage, "cache_creation_input_tokens", 0)
        read = getattr(usage, "cache_read_input_tokens", 0)

        self.input_tokens += inp
        self.output_tokens += out
        self.cache_creation_tokens += create
        self.cache_read_tokens += read

        # What we would have paid without caching
        self.baseline_cost += (
            (inp + create + read) * PRICING["input"] / 1_000_000
            + out * PRICING["output"] / 1_000_000
        )
        # What we actually pay with caching
        self.actual_cost += (
            inp * PRICING["input"] / 1_000_000
            + create * PRICING["cache_write"] / 1_000_000
            + read * PRICING["cache_read"] / 1_000_000
            + out * PRICING["output"] / 1_000_000
        )

    @property
    def savings_usd(self) -> float:
        return max(0.0, self.baseline_cost - self.actual_cost)

    @property
    def savings_pct(self) -> float:
        if self.baseline_cost == 0:
            return 0.0
        return self.savings_usd / self.baseline_cost * 100

    def report(self) -> dict:
        return {
            "requests": self.request_count,
            "cache_creation_tokens": self.cache_creation_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "baseline_cost_usd": round(self.baseline_cost, 6),
            "actual_cost_usd": round(self.actual_cost, 6),
            "savings_usd": round(self.savings_usd, 6),
            "savings_pct": round(self.savings_pct, 1),
        }


LARGE_SYSTEM = (
    "You are a senior software architect. Provide detailed technical guidance. "
    "Consider security, performance, scalability, and maintainability in every answer. "
    "Include code examples, trade-off analyses, and migration paths when relevant. "
) * 40  # ~2000 tokens


class CostTrackingAgent:
    def __init__(self):
        self.client = AsyncAnthropic()
        self.tracker = CostTracker()

    async def chat(self, message: str) -> str:
        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": LARGE_SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": message}],
        )
        self.tracker.record(response.usage, response.usage.output_tokens)
        return response.content[0].text

    def cost_report(self) -> dict:
        return self.tracker.report()


async def demo_cost_tracking():
    agent = CostTrackingAgent()
    questions = [
        "How do I design a rate limiter?",
        "What's the best approach for database connection pooling?",
        "How should I handle distributed transactions?",
        "What are the patterns for event-driven architecture?",
        "How do I implement blue-green deployments?",
    ]
    for q in questions:
        await agent.chat(q)

    report = agent.cost_report()
    print(f"\n=== Cache Cost Report ===")
    print(f"Requests: {report['requests']}")
    print(f"Cache creation tokens: {report['cache_creation_tokens']}")
    print(f"Cache read tokens: {report['cache_read_tokens']}")
    print(f"Baseline cost (no cache): ${report['baseline_cost_usd']:.6f}")
    print(f"Actual cost (with cache): ${report['actual_cost_usd']:.6f}")
    print(f"Savings: ${report['savings_usd']:.6f} ({report['savings_pct']}%)")
```

## Comparison Table

| Solution | What Gets Cached | Cache Reuse Pattern | Savings Profile | Best For |
|---|---|---|---|---|
| Basic System Prompt | System prompt | All requests | High (per request) | Any agent with large system prompt |
| Multi-Block (Tools) | System + tool schemas | All requests | High (tools are large) | Tool-using agents |
| Conversation History | Growing history | Per-session follow-ups | Medium (grows with turns) | Multi-turn chatbots |
| RAG Context | Retrieved documents | Follow-up queries on same docs | Very high (docs are large) | RAG/document QA agents |
| Cache Warming | System prompt | Pre-warmed for all requests | Eliminates first-request miss | High-traffic, low-latency services |
| Cost Tracking | System prompt | All requests | Measurable savings reporting | Cost-controlled production agents |

**Key Facts about Anthropic Prompt Caching:**
- Minimum cacheable block: **1,024 tokens** (shorter blocks are not cached)
- Cache TTL: **5 minutes** (ephemeral); re-send within TTL to extend
- Cache write cost: **125%** of normal input token price (one-time)
- Cache read cost: **10%** of normal input token price (every hit)
- Break-even: Cache pays off after just **2 requests** with the same block

**Recommended**: Add `cache_control: {type: "ephemeral"}` to your system prompt first (Solution 1) — it's a one-line change that saves ~90% on system prompt tokens for every request after the first. Add conversation history caching (Solution 3) for multi-turn agents and RAG context caching (Solution 4) for document QA workflows.
