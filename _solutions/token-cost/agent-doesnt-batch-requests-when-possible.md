---
layout: solution
title: "Agent Doesn't Batch Requests When Possible"
category: token-cost
description: "Agent fires one API call per item when processing a list of inputs — N items produce N calls, each paying the full system prompt and context overhead — instead of batching multiple items into a single call."
tags: [token-cost, batching, efficiency, throughput, prompt-caching, optimization]
---

## Symptom

An agent classifies 500 customer support tickets by routing category. It makes 500 API calls. Each call sends a 200-token system prompt, a 30-token instruction, and a 50-token ticket body — paying 230 tokens of overhead per item. Total input tokens: 140,000. A batched approach sending 20 tickets per call would pay the 230-token overhead once per batch of 20, reducing input tokens to ~19,000. The agent runs 5× slower and costs 7× more than necessary.

## Root Cause

Developers write `for item in items: call_api(item)` because it's the natural loop pattern. They don't consider that the system prompt and instruction preamble are repeated on every call. When items are independent (classification, extraction, translation) and the combined length of N items fits in the context window, batching them into a single call dramatically reduces per-item overhead — the fixed cost is paid once instead of N times.

## Fix

### Option 1 — Simple batch: process N items per call with numbered output

```python
import anthropic
import math

client = anthropic.Anthropic()

SYSTEM = "You are a customer support ticket classifier."

# Tickets to classify
TICKETS = [
    "My password reset email never arrived.",
    "I was charged twice for my subscription.",
    "How do I export my data to CSV?",
    "The mobile app crashes on startup.",
    "Can I add a second user to my account?",
    "I need an invoice for last month.",
    "Your website is down — I can't log in.",
    "How do I change my billing address?",
    "The search function returns no results.",
    "I want to cancel my subscription.",
]

CATEGORIES = ["billing", "technical", "account", "feature-request", "outage"]

def classify_one_by_one(tickets: list[str]) -> list[str]:
    """INEFFICIENT: one API call per ticket."""
    results = []
    for ticket in tickets:
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8,
            system=SYSTEM,
            messages=[{
                "role": "user",
                "content": f"Classify into exactly one of {CATEGORIES}. Reply with the category only.\n\nTicket: {ticket}"
            }],
        )
        results.append(r.content[0].text.strip())
    return results

def classify_batched(tickets: list[str], batch_size: int = 10) -> list[str]:
    """EFFICIENT: batch multiple tickets per call."""
    all_results: list[str] = []
    for i in range(0, len(tickets), batch_size):
        batch = tickets[i:i + batch_size]
        numbered = "\n".join(f"{j+1}. {t}" for j, t in enumerate(batch))
        prompt = (
            f"Classify each ticket into exactly one of {CATEGORIES}.\n"
            f"Reply with one category per line, numbered to match.\n\n"
            f"Tickets:\n{numbered}"
        )
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=len(batch) * 12,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        lines = [ln.split(".", 1)[-1].strip() for ln in r.content[0].text.strip().splitlines() if ln.strip()]
        all_results.extend(lines[:len(batch)])
    return all_results

import time

print("One-by-one (inefficient):")
t0 = time.perf_counter()
results_bad = classify_one_by_one(TICKETS)
bad_ms = (time.perf_counter() - t0) * 1000
print(f"  {len(TICKETS)} calls in {bad_ms:.0f}ms")
for ticket, cat in zip(TICKETS[:3], results_bad[:3]):
    print(f"  {cat:20s} ← {ticket[:50]}")

print("\nBatched (efficient):")
t0 = time.perf_counter()
results_good = classify_batched(TICKETS, batch_size=5)
good_ms = (time.perf_counter() - t0) * 1000
print(f"  {math.ceil(len(TICKETS)/5)} calls in {good_ms:.0f}ms")
for ticket, cat in zip(TICKETS[:3], results_good[:3]):
    print(f"  {cat:20s} ← {ticket[:50]}")

print(f"\nSpeedup: {bad_ms/good_ms:.1f}x | Calls reduced: {len(TICKETS)} → {math.ceil(len(TICKETS)/5)}")
```

**Expected Token Savings:** Batching 10 tickets per call reduces overhead tokens from 10 × 230 = 2,300 to 1 × 280 = 280 — an 88% reduction in overhead tokens; for 1,000 daily tickets, batching saves ~2M input tokens/month.
**Environment:** Classification, labelling, extraction, and translation agents processing lists of short, independent items; batching is the single highest-impact optimization for bulk processing pipelines.

---

### Option 2 — Async batched pipeline: fetch + classify concurrently

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

SYSTEM = """You are a sentiment analyser. For each numbered item, output one line:
<number>. <POSITIVE|NEGATIVE|NEUTRAL> <confidence 0.0-1.0>"""

async def analyse_batch(texts: list[str], batch_id: int) -> list[dict]:
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts))
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=len(texts) * 20,
        system=SYSTEM,
        messages=[{"role": "user", "content": f"Analyse sentiment:\n{numbered}"}],
    )
    results = []
    for line in r.content[0].text.strip().splitlines():
        parts = line.split(".", 1)
        if len(parts) == 2:
            fields = parts[1].strip().split()
            label = fields[0] if fields else "NEUTRAL"
            conf  = float(fields[1]) if len(fields) > 1 else 0.5
            results.append({"label": label, "confidence": conf})
    # Pad if model returned fewer lines than expected
    while len(results) < len(texts):
        results.append({"label": "NEUTRAL", "confidence": 0.5})
    print(f"  [batch {batch_id}] {len(texts)} items → {r.usage.input_tokens}in/{r.usage.output_tokens}out tok")
    return results[:len(texts)]

async def process_all(texts: list[str], batch_size: int = 8) -> list[dict]:
    batches = [texts[i:i+batch_size] for i in range(0, len(texts), batch_size)]
    sem     = asyncio.Semaphore(4)   # max 4 concurrent batch calls

    async def bounded(batch: list[str], bid: int) -> list[dict]:
        async with sem:
            return await analyse_batch(batch, bid)

    nested  = await asyncio.gather(*[bounded(b, i) for i, b in enumerate(batches)])
    return [item for batch in nested for item in batch]

async def main() -> None:
    import time
    reviews = [
        "Absolutely love this product, works perfectly!",
        "Terrible. Broke after two days.",
        "It's okay, nothing special.",
        "Best purchase I've made this year.",
        "Would not recommend to anyone.",
        "Does what it says, no complaints.",
        "Outstanding customer service!",
        "Packaging was damaged on arrival.",
        "Meets my expectations.",
        "Complete waste of money.",
        "Five stars, highly recommend.",
        "Average product at best.",
    ]
    t0      = time.perf_counter()
    results = await process_all(reviews, batch_size=4)
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"\n{len(reviews)} reviews in {elapsed:.0f}ms ({math.ceil(len(reviews)/4)} async batch calls)")
    for review, result in zip(reviews[:4], results[:4]):
        print(f"  {result['label']:8s} ({result['confidence']:.2f}) ← {review[:50]}")

import math
asyncio.run(main())
```

**Expected Token Savings:** Async batching compounds two optimisations: (1) batch overhead reduction, (2) concurrent execution — 12 items in 3 async batches of 4 complete in ~1× latency of one batch call, not 12× latency of individual calls.
**Environment:** High-throughput async pipelines processing hundreds of items; async batching with a semaphore is the standard production pattern for bulk LLM workloads.

---

### Option 3 — Prompt-cached batch: pin system prompt, batch items in user turn

```python
import anthropic

client = anthropic.Anthropic()

# Long, expensive system prompt that stays constant across all batches
EXTRACTION_SYSTEM = """You are a structured data extractor specialising in job postings.

For each numbered job description, extract:
- title: job title
- level: junior|mid|senior|lead|unknown
- remote: yes|no|hybrid|unknown
- salary_mentioned: true|false

Output one JSON object per line, prefixed with the item number.
Example: 1. {"title": "Software Engineer", "level": "senior", "remote": "yes", "salary_mentioned": true}

Rules:
- If a field cannot be determined, use the unknown/false default shown above.
- Output ONLY the numbered JSON lines — no preamble, no explanation.
- Maintain the numbering from the input exactly."""

# Cache the system prompt — it's long and repeated across batches
CACHED_SYSTEM = [
    {
        "type": "text",
        "text": EXTRACTION_SYSTEM,
        "cache_control": {"type": "ephemeral"},
    }
]

JOB_POSTINGS = [
    "Senior Python Engineer at Acme Corp, remote, $150k-$180k",
    "Junior React Developer, on-site NYC, no salary info",
    "Lead Data Scientist, hybrid Chicago, competitive pay",
    "Mid-level DevOps Engineer, fully remote, $120k",
    "Software Intern, office required, unpaid",
    "Principal Architect, remote-friendly, $200k+",
]

import json as _json

def extract_batch(postings: list[str], batch_size: int = 3) -> list[dict]:
    all_results = []
    for i in range(0, len(postings), batch_size):
        batch    = postings[i:i + batch_size]
        numbered = "\n".join(f"{j+1}. {p}" for j, p in enumerate(batch))
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=len(batch) * 60,
            system=CACHED_SYSTEM,
            messages=[{"role": "user", "content": f"Extract from these job postings:\n{numbered}"}],
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
        usage = r.usage
        print(f"  [batch {i//batch_size}] cache_read={getattr(usage,'cache_read_input_tokens',0)} "
              f"cache_write={getattr(usage,'cache_creation_input_tokens',0)}")
        for line in r.content[0].text.strip().splitlines():
            if ". {" in line:
                try:
                    json_part = line.split(". ", 1)[1]
                    all_results.append(_json.loads(json_part))
                except (_json.JSONDecodeError, IndexError):
                    all_results.append({})
    return all_results

results = extract_batch(JOB_POSTINGS, batch_size=3)
print(f"\nExtracted {len(results)} records:")
for posting, result in zip(JOB_POSTINGS, results):
    print(f"  {result} ← {posting[:50]}")
```

**Expected Token Savings:** Prompt caching + batching stacks two optimisations: the 300-token system prompt is cached after the first batch (90% discount on cache hits), AND each batch call processes multiple items — together reducing cost by 85-95% vs. one uncached call per item.
**Environment:** Extraction pipelines with long, stable system prompts; combining caching with batching is the highest-impact configuration for bulk structured data extraction.

---

### Option 4 — Adaptive batch sizer: maximise tokens-per-call within context limits

```python
import anthropic

client = anthropic.Anthropic()

SYSTEM = "Translate each numbered item to Spanish. Output one translation per line, numbered."

def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return max(1, len(text) // 4)

def adaptive_batch_translate(texts: list[str], model: str = "claude-haiku-4-5-20251001",
                              context_limit: int = 180_000, safety_margin: float = 0.8) -> list[str]:
    """Build batches that fill as much of the context window as possible."""
    usable_limit = int(context_limit * safety_margin)
    system_toks  = estimate_tokens(SYSTEM) + 50   # instruction overhead

    results: list[str] = []
    batch:   list[str] = []
    batch_toks = system_toks

    def flush_batch() -> None:
        if not batch:
            return
        numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(batch))
        r = client.messages.create(
            model=model,
            max_tokens=len(batch) * 30,
            system=SYSTEM,
            messages=[{"role": "user", "content": numbered}],
        )
        lines = r.content[0].text.strip().splitlines()
        translations = [ln.split(".", 1)[-1].strip() if "." in ln else ln for ln in lines]
        results.extend(translations[:len(batch)])
        print(f"  [batch of {len(batch)}] ~{batch_toks} input tokens estimated")
        batch.clear()

    nonlocal_toks = [system_toks]

    def flush_and_reset() -> None:
        flush_batch()
        nonlocal_toks[0] = system_toks

    for text in texts:
        item_toks = estimate_tokens(text) + 5   # numbering overhead
        if nonlocal_toks[0] + item_toks > usable_limit:
            flush_and_reset()
        batch.append(text)
        nonlocal_toks[0] += item_toks

    flush_batch()
    return results

import random, string

# Generate texts of varying lengths
random.seed(42)
texts = [
    "The quick brown fox jumps over the lazy dog.",
    "Artificial intelligence is transforming software development.",
    "Connection pooling reduces latency by reusing established TCP connections.",
    "Rate limiting protects APIs from abuse and ensures fair resource allocation.",
    "Asynchronous programming enables high-throughput I/O without blocking threads.",
    "Prompt caching reduces token costs for repeated context across API calls.",
    "Batch processing amortises fixed per-call overhead across multiple items.",
    "Circuit breakers prevent cascade failures in distributed systems.",
]

translations = adaptive_batch_translate(texts, batch_size_hint=4)
print(f"\n{len(texts)} translations:")
for src, tgt in zip(texts[:4], translations[:4]):
    print(f"  EN: {src[:50]}")
    print(f"  ES: {tgt[:50]}\n")
```

**Expected Token Savings:** Adaptive batching fills the context window as fully as possible — with 200K context and short items, a single call can process hundreds of items; for variable-length inputs, adaptive sizing prevents both under-utilisation (small batches) and context overflow errors (batches too large).
**Environment:** Pipelines with variable-length inputs (translations, summaries, classifications) where a fixed batch size would be either too small or too large; adaptive sizing maximises throughput within API limits.

---

### Option 5 — Message Batches API for offline bulk processing

```python
import anthropic
import time
import json as _json

client = anthropic.Anthropic()

# The Anthropic Message Batches API processes requests asynchronously
# at 50% cost reduction — ideal for non-real-time bulk workloads

TICKETS = [
    ("ticket-001", "My account was charged twice this month."),
    ("ticket-002", "The iOS app crashes immediately on launch."),
    ("ticket-003", "How do I export my contacts to CSV?"),
    ("ticket-004", "Password reset link expired before I could use it."),
    ("ticket-005", "I need a VAT invoice for my last payment."),
]

CATEGORIES = ["billing", "technical", "account", "outage", "feature-request"]
SYSTEM     = f"Classify the support ticket into one of: {CATEGORIES}. Reply with the category only."

def submit_batch(tickets: list[tuple[str, str]]) -> str:
    """Submit a batch and return the batch ID."""
    requests = [
        {
            "custom_id": ticket_id,
            "params": {
                "model":      "claude-haiku-4-5-20251001",
                "max_tokens": 16,
                "system":     SYSTEM,
                "messages":   [{"role": "user", "content": body}],
            },
        }
        for ticket_id, body in tickets
    ]
    batch = client.beta.messages.batches.create(requests=requests)
    print(f"  [batch] submitted id={batch.id} status={batch.processing_status}")
    return batch.id

def poll_batch(batch_id: str, max_wait: int = 120) -> dict[str, str]:
    """Poll until the batch completes and return results."""
    for attempt in range(max_wait // 5):
        batch = client.beta.messages.batches.retrieve(batch_id)
        print(f"  [poll {attempt+1}] status={batch.processing_status} "
              f"succeeded={batch.request_counts.succeeded} "
              f"processing={batch.request_counts.processing}")
        if batch.processing_status == "ended":
            break
        time.sleep(5)
    else:
        raise TimeoutError(f"Batch {batch_id} did not complete within {max_wait}s")

    results = {}
    for result in client.beta.messages.batches.results(batch_id):
        if result.result.type == "succeeded":
            results[result.custom_id] = result.result.message.content[0].text.strip()
        else:
            results[result.custom_id] = "error"
    return results

# Submit, poll, retrieve
batch_id = submit_batch(TICKETS)
print(f"\n  Batch submitted — in production this would run asynchronously.")
print(f"  Polling (may take a few seconds for small batches)...")

try:
    results = poll_batch(batch_id, max_wait=60)
    print(f"\nResults:")
    for ticket_id, body in TICKETS:
        print(f"  {ticket_id}: {results.get(ticket_id, 'pending'):20s} ← {body[:50]}")
except TimeoutError as e:
    print(f"  {e} — in production, store batch_id and poll later")
```

**Expected Token Savings:** Message Batches API provides a 50% cost reduction on all input and output tokens with no code changes beyond using the batches endpoint; for nightly processing of 100,000 items, batches save 50% of the cost compared to real-time calls.
**Environment:** Non-real-time bulk processing (nightly classification runs, document indexing, audit pipelines); Message Batches is the highest-discount option when latency requirements allow async processing.

---

### Option 6 — Fan-out with result aggregation: parallel batches with structured merge

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

SYSTEM = """For each numbered product review, extract:
- sentiment: positive|negative|neutral
- topic: quality|shipping|price|support|other
Output one line per item: <number>. <sentiment> <topic>"""

async def extract_batch(reviews: list[str], batch_id: int) -> list[dict]:
    numbered = "\n".join(f"{i+1}. {r}" for i, r in enumerate(reviews))
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=len(reviews) * 15,
        system=SYSTEM,
        messages=[{"role": "user", "content": f"Extract from reviews:\n{numbered}"}],
    )
    items = []
    for line in resp.content[0].text.strip().splitlines():
        parts = line.split(".", 1)
        if len(parts) == 2:
            fields = parts[1].strip().split()
            items.append({
                "sentiment": fields[0] if fields else "neutral",
                "topic":     fields[1] if len(fields) > 1 else "other",
            })
    while len(items) < len(reviews):
        items.append({"sentiment": "neutral", "topic": "other"})
    print(f"  [batch {batch_id}] {len(reviews)} reviews → {resp.usage.output_tokens} output tokens")
    return items[:len(reviews)]

async def fan_out_extract(reviews: list[str], batch_size: int = 5) -> list[dict]:
    batches  = [reviews[i:i+batch_size] for i in range(0, len(reviews), batch_size)]
    sem      = asyncio.Semaphore(3)

    async def bounded(batch: list[str], bid: int) -> list[dict]:
        async with sem:
            return await extract_batch(batch, bid)

    nested   = await asyncio.gather(*[bounded(b, i) for i, b in enumerate(batches)])
    return [item for batch in nested for item in batch]

async def main() -> None:
    import time
    reviews = [
        "Arrived fast, great quality!",
        "Expensive for what it is.",
        "Support team resolved my issue quickly.",
        "Packaging was damaged.",
        "Exactly as described, would buy again.",
        "Price dropped a week after I bought it, disappointing.",
        "Quality is poor, fell apart in a week.",
        "Shipping was slower than expected.",
        "Great value for money.",
        "Had an issue but support fixed it same day.",
        "Product works perfectly.",
        "Way overpriced.",
    ]
    t0      = time.perf_counter()
    results = await fan_out_extract(reviews, batch_size=4)
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"\n{len(reviews)} reviews in {elapsed:.0f}ms")

    # Aggregate
    from collections import Counter
    sentiments = Counter(r["sentiment"] for r in results)
    topics     = Counter(r["topic"]     for r in results)
    print(f"Sentiment: {dict(sentiments)}")
    print(f"Topics:    {dict(topics)}")

asyncio.run(main())
```

**Expected Token Savings:** Fan-out batching combines async concurrency with per-batch overhead amortisation — 12 items in 3 concurrent batches of 4 complete in ~1 batch call's latency, paying overhead 3× instead of 12×; aggregation happens client-side at zero token cost.
**Environment:** Analytics and reporting agents that need aggregate statistics over large corpora; fan-out enables both speed (async) and cost efficiency (batch overhead amortisation) simultaneously.

---

## Comparison

| Option | Calls for N Items | Async | Overhead Reduction | Best For |
|---|---|---|---|---|
| 1. Simple batch | N/batch_size | No | 80-90% | Synchronous pipelines, easy retrofit |
| 2. Async batched pipeline | N/batch_size | Yes | 80-90% + concurrency | High-throughput async agents |
| 3. Prompt-cached batch | N/batch_size | No | 90-95% (cache+batch) | Long system prompts, bulk extraction |
| 4. Adaptive batch sizer | Minimal | No | Maximised per context | Variable-length inputs |
| 5. Message Batches API | 1 (async) | Yes (API-side) | 50% cost discount | Nightly/offline bulk processing |
| 6. Fan-out aggregation | N/batch_size | Yes | 80-90% + concurrency | Analytics over large corpora |
