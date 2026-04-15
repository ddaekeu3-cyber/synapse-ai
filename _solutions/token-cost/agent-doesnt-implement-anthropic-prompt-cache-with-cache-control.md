---
layout: solution
title: "Agent Doesn't Implement Anthropic Prompt Cache with cache_control"
category: token-cost
description: "Agents that repeatedly inject large documents, tool definitions, or reference materials as messages pay full input token costs on every call. Anthropic's cache_control API reduces repeated-context costs by 90% by marking stable blocks with type:ephemeral, caching them server-side for up to 5 minutes."
tags: [token-cost, prompt-caching, cache-control, anthropic-api, documents, tool-definitions, cost-optimization]
---

## Problem

When an agent loads a large code file, legal document, or reference manual into every API call, it pays 100% input token cost each time. Anthropic's prompt caching feature lets you mark stable content blocks with `"cache_control": {"type": "ephemeral"}` to cache them server-side. Cached tokens cost 10% of normal input price on cache hits (within a 5-minute TTL), making repeated document-heavy calls 50-90% cheaper. Agents that don't use this feature are leaving significant savings on the table.

## Solutions

### Option 1: Cache a Large Document in the User Message

```python
import anthropic

client = anthropic.Anthropic()

# Simulate a large reference document (in production, load from file)
LARGE_DOCUMENT = """
# Python Style Guide

## Naming Conventions
- Variables and functions: snake_case
- Classes: PascalCase
- Constants: SCREAMING_SNAKE_CASE
- Private attributes: _leading_underscore

## Code Structure
- Maximum line length: 88 characters (Black formatter default)
- Use type hints for all public functions
- Docstrings required for public modules, classes, and functions

## Error Handling
- Never use bare except: clauses
- Always catch specific exceptions
- Use context managers for resource management

## Testing
- Test files: test_<module_name>.py
- Test classes: Test<ClassName>
- Test functions: test_<behavior>_<condition>
- Use pytest fixtures for setup/teardown

""" * 20  # Repeat to simulate a large document

def ask_about_document(question: str, use_cache: bool = True) -> tuple[str, object]:
    """
    Ask a question about the document.
    With cache_control, the document is cached server-side after the first call.
    """
    doc_block = {
        "type": "text",
        "text": f"Reference document:\n{LARGE_DOCUMENT}",
    }
    if use_cache:
        doc_block["cache_control"] = {"type": "ephemeral"}

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[
            {
                "role": "user",
                "content": [
                    doc_block,
                    {"type": "text", "text": question},
                ],
            }
        ],
    )
    return resp.content[0].text, resp.usage

if __name__ == "__main__":
    questions = [
        "What naming convention should I use for constants?",
        "How should I name test functions?",
        "What is the maximum line length?",
    ]

    print("=== Without cache_control ===")
    for q in questions[:2]:
        text, usage = ask_about_document(q, use_cache=False)
        print(f"  Q: {q[:40]}")
        print(f"  A: {text.strip()[:60]}")
        print(f"  Tokens: in={usage.input_tokens} out={usage.output_tokens}")
        print()

    print("=== With cache_control ===")
    for q in questions:
        text, usage = ask_about_document(q, use_cache=True)
        cached = getattr(usage, "cache_read_input_tokens", 0)
        created = getattr(usage, "cache_creation_input_tokens", 0)
        print(f"  Q: {q[:40]}")
        print(f"  A: {text.strip()[:60]}")
        print(f"  Tokens: in={usage.input_tokens} cache_hit={cached} cache_create={created}")
        print()

# Expected Token Savings: 90% reduction on cache hits; document cached for 5 minutes
# Environment: document Q&A agents; any agent re-injecting same large context on every call
```

### Option 2: Cache Tool Definitions for Tool-Heavy Agents

```python
import anthropic

client = anthropic.Anthropic()

# Large tool suite — cached once, reused across many calls
CACHED_TOOLS = [
    {
        "name": f"tool_{i}",
        "description": f"Tool {i}: performs operation {i} on the given input data with full validation and error handling",
        "input_schema": {
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": f"Input for tool {i}"},
                "options": {"type": "object", "description": "Optional configuration"},
            },
            "required": ["input"],
        },
    }
    for i in range(15)  # 15 tools with verbose descriptions
]

def call_with_cached_tools(task: str) -> tuple[str, object]:
    """
    Mark the system prompt (which includes tool context) with cache_control.
    Tool definitions themselves are cached via the system prompt approach.
    """
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=[
            {
                "type": "text",
                "text": (
                    "You are a tool orchestration agent. "
                    "You have access to a comprehensive tool suite. "
                    "Select the most appropriate tool for each task."
                ),
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tools=CACHED_TOOLS,
        messages=[{"role": "user", "content": task}],
    )
    return (
        next((b.text for b in resp.content if hasattr(b, "text")), "tool call made"),
        resp.usage,
    )

def cost_comparison():
    """Compare token usage across repeated calls."""
    tasks = [
        "What would tool_3 do?",
        "Describe tool_7's purpose.",
        "When should I use tool_12?",
    ]
    results = []
    for task in tasks:
        _, usage = call_with_cached_tools(task)
        cached = getattr(usage, "cache_read_input_tokens", 0)
        created = getattr(usage, "cache_creation_input_tokens", 0)
        results.append({
            "task": task[:30],
            "input": usage.input_tokens,
            "cache_hit": cached,
            "cache_create": created,
        })
        print(f"  [{task[:30]}] in={usage.input_tokens} hit={cached} create={created}")

    total_input = sum(r["input"] for r in results)
    total_hit = sum(r["cache_hit"] for r in results)
    print(f"\nTotal input: {total_input} | Total cache hits: {total_hit}")
    if total_input > 0:
        savings_pct = total_hit / total_input * 90  # 90% discount on hits
        print(f"Estimated savings: ~{savings_pct:.0f}% of token cost")

if __name__ == "__main__":
    cost_comparison()

# Expected Token Savings: tool definitions are often 30-60% of input tokens; caching eliminates that cost on hits
# Environment: agents with 5+ tools called repeatedly; especially effective in multi-turn conversations
```

### Option 3: Multi-Block Caching — System + Document + Examples

```python
import anthropic

client = anthropic.Anthropic()

SYSTEM_INSTRUCTIONS = "You are a legal document analyzer. Extract key clauses and obligations."

LEGAL_REFERENCE = """
CONTRACT ANALYSIS GUIDELINES

Section 1: Liability Clauses
- Look for indemnification provisions
- Identify limitation of liability caps
- Note consequential damages waivers

Section 2: Termination
- Identify notice periods (standard: 30-90 days)
- Look for for-cause vs for-convenience termination
- Note survival clauses

Section 3: Payment Terms
- Net 30, Net 60, or Net 90 payment windows
- Late payment penalties and interest rates
- Dispute resolution for invoices

""" * 15  # Large reference document

FEW_SHOT_EXAMPLES = """
Example analysis 1:
Contract: "Either party may terminate with 30 days written notice."
Analysis: Standard for-convenience termination clause. Notice period: 30 days.

Example analysis 2:
Contract: "Liability limited to fees paid in prior 12 months."
Analysis: Standard limitation of liability. Cap: 12-month fee value.

Example analysis 3:
Contract: "Payment due Net 45 from invoice date."
Analysis: Payment terms: Net 45. No late fee specified.
"""

def analyze_contract(contract_text: str) -> tuple[str, object]:
    """Analyze a contract clause using cached reference materials."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=[
            # Block 1: Instructions (cached — most stable)
            {
                "type": "text",
                "text": SYSTEM_INSTRUCTIONS,
                "cache_control": {"type": "ephemeral"},
            },
            # Block 2: Reference manual (cached — stable)
            {
                "type": "text",
                "text": f"Reference guidelines:\n{LEGAL_REFERENCE}",
                "cache_control": {"type": "ephemeral"},
            },
            # Block 3: Examples (cached — stable)
            {
                "type": "text",
                "text": f"Analysis examples:\n{FEW_SHOT_EXAMPLES}",
                "cache_control": {"type": "ephemeral"},
            },
        ],
        messages=[
            {
                "role": "user",
                "content": f"Analyze this contract clause:\n\n{contract_text}",
            }
        ],
    )
    return resp.content[0].text, resp.usage

def format_usage(usage) -> str:
    hit = getattr(usage, "cache_read_input_tokens", 0)
    create = getattr(usage, "cache_creation_input_tokens", 0)
    return f"in={usage.input_tokens} hit={hit} create={create} out={usage.output_tokens}"

if __name__ == "__main__":
    clauses = [
        "The Company may terminate this Agreement immediately upon written notice if the other party materially breaches.",
        "Liability of either party shall not exceed the total fees paid in the preceding six months.",
        "Payment is due within 60 days of receipt of a valid invoice.",
    ]

    print("Analyzing contracts with multi-block caching:\n")
    for clause in clauses:
        result, usage = analyze_contract(clause)
        print(f"Clause: {clause[:60]}...")
        print(f"  Analysis: {result.strip()[:80]}")
        print(f"  Usage: {format_usage(usage)}")
        print()

# Expected Token Savings: 3 cached blocks; each re-call pays only for the unique contract text
# Environment: legal, compliance, or document review agents processing many similar documents
```

### Option 4: Conversational Cache — Reuse Context Across Turns

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class CachedConversation:
    """
    Maintains a conversation where the base context is cached.
    Only new turns are sent as uncached content.
    """
    base_context: str
    history: list[dict] = field(default_factory=list)
    total_cache_hits: int = 0
    total_cache_creates: int = 0
    total_input_tokens: int = 0

    def send(self, user_message: str) -> str:
        # Build messages with cached base context on the first user message
        if not self.history:
            first_content = [
                {
                    "type": "text",
                    "text": self.base_context,
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": user_message},
            ]
        else:
            first_content = user_message

        messages = []
        if not self.history:
            messages.append({"role": "user", "content": first_content})
        else:
            # Replay history (first message still uses cached context)
            messages.append({"role": "user", "content": [
                {
                    "type": "text",
                    "text": self.base_context,
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": self.history[0]["user"]},
            ]})
            for turn in self.history:
                messages.append({"role": "assistant", "content": turn["assistant"]})
                messages.append({"role": "user", "content": turn["user"] if "user" in turn else ""})

            # Last message is the new one
            if len(self.history) > 0:
                messages[-1] = {"role": "user", "content": user_message}

        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=messages if not self.history else [
                {"role": "user", "content": [
                    {"type": "text", "text": self.base_context, "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": self.history[0]["user"] if self.history else user_message},
                ]},
                *[msg for turn in self.history[1:]
                  for msg in [
                      {"role": "assistant", "content": turn.get("assistant", "")},
                      {"role": "user", "content": turn.get("user", "")},
                  ]],
                *([] if not self.history else [{"role": "assistant", "content": self.history[-1]["assistant"]},
                                               {"role": "user", "content": user_message}]),
            ] if self.history else [{"role": "user", "content": [
                {"type": "text", "text": self.base_context, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": user_message},
            ]}],
        )

        reply = resp.content[0].text
        hit = getattr(resp.usage, "cache_read_input_tokens", 0)
        create = getattr(resp.usage, "cache_creation_input_tokens", 0)
        self.total_cache_hits += hit
        self.total_cache_creates += create
        self.total_input_tokens += resp.usage.input_tokens
        self.history.append({"user": user_message, "assistant": reply})
        print(f"  [turn {len(self.history)}] hit={hit} create={create} in={resp.usage.input_tokens}")
        return reply

    def stats(self) -> str:
        return (
            f"Total input: {self.total_input_tokens} | "
            f"Cache hits: {self.total_cache_hits} | "
            f"Cache creates: {self.total_cache_creates}"
        )

if __name__ == "__main__":
    BASE_CONTEXT = (
        "You are analyzing a Python codebase. "
        + "The codebase has 15,000 lines across 120 files. "
        + "Key modules: auth/, api/, models/, utils/. "
        + "Tech stack: FastAPI, SQLAlchemy, Redis, Celery. " * 50  # Large context
    )

    conv = CachedConversation(base_context=BASE_CONTEXT)

    questions = [
        "What modules does this codebase have?",
        "What is the tech stack?",
        "How large is the codebase?",
    ]

    for q in questions:
        answer = conv.send(q)
        print(f"  Q: {q}")
        print(f"  A: {answer.strip()[:60]}")
        print()

    print("Session stats:", conv.stats())

# Expected Token Savings: base context (large) cached; each turn only pays for new user message tokens
# Environment: multi-turn agents with stable context; code review, document analysis, RAG conversations
```

### Option 5: Batch Processing with Shared Cached Preamble

```python
import anthropic
import asyncio

client = anthropic.AsyncAnthropic()

SHARED_PREAMBLE = """
You are a product categorization expert for an e-commerce platform.

Category taxonomy:
- Electronics > Computers > Laptops
- Electronics > Computers > Desktops
- Electronics > Audio > Headphones
- Electronics > Audio > Speakers
- Home & Garden > Furniture > Chairs
- Home & Garden > Furniture > Tables
- Clothing > Men > Shirts
- Clothing > Women > Dresses
- Sports > Fitness > Weights
- Sports > Outdoor > Camping

Categorization rules:
1. Always use the most specific applicable category
2. If ambiguous, pick the primary use case
3. Return format: {"category": "path", "confidence": 0.0-1.0}
""" * 8

async def categorize_product(sem: asyncio.Semaphore, product: str) -> dict:
    """Categorize a product using cached shared preamble."""
    async with sem:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": SHARED_PREAMBLE,
                            "cache_control": {"type": "ephemeral"},
                        },
                        {
                            "type": "text",
                            "text": f"Categorize: {product}",
                        },
                    ],
                }
            ],
        )
        hit = getattr(resp.usage, "cache_read_input_tokens", 0)
        return {
            "product": product,
            "result": resp.content[0].text.strip(),
            "cache_hit": hit > 0,
            "total_tokens": resp.usage.input_tokens,
        }

async def batch_categorize(products: list[str]) -> list[dict]:
    sem = asyncio.Semaphore(5)
    results = await asyncio.gather(*[categorize_product(sem, p) for p in products])
    cache_hits = sum(1 for r in results if r["cache_hit"])
    total_tokens = sum(r["total_tokens"] for r in results)
    print(f"\nBatch complete: {cache_hits}/{len(results)} cache hits | {total_tokens} total input tokens")
    return results

if __name__ == "__main__":
    products = [
        "Sony WH-1000XM5 Noise Cancelling Headphones",
        "IKEA POÄNG Armchair",
        "MacBook Pro 14-inch M3",
        "Nike Running Shoes",
        "Weber Charcoal Grill",
        "Levi's 501 Jeans",
        "Dumbbells Set 5-50lb",
        "Samsung 65-inch QLED TV",
    ]

    results = asyncio.run(batch_categorize(products))
    for r in results:
        cache_label = "HIT" if r["cache_hit"] else "MISS"
        print(f"  [{cache_label}] {r['product'][:35]:35s} → {r['result'][:40]}")

# Expected Token Savings: preamble cached after first call; N products pay 1 preamble + N product descriptions
# Environment: bulk classification, extraction, or transformation pipelines
```

### Option 6: Cache Freshness Monitoring and Invalidation Strategy

```python
import anthropic
import hashlib
import sqlite3
import time
from pathlib import Path

DB = Path("/tmp/cache_monitor.db")
client = anthropic.Anthropic()

def init_db():
    con = sqlite3.connect(DB)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS cache_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            context_hash TEXT NOT NULL,
            call_at REAL NOT NULL,
            input_tokens INTEGER,
            cache_hit_tokens INTEGER,
            cache_create_tokens INTEGER,
            output_tokens INTEGER
        );
    """)
    con.commit()
    con.close()

def context_hash(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()[:12]

def record_call(ctx_hash: str, usage):
    hit = getattr(usage, "cache_read_input_tokens", 0)
    create = getattr(usage, "cache_creation_input_tokens", 0)
    con = sqlite3.connect(DB)
    con.execute("""
        INSERT INTO cache_calls (context_hash, call_at, input_tokens, cache_hit_tokens, cache_create_tokens, output_tokens)
        VALUES (?,?,?,?,?,?)
    """, (ctx_hash, time.time(), usage.input_tokens, hit, create, usage.output_tokens))
    con.commit()
    con.close()

def cache_efficiency_report() -> dict:
    con = sqlite3.connect(DB)
    rows = con.execute("""
        SELECT
            SUM(input_tokens) total_input,
            SUM(cache_hit_tokens) total_hits,
            SUM(cache_create_tokens) total_creates,
            COUNT(*) total_calls
        FROM cache_calls
    """).fetchone()
    con.close()
    total_input, total_hits, total_creates, calls = rows
    if not total_input:
        return {}
    hit_rate = (total_hits or 0) / total_input * 100
    # Cache hits cost 10% of normal; estimate savings
    normal_cost = total_input * 0.80 / 1_000_000  # haiku input price
    actual_cost = (
        ((total_input - (total_hits or 0) - (total_creates or 0)) * 0.80
         + (total_creates or 0) * 1.25   # cache write is slightly more expensive
         + (total_hits or 0) * 0.08)     # cache read is 10% of normal
    ) / 1_000_000
    return {
        "calls": calls,
        "total_input_tokens": total_input,
        "cache_hit_tokens": total_hits or 0,
        "hit_rate_pct": round(hit_rate, 1),
        "estimated_normal_cost_usd": round(normal_cost, 5),
        "estimated_actual_cost_usd": round(actual_cost, 5),
        "estimated_savings_usd": round(normal_cost - actual_cost, 5),
    }

SHARED_CONTEXT = "You are a Q&A assistant. " + "Answer factual questions concisely. " * 100

def ask(question: str) -> str:
    ctx_hash = context_hash(SHARED_CONTEXT)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": SHARED_CONTEXT, "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": question},
                ],
            }
        ],
    )
    record_call(ctx_hash, resp.usage)
    return resp.content[0].text.strip()

if __name__ == "__main__":
    init_db()
    questions = [
        "What is the capital of Japan?",
        "How many planets are in the solar system?",
        "What year did World War II end?",
        "What is the speed of light?",
        "Who wrote Romeo and Juliet?",
    ]
    for q in questions:
        answer = ask(q)
        print(f"  Q: {q[:40]:40s} A: {answer[:40]}")

    report = cache_efficiency_report()
    if report:
        print(f"\n--- Cache Efficiency Report ---")
        for k, v in report.items():
            print(f"  {k}: {v}")

# Expected Token Savings: hit_rate and savings_usd show real-time ROI of cache_control usage
# Environment: production agents; monitor report reveals if context changes too often (low hit rate = wasteful)
```

## Comparison

| Option | What's Cached | Cache Location | TTL | Best For |
|--------|--------------|---------------|-----|---------|
| 1 — Large document in message | Document block in user message | Server-side | 5 min | Document Q&A agents |
| 2 — Tool definitions | System prompt with tool context | Server-side | 5 min | Tool-heavy agents (5+ tools) |
| 3 — Multi-block system | System instructions + reference + examples | Server-side | 5 min | Legal, compliance, review agents |
| 4 — Conversational base context | Base context in first message | Server-side | 5 min | Multi-turn agents with stable context |
| 5 — Batch shared preamble | Preamble in each parallel call | Server-side | 5 min | Bulk classification/extraction |
| 6 — Cache monitoring | System prompt | Server-side | 5 min | Production; measures actual savings |
