---
layout: solution
title: "Agent Doesn't Implement Dynamic Batching for Concurrent Requests"
category: performance
description: "Collect concurrent requests arriving within a short time window and process them together — reducing per-request overhead, amortizing model startup cost, and improving throughput under burst load without increasing latency for individual callers."
tags: [performance, batching, concurrency, throughput, asyncio, python]
---

# Agent Doesn't Implement Dynamic Batching for Concurrent Requests

Agents that process every incoming request immediately in isolation waste latency budget on repeated API overhead when multiple requests arrive simultaneously. Dynamic batching collects requests in a short window, dispatches them together, and fans results back to waiting callers — improving throughput under burst load while keeping tail latency bounded.

## Option 1: Time-Window Batcher with asyncio.Queue

```python
import anthropic
import asyncio
import time

client = anthropic.AsyncAnthropic()

class DynamicBatcher:
    """Collect requests arriving within window_ms, dispatch as a batch."""
    def __init__(self, window_ms: float = 20.0, max_batch: int = 8):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._window  = window_ms / 1000
        self._max     = max_batch
        self._task: asyncio.Task | None = None

    def start(self):
        self._task = asyncio.create_task(self._dispatch_loop())

    async def submit(self, prompt: str) -> str:
        """Submit a prompt; await the result."""
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        await self._queue.put((prompt, future))
        return await future

    async def _dispatch_loop(self):
        while True:
            # Wait for first item
            first = await self._queue.get()
            batch = [first]
            deadline = time.monotonic() + self._window

            # Collect more within window
            while len(batch) < self._max:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                    batch.append(item)
                except asyncio.TimeoutError:
                    break

            await self._process_batch(batch)

    async def _process_batch(self, batch: list):
        prompts  = [p for p, _ in batch]
        futures  = [f for _, f in batch]
        print(f"  [BATCH] dispatching {len(batch)} requests")

        results = await asyncio.gather(*[
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": p}],
            )
            for p in prompts
        ], return_exceptions=True)

        for future, result in zip(futures, results):
            if isinstance(result, Exception):
                future.set_exception(result)
            else:
                future.set_result(result.content[0].text)

    async def stop(self):
        if self._task:
            self._task.cancel()

async def main():
    batcher = DynamicBatcher(window_ms=30, max_batch=6)
    batcher.start()

    # Simulate 10 concurrent requests arriving close together
    prompts = [f"What is Python feature #{i}?" for i in range(10)]
    t0 = time.monotonic()
    results = await asyncio.gather(*[batcher.submit(p) for p in prompts])
    elapsed = (time.monotonic() - t0) * 1000

    print(f"\n{len(results)} results in {elapsed:.0f}ms")
    for i, r in enumerate(results[:3]):
        print(f"  [{i}] {r[:50]!r}")

    await batcher.stop()

asyncio.run(main())

# Expected Token Savings: N parallel calls in one window vs N sequential = ~Nx speedup; overhead amortized across batch
# Environment: asyncio; window_ms=20-50ms works for LLM latency; max_batch bounded by API concurrency limits
```

## Option 2: Semaphore-Gated Batch Collector

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class BatchRequest:
    prompt: str
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_event_loop().create_future())

class SemaphoreBatcher:
    """
    Gate concurrent requests with a semaphore; when semaphore is full,
    flush accumulated requests as a batch.
    """
    def __init__(self, max_concurrent: int = 4, flush_interval_ms: float = 25.0):
        self._sem  = asyncio.Semaphore(max_concurrent)
        self._pending: list[BatchRequest] = []
        self._lock = asyncio.Lock()
        self._flush_interval = flush_interval_ms / 1000

    async def submit(self, prompt: str) -> str:
        req = BatchRequest(prompt=prompt)
        async with self._lock:
            self._pending.append(req)
            should_flush = len(self._pending) >= 4  # flush at 4 accumulated

        if should_flush:
            asyncio.create_task(self._flush())

        # Also schedule a time-based flush
        asyncio.get_event_loop().call_later(
            self._flush_interval, lambda: asyncio.create_task(self._flush())
        )
        return await req.future

    async def _flush(self):
        async with self._lock:
            if not self._pending:
                return
            batch = self._pending[:]
            self._pending.clear()

        if not batch:
            return
        print(f"  [FLUSH] {len(batch)} items")
        async with self._sem:
            results = await asyncio.gather(*[
                client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=64,
                    messages=[{"role": "user", "content": r.prompt}],
                )
                for r in batch
            ], return_exceptions=True)

        for req, result in zip(batch, results):
            if not req.future.done():
                if isinstance(result, Exception):
                    req.future.set_exception(result)
                else:
                    req.future.set_result(result.content[0].text)

async def main():
    batcher = SemaphoreBatcher(max_concurrent=4, flush_interval_ms=30)
    prompts = [f"Name one Python library starting with letter {chr(65+i)}." for i in range(8)]
    t0 = time.monotonic()
    results = await asyncio.gather(*[batcher.submit(p) for p in prompts])
    print(f"{len(results)} results in {(time.monotonic()-t0)*1000:.0f}ms")
    for r in results[:3]:
        print(f"  {r[:60]!r}")

asyncio.run(main())

# Expected Token Savings: Semaphore prevents thundering herd; batch flush reduces total API round trips
# Environment: asyncio; tune max_concurrent to API rate limits; flush_interval to burst arrival pattern
```

## Option 3: Adaptive Batch Size Based on Queue Depth

```python
import anthropic
import asyncio
import time
from collections import deque

client = anthropic.AsyncAnthropic()

class AdaptiveBatcher:
    """Adjust batch size based on queue depth: larger batches when backlogged."""
    MIN_BATCH = 1
    MAX_BATCH = 8
    BASE_WINDOW_MS = 15.0

    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._latencies: deque = deque(maxlen=20)

    def _optimal_batch_size(self) -> int:
        depth = self._queue.qsize()
        if depth == 0:   return self.MIN_BATCH
        if depth <= 2:   return 2
        if depth <= 4:   return 4
        return self.MAX_BATCH

    def _window_ms(self) -> float:
        """Shorter window when backlogged — don't make callers wait longer."""
        depth = self._queue.qsize()
        if depth > 4:   return 5.0    # already have enough, dispatch fast
        if depth > 2:   return 10.0
        return self.BASE_WINDOW_MS

    async def submit(self, prompt: str) -> str:
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        await self._queue.put((prompt, future))
        return await future

    async def run(self):
        while True:
            first = await self._queue.get()
            batch = [first]
            target = self._optimal_batch_size()
            window = self._window_ms() / 1000

            deadline = time.monotonic() + window
            while len(batch) < target:
                remaining = deadline - time.monotonic()
                if remaining <= 0: break
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                    batch.append(item)
                except asyncio.TimeoutError:
                    break

            t0 = time.monotonic()
            await self._dispatch(batch)
            latency = (time.monotonic() - t0) * 1000
            self._latencies.append(latency)
            avg = sum(self._latencies) / len(self._latencies)
            print(f"  [ADAPTIVE] batch={len(batch)} target={target} "
                  f"latency={latency:.0f}ms avg={avg:.0f}ms queue={self._queue.qsize()}")

    async def _dispatch(self, batch: list):
        prompts = [p for p, _ in batch]
        futures = [f for _, f in batch]
        results = await asyncio.gather(*[
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": p}],
            )
            for p in prompts
        ], return_exceptions=True)
        for future, result in zip(futures, results):
            if not future.done():
                if isinstance(result, Exception):
                    future.set_exception(result)
                else:
                    future.set_result(result.content[0].text)

async def main():
    batcher = AdaptiveBatcher()
    runner  = asyncio.create_task(batcher.run())

    async def wave(n: int, delay: float = 0.0):
        await asyncio.sleep(delay)
        return await asyncio.gather(*[
            batcher.submit(f"One-word synonym for 'fast' #{i}.") for i in range(n)
        ])

    # First wave: 3 requests (small batch)
    # Second wave after 0.1s: 7 requests (large backlog)
    r1, r2 = await asyncio.gather(wave(3), wave(7, delay=0.05))
    print(f"\nWave 1: {len(r1)} results, Wave 2: {len(r2)} results")
    runner.cancel()

asyncio.run(main())

# Expected Token Savings: Adaptive sizing prevents under-batching during bursts; short window prevents over-waiting
# Environment: asyncio; tune MIN/MAX_BATCH to API concurrency limits; latency deque surfaces p50 trends
```

## Option 4: Request Coalescing — Deduplicate Identical Prompts

```python
import anthropic
import asyncio
import hashlib
import time

client = anthropic.AsyncAnthropic()

class CoalescingBatcher:
    """
    Identical prompts within the same window share one API call.
    All waiters for the same prompt get the same response.
    """
    def __init__(self, window_ms: float = 30.0):
        self._pending: dict[str, list[asyncio.Future]] = {}
        self._lock   = asyncio.Lock()
        self._window = window_ms / 1000
        self._stats  = {"coalesced": 0, "unique": 0}

    def _key(self, prompt: str) -> str:
        return hashlib.sha256(prompt.encode()).hexdigest()[:12]

    async def submit(self, prompt: str) -> str:
        key = self._key(prompt)
        future: asyncio.Future = asyncio.get_event_loop().create_future()

        async with self._lock:
            is_new = key not in self._pending
            self._pending.setdefault(key, []).append((prompt, future))
            if not is_new:
                self._stats["coalesced"] += 1

        if is_new:
            # First submitter drives the call after window expires
            await asyncio.sleep(self._window)
            await self._dispatch_key(key)

        return await future

    async def _dispatch_key(self, key: str):
        async with self._lock:
            waiters = self._pending.pop(key, [])

        if not waiters:
            return
        prompt, _ = waiters[0]
        self._stats["unique"] += 1
        print(f"  [COALESCE] key={key} waiters={len(waiters)}")

        try:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text
        except Exception as e:
            for _, f in waiters:
                if not f.done(): f.set_exception(e)
            return

        for _, f in waiters:
            if not f.done(): f.set_result(text)

    def stats(self) -> dict:
        return {**self._stats,
                "coalesce_rate": self._stats["coalesced"] /
                                 (self._stats["unique"] + self._stats["coalesced"] + 1e-9)}

async def main():
    batcher = CoalescingBatcher(window_ms=40)
    # Simulate 5 requests for same prompt + 3 unique
    same = "What is Python?"
    prompts = [same] * 5 + ["What is asyncio?", "What is FastAPI?", same]
    t0 = time.monotonic()
    results = await asyncio.gather(*[batcher.submit(p) for p in prompts])
    elapsed = (time.monotonic() - t0) * 1000

    s = batcher.stats()
    print(f"\n{len(results)} results in {elapsed:.0f}ms")
    print(f"Coalesced: {s['coalesced']} | Unique calls: {s['unique']} | Rate: {s['coalesce_rate']:.0%}")
    # All "What is Python?" requests get the same answer
    assert results[0] == results[1] == results[2]
    print("Coalesced responses identical ✓")

asyncio.run(main())

# Expected Token Savings: 6 "What is Python?" requests → 1 API call; coalesce_rate shows ROI
# Environment: asyncio; key by prompt hash; add normalization (lowercase, strip) to increase coalesce rate
```

## Option 5: Priority Queue Batcher

```python
import anthropic
import asyncio
import time
import heapq
from dataclasses import dataclass, field
from typing import Any

client = anthropic.AsyncAnthropic()

@dataclass(order=True)
class PrioritizedRequest:
    priority: int                    # lower = higher priority
    seq: int                         # tie-break by arrival order
    prompt: str      = field(compare=False)
    future: Any      = field(compare=False)

class PriorityBatcher:
    """Batch requests but dispatch higher-priority ones first within each window."""
    def __init__(self, window_ms: float = 25.0, max_batch: int = 6):
        self._heap: list[PrioritizedRequest] = []
        self._lock = asyncio.Lock()
        self._seq  = 0
        self._window  = window_ms / 1000
        self._max     = max_batch

    async def submit(self, prompt: str, priority: int = 5) -> str:
        """priority: 1=urgent, 5=normal, 10=background"""
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        async with self._lock:
            req = PrioritizedRequest(priority, self._seq, prompt, future)
            heapq.heappush(self._heap, req)
            self._seq += 1
        return await future

    async def flush(self):
        async with self._lock:
            batch = []
            while self._heap and len(batch) < self._max:
                batch.append(heapq.heappop(self._heap))
        if not batch:
            return
        print(f"  [PRIORITY BATCH] {len(batch)} items, "
              f"priorities={[r.priority for r in batch]}")
        results = await asyncio.gather(*[
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": r.prompt}],
            )
            for r in batch
        ], return_exceptions=True)
        for req, result in zip(batch, results):
            if not req.future.done():
                if isinstance(result, Exception):
                    req.future.set_exception(result)
                else:
                    req.future.set_result(result.content[0].text)

    async def run(self):
        while True:
            await asyncio.sleep(self._window)
            await self.flush()

async def main():
    batcher = PriorityBatcher(window_ms=30, max_batch=5)
    runner  = asyncio.create_task(batcher.run())

    # Mix of urgent and background requests
    reqs = [
        ("URGENT: What is the capital of France?", 1),
        ("Background: Explain the history of Python", 10),
        ("Normal: What is asyncio?", 5),
        ("URGENT: What is 2+2?", 1),
        ("Background: Describe the Python ecosystem", 10),
        ("Normal: What is FastAPI?", 5),
    ]
    futures = await asyncio.gather(*[batcher.submit(p, pri) for p, pri in reqs])
    print(f"\n{len(futures)} results received")
    for (prompt, pri), result in zip(reqs, futures):
        print(f"  [pri={pri}] {prompt[:40]!r}: {result[:30]!r}")
    runner.cancel()

asyncio.run(main())

# Expected Token Savings: Priority ordering ensures latency SLAs for urgent requests; background requests don't block
# Environment: asyncio + heapq; extend with per-priority token budgets; add starvation protection for low priority
```

## Option 6: Batching with SQLite Throughput Metrics

```python
import anthropic
import asyncio
import sqlite3
import time
import uuid

client = anthropic.AsyncAnthropic()
DB = "batch_metrics.db"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS batch_log (
            batch_id TEXT, batch_size INTEGER,
            window_ms REAL, dispatch_ms REAL,
            total_tokens INTEGER, ts REAL
        )
    """)
    con.commit(); con.close()

def log_batch(batch_id: str, size: int, window_ms: float, dispatch_ms: float, tokens: int):
    con = sqlite3.connect(DB)
    con.execute("INSERT INTO batch_log VALUES (?,?,?,?,?,?)",
                (batch_id, size, window_ms, dispatch_ms, tokens, time.time()))
    con.commit(); con.close()

def throughput_report() -> dict:
    con = sqlite3.connect(DB)
    row = con.execute("""
        SELECT COUNT(*) batches, SUM(batch_size) total_reqs,
               ROUND(AVG(batch_size),1) avg_size,
               ROUND(AVG(dispatch_ms),1) avg_dispatch_ms,
               SUM(total_tokens) total_tokens
        FROM batch_log
    """).fetchone()
    con.close()
    return {"batches": row[0], "total_requests": row[1], "avg_batch_size": row[2],
            "avg_dispatch_ms": row[3], "total_tokens": row[4]}

class MeteredBatcher:
    def __init__(self, window_ms: float = 25.0, max_batch: int = 6):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._window  = window_ms / 1000
        self._max     = max_batch
        init_db()

    async def submit(self, prompt: str) -> str:
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        await self._queue.put((prompt, future))
        return await future

    async def run(self):
        while True:
            first = await self._queue.get()
            batch = [first]
            deadline = time.monotonic() + self._window
            while len(batch) < self._max:
                remaining = deadline - time.monotonic()
                if remaining <= 0: break
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                    batch.append(item)
                except asyncio.TimeoutError:
                    break

            await self._dispatch(batch)

    async def _dispatch(self, batch: list):
        batch_id = str(uuid.uuid4())[:8]
        window_ms = self._window * 1000
        t0 = time.monotonic()
        prompts = [p for p, _ in batch]
        futures = [f for _, f in batch]
        results = await asyncio.gather(*[
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": p}],
            )
            for p in prompts
        ], return_exceptions=True)
        dispatch_ms = (time.monotonic() - t0) * 1000
        total_tokens = sum(
            r.usage.input_tokens + r.usage.output_tokens
            for r in results if not isinstance(r, Exception)
        )
        log_batch(batch_id, len(batch), window_ms, dispatch_ms, total_tokens)
        for future, result in zip(futures, results):
            if not future.done():
                if isinstance(result, Exception):
                    future.set_exception(result)
                else:
                    future.set_result(result.content[0].text)

async def main():
    batcher = MeteredBatcher(window_ms=30, max_batch=5)
    runner  = asyncio.create_task(batcher.run())
    prompts = [f"One word for 'fast' variation #{i}" for i in range(12)]
    results = await asyncio.gather(*[batcher.submit(p) for p in prompts])
    runner.cancel()
    print(f"{len(results)} results")
    rpt = throughput_report()
    print(f"Batches: {rpt['batches']} | Avg size: {rpt['avg_batch_size']} | "
          f"Avg dispatch: {rpt['avg_dispatch_ms']}ms | Tokens: {rpt['total_tokens']}")

asyncio.run(main())

# Expected Token Savings: SQLite metrics show actual batch efficiency; avg_batch_size vs max_batch reveals tuning needs
# Environment: asyncio; log_batch adds ~1ms overhead; query batch_log for per-window throughput analysis
```

## Comparison

| Option | Collection Mechanism | Deduplication | Priority | Metrics |
|--------|---------------------|--------------|---------|---------|
| 1 — Time Window Queue | asyncio.Queue | No | No | No |
| 2 — Semaphore Gated | Lock + flush | No | No | No |
| 3 — Adaptive Size | Queue depth heuristic | No | No | Latency deque |
| 4 — Request Coalescing | Hash-based dedupe | Yes | No | Coalesce rate |
| 5 — Priority Queue | Min-heap by priority | No | Yes | No |
| 6 — Metered Batcher | Time Window | No | No | SQLite log |
