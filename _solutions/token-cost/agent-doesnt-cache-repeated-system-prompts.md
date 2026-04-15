---
layout: solution
title: "Agent Doesn't Cache Repeated System Prompts"
category: token-cost
description: "Every request re-sends the full system prompt and knowledge base, multiplying token costs on each call."
tags: [token-cost, prompt-caching, performance, anthropic-sdk, cost-optimization]
---

## Symptom

Your agent's system prompt contains a large knowledge base, role description, or static context. Every API call sends this entire block, and your token bill reflects it — thousands of input tokens charged on every request even though nothing changed.

## Root Cause

Anthropic's prompt caching feature can store up to 4 cached prefixes per request. When identical content appears at the same position with `cache_control: {"type": "ephemeral"}`, the API returns a cache hit and charges only 10% of the normal input token cost. Without this annotation, every call is treated as fresh and billed at full price.

## Fix

### Option 1 — Annotate the system prompt directly

```python
import anthropic

client = anthropic.Anthropic()

SYSTEM_PROMPT = """You are a senior financial analyst specialising in SEC filings.
You have deep knowledge of GAAP accounting, revenue recognition (ASC 606),
and segment reporting standards.

[... large knowledge base, 2 000+ tokens ...]
"""

def ask(user_message: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_message}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    usage = response.usage
    print(
        f"cache_read={usage.cache_read_input_tokens} "
        f"cache_write={usage.cache_creation_input_tokens} "
        f"uncached={usage.input_tokens}"
    )
    return response.content[0].text

# First call: cache_write charged at 1.25x; subsequent calls: cache_read at 0.1x
print(ask("What is revenue recognition under ASC 606?"))
print(ask("Explain deferred revenue treatment."))
```

**Expected Token Savings:** 85–90% reduction on input tokens for the system prompt portion after the first call.
**Environment:** Any stateless or high-frequency agent with a fixed system prompt > 1 024 tokens.

---

### Option 2 — Multi-block caching: static context + dynamic prefix

```python
import anthropic

client = anthropic.Anthropic()

STATIC_KNOWLEDGE = "..." * 500   # 2 500 tokens of reference material
COMPANY_PROFILE  = "..." * 200   # 1 000 tokens of per-company context

def analyze(user_question: str, company_id: str) -> str:
    """Cache two prefixes: global knowledge base and per-company profile."""
    company_profile = load_company_profile(company_id)   # fetch from DB once

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=[
            # Block 1: large static knowledge base — shared across all companies
            {
                "type": "text",
                "text": STATIC_KNOWLEDGE,
                "cache_control": {"type": "ephemeral"},
            },
            # Block 2: company-specific context — cached per company_id
            {
                "type": "text",
                "text": company_profile,
                "cache_control": {"type": "ephemeral"},
            },
        ],
        messages=[{"role": "user", "content": user_question}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    return response.content[0].text


def load_company_profile(company_id: str) -> str:
    # In practice: hit a cache layer or DB; string content is stable per session
    return f"Company {company_id}: sector=tech, employees=12000, revenue=$4.2B ..."
```

**Expected Token Savings:** Up to 90% on static knowledge block; up to 85% on per-company profile after first request for that company.
**Environment:** Multi-tenant agents where each tenant has a distinct but stable context.

---

### Option 3 — Cache-aware conversation manager

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

SYSTEM_PROMPT = "You are a coding assistant with knowledge of Python, Rust, and Go.\n" + "x" * 3000


@dataclass
class CachedSession:
    history: list = field(default_factory=list)
    total_cache_savings: int = 0

    def chat(self, user_message: str) -> str:
        self.history.append({"role": "user", "content": user_message})

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=self.history,
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )

        usage = response.usage
        # cache_read_input_tokens * 0.9 = tokens saved vs full price
        saved = int(usage.cache_read_input_tokens * 0.9)
        self.total_cache_savings += saved

        reply = response.content[0].text
        self.history.append({"role": "assistant", "content": reply})
        print(f"[cache] saved ~{saved} tokens this turn | total saved: {self.total_cache_savings}")
        return reply


session = CachedSession()
session.chat("How do I write a generic stack in Rust?")
session.chat("Convert that to Python.")
session.chat("And Go?")
```

**Expected Token Savings:** Accumulates per multi-turn session; typically 80–90% on system prompt across turns 2+.
**Environment:** Interactive chatbots or long multi-turn agent sessions.

---

### Option 4 — Lazy cache warmer with TTL tracking

```python
import time
import anthropic

client = anthropic.Anthropic()

CACHE_TTL_SECONDS = 300  # Anthropic caches for 5 minutes; refresh before expiry

class PromptCacheManager:
    def __init__(self, system_prompt: str, model: str = "claude-haiku-4-5-20251001"):
        self.system_prompt = system_prompt
        self.model = model
        self._last_write: float = 0.0

    def _needs_warmup(self) -> bool:
        return (time.time() - self._last_write) > (CACHE_TTL_SECONDS - 30)

    def warmup(self) -> None:
        """Send a minimal warmup request to prime the cache before heavy traffic."""
        client.messages.create(
            model=self.model,
            max_tokens=1,
            system=[{"type": "text", "text": self.system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": "ping"}],
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
        self._last_write = time.time()
        print("[cache] warmed up")

    def create(self, messages: list, max_tokens: int = 1024) -> anthropic.types.Message:
        if self._needs_warmup():
            self.warmup()

        return client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": self.system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )


LARGE_SYSTEM = "Domain knowledge: " + "rule " * 600
cache_mgr = PromptCacheManager(LARGE_SYSTEM)

# Warm before serving traffic
cache_mgr.warmup()

for question in ["Explain rule 42.", "What about rule 100?", "Summarise rule 7."]:
    r = cache_mgr.create([{"role": "user", "content": question}])
    print(r.content[0].text)
```

**Expected Token Savings:** Near 90% on system prompt for all requests within 5-minute cache window.
**Environment:** Batch processors or scheduled jobs that send bursts of requests.

---

### Option 5 — Tool-call pipeline with cached knowledge

```python
import json
import anthropic

client = anthropic.Anthropic()

REFERENCE_DOCS = """
[Full API reference, 3 000 tokens of stable documentation]
""" + "doc_entry " * 400

TOOLS = [
    {
        "name": "lookup_endpoint",
        "description": "Look up API endpoint details from the reference docs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "endpoint": {"type": "string", "description": "Endpoint path, e.g. /v1/users"}
            },
            "required": ["endpoint"],
        },
    }
]

def run_agent(user_query: str) -> str:
    messages = [{"role": "user", "content": user_query}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=[
                {
                    "type": "text",
                    "text": REFERENCE_DOCS,
                    "cache_control": {"type": "ephemeral"},  # cache the entire reference
                }
            ],
            tools=TOOLS,
            messages=messages,
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for tu in tool_uses:
            result = {"endpoint": tu.input["endpoint"], "method": "GET", "auth": "Bearer"}
            tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": json.dumps(result)})

        messages.append({"role": "user", "content": tool_results})


print(run_agent("How do I authenticate to the /v1/users endpoint?"))
```

**Expected Token Savings:** 85–90% on the reference docs block on every agentic loop iteration.
**Environment:** Tool-using agents with large, stable reference material in the system prompt.

---

### Option 6 — Cost dashboard comparing cached vs uncached spend

```python
import anthropic
from decimal import Decimal

client = anthropic.Anthropic()

# Haiku pricing per million tokens (as of 2025)
PRICE = {
    "input":         Decimal("0.80"),   # $ / 1M tokens
    "cache_write":   Decimal("1.00"),   # $ / 1M tokens (1.25x input)
    "cache_read":    Decimal("0.08"),   # $ / 1M tokens (0.1x input)
    "output":        Decimal("4.00"),   # $ / 1M tokens
}

SYSTEM_PROMPT = "Extensive knowledge base: " + "knowledge " * 500  # ~1 500 tokens

def compute_cost(usage: anthropic.types.Usage) -> Decimal:
    m = Decimal("1_000_000")
    return (
        Decimal(usage.input_tokens)              / m * PRICE["input"]
        + Decimal(usage.cache_creation_input_tokens) / m * PRICE["cache_write"]
        + Decimal(usage.cache_read_input_tokens)     / m * PRICE["cache_read"]
        + Decimal(usage.output_tokens)               / m * PRICE["output"]
    )

def simulate_cost_without_cache(usage: anthropic.types.Usage) -> Decimal:
    m = Decimal("1_000_000")
    total_input = (
        usage.input_tokens
        + usage.cache_creation_input_tokens
        + usage.cache_read_input_tokens
    )
    return Decimal(total_input) / m * PRICE["input"] + Decimal(usage.output_tokens) / m * PRICE["output"]

total_with    = Decimal("0")
total_without = Decimal("0")

questions = ["What is X?", "Explain Y.", "Compare X and Y.", "Summarise Z.", "How does W relate?"]

for q in questions:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": q}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    with_cache    = compute_cost(r.usage)
    without_cache = simulate_cost_without_cache(r.usage)
    total_with    += with_cache
    total_without += without_cache
    print(f"Q: {q!r:40s} | cached=${with_cache:.6f}  uncached=${without_cache:.6f}")

savings_pct = (total_without - total_with) / total_without * 100
print(f"\nTotal with cache:    ${total_with:.4f}")
print(f"Total without cache: ${total_without:.4f}")
print(f"Savings:             {savings_pct:.1f}%")
```

**Expected Token Savings:** Demonstrates 70–90% cost reduction across a batch of requests sharing the same system prompt.
**Environment:** Any production agent — use this script to justify enabling prompt caching to stakeholders.

---

## Comparison

| Option | Complexity | Best For | Cache Blocks | Savings |
|---|---|---|---|---|
| 1. Direct annotation | Minimal | Simple stateless agents | 1 | ~88% on system |
| 2. Multi-block | Low | Multi-tenant with per-tenant context | 2 | ~88% + ~85% |
| 3. Session manager | Low | Multi-turn chat sessions | 1 | ~88% per turn 2+ |
| 4. TTL warmer | Medium | Burst traffic, batch jobs | 1 | ~88% within window |
| 5. Tool pipeline | Medium | Agentic loops with reference docs | 1 | ~88% per loop |
| 6. Cost dashboard | Low | Justifying adoption, monitoring | 1 | Measurement tool |
