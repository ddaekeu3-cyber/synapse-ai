---
layout: solution
title: "Agent Doesn't Implement Hierarchical Cache with Prefix Sharing"
category: token-cost
description: "Structure prompts so that shared prefixes (system prompt, tools, static context) are cached at the API level, and layer additional context incrementally so each tier of cache is maximally reused."
tags: [token-cost, caching, prompt-caching, prefix, anthropic, efficiency]
---

Anthropic's prompt caching charges only a fraction of the normal input token price for cached content. But most agents don't structure their prompts to maximize cache hits. When the system prompt, tool definitions, and shared context are rebuilt on every call — or vary between calls — the cache is never warm. Hierarchical prefix sharing organizes prompts into stable layers that build on each other, maximizing cache utilization and cutting input token costs by 60-90%.

## Option 1: System Prompt Cache with cache_control

Add `cache_control: {type: "ephemeral"}` to the system prompt so it is cached after the first call. Subsequent calls with the same system prompt pay only the cache read price (~10% of normal). Requires the system prompt to be stable across calls.

```python
import anthropic
from dataclasses import dataclass

@dataclass
class CacheStats:
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def savings_pct(self) -> float:
        if self.cache_read_tokens == 0:
            return 0.0
        # Cache reads cost ~10% of normal input tokens
        normal_cost = (self.cache_creation_tokens + self.cache_read_tokens) * 1.0
        actual_cost = self.cache_creation_tokens * 1.0 + self.cache_read_tokens * 0.1
        return (1 - actual_cost / normal_cost) * 100

SYSTEM_PROMPT = """You are an expert Python software engineer with deep knowledge of:
- Async programming patterns (asyncio, aiohttp, fastapi)
- Data structures and algorithms
- System design and distributed systems
- Testing best practices (pytest, hypothesis)
- Performance optimization and profiling

When answering questions:
1. Provide concrete, runnable code examples
2. Explain the reasoning behind your approach
3. Mention relevant trade-offs and edge cases
4. Use Python 3.10+ features where appropriate

Always prioritize correctness, then readability, then performance."""

_stats = CacheStats()

def call_with_cached_system(user_message: str) -> str:
    client = anthropic.Anthropic()

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},  # Cache the system prompt
            }
        ],
        messages=[{"role": "user", "content": user_message}],
    )

    usage = response.usage
    _stats.cache_creation_tokens += getattr(usage, "cache_creation_input_tokens", 0)
    _stats.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0)
    _stats.input_tokens += usage.input_tokens
    _stats.output_tokens += usage.output_tokens

    cache_hit = getattr(usage, "cache_read_input_tokens", 0) > 0
    print(f"[Cache] {'HIT' if cache_hit else 'MISS'} | created={getattr(usage,'cache_creation_input_tokens',0)} read={getattr(usage,'cache_read_input_tokens',0)} | savings≈{_stats.savings_pct:.1f}%")

    return response.content[0].text

if __name__ == "__main__":
    questions = [
        "How do I implement a thread-safe singleton in Python?",
        "What's the difference between asyncio.gather and asyncio.wait?",
        "How do I profile Python code to find performance bottlenecks?",
        "Explain Python's descriptor protocol with examples",
        "How do I write property-based tests with hypothesis?",
    ]
    for q in questions:
        result = call_with_cached_system(q)
        print(f"Q: {q[:50]}\nA: {result[:100]}...\n")

    print(f"\nFinal: created={_stats.cache_creation_tokens} read={_stats.cache_read_tokens} savings≈{_stats.savings_pct:.1f}%")

# Expected Token Savings: 60-90% on input tokens for repeated system prompts after first call
# Environment: pip install anthropic
```

## Option 2: Two-Tier Cache — System + Static Context

Structure prompts as two cached layers: (1) system prompt at the top, (2) static knowledge base or document below it. Both are cached. The user query sits at the bottom uncached. Each API call gets two cache reads and pays full price only for the query.

```python
import anthropic

# Layer 1: System prompt (stable, cached)
SYSTEM_PROMPT = "You are an expert Python engineer. Answer concisely with runnable code."

# Layer 2: Static knowledge base (stable for session, cached)
PYTHON_REFERENCE = """
## Python Quick Reference

### Async Patterns
- Use `asyncio.create_task()` for fire-and-forget coroutines
- Use `async with asyncio.TaskGroup() as tg:` for structured concurrency (3.11+)
- Use `asyncio.Queue` for producer/consumer patterns
- `asyncio.gather(*coros, return_exceptions=True)` to run concurrently without cancelling on error

### Performance
- Use `__slots__` on data classes for memory efficiency
- `collections.deque` for O(1) append/pop from both ends
- `functools.lru_cache` for memoizing pure functions
- `itertools` for lazy iteration (avoids building full lists)

### Testing
- `pytest.mark.parametrize` for data-driven tests
- `pytest-asyncio` for async test functions
- `unittest.mock.AsyncMock` for mocking coroutines
- `hypothesis` for property-based testing

### Error Handling
- Use specific exception types, not bare `except:`
- Context managers for resource cleanup (`with`, `async with`)
- `contextlib.suppress` to swallow specific exceptions cleanly
"""

def call_two_tier_cached(user_question: str) -> str:
    client = anthropic.Anthropic()

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},  # Tier 1 cache
            }
        ],
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": PYTHON_REFERENCE,
                        "cache_control": {"type": "ephemeral"},  # Tier 2 cache
                    },
                    {
                        "type": "text",
                        "text": f"\nQuestion: {user_question}",  # Not cached — varies per call
                    },
                ],
            }
        ],
    )

    usage = response.usage
    created = getattr(usage, "cache_creation_input_tokens", 0)
    read = getattr(usage, "cache_read_input_tokens", 0)
    uncached = usage.input_tokens - created - read
    print(f"[2-Tier] created={created} read={read} uncached={uncached} out={usage.output_tokens}")
    return response.content[0].text

if __name__ == "__main__":
    questions = [
        "How do I use asyncio.TaskGroup?",
        "Show me an example of functools.lru_cache",
        "How do I mock an async function in pytest?",
        "What's the best way to handle multiple exceptions in Python?",
        "How do I use itertools.groupby?",
    ]
    for q in questions:
        ans = call_two_tier_cached(q)
        print(f"Q: {q}\nA: {ans[:120]}...\n")

# Expected Token Savings: 70-85% — both system prompt and knowledge base cached after first call
# Environment: pip install anthropic
```

## Option 3: Conversational Cache with History Anchoring

In multi-turn conversations, pin the earliest turns with `cache_control` so they are cached across calls. Only the most recent turns are sent uncached. The agent "anchors" the conversation history at regular checkpoints.

```python
import anthropic
from dataclasses import dataclass, field

@dataclass
class CachedConversation:
    system: str
    _turns: list[dict] = field(default_factory=list)
    _cache_anchor_at: int = 0      # index of last cached turn
    _anchor_every: int = 4         # re-anchor every N turns
    _client: anthropic.Anthropic = field(default_factory=anthropic.Anthropic)

    def _build_messages(self) -> list[dict]:
        """Build message list with cache_control on anchored turns."""
        messages = []
        for i, turn in enumerate(self._turns):
            msg = dict(turn)
            # Apply cache_control to turns at or before the anchor point
            if i == self._cache_anchor_at and isinstance(msg.get("content"), str):
                msg["content"] = [
                    {
                        "type": "text",
                        "text": msg["content"],
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            messages.append(msg)
        return messages

    def _maybe_update_anchor(self) -> None:
        """Move anchor forward every N turns to keep cache warm."""
        uncached_turns = len(self._turns) - self._cache_anchor_at
        if uncached_turns >= self._anchor_every:
            # New anchor = last complete user/assistant pair before current
            self._cache_anchor_at = max(0, len(self._turns) - 2)
            print(f"[CacheAnchor] Moved to turn {self._cache_anchor_at} ({len(self._turns)} total)")

    def chat(self, user_message: str) -> str:
        self._turns.append({"role": "user", "content": user_message})
        self._maybe_update_anchor()

        messages = self._build_messages()
        response = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
        )
        reply = response.content[0].text
        self._turns.append({"role": "assistant", "content": reply})

        usage = response.usage
        created = getattr(usage, "cache_creation_input_tokens", 0)
        read = getattr(usage, "cache_read_input_tokens", 0)
        print(f"[ConvCache] Turn {len(self._turns)//2}: created={created} read={read} uncached={usage.input_tokens}")
        return reply

if __name__ == "__main__":
    conv = CachedConversation(
        system="You are a helpful Python tutor. Give concise answers with code examples.",
        _anchor_every=4,
    )
    topics = [
        "What are Python generators?",
        "Show me a generator expression vs list comprehension",
        "What is yield from?",
        "How do I make a generator that never ends?",
        "What is send() on a generator?",
        "How do I chain generators together?",
        "What is itertools.islice useful for?",
        "How do I convert a generator to a list efficiently?",
    ]
    for topic in topics:
        reply = conv.chat(topic)
        print(f"Q: {topic}\nA: {reply[:80]}...\n")

# Expected Token Savings: 40-70% on long conversations by anchoring cached history
# Environment: pip install anthropic
```

## Option 4: Tool Definition Cache with Dynamic Tool Selection

Tool definitions consume hundreds of tokens on every call. Cache the full tool library once, then inject only the relevant subset per call. The cached tool library is reused across all calls; only the active subset costs full price.

```python
import anthropic
import json
from dataclasses import dataclass

# Full tool library — cached on first use
TOOL_LIBRARY_DOC = """
## Available Tools

### search_web(query: str, num_results: int = 5) -> list[dict]
Search the web and return a list of results with title, url, and snippet.

### search_database(table: str, filters: dict, limit: int = 10) -> list[dict]
Query the internal database. Tables: users, orders, products, logs.

### send_email(to: str, subject: str, body: str, cc: list[str] = []) -> bool
Send an email. Returns True on success.

### create_ticket(title: str, priority: str, description: str) -> str
Create a support ticket. Priority: low, medium, high, critical. Returns ticket ID.

### analyze_data(data: list, analysis_type: str) -> dict
Run statistical analysis. Types: summary, correlation, anomaly_detection, forecast.

### generate_report(data: dict, format: str = "markdown") -> str
Generate a formatted report. Formats: markdown, json, csv.

### call_api(endpoint: str, method: str, payload: dict = {}) -> dict
Make an authenticated API call to internal services.

### translate_text(text: str, target_language: str) -> str
Translate text to the target language using ISO 639-1 codes.
"""

@dataclass
class ToolRouter:
    client: anthropic.Anthropic = None

    def __post_init__(self):
        self.client = self.client or anthropic.Anthropic()

    def route_and_call(self, user_request: str, relevant_tools: list[str]) -> str:
        # Build actual tool definitions for only the relevant subset
        tool_subset_docs = "\n\n".join(
            section for section in TOOL_LIBRARY_DOC.split("###")
            if any(t in section for t in relevant_tools)
        )

        response = self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": TOOL_LIBRARY_DOC,
                            "cache_control": {"type": "ephemeral"},  # Cache full library
                        },
                        {
                            "type": "text",
                            "text": f"\nActive tools for this request: {', '.join(relevant_tools)}\n\nRequest: {user_request}",
                        },
                    ],
                }
            ],
        )
        usage = response.usage
        created = getattr(usage, "cache_creation_input_tokens", 0)
        read = getattr(usage, "cache_read_input_tokens", 0)
        print(f"[ToolCache] tools={relevant_tools} | created={created} read={read}")
        return response.content[0].text

if __name__ == "__main__":
    router = ToolRouter()
    requests = [
        ("Find the top 5 Python articles published this week", ["search_web"]),
        ("Get all premium users from the database", ["search_database"]),
        ("Send a summary report to the team", ["generate_report", "send_email"]),
        ("Create a critical ticket for the production outage", ["create_ticket"]),
        ("Search for asyncio documentation and translate to Spanish", ["search_web", "translate_text"]),
    ]
    for req, tools in requests:
        result = router.route_and_call(req, tools)
        print(f"Q: {req[:60]}\nA: {result[:80]}...\n")

# Expected Token Savings: 50-80% — tool library (~500 tokens) cached; only small subset sent per call
# Environment: pip install anthropic
```

## Option 5: Batch Processing with Shared Prefix Cache

For batch jobs that process many items with the same instructions, structure each item call to share the maximum prefix. Instructions, examples, and output schema are cached once; each item call pays only for the item content itself.

```python
import anthropic
import asyncio
from dataclasses import dataclass

BATCH_SYSTEM = "You are a data extraction specialist. Extract structured information exactly as specified."

EXTRACTION_INSTRUCTIONS = """
## Extraction Task

Extract the following fields from each product description:
- name: Product name (string)
- price_usd: Price in USD (float, null if not mentioned)
- category: One of: electronics, clothing, food, home, sports, other
- key_features: List of up to 3 main features (list of strings)
- sentiment: Customer sentiment: positive, neutral, negative

Output ONLY valid JSON matching this schema exactly. No explanation.

## Examples

Input: "Sony WH-1000XM5 wireless headphones - $349. Industry-leading noise cancellation, 30-hour battery, multipoint connection. Customers love the sound quality."
Output: {"name": "Sony WH-1000XM5", "price_usd": 349.0, "category": "electronics", "key_features": ["noise cancellation", "30-hour battery", "multipoint connection"], "sentiment": "positive"}

Input: "Generic cotton t-shirt. Available in S/M/L/XL. Some customers report sizing runs small."
Output: {"name": "Generic cotton t-shirt", "price_usd": null, "category": "clothing", "key_features": ["cotton material", "multiple sizes"], "sentiment": "neutral"}
"""

async def extract_one(
    client: anthropic.AsyncAnthropic,
    item: str,
    item_index: int,
) -> dict:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=[{"type": "text", "text": BATCH_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": EXTRACTION_INSTRUCTIONS,
                        "cache_control": {"type": "ephemeral"},  # Cache instructions
                    },
                    {
                        "type": "text",
                        "text": f"\nExtract from: {item}",  # Only item varies
                    },
                ],
            }
        ],
    )
    usage = response.usage
    created = getattr(usage, "cache_creation_input_tokens", 0)
    read = getattr(usage, "cache_read_input_tokens", 0)
    print(f"[Batch {item_index}] created={created} read={read} out={usage.output_tokens}")
    import json
    try:
        return json.loads(response.content[0].text)
    except Exception:
        return {"error": "parse failed", "raw": response.content[0].text[:100]}

async def process_batch(items: list[str], concurrency: int = 3) -> list[dict]:
    client = anthropic.AsyncAnthropic()
    semaphore = asyncio.Semaphore(concurrency)

    async def bounded_extract(item: str, idx: int) -> dict:
        async with semaphore:
            return await extract_one(client, item, idx)

    return await asyncio.gather(*[bounded_extract(item, i) for i, item in enumerate(items)])

if __name__ == "__main__":
    products = [
        "Apple AirPods Pro 2nd Gen - $249. Active noise cancellation, adaptive transparency, up to 30 hours battery with case.",
        "Levi's 501 Original Jeans - $69.50. Classic straight fit, 100% cotton denim, button fly. Customers say true to size.",
        "Instant Pot Duo 7-in-1 - $99. Pressure cooker, slow cooker, rice cooker, steamer, sauté, yogurt maker, warmer. Highly rated.",
        "Nike Air Max 270 - $150. Max Air cushioning, mesh upper, rubber outsole. Available in 12 colorways.",
        "Organic Matcha Green Tea Powder - $24.99. 100% pure ceremonial grade, USDA certified organic. Some find taste bitter.",
    ]
    import asyncio
    results = asyncio.run(process_batch(products))
    import json
    for item, result in zip(products, results):
        print(f"Item: {item[:50]}...")
        print(f"Extracted: {json.dumps(result, indent=2)[:150]}\n")

# Expected Token Savings: 70-90% on batch jobs — instructions cached after first item
# Environment: pip install anthropic
```

## Option 6: Cache-Aware Conversation Router

For agents that serve multiple conversation types (support, coding, analysis), route each conversation to a system-prompt variant optimized for that type. All variants are cached independently. The router picks the right cached prefix so no variant needs cold-start after the first call of its type.

```python
import anthropic
from dataclasses import dataclass

@dataclass
class ConversationType:
    name: str
    system_prompt: str
    model: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 512

CONVERSATION_TYPES = {
    "coding": ConversationType(
        name="coding",
        system_prompt="""You are an expert software engineer. Provide precise, runnable code.
Always include: imports, type hints, error handling, and a brief explanation.
Prefer Python 3.10+ idioms. Use async/await for I/O-bound operations.""",
    ),
    "support": ConversationType(
        name="support",
        system_prompt="""You are a friendly customer support agent. Be empathetic and solution-focused.
Always: acknowledge the issue, provide a clear solution, offer next steps.
Escalate to human if: billing disputes, account termination, legal issues.""",
        max_tokens=256,
    ),
    "analysis": ConversationType(
        name="analysis",
        system_prompt="""You are a senior data analyst. Provide structured, evidence-based analysis.
Format: Executive Summary → Key Findings → Data Observations → Recommendations.
Quantify insights where possible. Acknowledge uncertainty explicitly.""",
        model="claude-sonnet-4-6",
    ),
    "writing": ConversationType(
        name="writing",
        system_prompt="""You are a professional technical writer. Produce clear, well-structured content.
Adapt tone and format to the target audience. Use active voice.
Structure: clear hierarchy, concrete examples, no jargon without explanation.""",
    ),
}

_cache_usage: dict[str, dict] = {}

def detect_type(message: str) -> str:
    msg_lower = message.lower()
    if any(w in msg_lower for w in ["code", "function", "bug", "error", "implement", "python", "api"]):
        return "coding"
    if any(w in msg_lower for w in ["help", "issue", "problem", "refund", "cancel", "account"]):
        return "support"
    if any(w in msg_lower for w in ["analyze", "data", "trend", "metric", "report", "compare"]):
        return "analysis"
    return "writing"

def route_and_respond(user_message: str, conv_type: str = None) -> tuple[str, str]:
    conv_type = conv_type or detect_type(user_message)
    config = CONVERSATION_TYPES.get(conv_type, CONVERSATION_TYPES["writing"])
    client = anthropic.Anthropic()

    response = client.messages.create(
        model=config.model,
        max_tokens=config.max_tokens,
        system=[
            {
                "type": "text",
                "text": config.system_prompt,
                "cache_control": {"type": "ephemeral"},  # Each type cached independently
            }
        ],
        messages=[{"role": "user", "content": user_message}],
    )

    usage = response.usage
    created = getattr(usage, "cache_creation_input_tokens", 0)
    read = getattr(usage, "cache_read_input_tokens", 0)
    _cache_usage.setdefault(conv_type, {"created": 0, "read": 0})
    _cache_usage[conv_type]["created"] += created
    _cache_usage[conv_type]["read"] += read
    print(f"[Router] type={conv_type} created={created} read={read}")

    return response.content[0].text, conv_type

if __name__ == "__main__":
    requests = [
        "How do I implement rate limiting in Python?",
        "My account was charged twice last month, I need a refund",
        "Analyze our Q1 vs Q2 conversion rate: Q1=3.2%, Q2=2.8%",
        "Write a README for a Python CLI tool",
        "Fix this bug: TypeError: unsupported operand type(s) for +: int and str",
        "I can't log into my account, says invalid password",
    ]
    for req in requests:
        result, t = route_and_respond(req)
        print(f"[{t}] {req[:50]}\n→ {result[:80]}...\n")

    print("\n=== Cache Usage by Type ===")
    for t, stats in _cache_usage.items():
        total = stats["created"] + stats["read"]
        hit_rate = stats["read"] / total * 100 if total else 0
        print(f"  {t}: created={stats['created']} read={stats['read']} hit_rate={hit_rate:.1f}%")

# Expected Token Savings: 60-80% per conversation type after first call of each type
# Environment: pip install anthropic
```

## Comparison

| Option | Cache Layers | Use Case | Cache Hit Rate | Best For |
|--------|-------------|----------|---------------|----------|
| 1. System Prompt | 1 | Single system prompt | High after 1st call | Simple agents |
| 2. Two-Tier | 2 | System + knowledge base | Very high after 1st | RAG, doc Q&A |
| 3. Conversation Anchor | 1-2 | Multi-turn chat | Medium (shifts with turns) | Long conversations |
| 4. Tool Library | 1 | Many tools, varying subsets | High after 1st | Tool-heavy agents |
| 5. Batch Instructions | 2 | Batch extraction/classification | Very high from item 2 | Bulk processing |
| 6. Type-Based Router | 1 per type | Multi-purpose agents | High per type | Multi-domain agents |
