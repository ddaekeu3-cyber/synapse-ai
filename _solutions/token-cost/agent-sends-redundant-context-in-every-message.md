---
layout: solution
title: "Agent sends redundant context in every message"
category: token-cost
description: "Agent prepends the same large block of static context (instructions, knowledge base, persona) to every user message body instead of using the system prompt, paying full input token cost on every API call."
tags: [token-cost, redundant-context, system-prompt, prompt-caching, optimization]
---

## Symptom

Every API call contains thousands of tokens of boilerplate at the start of the first user message:

```
User: [3,000-word company policy document]
      [500-word persona definition]
      [200-word task instructions]
      Now answer: What is our refund policy?
```

The context block is identical across all calls. The Anthropic API bills for every input token on every call — the overhead costs more than the actual user query on most requests.

## Root Cause

The context block was originally added by concatenating strings: `context + "\n\n" + user_message`. As the context grew, nobody measured its token cost or moved it to the system prompt. The system prompt was either unknown or avoided because the team wasn't sure what to put there. The result is the same static text being billed as fresh input tokens on every single call.

---

## Option 1 — Move static context to the system prompt

**The single most impactful change: static context belongs in `system`, not in `messages[0].content`.**

```python
import anthropic

client = anthropic.Anthropic()

# BEFORE — static context prepended to every user message
COMPANY_POLICY = """
Our refund policy: customers may return products within 30 days for a full refund.
Shipping costs are non-refundable. Digital products cannot be returned once downloaded.
[... 2,000 more words of policy ...]
"""

def ask_before(user_question: str) -> str:
    """BAD: pays for COMPANY_POLICY tokens on every call."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": COMPANY_POLICY + "\n\nQuestion: " + user_question,
        }],
    )
    return response.content[0].text


# AFTER — static context in system prompt (cached after first call)
def ask_after(user_question: str) -> str:
    """GOOD: COMPANY_POLICY counted once (and cached with prompt caching)."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=COMPANY_POLICY,
        messages=[{"role": "user", "content": user_question}],
    )
    return response.content[0].text


# Measure the difference
import time

questions = [
    "What is your refund policy?",
    "Can I return a digital product?",
    "How long do I have to make a return?",
]

for q in questions:
    print(f"Q: {q}")
    print(f"A: {ask_after(q)[:80]}\n")
```

**Token cost comparison (2,000-word policy = ~500 tokens):**
```
BEFORE: 500 (policy) + 20 (question) = 520 tokens × 1,000 calls = 520,000 tokens
AFTER:  500 (system) + 20 (question) = 520 tokens on first call
        20 (question only) = 20 tokens on subsequent calls (with prompt caching)
Saving: ~96% on repeat calls
```

**Expected Token Savings:** Moving 500 tokens of static context to the system prompt saves 500 tokens × number of calls. With prompt caching enabled, the system prompt is cached after the first call — saving ~96% of context tokens on all subsequent calls.

**Environment:** Any agent with static system-level context; zero architecture changes required.

---

## Option 2 — Enable prompt caching on the system prompt

**After moving context to the system prompt, add `cache_control` to cache it server-side. Cached tokens cost 10% of normal input token price.**

```python
import anthropic

client = anthropic.Anthropic()

LARGE_KNOWLEDGE_BASE = """
[Product catalogue with 500 items, each with description, price, and specs]
[Company FAQ: 200 questions and answers]
[Support procedures: 50 standard operating procedures]
""" + "detailed knowledge base content " * 500   # ~2,500 tokens


def ask_with_caching(user_question: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=[
            {
                "type": "text",
                "text": LARGE_KNOWLEDGE_BASE,
                "cache_control": {"type": "ephemeral"},   # cache this block
            }
        ],
        messages=[{"role": "user", "content": user_question}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )

    # Inspect cache performance
    usage = response.usage
    cache_read  = getattr(usage, "cache_read_input_tokens",  0)
    cache_write = getattr(usage, "cache_creation_input_tokens", 0)

    if cache_write:
        print(f"  Cache MISS (write): {cache_write} tokens cached")
    elif cache_read:
        print(f"  Cache HIT: {cache_read} tokens served from cache (10% cost)")

    return response.content[0].text


questions = [
    "What is the price of product SKU-1042?",
    "How do I escalate a support ticket?",
    "What is your return window?",
]

for q in questions:
    print(f"Q: {q}")
    print(f"A: {ask_with_caching(q)[:80]}\n")
```

**Cost breakdown:**
```
First call:  2,500 tokens cached at normal price + 15 tokens question
             Cache write cost = 125% of normal input price (one-time)
Calls 2–N:  2,500 tokens served from cache at 10% price + 15 tokens question
             Effective cost per call: 250 + 15 = 265 tokens equivalent
Breakeven:  After call 2, every subsequent call saves 90% of knowledge base cost
```

**Expected Token Savings:** For 1,000 daily calls with a 2,500-token knowledge base: caching saves ~2,250 tokens/call × 999 calls = ~2,247,750 effective tokens saved (at 90% discount).

**Environment:** Agents making repeated calls with the same static knowledge base; Anthropic prompt caching beta; minimum cacheable block: 1,024 tokens.

---

## Option 3 — Split context into stable and dynamic blocks; cache only stable

**Some context is truly static (product catalogue), some changes per-user (user profile). Cache the static block; keep the dynamic block uncached.**

```python
import anthropic

client = anthropic.Anthropic()

# Static — never changes between calls
PRODUCT_CATALOGUE = "Catalogue: " + "product details, specs, prices. " * 400  # ~2,000 tokens

# Dynamic — changes per user session
def get_user_context(user_id: str) -> str:
    return f"User {user_id}: premium member since 2022, prefers email contact, region: EU."


def ask_split_cache(user_id: str, question: str) -> str:
    user_context = get_user_context(user_id)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=[
            {
                "type": "text",
                "text": PRODUCT_CATALOGUE,
                "cache_control": {"type": "ephemeral"},   # cached — stable
            },
            {
                "type": "text",
                "text": user_context,
                # No cache_control — dynamic, unique per user
            },
        ],
        messages=[{"role": "user", "content": question}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )

    usage = response.usage
    print(f"  cache_read={getattr(usage, 'cache_read_input_tokens', 0)} "
          f"cache_write={getattr(usage, 'cache_creation_input_tokens', 0)} "
          f"input={usage.input_tokens}")

    return response.content[0].text


# Different users, same catalogue — catalogue is served from cache for all
for user_id, question in [
    ("u001", "Do you have a 4K TV under $500?"),
    ("u002", "What laptops do you carry?"),
    ("u001", "Can I return my recent purchase?"),
]:
    print(f"[{user_id}] Q: {question}")
    print(f"         A: {ask_split_cache(user_id, question)[:60]}\n")
```

**Expected Token Savings:** The static catalogue (2,000 tokens) is cached and shared across all users — 90% cost reduction on catalogue tokens for every user after the first. Dynamic user context (~30 tokens) is never cached, which is correct since it changes per user.

**Environment:** Multi-user agents with a shared knowledge base and per-user personalisation.

---

## Option 4 — Lazy context injection: include context only when relevant

**Classify the query first; inject the relevant context subset rather than the full knowledge base.**

```python
import anthropic

client = anthropic.Anthropic()

# Domain-specific context blocks (each ~400 tokens)
CONTEXT_BLOCKS = {
    "returns":   "Return policy: 30-day return window, full refund, no questions asked. " * 20,
    "shipping":  "Shipping policy: free over $50, 3–5 days standard, 1-day express available. " * 20,
    "products":  "Product catalogue: electronics, clothing, home goods. " * 20,
    "accounts":  "Account management: password reset, subscription, billing. " * 20,
    "support":   "Support escalation: tier 1 → tier 2 → engineering, 24h SLA. " * 20,
}


def classify_query(question: str) -> list[str]:
    """Return relevant context keys for this query (haiku, cheap)."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=30,
        messages=[{
            "role": "user",
            "content": (
                "Which topics are relevant to this question? "
                "Reply with a comma-separated list from: returns, shipping, products, accounts, support.\n\n"
                f"Question: {question}"
            ),
        }],
    )
    raw = response.content[0].text.lower()
    return [k for k in CONTEXT_BLOCKS if k in raw]


def ask_lazy_context(question: str) -> str:
    relevant_keys = classify_query(question)
    print(f"  Relevant context: {relevant_keys} ({len(relevant_keys)}/{len(CONTEXT_BLOCKS)} blocks)")

    if not relevant_keys:
        relevant_keys = list(CONTEXT_BLOCKS.keys())   # fallback: all

    context = "\n\n".join(CONTEXT_BLOCKS[k] for k in relevant_keys)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=context,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text


questions = [
    "Can I return a pair of shoes I bought last week?",
    "How do I reset my account password?",
    "What is the shipping time to Canada?",
]

for q in questions:
    print(f"Q: {q}")
    print(f"A: {ask_lazy_context(q)[:80]}\n")
```

**Expected Token Savings:** A 5-block knowledge base (2,000 tokens total) reduced to 1 relevant block (400 tokens) saves 1,600 tokens of system prompt per call. Classification costs ~50 haiku tokens — net saving: ~1,550 tokens per call (77% reduction).

**Environment:** Agents with domain-segmented knowledge bases; most effective when queries are topic-specific.

---

## Option 5 — Reference architecture: measure context overhead before optimising

**Add token counting to every API call to identify which messages are causing the most overhead.**

```python
import json
from collections import defaultdict
import anthropic

client = anthropic.Anthropic()

# Metrics accumulator
_token_log: dict[str, list[int]] = defaultdict(list)


def tracked_create(label: str, **kwargs) -> anthropic.types.Message:
    """Wrapper that logs token usage by call label."""
    response = client.messages.create(**kwargs)
    usage = response.usage
    _token_log[label].append(usage.input_tokens)

    cache_read  = getattr(usage, "cache_read_input_tokens",     0)
    cache_write = getattr(usage, "cache_creation_input_tokens", 0)
    print(
        f"  [{label}] input={usage.input_tokens} output={usage.output_tokens} "
        f"cache_read={cache_read} cache_write={cache_write}"
    )
    return response


def print_token_report() -> None:
    print("\n=== Token Usage Report ===")
    for label, counts in sorted(_token_log.items()):
        avg = sum(counts) / len(counts)
        total = sum(counts)
        print(f"  {label}: {len(counts)} calls | avg={avg:.0f} input tokens | total={total:,}")


# Example: compare before vs after
STATIC_CONTEXT = "Company policy: " + "rules and regulations. " * 200   # ~500 tokens

# BEFORE: context in user message
for q in ["Question A", "Question B", "Question C"]:
    tracked_create(
        label="before",
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": STATIC_CONTEXT + "\n\n" + q}],
    )

# AFTER: context in system prompt
for q in ["Question A", "Question B", "Question C"]:
    tracked_create(
        label="after",
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=STATIC_CONTEXT,
        messages=[{"role": "user", "content": q}],
    )

print_token_report()
```

**Expected Token Savings:** Measurement-first approach identifies the highest-value optimisation target. Teams typically discover that 60–90% of input tokens come from 1–2 context blocks that can be moved to the system prompt or cached.

**Environment:** Any existing agent pipeline; run this audit before deciding which optimisation to apply.

---

## Option 6 — Shared system prompt singleton for multi-agent workflows

**When many agents share the same base context, build the system prompt once and reuse it — ensuring prompt caching is maximally effective.**

```python
import anthropic
from functools import lru_cache

client = anthropic.Anthropic()

BASE_KNOWLEDGE = "Shared company knowledge: " + "policies, products, procedures. " * 300


@lru_cache(maxsize=1)
def get_base_system_prompt() -> list[dict]:
    """Build the cached system prompt block once per process."""
    return [
        {
            "type": "text",
            "text": BASE_KNOWLEDGE,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def make_agent_system(role_instructions: str) -> list[dict]:
    """Compose agent-specific system prompt: cached base + uncached role."""
    return get_base_system_prompt() + [
        {"type": "text", "text": role_instructions}
    ]


class SupportAgent:
    ROLE = "You are a customer support specialist. Focus on resolving issues empathetically."

    def respond(self, user_message: str) -> str:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=make_agent_system(self.ROLE),
            messages=[{"role": "user", "content": user_message}],
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
        return response.content[0].text


class SalesAgent:
    ROLE = "You are a sales assistant. Focus on product benefits and upsell opportunities."

    def respond(self, user_message: str) -> str:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=make_agent_system(self.ROLE),
            messages=[{"role": "user", "content": user_message}],
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
        return response.content[0].text


support = SupportAgent()
sales   = SalesAgent()

# Both agents share the same cached BASE_KNOWLEDGE block
print(support.respond("My order hasn't arrived in 2 weeks."))
print(sales.respond("I'm looking for a new laptop under $1,000."))
print(support.respond("I need to return a damaged item."))
```

**Expected Token Savings:** One cache write for `BASE_KNOWLEDGE` shared across all agent types — subsequent calls from any agent type hit the cache. For 5 agent types × 200 daily calls each = 1,000 calls/day with a 1,500-token base: saves ~1,350 tokens × 999 calls = ~1,348,650 effective tokens/day.

**Environment:** Multi-agent architectures where different agent roles share a common knowledge base; prompt caching beta required.

---

## Comparison

| Option | Approach | Extra API Calls | Cache Eligible | Complexity |
|--------|---------|----------------|----------------|------------|
| 1. Move to system prompt | Structural refactor | Zero | Yes | Very Low |
| 2. Add `cache_control` | One-line change | Zero | Yes (10% cost) | Very Low |
| 3. Stable/dynamic split | Structural refactor | Zero | Partial | Low |
| 4. Lazy context injection | Classify then subset | One (haiku) | Per-block | Medium |
| 5. Token audit | Measurement | Zero | N/A | Low |
| 6. Shared prompt singleton | Architecture pattern | Zero | Yes (shared) | Low |

**Recommended path:** Apply Option 1 (move to system prompt) first — zero risk, immediate 30–50% reduction. Then add Option 2 (`cache_control`) for 90% cost reduction on the cached block. Use Option 3 (stable/dynamic split) when user-specific context must remain separate. Run Option 5 (token audit) regularly to catch regressions.
