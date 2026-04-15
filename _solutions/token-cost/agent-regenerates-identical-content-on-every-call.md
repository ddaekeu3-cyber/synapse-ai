---
layout: solution
title: "Agent Regenerates Identical Content on Every Call"
category: token-cost
description: "Agent re-generates the same boilerplate, headers, disclaimers, or static content on every request — paying full generation cost for tokens that never change."
tags: [token-cost, caching, prompt-caching, deduplication, static-content, cost-optimisation]
---

## Symptom

Every API call generates the same 500-token legal disclaimer, company bio, or boilerplate header — regardless of the user's actual question. Token logs reveal:

```
Prompt tokens:     200
Output tokens:     712  ← 500 are identical to the previous call
Total cost/call:   $0.009
Daily calls:       50,000
Daily waste:       $225 in redundant generation
```

## Root Cause

Static content is included in the prompt as if it were dynamic, causing the model to regenerate it on every completion. Without caching or pre-generation, the cost compounds with every call.

## Fix

---

### Option 1 — Pre-Generate Static Sections and Concatenate

Generate static sections once, store them, and concatenate at runtime. The model only generates the dynamic portion of the response.

```python
import anthropic
from functools import lru_cache

client = anthropic.Anthropic()

# Static content generated once at startup
@lru_cache(maxsize=1)
def get_legal_disclaimer() -> str:
    """Generate legal disclaimer once — cached forever in process."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": "Write a concise legal disclaimer for an AI assistant that provides information but not professional advice. 3 sentences max.",
        }],
    )
    text = response.content[0].text
    print("[GENERATED] Legal disclaimer (will be cached)")
    return text

@lru_cache(maxsize=1)
def get_company_intro() -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{
            "role": "user",
            "content": "Write a one-sentence company introduction for 'Acme AI Assistant'.",
        }],
    )
    return response.content[0].text

def answer_user_question(question: str) -> str:
    """
    Only the answer to the user's question is generated fresh.
    Static sections are pre-generated and concatenated.
    """
    # Dynamic: generate only the answer
    answer_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system="You are a helpful assistant. Answer the question directly and concisely.",
        messages=[{"role": "user", "content": question}],
    )
    answer = answer_response.content[0].text

    # Static: loaded from cache — no generation cost
    disclaimer = get_legal_disclaimer()
    intro = get_company_intro()

    # Assemble full response
    return f"{intro}\n\n{answer}\n\n---\n*{disclaimer}*"

# First calls trigger generation; all subsequent calls use cached values
for question in [
    "What is machine learning?",
    "How does Python work?",
    "What is the capital of France?",
]:
    result = answer_user_question(question)
    print(f"Q: {question}")
    print(f"A: {result[:120]}...\n")
```

**Expected Token Savings:** ~70% reduction in output tokens (static sections cost 0 after first call)
**Environment:** `pip install anthropic`

---

### Option 2 — Prompt Caching for Static System Prompts

Use Anthropic's prompt caching (`cache_control: ephemeral`) to cache a large, static system prompt. Subsequent requests read from cache — paying only 10% of the input token cost.

```python
import anthropic

client = anthropic.Anthropic()

# Large static content — knowledge base, product catalogue, policy document
PRODUCT_CATALOGUE = """
PRODUCT CATALOGUE (Version 3.1, effective 2026-01-01)

CATEGORY: Software Tools
- ProductA Pro ($49/mo): Advanced analytics, unlimited users, API access, 99.9% SLA
- ProductA Starter ($9/mo): Basic analytics, up to 5 users, no API access
- ProductA Enterprise (custom): All Pro features + dedicated support + custom integrations

CATEGORY: Consulting Services
- Setup Package ($500): One-time onboarding, 3 hours of expert guidance
- Monthly Support ($200/mo): 4 hours/month support tickets + monthly review call
- Custom Development (from $5,000): Bespoke feature development, 4-week minimum

POLICIES:
- All plans: 14-day free trial, no credit card required
- Cancellations: Cancel any time, prorated refunds on annual plans
- Data retention: 90 days after cancellation, then deleted per GDPR

FREQUENTLY ASKED QUESTIONS:
Q: Can I upgrade mid-cycle? A: Yes, prorated charges apply immediately.
Q: Is there an API rate limit? A: Pro plan: 1,000 req/min; Enterprise: unlimited.
Q: What payment methods? A: Credit card, wire transfer (Enterprise only), invoice (annual).
""" * 5  # Simulate a large document

def answer_product_question(question: str) -> str:
    """
    The large product catalogue is cached in the system prompt.
    After the first call, it costs only 10% of normal input token cost.
    """
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=[
            {
                "type": "text",
                "text": (
                    "You are a product support assistant. "
                    "Answer questions based on the catalogue below.\n\n"
                    + PRODUCT_CATALOGUE
                ),
                "cache_control": {"type": "ephemeral"},  # Cache this block
            }
        ],
        messages=[{"role": "user", "content": question}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )

    usage = response.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0)
    cache_write = getattr(usage, "cache_creation_input_tokens", 0)

    if cache_read > 0:
        print(f"[CACHE HIT] {cache_read} tokens from cache (90% savings)")
    elif cache_write > 0:
        print(f"[CACHE WRITE] {cache_write} tokens cached for future calls")

    return response.content[0].text

questions = [
    "What's the price of the Pro plan?",
    "Can I get a refund if I cancel?",
    "What's the API rate limit on the Pro plan?",
    "How do I upgrade my plan?",
]

for q in questions:
    print(f"\nQ: {q}")
    print(f"A: {answer_product_question(q)[:120]}")
```

**Expected Token Savings:** ~90% on system prompt tokens after first call (cached at 10% cost)
**Environment:** `pip install anthropic`

---

### Option 3 — Response Fragment Cache with TTL

Cache complete response sections keyed by content hash. When the same logical content is requested again within the TTL, return the cached fragment without re-generating.

```python
import hashlib
import time
import anthropic
from dataclasses import dataclass

@dataclass
class CachedFragment:
    content: str
    generated_at: float
    ttl_seconds: float

    def is_valid(self) -> bool:
        return time.time() - self.generated_at < self.ttl_seconds

class FragmentCache:
    def __init__(self):
        self._cache: dict[str, CachedFragment] = {}
        self.hits = 0
        self.misses = 0

    def _key(self, prompt_hash: str) -> str:
        return prompt_hash

    def get(self, prompt: str) -> str | None:
        key = hashlib.sha256(prompt.encode()).hexdigest()[:24]
        fragment = self._cache.get(key)
        if fragment and fragment.is_valid():
            self.hits += 1
            return fragment.content
        self.misses += 1
        return None

    def set(self, prompt: str, content: str, ttl_seconds: float = 3600):
        key = hashlib.sha256(prompt.encode()).hexdigest()[:24]
        self._cache[key] = CachedFragment(content, time.time(), ttl_seconds)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0

fragment_cache = FragmentCache()
client = anthropic.Anthropic()

def generate_cached(prompt: str, ttl_seconds: float = 3600, max_tokens: int = 512) -> str:
    cached = fragment_cache.get(prompt)
    if cached:
        print(f"[CACHE HIT] Returning cached fragment")
        return cached

    print(f"[GENERATE] Cache miss — generating content")
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    content = response.content[0].text
    fragment_cache.set(prompt, content, ttl_seconds)
    return content

def compose_response(user_question: str) -> str:
    # Static sections are prompt-cached
    intro = generate_cached(
        "Write a 2-sentence intro for an AI customer service bot named ARIA.",
        ttl_seconds=86400,
    )
    cta = generate_cached(
        "Write a 1-sentence call-to-action for users to visit help.example.com",
        ttl_seconds=86400,
    )

    # Only the answer to the user's question is generated fresh
    answer_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": user_question}],
    )
    answer = answer_response.content[0].text

    return f"{intro}\n\n{answer}\n\n{cta}"

for q in ["How do I reset my password?", "What are your hours?", "How do I cancel?"]:
    print(f"\nQ: {q}")
    result = compose_response(q)
    print(f"A: {result[:100]}...")

print(f"\nFragment cache hit rate: {fragment_cache.hit_rate:.0%}")
```

**Expected Token Savings:** ~50% on repeated static sections; increases with request volume
**Environment:** `pip install anthropic`

---

### Option 4 — Streaming with Template Injection

Pre-stream static header content from a template, then stream only the dynamic LLM-generated content. Users see the full response instantly; static sections cost zero tokens.

```python
import anthropic
from collections.abc import Generator

client = anthropic.Anthropic()

# Static templates — no LLM cost
TEMPLATES = {
    "response_header": "**ARIA Customer Support**\n\n",
    "response_footer": "\n\n---\n*Need more help? Visit [help.example.com](https://help.example.com)*",
    "escalation_note": "\n\n> *If this doesn't resolve your issue, type 'escalate' to reach a human agent.*",
}

def stream_composed_response(
    user_question: str,
    include_escalation: bool = True,
) -> Generator[str, None, None]:
    """
    Yields response tokens as they arrive.
    Static sections are yielded immediately — no API cost.
    """
    # 1. Yield static header instantly (0 tokens)
    yield TEMPLATES["response_header"]

    # 2. Stream only the dynamic answer
    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system="You are a helpful customer support assistant. Answer directly and concisely.",
        messages=[{"role": "user", "content": user_question}],
    ) as stream:
        for text in stream.text_stream:
            yield text

    # 3. Yield static footer instantly (0 tokens)
    yield TEMPLATES["response_footer"]

    if include_escalation:
        yield TEMPLATES["escalation_note"]

def print_streamed_response(question: str):
    print(f"Q: {question}")
    print("A: ", end="")
    for chunk in stream_composed_response(question):
        print(chunk, end="", flush=True)
    print("\n")

print_streamed_response("How do I change my email address?")
print_streamed_response("What payment methods do you accept?")
```

**Expected Token Savings:** ~30% — static header/footer cost 0 tokens; only answer is generated
**Environment:** `pip install anthropic`

---

### Option 5 — Batch Similar Requests with Shared Context

When many similar questions come in, batch them into a single API call. One call generates multiple answers — amortising the prompt overhead across N responses.

```python
import anthropic
import json

client = anthropic.Anthropic()

SHARED_CONTEXT = """
You are a product FAQ assistant for Acme Software.
Key facts:
- Support hours: Monday-Friday, 9am-6pm EST
- Free trial: 14 days, no credit card required
- Pricing: Starter $9/mo, Pro $49/mo, Enterprise custom
- Cancellation: Any time, prorated refunds on annual plans
- API: Pro plan gets 1,000 req/min rate limit
"""

def batch_answer_questions(questions: list[str]) -> list[str]:
    """
    Answer N questions in a single API call.
    Shared context (SHARED_CONTEXT) is sent once — not once per question.
    """
    if not questions:
        return []

    numbered = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(questions))

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": SHARED_CONTEXT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{
            "role": "user",
            "content": (
                f"Answer each of the following {len(questions)} questions briefly. "
                f"Number your answers to match the questions.\n\n{numbered}"
            ),
        }],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )

    raw = response.content[0].text

    # Parse numbered answers
    answers = []
    for i in range(1, len(questions) + 1):
        import re
        pattern = rf"{i}\.\s*(.*?)(?={i + 1}\.|$)"
        match = re.search(pattern, raw, re.DOTALL)
        answers.append(match.group(1).strip() if match else raw)

    usage = response.usage
    print(f"[BATCH] {len(questions)} questions in 1 API call")
    print(f"[USAGE] Input: {usage.input_tokens}, Output: {usage.output_tokens}")
    print(f"[EFFICIENCY] ~{usage.input_tokens // len(questions)} tokens per question (shared context)")

    return answers

questions = [
    "What are your support hours?",
    "Do I need a credit card for the trial?",
    "Can I cancel anytime?",
    "What is the API rate limit on Pro?",
    "How much does the Pro plan cost?",
]

answers = batch_answer_questions(questions)
for q, a in zip(questions, answers):
    print(f"\nQ: {q}")
    print(f"A: {a[:100]}")
```

**Expected Token Savings:** ~60% vs N individual calls (context sent once, answers batched)
**Environment:** `pip install anthropic`

---

### Option 6 — Content Hash Deduplication Across Sessions

Store generated content in a persistent hash store. Identical logical requests (same input hash) return stored content. Tracks savings over time.

```python
import sqlite3
import hashlib
import json
import time
import anthropic
from pathlib import Path

DB_PATH = Path("content_cache.db")
client = anthropic.Anthropic()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS content_cache (
            prompt_hash TEXT PRIMARY KEY,
            prompt_preview TEXT,
            content TEXT,
            token_cost INTEGER,
            created_at REAL,
            expires_at REAL,
            hit_count INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS savings_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt_hash TEXT,
            tokens_saved INTEGER,
            saved_at REAL
        )
    """)
    conn.commit()
    conn.close()

def hash_prompt(prompt: str) -> str:
    return hashlib.sha256(prompt.strip().lower().encode()).hexdigest()[:32]

def get_cached(prompt: str) -> str | None:
    h = hash_prompt(prompt)
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT content, token_cost FROM content_cache WHERE prompt_hash = ? AND expires_at > ?",
        (h, time.time()),
    ).fetchone()

    if row:
        conn.execute(
            "UPDATE content_cache SET hit_count = hit_count + 1 WHERE prompt_hash = ?", (h,)
        )
        conn.execute(
            "INSERT INTO savings_log (prompt_hash, tokens_saved, saved_at) VALUES (?, ?, ?)",
            (h, row[1], time.time()),
        )
        conn.commit()
    conn.close()
    return row[0] if row else None

def store_cached(prompt: str, content: str, token_cost: int, ttl: int = 86400):
    h = hash_prompt(prompt)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT OR REPLACE INTO content_cache
        (prompt_hash, prompt_preview, content, token_cost, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (h, prompt[:80], content, token_cost, time.time(), time.time() + ttl))
    conn.commit()
    conn.close()

def total_savings() -> dict:
    conn = sqlite3.connect(DB_PATH)
    result = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(tokens_saved), 0) FROM savings_log"
    ).fetchone()
    conn.close()
    return {"cache_hits": result[0], "tokens_saved": result[1]}

def generate_with_cache(prompt: str, max_tokens: int = 512, ttl: int = 3600) -> str:
    cached = get_cached(prompt)
    if cached:
        print(f"[CACHE HIT] Saved ~{max_tokens // 2} output tokens")
        return cached

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    content = response.content[0].text
    tokens = response.usage.output_tokens
    store_cached(prompt, content, tokens, ttl)
    print(f"[GENERATED] {tokens} output tokens — cached for {ttl}s")
    return content

init_db()

test_prompts = [
    "Write a one-paragraph company overview for Acme AI.",
    "Write a one-paragraph company overview for Acme AI.",  # Duplicate
    "Write a concise privacy policy summary for an AI chatbot.",
    "Write a one-paragraph company overview for Acme AI.",  # Duplicate again
]

for p in test_prompts:
    result = generate_with_cache(p, ttl=3600)
    print(f"Result: {result[:60]}...\n")

savings = total_savings()
print(f"Total cache hits: {savings['cache_hits']}")
print(f"Total tokens saved: {savings['tokens_saved']}")
```

**Expected Token Savings:** ~100% output token savings on cache hits; compounds with request volume
**Environment:** `pip install anthropic`

---

## Comparison

| Option | Caching Layer | Persistence | Token Reduction | Best For |
|--------|--------------|-------------|-----------------|----------|
| Pre-Generate Static | In-process (lru_cache) | Session only | ~70% output | Static boilerplate |
| Prompt Caching | Anthropic cache | Session | ~90% input | Large system prompts |
| Fragment Cache + TTL | In-process dict | Session | ~50% output | Reused content sections |
| Streaming + Template | None (static injection) | N/A | ~30% output | User-facing chat |
| Batch Similar Requests | None (amortisation) | N/A | ~60% input | FAQ / bulk processing |
| Content Hash Store | SQLite | Persistent | ~100% output | Cross-session dedup |

**Recommended starting point:** Option 2 (Prompt Caching) for any agent with a large static system prompt. Add Option 6 (Hash Store) for content that repeats across sessions.
