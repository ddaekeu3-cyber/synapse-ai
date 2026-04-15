---
layout: solution
title: "Agent Doesn't Implement Parallel Batch API Calls"
category: token-cost
description: "Agents that process lists of items sequentially (one Claude call per item) are 5-20x slower and pay retry overhead on each call independently. Parallel batching fans out multiple API calls concurrently, reducing wall time proportionally to the batch size while respecting rate limits."
tags: [token-cost, batching, parallel, asyncio, concurrency, throughput, rate-limit, performance]
---

## Problem

Sequential processing of N items takes N × (latency per call) seconds. For 20 items at 2s each, that's 40 seconds. Parallel batching reduces this to ~2 seconds (limited by the slowest call) with the same total token cost. Agents without parallel batching waste user time, hold locks longer, and hit rate limits more unevenly. Proper parallel batching includes concurrency limits to respect Anthropic's RPM/TPM quotas.

## Solutions

### Option 1: asyncio.gather with Semaphore Concurrency Limit

```python
import anthropic
import asyncio
import time

client = anthropic.AsyncAnthropic()

async def process_item(sem: asyncio.Semaphore, item: str, idx: int) -> dict:
    async with sem:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": f"Classify as positive/negative/neutral: '{item}'"}],
        )
        return {
            "idx": idx,
            "item": item,
            "result": resp.content[0].text.strip(),
            "tokens": resp.usage.input_tokens + resp.usage.output_tokens,
        }

async def batch_classify(items: list[str], max_concurrent: int = 5) -> list[dict]:
    sem = asyncio.Semaphore(max_concurrent)
    tasks = [process_item(sem, item, i) for i, item in enumerate(items)]
    return await asyncio.gather(*tasks)

def compare_sequential_vs_parallel(items: list[str]):
    # Sequential estimate (don't actually run to save tokens)
    avg_latency = 1.5  # seconds per call
    sequential_est = len(items) * avg_latency

    print(f"Items: {len(items)}")
    print(f"Sequential estimate: {sequential_est:.0f}s")

    t0 = time.time()
    results = asyncio.run(batch_classify(items, max_concurrent=5))
    elapsed = time.time() - t0

    total_tokens = sum(r["tokens"] for r in results)
    print(f"Parallel actual: {elapsed:.1f}s")
    print(f"Speedup: {sequential_est/elapsed:.1f}x")
    print(f"Total tokens: {total_tokens}")
    return results

if __name__ == "__main__":
    texts = [
        "The product exceeded all my expectations!",
        "Delivery was late and packaging was damaged.",
        "It's okay, nothing special.",
        "Absolutely love this, will buy again.",
        "Terrible customer service, never again.",
        "Works as described.",
        "Best purchase I've made this year!",
        "The quality is disappointing for the price.",
    ]
    results = compare_sequential_vs_parallel(texts)
    for r in results:
        print(f"  [{r['idx']}] {r['result'][:20]:20s} | {r['item'][:40]}")

# Expected Token Savings: same total tokens as sequential; parallel reduces wall time 3-5x with 5 concurrent slots
# Environment: classification, extraction, summarization pipelines; semaphore prevents RPM limit violations
```

### Option 2: Chunked Batch Processing with Progress Tracking

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class BatchProgress:
    total: int
    completed: int = 0
    failed: int = 0
    start_time: float = field(default_factory=time.time)
    total_tokens: int = 0

    def record(self, tokens: int = 0, success: bool = True):
        self.completed += 1
        if not success:
            self.failed += 1
        self.total_tokens += tokens

    @property
    def rate(self) -> float:
        elapsed = time.time() - self.start_time
        return self.completed / elapsed if elapsed > 0 else 0

    @property
    def eta_seconds(self) -> float:
        remaining = self.total - self.completed
        return remaining / self.rate if self.rate > 0 else float("inf")

    def __str__(self) -> str:
        pct = self.completed / self.total * 100
        eta = f"{self.eta_seconds:.0f}s" if self.eta_seconds < 3600 else "?"
        return (
            f"  Progress: {self.completed}/{self.total} ({pct:.0f}%) | "
            f"rate={self.rate:.1f}/s | ETA={eta} | tokens={self.total_tokens} | failed={self.failed}"
        )

async def process_one(item: dict, progress: BatchProgress, sem: asyncio.Semaphore) -> dict:
    async with sem:
        try:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": item["prompt"]}],
            )
            result = resp.content[0].text.strip()
            tokens = resp.usage.input_tokens + resp.usage.output_tokens
            progress.record(tokens=tokens, success=True)
            return {"id": item["id"], "result": result, "ok": True}
        except Exception as e:
            progress.record(success=False)
            return {"id": item["id"], "error": str(e), "ok": False}

async def batch_process(
    items: list[dict],
    max_concurrent: int = 5,
    chunk_size: int = 20,
    progress_interval: int = 5,
) -> list[dict]:
    progress = BatchProgress(total=len(items))
    results = []
    sem = asyncio.Semaphore(max_concurrent)

    for i in range(0, len(items), chunk_size):
        chunk = items[i:i + chunk_size]
        chunk_results = await asyncio.gather(*[process_one(item, progress, sem) for item in chunk])
        results.extend(chunk_results)
        if (i // chunk_size + 1) % progress_interval == 0 or i + chunk_size >= len(items):
            print(str(progress))

    return results

if __name__ == "__main__":
    items = [
        {"id": f"item_{i}", "prompt": f"What is {i} squared? Answer with just the number."}
        for i in range(1, 21)
    ]
    t0 = time.time()
    results = asyncio.run(batch_process(items, max_concurrent=5, chunk_size=10))
    elapsed = time.time() - t0

    ok = sum(1 for r in results if r["ok"])
    print(f"\nCompleted {ok}/{len(results)} in {elapsed:.1f}s")
    for r in results[:5]:
        print(f"  {r['id']}: {r.get('result', r.get('error', ''))[:30]}")

# Expected Token Savings: chunked processing prevents memory spikes; progress tracking catches cost overruns early
# Environment: large batch jobs (100+ items); chunk_size controls memory; ETA helps cost forecasting
```

### Option 3: Fan-Out with Result Aggregation and Cost Report

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

# Haiku pricing (per million tokens)
HAIKU_INPUT_PRICE = 0.80
HAIKU_OUTPUT_PRICE = 4.00

@dataclass
class CallResult:
    item_id: str
    result: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    error: str = ""

    @property
    def cost_usd(self) -> float:
        return (
            self.input_tokens * HAIKU_INPUT_PRICE
            + self.output_tokens * HAIKU_OUTPUT_PRICE
        ) / 1_000_000

async def fan_out_call(
    sem: asyncio.Semaphore,
    item_id: str,
    prompt: str,
    system: str = "",
    max_tokens: int = 128,
) -> CallResult:
    async with sem:
        t0 = time.time()
        try:
            kwargs = dict(
                model="claude-haiku-4-5-20251001",
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            if system:
                kwargs["system"] = system
            resp = await client.messages.create(**kwargs)
            return CallResult(
                item_id=item_id,
                result=resp.content[0].text.strip(),
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
                latency_ms=(time.time() - t0) * 1000,
            )
        except Exception as e:
            return CallResult(
                item_id=item_id, result="", input_tokens=0, output_tokens=0,
                latency_ms=(time.time() - t0) * 1000, error=str(e),
            )

def print_cost_report(results: list[CallResult], elapsed: float):
    total_input = sum(r.input_tokens for r in results)
    total_output = sum(r.output_tokens for r in results)
    total_cost = sum(r.cost_usd for r in results)
    avg_latency = sum(r.latency_ms for r in results) / len(results) if results else 0
    errors = [r for r in results if r.error]

    print(f"\n--- Batch Cost Report ---")
    print(f"  Items:        {len(results)}")
    print(f"  Wall time:    {elapsed:.1f}s")
    print(f"  Avg latency:  {avg_latency:.0f}ms per call")
    print(f"  Input tokens: {total_input:,}")
    print(f"  Output tokens:{total_output:,}")
    print(f"  Total cost:   ${total_cost:.5f}")
    print(f"  Cost/item:    ${total_cost/len(results):.6f}")
    if errors:
        print(f"  Errors:       {len(errors)}")

async def main():
    products = [
        ("p001", "Wireless mouse with 3-month battery life"),
        ("p002", "Standing desk with electric height adjustment"),
        ("p003", "Noise-canceling headphones with 30hr battery"),
        ("p004", "Mechanical keyboard with RGB backlighting"),
        ("p005", "4K monitor 27-inch IPS panel"),
        ("p006", "USB-C hub with 7 ports"),
        ("p007", "Webcam 1080p with built-in microphone"),
        ("p008", "Laptop stand adjustable aluminum"),
    ]
    SYSTEM = "Write a one-sentence marketing tagline for this product."

    sem = asyncio.Semaphore(4)
    t0 = time.time()
    results = await asyncio.gather(*[
        fan_out_call(sem, pid, desc, system=SYSTEM, max_tokens=64)
        for pid, desc in products
    ])
    elapsed = time.time() - t0

    for r in results:
        status = "OK" if not r.error else "ERR"
        print(f"  [{status}] {r.item_id}: {r.result[:60]}")

    print_cost_report(results, elapsed)

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: cost report reveals per-item spend; identify high-cost items to optimize prompts
# Environment: product catalogues, document processing; cost/item metric guides max_tokens tuning
```

### Option 4: Retry-Aware Parallel Batch with Exponential Backoff

```python
import anthropic
import asyncio
import random
import time

client = anthropic.AsyncAnthropic()

async def call_with_retry(
    sem: asyncio.Semaphore,
    item_id: str,
    prompt: str,
    max_retries: int = 3,
    base_delay: float = 1.0,
) -> dict:
    async with sem:
        last_err = None
        for attempt in range(max_retries + 1):
            try:
                resp = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=64,
                    messages=[{"role": "user", "content": prompt}],
                )
                return {
                    "id": item_id,
                    "result": resp.content[0].text.strip(),
                    "attempts": attempt + 1,
                    "tokens": resp.usage.input_tokens + resp.usage.output_tokens,
                }
            except anthropic.RateLimitError as e:
                last_err = e
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                    print(f"  [{item_id}] rate limit, retrying in {delay:.1f}s (attempt {attempt+1})")
                    await asyncio.sleep(delay)
            except anthropic.APIStatusError as e:
                if e.status_code >= 500 and attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    await asyncio.sleep(delay)
                    last_err = e
                else:
                    return {"id": item_id, "error": str(e), "attempts": attempt + 1, "tokens": 0}
            except Exception as e:
                return {"id": item_id, "error": str(e), "attempts": attempt + 1, "tokens": 0}
        return {"id": item_id, "error": str(last_err), "attempts": max_retries + 1, "tokens": 0}

async def resilient_batch(
    items: list[tuple[str, str]],  # [(id, prompt), ...]
    max_concurrent: int = 5,
) -> list[dict]:
    sem = asyncio.Semaphore(max_concurrent)
    return await asyncio.gather(*[
        call_with_retry(sem, item_id, prompt)
        for item_id, prompt in items
    ])

if __name__ == "__main__":
    items = [
        (f"q{i}", f"What is the {i}th prime number? Just the number.")
        for i in range(1, 11)
    ]
    t0 = time.time()
    results = asyncio.run(resilient_batch(items, max_concurrent=5))
    elapsed = time.time() - t0

    print(f"Completed {len(results)} items in {elapsed:.1f}s")
    multi_attempt = [r for r in results if r.get("attempts", 1) > 1]
    if multi_attempt:
        print(f"  Items needing retry: {len(multi_attempt)}")
    for r in results:
        if "error" not in r:
            print(f"  {r['id']}: {r['result'][:20]} (attempts={r['attempts']})")

# Expected Token Savings: per-item retry with jitter avoids synchronized retry storms; failed items don't block others
# Environment: production batches; semaphore + retry handles 429s without losing work for other items
```

### Option 5: Two-Stage Batch — Cheap Pre-Filter then Expensive Process

```python
import anthropic
import asyncio
import time

client = anthropic.AsyncAnthropic()

async def prefilter(sem: asyncio.Semaphore, item: dict) -> dict:
    """Stage 1: Haiku decides if item needs expensive processing."""
    async with sem:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8,
            messages=[{"role": "user", "content": (
                f"Does this text need detailed analysis? yes/no only.\n\n{item['text'][:200]}"
            )}],
        )
        needs_analysis = "yes" in resp.content[0].text.lower()
        return {**item, "needs_analysis": needs_analysis, "filter_tokens": resp.usage.input_tokens + resp.usage.output_tokens}

async def deep_analyze(sem: asyncio.Semaphore, item: dict) -> dict:
    """Stage 2: Sonnet does detailed analysis on filtered items."""
    async with sem:
        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            messages=[{"role": "user", "content": f"Analyze in detail:\n\n{item['text']}"}],
        )
        return {**item, "analysis": resp.content[0].text.strip(),
                "analysis_tokens": resp.usage.input_tokens + resp.usage.output_tokens}

async def two_stage_batch(items: list[dict], max_concurrent: int = 5) -> list[dict]:
    sem = asyncio.Semaphore(max_concurrent)

    # Stage 1: pre-filter all items with Haiku (cheap)
    filtered = await asyncio.gather(*[prefilter(sem, item) for item in items])

    needs_analysis = [item for item in filtered if item["needs_analysis"]]
    skipped = [item for item in filtered if not item["needs_analysis"]]

    total_filter_tokens = sum(i["filter_tokens"] for i in filtered)
    print(f"  Stage 1: {len(needs_analysis)}/{len(items)} items need analysis ({total_filter_tokens} filter tokens)")

    # Stage 2: deep analysis only on selected items (expensive)
    analyzed = await asyncio.gather(*[deep_analyze(sem, item) for item in needs_analysis])
    total_analysis_tokens = sum(i["analysis_tokens"] for i in analyzed)
    print(f"  Stage 2: {len(analyzed)} analyses ({total_analysis_tokens} analysis tokens)")

    # Merge results
    results = list(analyzed) + [{"analysis": "skipped (not needed)", **i} for i in skipped]
    results.sort(key=lambda x: x["id"])
    return results

if __name__ == "__main__":
    items = [
        {"id": 1, "text": "Hi there!"},
        {"id": 2, "text": "The financial report shows a 47% decline in Q3 revenue driven by supply chain disruptions and decreased consumer demand in APAC markets."},
        {"id": 3, "text": "ok"},
        {"id": 4, "text": "The clinical trial results indicate statistically significant improvement in patient outcomes with p<0.001, but the confidence intervals suggest further investigation is warranted."},
        {"id": 5, "text": "thanks"},
        {"id": 6, "text": "Market analysis reveals emerging opportunities in renewable energy sectors despite regulatory uncertainty."},
    ]

    t0 = time.time()
    results = asyncio.run(two_stage_batch(items))
    elapsed = time.time() - t0

    print(f"\nCompleted in {elapsed:.1f}s")
    for r in results:
        print(f"  [{r['id']}] {r['analysis'][:60]}")

# Expected Token Savings: 40-70% cost reduction by pre-filtering trivial items; Haiku filter cost ~0.001x Sonnet analysis cost
# Environment: mixed-content batches (support tickets, news feeds, logs); pre-filter routes cheap items away from expensive models
```

### Option 6: SQLite-Backed Batch Job with Resume on Failure

```python
import anthropic
import asyncio
import json
import sqlite3
import time
import uuid
from pathlib import Path

DB = Path("/tmp/batch_jobs.db")
client = anthropic.AsyncAnthropic()

def init_db():
    con = sqlite3.connect(DB)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS batch_items (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            prompt TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            result TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            error TEXT,
            processed_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_job_status ON batch_items(job_id, status);
    """)
    con.commit()
    con.close()

def create_job(prompts: list[str]) -> str:
    job_id = str(uuid.uuid4())[:8]
    con = sqlite3.connect(DB)
    for prompt in prompts:
        con.execute("""
            INSERT INTO batch_items (id, job_id, prompt)
            VALUES (?,?,?)
        """, (str(uuid.uuid4())[:8], job_id, prompt))
    con.commit()
    con.close()
    return job_id

def get_pending(job_id: str, limit: int = 50) -> list[dict]:
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT id, prompt FROM batch_items WHERE job_id=? AND status='pending' LIMIT ?",
        (job_id, limit),
    ).fetchall()
    con.close()
    return [{"id": r[0], "prompt": r[1]} for r in rows]

def save_result(item_id: str, result: str | None, input_tokens: int, output_tokens: int, error: str = ""):
    con = sqlite3.connect(DB)
    status = "done" if not error else "failed"
    con.execute("""
        UPDATE batch_items SET status=?, result=?, input_tokens=?, output_tokens=?, error=?, processed_at=?
        WHERE id=?
    """, (status, result, input_tokens, output_tokens, error, time.time(), item_id))
    con.commit()
    con.close()

async def process_item(sem: asyncio.Semaphore, item: dict):
    async with sem:
        try:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": item["prompt"]}],
            )
            save_result(item["id"], resp.content[0].text.strip(),
                        resp.usage.input_tokens, resp.usage.output_tokens)
        except Exception as e:
            save_result(item["id"], None, 0, 0, error=str(e))

async def run_job(job_id: str, max_concurrent: int = 5, batch_size: int = 10):
    sem = asyncio.Semaphore(max_concurrent)
    total_processed = 0
    while True:
        pending = get_pending(job_id, limit=batch_size)
        if not pending:
            break
        await asyncio.gather(*[process_item(sem, item) for item in pending])
        total_processed += len(pending)
        print(f"  [{job_id}] processed {total_processed} items so far")
    return total_processed

def job_summary(job_id: str) -> dict:
    con = sqlite3.connect(DB)
    rows = con.execute("""
        SELECT status, COUNT(*), SUM(input_tokens), SUM(output_tokens)
        FROM batch_items WHERE job_id=? GROUP BY status
    """, (job_id,)).fetchall()
    con.close()
    return {r[0]: {"count": r[1], "input_tokens": r[2], "output_tokens": r[3]} for r in rows}

if __name__ == "__main__":
    init_db()
    prompts = [f"What is {i} * {i+1}? Just the number." for i in range(1, 16)]

    job_id = create_job(prompts)
    print(f"Job created: {job_id}")

    t0 = time.time()
    asyncio.run(run_job(job_id))
    elapsed = time.time() - t0

    summary = job_summary(job_id)
    done = summary.get("done", {})
    print(f"\nJob {job_id} complete in {elapsed:.1f}s")
    print(f"  Done: {done.get('count', 0)} | Tokens: {done.get('input_tokens', 0)}in + {done.get('output_tokens', 0)}out")

# Expected Token Savings: resumable jobs avoid re-processing already-done items after failures; SQLite state survives crashes
# Environment: overnight batch jobs; re-run after partial failure resumes from checkpoint, not from scratch
```

## Comparison

| Option | Concurrency | Retry | Persistence | Cost Tracking | Best For |
|--------|------------|-------|-------------|--------------|---------|
| 1 — gather + Semaphore | asyncio gather | No | No | No | Simple parallel fan-out |
| 2 — Chunked + progress | asyncio gather | No | No | Per-run | Large batches with ETA |
| 3 — Fan-out + cost report | asyncio gather | No | No | Yes (USD) | Cost optimization analysis |
| 4 — Retry with backoff | asyncio gather | Yes (429-aware) | No | No | Production with rate limits |
| 5 — Two-stage pre-filter | asyncio gather | No | No | By model | Mixed-content cost reduction |
| 6 — SQLite resumable job | asyncio gather | No | Yes | Per-item | Long-running overnight jobs |
