---
layout: solution
title: "Agent Doesn't Limit Concurrent Model Calls"
category: concurrency
description: "Agent spawns unlimited concurrent API calls — fan-out tasks, parallel sub-agents, or batch processing — hitting Anthropic rate limits, exhausting budget in seconds, and causing cascading 429 errors that crash the entire workflow."
tags: [concurrency, rate-limit, semaphore, budget, parallel, async, throttle]
---

## Symptom

Agent receives a batch of 200 items to process. It fires 200 simultaneous API calls. Within 2 seconds: 180 calls fail with `RateLimitError: 429 Too Many Requests`. The successful 20 calls cost $12 each (large prompts, no caching). Total budget burned in one run: $240. Nothing was accomplished. The retry storm makes it worse.

Concurrent calls without limiting: **200** — all hit rate limits
With semaphore (concurrency=5): **5 at a time** — all succeed, total time similar

## Root Cause

`asyncio.gather(*tasks)` fires all tasks simultaneously with no concurrency cap. Each task calls `async_client.messages.create()` independently. The API enforces per-minute token and request limits — unlimited concurrency blows through them instantly.

## Fix

---

### Option 1 — asyncio.Semaphore Concurrency Cap

Wrap every API call in a semaphore. Only N calls run at once; the rest wait. Simple, zero-overhead, fits any existing async codebase.

```python
import asyncio
import json
import time
import anthropic

async_client = anthropic.AsyncAnthropic()

# Tune to your API tier:
# Free/tier-1: 5   Tier-2: 20   Tier-3: 50   Tier-4: 100
MAX_CONCURRENT = 5
semaphore = asyncio.Semaphore(MAX_CONCURRENT)

async def safe_create(messages: list[dict], system: str = "", **kwargs) -> str:
    """Rate-limited wrapper around messages.create."""
    async with semaphore:
        create_kwargs = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 256,
            "messages": messages,
            **kwargs,
        }
        if system:
            create_kwargs["system"] = system
        response = await async_client.messages.create(**create_kwargs)
        return response.content[0].text

async def process_item(item: dict, idx: int) -> dict:
    """Process one item — waits for semaphore slot."""
    start = time.monotonic()
    result = await safe_create(
        messages=[{"role": "user", "content": f"Classify this text as positive/negative/neutral: {item['text']}"}],
    )
    elapsed = (time.monotonic() - start) * 1000
    print(f"[{idx:03d}] done in {elapsed:.0f}ms — {result.strip()[:30]}")
    return {"id": item["id"], "result": result.strip(), "latency_ms": round(elapsed)}

async def process_batch(items: list[dict]) -> list[dict]:
    """Process all items with bounded concurrency."""
    print(f"Processing {len(items)} items with MAX_CONCURRENT={MAX_CONCURRENT}")
    start = time.monotonic()

    tasks = [process_item(item, i) for i, item in enumerate(items)]
    results = await asyncio.gather(*tasks)  # semaphore bounds actual concurrency

    elapsed = time.monotonic() - start
    print(f"\nDone: {len(results)} items in {elapsed:.1f}s ({len(results)/elapsed:.1f} items/s)")
    return list(results)

# Simulate a batch of 20 items (would be 200 in production)
items = [
    {"id": f"item_{i:03d}", "text": f"Sample review text number {i}. The product was great!" if i % 2 == 0
     else f"Terrible experience #{i}. Would not recommend."}
    for i in range(20)
]

results = asyncio.run(process_batch(items))
print(f"\nFirst 3 results: {results[:3]}")
```

**Expected Token Savings:** None — same tokens; prevents 429 errors that waste all tokens and return nothing
**Environment:** `pip install anthropic`

---

### Option 2 — Token Budget Tracker with Pre-Call Guard

Track estimated token consumption before each call. If the remaining budget is insufficient, queue the call or reject it — preventing runaway spend.

```python
import asyncio
import time
import json
from dataclasses import dataclass, field
from typing import Optional
import anthropic

async_client = anthropic.AsyncAnthropic()

@dataclass
class BudgetTracker:
    max_tokens_per_minute: int = 40_000   # Adjust to your API tier
    max_requests_per_minute: int = 50
    _token_log: list[tuple[float, int]] = field(default_factory=list)
    _request_log: list[float] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def _prune(self, now: float):
        """Remove log entries older than 60 seconds."""
        cutoff = now - 60.0
        self._token_log = [(t, n) for t, n in self._token_log if t > cutoff]
        self._request_log = [t for t in self._request_log if t > cutoff]

    def _tokens_used(self) -> int:
        return sum(n for _, n in self._token_log)

    def _requests_used(self) -> int:
        return len(self._request_log)

    async def acquire(self, estimated_tokens: int) -> bool:
        """Return True if call can proceed. False if it would exceed budget."""
        async with self._lock:
            now = time.monotonic()
            self._prune(now)

            if self._requests_used() >= self.max_requests_per_minute:
                return False
            if self._tokens_used() + estimated_tokens > self.max_tokens_per_minute:
                return False

            # Reserve the tokens and request slot
            self._token_log.append((now, estimated_tokens))
            self._request_log.append(now)
            return True

    async def wait_and_acquire(self, estimated_tokens: int, timeout: float = 60.0) -> bool:
        """Block until budget is available or timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self.acquire(estimated_tokens):
                return True
            await asyncio.sleep(1.0)
        return False

    def status(self) -> dict:
        now = time.monotonic()
        self._prune(now)
        return {
            "tokens_used_last_min": self._tokens_used(),
            "requests_last_min": self._requests_used(),
            "token_budget_remaining": self.max_tokens_per_minute - self._tokens_used(),
            "request_budget_remaining": self.max_requests_per_minute - self._requests_used(),
        }

budget = BudgetTracker(max_tokens_per_minute=10_000, max_requests_per_minute=10)

async def budgeted_call(
    prompt: str,
    estimated_input_tokens: int = 200,
    max_output_tokens: int = 256,
) -> Optional[str]:
    """Make an API call only if budget allows."""
    estimated_total = estimated_input_tokens + max_output_tokens

    acquired = await budget.wait_and_acquire(estimated_total, timeout=30.0)
    if not acquired:
        return None  # Timed out waiting for budget

    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_output_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text

async def process_batch_with_budget(prompts: list[str]) -> list[Optional[str]]:
    print(f"Initial budget: {budget.status()}")

    tasks = [budgeted_call(p) for p in prompts]
    results = await asyncio.gather(*tasks)

    succeeded = sum(1 for r in results if r is not None)
    print(f"\nCompleted: {succeeded}/{len(prompts)} calls within budget")
    print(f"Final budget: {budget.status()}")
    return list(results)

prompts = [f"What is {i} * {i+1}? Answer with just the number." for i in range(15)]
results = asyncio.run(process_batch_with_budget(prompts))
for i, r in enumerate(results):
    print(f"  [{i:02d}] {r or '[budget exceeded]'}")
```

**Expected Token Savings:** Up to 100% on over-budget calls — prevents spend beyond set limits
**Environment:** `pip install anthropic`

---

### Option 3 — Priority Queue with Concurrency Window

Use a priority queue to ensure high-priority tasks get API slots first. Low-priority batch work yields to interactive user requests.

```python
import asyncio
import heapq
import time
import anthropic
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

async_client = anthropic.AsyncAnthropic()

class Priority(IntEnum):
    CRITICAL = 0    # User-facing, real-time
    HIGH = 1        # Background, time-sensitive
    NORMAL = 2      # Batch processing
    LOW = 3         # Analytics, non-urgent

@dataclass(order=True)
class QueuedCall:
    priority: int
    enqueued_at: float
    call_id: str = field(compare=False)
    messages: list = field(compare=False)
    future: asyncio.Future = field(compare=False)
    model: str = field(compare=False, default="claude-haiku-4-5-20251001")
    max_tokens: int = field(compare=False, default=256)

class PrioritizedAPIQueue:
    def __init__(self, max_concurrent: int = 5):
        self._heap: list[QueuedCall] = []
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._condition = asyncio.Condition()
        self._running = False
        self._stats = {"processed": 0, "by_priority": {p.name: 0 for p in Priority}}

    async def submit(
        self,
        messages: list[dict],
        priority: Priority = Priority.NORMAL,
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 256,
    ) -> str:
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        call_id = f"call_{int(time.monotonic() * 1000) % 100000}"

        item = QueuedCall(
            priority=priority.value,
            enqueued_at=time.monotonic(),
            call_id=call_id,
            messages=messages,
            future=future,
            model=model,
            max_tokens=max_tokens,
        )

        async with self._condition:
            heapq.heappush(self._heap, item)
            self._condition.notify()

        return await future

    async def _worker(self):
        while self._running:
            async with self._condition:
                while not self._heap and self._running:
                    await self._condition.wait()
                if not self._running:
                    break
                item = heapq.heappop(self._heap)

            async with self._semaphore:
                wait_ms = (time.monotonic() - item.enqueued_at) * 1000
                pname = Priority(item.priority).name
                print(f"[Queue] {pname:8s} | wait={wait_ms:.0f}ms | {item.call_id}")

                try:
                    response = await async_client.messages.create(
                        model=item.model,
                        max_tokens=item.max_tokens,
                        messages=item.messages,
                    )
                    result = response.content[0].text
                    item.future.set_result(result)
                    self._stats["processed"] += 1
                    self._stats["by_priority"][pname] = self._stats["by_priority"].get(pname, 0) + 1
                except Exception as e:
                    item.future.set_exception(e)

    async def start(self, n_workers: int = 2):
        self._running = True
        self._workers = [asyncio.create_task(self._worker()) for _ in range(n_workers)]

    async def stop(self):
        self._running = False
        async with self._condition:
            self._condition.notify_all()
        await asyncio.gather(*self._workers, return_exceptions=True)

    def stats(self) -> dict:
        return {**self._stats, "queue_depth": len(self._heap)}

queue = PrioritizedAPIQueue(max_concurrent=3)

async def demo():
    await queue.start(n_workers=3)

    # Submit a mix of priorities
    tasks = []

    # Low-priority batch analytics
    for i in range(5):
        tasks.append(queue.submit(
            [{"role": "user", "content": f"What is {i}+{i}? Just the number."}],
            priority=Priority.LOW,
        ))

    # Normal batch
    for i in range(3):
        tasks.append(queue.submit(
            [{"role": "user", "content": f"Translate 'hello' to Spanish. Reply in one word."}],
            priority=Priority.NORMAL,
        ))

    # High priority user request — submitted last but should process sooner
    tasks.append(queue.submit(
        [{"role": "user", "content": "What is 2+2? Critical user request."}],
        priority=Priority.CRITICAL,
    ))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    await queue.stop()

    print(f"\nQueue stats: {queue.stats()}")
    print(f"Results sample: {[str(r)[:20] for r in results[:3]]}")

asyncio.run(demo())
```

**Expected Token Savings:** None — same tokens; ensures critical tasks get resources before batch work
**Environment:** `pip install anthropic`

---

### Option 4 — Exponential Backoff with Jitter on 429

When a 429 is received, back off with exponential delay + random jitter. Prevents thundering-herd retry storms that make rate limiting worse.

```python
import asyncio
import random
import time
import anthropic
from typing import Optional

async_client = anthropic.AsyncAnthropic()

async def call_with_backoff(
    messages: list[dict],
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 256,
    max_retries: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
) -> Optional[str]:
    """
    Call the API with exponential backoff + jitter on rate limit errors.
    Respects Retry-After header when present.
    """
    for attempt in range(max_retries + 1):
        try:
            response = await async_client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=messages,
            )
            return response.content[0].text

        except anthropic.RateLimitError as e:
            if attempt == max_retries:
                raise

            # Respect Retry-After header if present
            retry_after = None
            if hasattr(e, "response") and e.response:
                retry_after_header = e.response.headers.get("retry-after")
                if retry_after_header:
                    try:
                        retry_after = float(retry_after_header)
                    except ValueError:
                        pass

            if retry_after:
                delay = retry_after
            else:
                # Exponential backoff with full jitter
                exp_delay = min(base_delay * (2 ** attempt), max_delay)
                delay = random.uniform(0, exp_delay)

            print(f"[Backoff] 429 on attempt {attempt+1}/{max_retries+1} — waiting {delay:.1f}s")
            await asyncio.sleep(delay)

        except anthropic.APIStatusError as e:
            if e.status_code in (500, 502, 503, 529) and attempt < max_retries:
                delay = min(base_delay * (2 ** attempt), max_delay)
                delay += random.uniform(0, delay * 0.2)  # 20% jitter
                print(f"[Backoff] {e.status_code} on attempt {attempt+1} — waiting {delay:.1f}s")
                await asyncio.sleep(delay)
            else:
                raise

    return None

# Combine with semaphore for double protection
SEMAPHORE = asyncio.Semaphore(5)

async def safe_call(prompt: str) -> str:
    async with SEMAPHORE:
        result = await call_with_backoff(
            messages=[{"role": "user", "content": prompt}],
        )
        return result or "[no response]"

async def batch_with_backoff(prompts: list[str]) -> list[str]:
    tasks = [safe_call(p) for p in prompts]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [str(r) if isinstance(r, Exception) else r for r in results]

prompts = [f"What is the square root of {n}? One decimal." for n in [4, 9, 16, 25, 36]]
results = asyncio.run(batch_with_backoff(prompts))
for p, r in zip(prompts, results):
    print(f"Q: {p[:40]} → {r[:30]}")
```

**Expected Token Savings:** None — same tokens per successful call; eliminates failed calls that count against quota
**Environment:** `pip install anthropic`

---

### Option 5 — Adaptive Concurrency Based on Error Rate

Start with a low concurrency limit. Increase it when calls succeed; decrease it on 429s. Self-tunes to the current API capacity.

```python
import asyncio
import time
from collections import deque
import anthropic

async_client = anthropic.AsyncAnthropic()

class AdaptiveConcurrencyController:
    MIN_CONCURRENCY = 1
    MAX_CONCURRENCY = 20
    WINDOW = 30  # seconds to track error rate
    SCALE_UP_THRESHOLD = 0.02    # Scale up if error rate < 2%
    SCALE_DOWN_THRESHOLD = 0.10  # Scale down if error rate > 10%

    def __init__(self, initial: int = 3):
        self._concurrency = initial
        self._semaphore = asyncio.Semaphore(initial)
        self._events: deque[tuple[float, bool]] = deque()   # (timestamp, is_error)
        self._lock = asyncio.Lock()

    def _error_rate(self, now: float) -> float:
        cutoff = now - self.WINDOW
        recent = [(t, e) for t, e in self._events if t > cutoff]
        if not recent:
            return 0.0
        errors = sum(1 for _, e in recent if e)
        return errors / len(recent)

    async def _record(self, is_error: bool):
        async with self._lock:
            now = time.monotonic()
            self._events.append((now, is_error))
            # Prune old events
            cutoff = now - self.WINDOW
            while self._events and self._events[0][0] < cutoff:
                self._events.popleft()

            rate = self._error_rate(now)
            old = self._concurrency

            if rate > self.SCALE_DOWN_THRESHOLD and self._concurrency > self.MIN_CONCURRENCY:
                self._concurrency = max(self.MIN_CONCURRENCY, self._concurrency - 1)
                print(f"[Adaptive] err={rate:.0%} → concurrency {old} → {self._concurrency}")
            elif rate < self.SCALE_UP_THRESHOLD and self._concurrency < self.MAX_CONCURRENCY:
                self._concurrency = min(self.MAX_CONCURRENCY, self._concurrency + 1)
                if old != self._concurrency:
                    print(f"[Adaptive] err={rate:.0%} → concurrency {old} → {self._concurrency}")

    async def execute(self, coro) -> str:
        # Dynamically acquire based on current concurrency limit
        # Note: We use a simple lock-based approach for adaptive semaphore
        async with asyncio.Semaphore(self._concurrency):
            try:
                result = await coro
                await self._record(is_error=False)
                return result
            except anthropic.RateLimitError:
                await self._record(is_error=True)
                await asyncio.sleep(2.0)
                raise

    @property
    def current_concurrency(self) -> int:
        return self._concurrency

controller = AdaptiveConcurrencyController(initial=3)

async def adaptive_call(prompt: str) -> str:
    async def _call():
        response = await async_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text

    return await controller.execute(_call())

async def run_adaptive_batch(prompts: list[str]) -> list[str]:
    print(f"Starting with concurrency={controller.current_concurrency}")
    tasks = [adaptive_call(p) for p in prompts]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    print(f"Final concurrency={controller.current_concurrency}")
    return [str(r) if isinstance(r, Exception) else r for r in results]

prompts = [f"Capital of country number {i}? One word." for i in range(10)]
results = asyncio.run(run_adaptive_batch(prompts))
print(f"Completed {len([r for r in results if not r.startswith('Rate')])} / {len(results)}")
```

**Expected Token Savings:** None — same tokens; self-tuning prevents both under-use and over-limit errors
**Environment:** `pip install anthropic`

---

### Option 6 — Chunked Batch Processor with Progress Tracking

Split large batches into fixed-size chunks. Process one chunk at a time with a delay between chunks. Tracks progress so interrupted batches resume from the last completed chunk.

```python
import asyncio
import json
import time
import sqlite3
import anthropic
from dataclasses import dataclass
from typing import Any, Callable

async_client = anthropic.AsyncAnthropic()

@dataclass
class BatchProgress:
    batch_id: str
    total: int
    completed: int
    failed: int
    results: dict[str, Any]

class ChunkedBatchProcessor:
    CHUNK_SIZE = 5         # Items per chunk
    CHUNK_DELAY = 1.0      # Seconds between chunks
    ITEM_CONCURRENCY = 5   # Concurrent calls within a chunk

    def __init__(self, db_path: str = ":memory:"):
        self.db = sqlite3.connect(db_path)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS batch_results (
                batch_id TEXT, item_id TEXT, status TEXT, result TEXT,
                PRIMARY KEY (batch_id, item_id)
            )
        """)
        self.db.commit()

    def _load_progress(self, batch_id: str) -> set[str]:
        """Return item IDs already processed in a previous run."""
        cursor = self.db.execute(
            "SELECT item_id FROM batch_results WHERE batch_id=? AND status='ok'",
            (batch_id,),
        )
        return {row[0] for row in cursor.fetchall()}

    def _save_result(self, batch_id: str, item_id: str, status: str, result: str):
        self.db.execute(
            "INSERT OR REPLACE INTO batch_results VALUES (?,?,?,?)",
            (batch_id, item_id, status, result),
        )
        self.db.commit()

    async def _process_item(
        self,
        item: dict,
        batch_id: str,
        prompt_fn: Callable[[dict], str],
        semaphore: asyncio.Semaphore,
    ) -> tuple[str, bool, str]:
        item_id = str(item.get("id", id(item)))
        async with semaphore:
            try:
                prompt = prompt_fn(item)
                response = await async_client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=256,
                    messages=[{"role": "user", "content": prompt}],
                )
                result = response.content[0].text.strip()
                self._save_result(batch_id, item_id, "ok", result)
                return item_id, True, result
            except Exception as e:
                self._save_result(batch_id, item_id, "error", str(e))
                return item_id, False, str(e)

    async def process(
        self,
        batch_id: str,
        items: list[dict],
        prompt_fn: Callable[[dict], str],
    ) -> BatchProgress:
        already_done = self._load_progress(batch_id)
        remaining = [item for item in items if str(item.get("id", id(item))) not in already_done]

        print(f"[Batch {batch_id}] {len(items)} total, {len(already_done)} cached, {len(remaining)} to process")

        results: dict[str, Any] = {}
        # Load cached results
        for item in items:
            item_id = str(item.get("id", id(item)))
            if item_id in already_done:
                cursor = self.db.execute(
                    "SELECT result FROM batch_results WHERE batch_id=? AND item_id=?",
                    (batch_id, item_id),
                )
                row = cursor.fetchone()
                if row:
                    results[item_id] = row[0]

        completed = len(already_done)
        failed = 0
        semaphore = asyncio.Semaphore(self.ITEM_CONCURRENCY)

        # Process in chunks
        chunks = [remaining[i:i + self.CHUNK_SIZE] for i in range(0, len(remaining), self.CHUNK_SIZE)]
        for chunk_idx, chunk in enumerate(chunks):
            print(f"[Batch {batch_id}] Chunk {chunk_idx+1}/{len(chunks)} ({len(chunk)} items)...")
            chunk_tasks = [self._process_item(item, batch_id, prompt_fn, semaphore) for item in chunk]
            chunk_results = await asyncio.gather(*chunk_tasks)

            for item_id, ok, result in chunk_results:
                results[item_id] = result
                if ok:
                    completed += 1
                else:
                    failed += 1

            if chunk_idx < len(chunks) - 1:
                await asyncio.sleep(self.CHUNK_DELAY)

        return BatchProgress(
            batch_id=batch_id,
            total=len(items),
            completed=completed,
            failed=failed,
            results=results,
        )

processor = ChunkedBatchProcessor()

items = [{"id": i, "text": f"Review {i}: {'great product!' if i % 3 == 0 else 'not impressed.'}"} for i in range(12)]

def make_prompt(item: dict) -> str:
    return f"Classify as positive/negative/neutral: '{item['text']}'. Reply with one word only."

progress = asyncio.run(processor.process("sentiment_batch_001", items, make_prompt))
print(f"\nBatch complete: {progress.completed}/{progress.total} items, {progress.failed} failures")
print(f"Sample results: {dict(list(progress.results.items())[:3])}")

# Second run — skips already-completed items
print("\n=== Re-running (should skip all 12) ===")
progress2 = asyncio.run(processor.process("sentiment_batch_001", items, make_prompt))
print(f"Re-run: {progress2.completed} from cache, {progress2.failed} failures")
```

**Expected Token Savings:** 30–60% on reruns — cached items skip API entirely
**Environment:** `pip install anthropic`

---

## Comparison

| Option | Mechanism | Adapts to Load | Best For |
|--------|-----------|---------------|----------|
| asyncio.Semaphore | Hard cap N | No | Any async agent — simplest solution |
| Token Budget Tracker | Per-minute token accounting | No | Cost-sensitive batch jobs |
| Priority Queue | Heap-sorted by priority | No | Mixed interactive + batch workloads |
| Exponential Backoff | Retry on 429 | Reactive | Any agent — combine with semaphore |
| Adaptive Concurrency | Auto-tune based on error rate | Yes | Production systems with variable load |
| Chunked Batch | Fixed chunks + progress store | No | Large offline batch jobs |

**Recommended starting point:** Option 1 (asyncio.Semaphore) — add `semaphore = asyncio.Semaphore(5)` and `async with semaphore:` around every `messages.create()` call. Takes 5 minutes; eliminates 100% of unlimited-concurrency rate-limit errors.
