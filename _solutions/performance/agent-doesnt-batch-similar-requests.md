---
layout: solution
title: "Agent Doesn't Batch Similar Requests"
category: performance
description: "Agent fires one API call per item in a list instead of grouping them, multiplying latency and per-request overhead."
tags: [performance, batching, concurrency, token-cost, asyncio]
---

## Symptom

Processing 50 items takes 50 sequential API calls and 2–3 minutes. Each call pays the per-request network overhead and the model's context-initialisation cost. Total latency and cost grow linearly with item count when they could grow much more slowly with batching or concurrency.

## Root Cause

The simplest implementation loops over items and calls the API once per item. This is correct but inefficient. Batching multiple items into a single prompt reduces the number of round-trips and amortises fixed costs (network RTT, context initialisation, rate-limit slot consumption). Concurrency reduces wall-clock time without changing the number of calls.

## Fix

### Option 1 — Batch multiple items into a single prompt

```python
import json
import anthropic

client = anthropic.Anthropic()

ITEMS_PER_BATCH = 10

def classify_batch(items: list[str]) -> list[dict]:
    """Classify a batch of texts in a single API call."""
    numbered = "\n".join(f"{i+1}. {text}" for i, text in enumerate(items))
    prompt = (
        f"Classify each of the following {len(items)} texts as positive, neutral, or negative.\n"
        "Return a JSON array with one object per item in order: "
        '[{"index": 1, "sentiment": "positive"}, ...]\n\n'
        f"{numbered}"
    )
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=len(items) * 40,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip().lstrip("```json").rstrip("```").strip()
    return json.loads(raw)

def classify_all(texts: list[str]) -> list[dict]:
    results = []
    for i in range(0, len(texts), ITEMS_PER_BATCH):
        batch   = texts[i:i + ITEMS_PER_BATCH]
        batch_r = classify_batch(batch)
        # Re-index to absolute position
        for item in batch_r:
            item["index"] += i
        results.extend(batch_r)
        print(f"[batch] {i}–{i + len(batch) - 1} done ({len(results)}/{len(texts)} total)")
    return results

texts = [
    "Great product, very happy!",
    "Arrived broken. Terrible.",
    "It's okay, does the job.",
    "Absolutely love it!",
    "Not what I expected.",
    "Five stars, will buy again.",
    "Waste of money.",
    "Average at best.",
    "Highly recommend!",
    "Disappointed with quality.",
    "Works perfectly.",
    "Would not buy again.",
]
results = classify_all(texts)
for r in results:
    print(f"[{r['index']}] {texts[r['index'] - 1][:40]!r} → {r['sentiment']}")
```

**Expected Token Savings:** 10-item batches eliminate 9 out of 10 round-trips; fixed per-call overhead drops 90%; total latency cut by 70–80%.
**Environment:** Classification, extraction, or labelling tasks applied to lists of short items.

---

### Option 2 — Async concurrent calls with a semaphore

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

MAX_CONCURRENT = 5  # stay within rate limits

async def classify_one(sem: asyncio.Semaphore, item_id: int, text: str) -> dict:
    async with sem:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=16,
            messages=[{
                "role": "user",
                "content": f"Classify as positive/neutral/negative. Reply with one word only.\n\n{text}",
            }],
        )
        sentiment = response.content[0].text.strip().lower()
        return {"id": item_id, "text": text, "sentiment": sentiment}

async def classify_concurrent(texts: list[str]) -> list[dict]:
    sem     = asyncio.Semaphore(MAX_CONCURRENT)
    tasks   = [classify_one(sem, i, t) for i, t in enumerate(texts)]
    results = await asyncio.gather(*tasks)
    return list(results)

texts = [f"Review number {i}: {'great' if i % 3 == 0 else 'bad' if i % 3 == 1 else 'okay'}." for i in range(20)]

async def main():
    results = await classify_concurrent(texts)
    print(f"Processed {len(results)} items")
    for r in results[:5]:
        print(f"  [{r['id']}] {r['sentiment']}")

asyncio.run(main())
```

**Expected Token Savings:** 5x concurrent calls = 5x throughput; wall-clock time drops proportionally while token cost stays the same.
**Environment:** Items that must be processed independently (each needs its own context); pairs with the semaphore to stay within rate limits.

---

### Option 3 — Adaptive batching: combine small items, split large ones

```python
import json
import anthropic

client = anthropic.Anthropic()

MAX_BATCH_TOKENS  = 1500   # rough limit per batch
CHARS_PER_TOKEN   = 4

def token_estimate(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN

def adaptive_batches(items: list[str]) -> list[list[str]]:
    batches: list[list[str]] = []
    current: list[str]       = []
    current_tokens            = 0

    for item in items:
        t = token_estimate(item)
        if t > MAX_BATCH_TOKENS:
            # Item too large — process alone
            if current:
                batches.append(current)
                current, current_tokens = [], 0
            batches.append([item])
        elif current_tokens + t > MAX_BATCH_TOKENS:
            batches.append(current)
            current, current_tokens = [item], t
        else:
            current.append(item)
            current_tokens += t

    if current:
        batches.append(current)
    return batches

def extract_batch(items: list[str]) -> list[dict]:
    if len(items) == 1:
        prompt = f"Extract the main topic from this text. Return JSON: {{\"topic\": \"...\"}}\n\n{items[0]}"
    else:
        numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(items))
        prompt = (f"Extract the main topic from each text. "
                  f"Return a JSON array: [{{\"index\": 1, \"topic\": \"...\"}}, ...]\n\n{numbered}")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=len(items) * 30 + 64,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip().lstrip("```json").rstrip("```").strip()
    data = json.loads(raw)
    return data if isinstance(data, list) else [data]

# Mix of short and long items
items = [
    "Python is a programming language.",
    "Machine learning uses statistics.",
    "The " + "very " * 300 + "long document about astrophysics.",
    "Docker containers package applications.",
    "React is a JavaScript library.",
]
batches = adaptive_batches(items)
print(f"Adaptive batches: {[len(b) for b in batches]}")
for b in batches:
    results = extract_batch(b)
    print(results)
```

**Expected Token Savings:** Prevents oversized batches that exceed context; prevents under-utilisation from fixed batch sizes; minimises total API calls.
**Environment:** Mixed-length corpora where items vary from 50 to 2 000+ tokens.

---

### Option 4 — Batch with prompt caching on shared system context

```python
import json
import anthropic

client = anthropic.Anthropic()

SHARED_REFERENCE = """Product classification taxonomy:
- electronics: phones, laptops, tablets, accessories
- clothing: shirts, shoes, jackets, sportswear
- food: groceries, beverages, snacks, supplements
- home: furniture, appliances, decor, tools
- sports: equipment, activewear, outdoor gear
""" + "taxonomy_detail " * 200  # simulate a larger shared reference

ITEMS_PER_BATCH = 8

def classify_batch_cached(items: list[str]) -> list[dict]:
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(items))
    prompt = (
        f"Using the taxonomy above, classify each of these {len(items)} products.\n"
        f"Return JSON array: [{{\"index\": 1, \"category\": \"electronics\"}}, ...]\n\n{numbered}"
    )
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=len(items) * 30 + 64,
        system=[{
            "type": "text",
            "text": SHARED_REFERENCE,
            "cache_control": {"type": "ephemeral"},  # cache taxonomy on every batch call
        }],
        messages=[{"role": "user", "content": prompt}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    usage = response.usage
    print(f"[cache] read={usage.cache_read_input_tokens} write={usage.cache_creation_input_tokens}")
    raw = response.content[0].text.strip().lstrip("```json").rstrip("```").strip()
    return json.loads(raw)

products = [
    "iPhone 15 Pro",
    "Nike Air Max",
    "Organic almonds",
    "Standing desk",
    "Tennis racket",
    "MacBook Air",
    "Levi's jeans",
    "Protein powder",
    "Coffee maker",
    "Yoga mat",
    "AirPods Pro",
    "Rain jacket",
]

all_results = []
for i in range(0, len(products), ITEMS_PER_BATCH):
    batch   = products[i:i + ITEMS_PER_BATCH]
    results = classify_batch_cached(batch)
    all_results.extend(results)

for r in all_results:
    idx = r["index"] - 1
    print(f"{products[idx]:25s} → {r['category']}")
```

**Expected Token Savings:** Batching + prompt caching combined: 90% reduction on shared taxonomy tokens AND fewer round-trips.
**Environment:** Classification tasks with a large shared taxonomy or knowledge base; the combination is highly cost-effective.

---

### Option 5 — Async batching queue: accumulate then flush

```python
import asyncio
import json
import anthropic
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

FLUSH_SIZE      = 8
FLUSH_TIMEOUT   = 0.5  # seconds

@dataclass
class BatchQueue:
    _items:   list[tuple[str, asyncio.Future]] = field(default_factory=list)
    _lock:    asyncio.Lock                     = field(default_factory=asyncio.Lock)
    _timer:   asyncio.Task | None              = None

    async def submit(self, text: str) -> str:
        loop   = asyncio.get_event_loop()
        future = loop.create_future()
        async with self._lock:
            self._items.append((text, future))
            if len(self._items) >= FLUSH_SIZE:
                await self._flush()
            elif self._timer is None:
                self._timer = asyncio.ensure_future(self._timeout_flush())
        return await future

    async def _timeout_flush(self):
        await asyncio.sleep(FLUSH_TIMEOUT)
        async with self._lock:
            if self._items:
                await self._flush()
            self._timer = None

    async def _flush(self):
        batch  = list(self._items)
        self._items.clear()
        if self._timer:
            self._timer.cancel()
            self._timer = None

        texts    = [t for t, _ in batch]
        futures  = [f for _, f in batch]
        numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts))
        prompt   = f"Classify as positive/neutral/negative. JSON array: [{{\"index\":1,\"sentiment\":\"...\"}},...]\n{numbered}"

        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=len(texts) * 30,
                messages=[{"role": "user", "content": prompt}],
            )
            raw     = response.content[0].text.strip().lstrip("```json").rstrip("```").strip()
            results = json.loads(raw)
            mapping = {r["index"]: r["sentiment"] for r in results}
            for i, future in enumerate(futures):
                future.set_result(mapping.get(i + 1, "unknown"))
        except Exception as e:
            for future in futures:
                if not future.done():
                    future.set_exception(e)

queue = BatchQueue()

async def main():
    texts = [f"This product is {'great' if i%2==0 else 'terrible'}. Item {i}." for i in range(20)]
    results = await asyncio.gather(*[queue.submit(t) for t in texts])
    for text, sentiment in zip(texts, results):
        print(f"{text[:50]!r} → {sentiment}")

asyncio.run(main())
```

**Expected Token Savings:** Items arriving within the timeout window are batched automatically; producers don't need to know about batching.
**Environment:** High-throughput async services where callers submit items independently.

---

### Option 6 — Map-reduce: batch process then aggregate

```python
import asyncio
import json
import anthropic

client = anthropic.AsyncAnthropic()

CHUNK_SIZE = 5

async def map_chunk(chunk: list[str], chunk_id: int) -> list[dict]:
    """Map: extract entities from a chunk of documents."""
    numbered = "\n".join(f"Doc {i+1}: {text}" for i, text in enumerate(chunk))
    prompt   = (
        "Extract named entities from each document. "
        "Return JSON array: [{\"doc\": 1, \"entities\": [\"name1\", ...]}, ...]\n\n"
        + numbered
    )
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=len(chunk) * 80,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip().lstrip("```json").rstrip("```").strip()
    results = json.loads(raw)
    print(f"[map] chunk {chunk_id} → {len(results)} results")
    return results

async def reduce_entities(all_results: list[list[dict]]) -> dict:
    """Reduce: count entity frequency across all chunks."""
    flat = [entity for chunk in all_results for item in chunk for entity in item.get("entities", [])]
    freq: dict[str, int] = {}
    for entity in flat:
        freq[entity] = freq.get(entity, 0) + 1
    return dict(sorted(freq.items(), key=lambda x: x[1], reverse=True)[:10])

async def main():
    documents = [
        "Apple and Google announced a new partnership.",
        "Elon Musk visited Tesla's Berlin factory.",
        "Microsoft acquired Activision Blizzard.",
        "Apple's Tim Cook met with Google CEO Sundar Pichai.",
        "Amazon Web Services expanded in Berlin.",
        "OpenAI and Microsoft extended their partnership.",
        "Google DeepMind published a new research paper.",
        "Elon Musk's SpaceX launched a new rocket.",
        "Apple released the iPhone 16 in Berlin.",
        "Amazon acquired a European startup.",
    ]

    chunks = [documents[i:i+CHUNK_SIZE] for i in range(0, len(documents), CHUNK_SIZE)]
    chunk_results = await asyncio.gather(*[map_chunk(c, i) for i, c in enumerate(chunks)])
    top_entities  = await reduce_entities(chunk_results)

    print("\nTop entities:")
    for entity, count in top_entities.items():
        print(f"  {entity}: {count}")

asyncio.run(main())
```

**Expected Token Savings:** Map phase uses batched chunks (5 docs → 1 call instead of 5); reduce phase uses 1 aggregation call; total calls = N/5 + 1 vs. N.
**Environment:** Document processing pipelines; NER, summarisation, keyword extraction over large corpora.

---

## Comparison

| Option | Approach | Concurrency | Latency | Best For |
|---|---|---|---|---|
| 1. Single-prompt batch | N items → 1 call | No | Low | Short uniform items |
| 2. Async semaphore | 1 call/item, parallel | Yes | Very low | Items needing independent context |
| 3. Adaptive batching | Variable batch size | No | Low | Mixed-length items |
| 4. Cache + batch | Batch + cached system | No | Low | Shared taxonomy/reference |
| 5. Async queue | Auto-flush on size/timeout | Yes | Near-zero | High-throughput streaming services |
| 6. Map-reduce | Chunk → extract → aggregate | Yes | Low | Large document corpora |
