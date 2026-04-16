---
layout: solution
title: "Agent Doesn't Implement Token-Aware Request Batching"
category: concurrency
description: "Group concurrent requests into batches constrained by total token budget, preventing individual bursts from exceeding rate limits while maximizing throughput for high-volume agent pipelines."
tags: [batching, concurrency, token-budget, rate-limiting, throughput]
---

# Agent Doesn't Implement Token-Aware Request Batching

## Problem

Processing requests one-at-a-time is slow, but firing all concurrent requests simultaneously causes rate limit errors when total token usage spikes. Without token-aware batching, agents either underutilize capacity or hammer rate limits unpredictably.

## Solution Options

### Option 1: Simple Token-Budget Batch Processor

```python
import anthropic
import asyncio
from dataclasses import dataclass

async_client = anthropic.AsyncAnthropic()

@dataclass
class BatchRequest:
    id: str
    prompt: str
    estimated_tokens: int  # input + expected output
    result: str = ""

async def process_request(req: BatchRequest) -> BatchRequest:
    resp = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": req.prompt}]
    )
    req.result = resp.content[0].text
    return req

async def token_aware_batch(requests: list[BatchRequest], token_budget: int = 50_000) -> list[BatchRequest]:
    """Process requests in batches where total estimated tokens <= token_budget."""
    results = []
    i = 0
    batch_num = 0

    while i < len(requests):
        batch = []
        batch_tokens = 0
        while i < len(requests) and batch_tokens + requests[i].estimated_tokens <= token_budget:
            batch.append(requests[i])
            batch_tokens += requests[i].estimated_tokens
            i += 1
        batch_num += 1
        print(f"[Batch {batch_num}] {len(batch)} requests, ~{batch_tokens} tokens")
        batch_results = await asyncio.gather(*[process_request(r) for r in batch])
        results.extend(batch_results)

    return results

# Build test requests with estimated token costs
requests = [
    BatchRequest(id=f"r{i}", prompt=f"Define term #{i} in distributed systems in one sentence.", estimated_tokens=80)
    for i in range(12)
]

results = asyncio.run(token_aware_batch(requests, token_budget=300))
for r in results[:3]:
    print(f"[{r.id}] {r.result[:60]}...")

# Expected Token Savings: prevents rate limit errors; maximizes tokens/second throughput
# Environment: bulk processing pipelines, batch inference, document processing queues
```

### Option 2: Priority Queue with Token Budget Enforcement

```python
import anthropic
import asyncio
import heapq
from dataclasses import dataclass, field

async_client = anthropic.AsyncAnthropic()

@dataclass(order=True)
class PrioritizedRequest:
    priority: int          # lower = higher priority
    id: str = field(compare=False)
    prompt: str = field(compare=False)
    estimated_tokens: int = field(compare=False, default=200)
    result: str = field(compare=False, default="")

class TokenBudgetQueue:
    def __init__(self, tokens_per_second: float = 10_000, max_batch_tokens: int = 30_000):
        self.heap: list[PrioritizedRequest] = []
        self.tokens_per_second = tokens_per_second
        self.max_batch_tokens = max_batch_tokens
        self.lock = asyncio.Lock()

    def push(self, req: PrioritizedRequest) -> None:
        heapq.heappush(self.heap, req)

    def pop_batch(self) -> list[PrioritizedRequest]:
        batch = []
        budget = self.max_batch_tokens
        temp = []
        while self.heap and budget > 0:
            req = heapq.heappop(self.heap)
            if req.estimated_tokens <= budget:
                batch.append(req)
                budget -= req.estimated_tokens
            else:
                temp.append(req)
                break
        for r in temp:
            heapq.heappush(self.heap, r)
        return batch

    async def drain(self) -> list[PrioritizedRequest]:
        all_results = []
        batch_num = 0
        while self.heap:
            batch = self.pop_batch()
            if not batch:
                break
            batch_num += 1
            total_tokens = sum(r.estimated_tokens for r in batch)
            print(f"[Batch {batch_num}] {len(batch)} reqs, ~{total_tokens} tokens (priorities: {[r.priority for r in batch]})")

            async def run(req: PrioritizedRequest) -> PrioritizedRequest:
                resp = await async_client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=256,
                    messages=[{"role": "user", "content": req.prompt}]
                )
                req.result = resp.content[0].text
                return req

            results = await asyncio.gather(*[run(r) for r in batch])
            all_results.extend(results)

            # Throttle between batches based on token rate
            wait_s = total_tokens / self.tokens_per_second
            if self.heap:
                await asyncio.sleep(min(wait_s, 2.0))

        return all_results

async def main():
    queue = TokenBudgetQueue(tokens_per_second=5000, max_batch_tokens=500)

    # Mix of priority and token size
    items = [
        (1, "URGENT: What is a deadlock?", 100),
        (5, "What is a semaphore?", 100),
        (5, "What is a mutex?", 100),
        (1, "URGENT: What is a race condition?", 100),
        (3, "What is a critical section?", 100),
        (5, "What is a monitor in concurrency?", 100),
    ]
    for priority, prompt, tokens in items:
        queue.push(PrioritizedRequest(priority=priority, id=f"p{priority}", prompt=prompt, estimated_tokens=tokens))

    results = await queue.drain()
    for r in results:
        print(f"[priority={r.priority}] {r.prompt[:40]}: {r.result[:50]}...")

asyncio.run(main())

# Expected Token Savings: priority queue ensures urgent requests run first within same token budget
# Environment: mixed-priority workloads, SLA-differentiated processing, API gateways
```

### Option 3: Sliding Window Token Rate Limiter

```python
import anthropic
import asyncio
import time
from collections import deque
from dataclasses import dataclass

async_client = anthropic.AsyncAnthropic()

class SlidingWindowRateLimiter:
    """Allows up to max_tokens tokens within the sliding window_seconds."""
    def __init__(self, max_tokens: int = 100_000, window_seconds: float = 60.0):
        self.max_tokens = max_tokens
        self.window_seconds = window_seconds
        self.usage_log: deque[tuple[float, int]] = deque()
        self.lock = asyncio.Lock()

    def _evict_old(self) -> None:
        cutoff = time.monotonic() - self.window_seconds
        while self.usage_log and self.usage_log[0][0] < cutoff:
            self.usage_log.popleft()

    def current_usage(self) -> int:
        self._evict_old()
        return sum(tokens for _, tokens in self.usage_log)

    async def acquire(self, tokens: int) -> float:
        """Wait until tokens can be consumed. Returns wait time."""
        while True:
            async with self.lock:
                self._evict_old()
                used = sum(t for _, t in self.usage_log)
                if used + tokens <= self.max_tokens:
                    self.usage_log.append((time.monotonic(), tokens))
                    return 0.0

            # Need to wait — estimate when oldest entry expires
            if self.usage_log:
                oldest_ts = self.usage_log[0][0]
                wait = max(0.01, oldest_ts + self.window_seconds - time.monotonic())
            else:
                wait = 0.1
            await asyncio.sleep(wait)

@dataclass
class Request:
    id: str
    prompt: str
    input_tokens: int = 50
    output_tokens: int = 200

async def process_with_rate_limit(req: Request, limiter: SlidingWindowRateLimiter) -> tuple[str, float]:
    total = req.input_tokens + req.output_tokens
    wait = await limiter.acquire(total)
    if wait > 0:
        print(f"  [{req.id}] waited {wait:.2f}s for token budget")

    resp = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=req.output_tokens,
        messages=[{"role": "user", "content": req.prompt}]
    )
    return resp.content[0].text, wait

async def main():
    # Simulate a 10k token/minute limit
    limiter = SlidingWindowRateLimiter(max_tokens=2000, window_seconds=60.0)

    requests = [
        Request(id=f"r{i}", prompt=f"In one sentence: what is concept #{i} in databases?",
                input_tokens=30, output_tokens=60)
        for i in range(8)
    ]

    tasks = [process_with_rate_limit(r, limiter) for r in requests]
    results = await asyncio.gather(*tasks)
    total_wait = sum(w for _, w in results)
    print(f"\n{len(results)} requests complete, total wait={total_wait:.2f}s")
    for i, (text, wait) in enumerate(results[:3]):
        print(f"  [r{i}] wait={wait:.2f}s: {text[:60]}...")

asyncio.run(main())

# Expected Token Savings: sliding window maximizes throughput vs conservative fixed windows
# Environment: continuous high-volume ingestion, async workers, batch API clients
```

### Option 4: Adaptive Batch Size Based on Observed Throughput

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

async_client = anthropic.AsyncAnthropic()

@dataclass
class ThroughputTracker:
    samples: list[float] = field(default_factory=list)
    MAX_SAMPLES = 10

    def record(self, tokens_per_second: float) -> None:
        self.samples.append(tokens_per_second)
        if len(self.samples) > self.MAX_SAMPLES:
            self.samples.pop(0)

    @property
    def avg_tps(self) -> float:
        return sum(self.samples) / max(len(self.samples), 1)

    def suggest_batch_size(self, tokens_per_request: int, target_tps: float) -> int:
        """How many requests fit in one batch to hit target TPS."""
        if not self.samples:
            return 4  # conservative default
        # If we're running below target, increase batch size
        ratio = target_tps / max(self.avg_tps, 1)
        return max(1, min(16, round(4 * ratio)))

async def process_batch(prompts: list[str]) -> tuple[list[str], float]:
    t0 = time.monotonic()
    tasks = [
        async_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": p}]
        )
        for p in prompts
    ]
    responses = await asyncio.gather(*tasks)
    elapsed = time.monotonic() - t0
    results = [r.content[0].text for r in responses]
    total_tokens = sum(r.usage.input_tokens + r.usage.output_tokens for r in responses)
    tps = total_tokens / max(elapsed, 0.001)
    return results, tps

async def adaptive_batch_processor(all_prompts: list[str], target_tps: float = 1000) -> list[str]:
    tracker = ThroughputTracker()
    all_results = []
    i = 0
    batch_num = 0
    tokens_per_request = 150  # estimate

    while i < len(all_prompts):
        batch_size = tracker.suggest_batch_size(tokens_per_request, target_tps)
        batch = all_prompts[i:i + batch_size]
        i += len(batch)
        batch_num += 1

        results, tps = await process_batch(batch)
        tracker.record(tps)
        all_results.extend(results)
        print(f"[Batch {batch_num}] size={len(batch)} tps={tps:.0f} avg_tps={tracker.avg_tps:.0f} next_size={tracker.suggest_batch_size(tokens_per_request, target_tps)}")

    return all_results

prompts = [f"What is distributed systems concept #{i}? One sentence." for i in range(10)]
results = asyncio.run(adaptive_batch_processor(prompts, target_tps=500))
print(f"\n{len(results)} results")
for r in results[:3]:
    print(f"  {r[:70]}...")

# Expected Token Savings: adaptive sizing maximizes throughput without manual tuning
# Environment: self-tuning batch processors, variable-load pipelines, auto-scaling agents
```

### Option 5: Token-Aware Fan-Out with Per-Shard Budgets

```python
import anthropic
import asyncio
import hashlib
from dataclasses import dataclass

async_client = anthropic.AsyncAnthropic()

@dataclass
class ShardedRequest:
    id: str
    prompt: str
    estimated_tokens: int

class TokenShardedDispatcher:
    """Distribute requests across N shards, each with its own token budget."""
    def __init__(self, num_shards: int = 3, tokens_per_shard: int = 20_000):
        self.num_shards = num_shards
        self.tokens_per_shard = tokens_per_shard
        self.shard_queues: list[list[ShardedRequest]] = [[] for _ in range(num_shards)]
        self.shard_usage: list[int] = [0] * num_shards

    def assign(self, req: ShardedRequest) -> int:
        """Assign to shard with most remaining capacity."""
        remaining = [self.tokens_per_shard - self.shard_usage[i] for i in range(self.num_shards)]
        best = max(range(self.num_shards), key=lambda i: remaining[i])
        if remaining[best] < req.estimated_tokens:
            raise ValueError(f"No shard has capacity for {req.estimated_tokens} tokens")
        self.shard_queues[best].append(req)
        self.shard_usage[best] += req.estimated_tokens
        return best

    async def process_shard(self, shard_id: int) -> list[tuple[str, str]]:
        requests = self.shard_queues[shard_id]
        if not requests:
            return []
        print(f"[Shard {shard_id}] {len(requests)} requests, ~{self.shard_usage[shard_id]} tokens")

        async def run_one(req: ShardedRequest) -> tuple[str, str]:
            resp = await async_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=min(req.estimated_tokens, 256),
                messages=[{"role": "user", "content": req.prompt}]
            )
            return req.id, resp.content[0].text

        results = await asyncio.gather(*[run_one(r) for r in requests])
        return list(results)

    async def dispatch_all(self) -> dict[str, str]:
        shard_tasks = [self.process_shard(i) for i in range(self.num_shards)]
        shard_results = await asyncio.gather(*shard_tasks)
        return {req_id: text for shard in shard_results for req_id, text in shard}

async def main():
    dispatcher = TokenShardedDispatcher(num_shards=3, tokens_per_shard=1000)

    requests = [
        ShardedRequest(id=f"req_{i:02d}", prompt=f"Define '{term}' in one sentence.",
                       estimated_tokens=80)
        for i, term in enumerate(["CRDT", "Paxos", "Raft", "2PC", "MVCC",
                                    "WAL", "LSM", "B-tree", "SSTable", "Bloom filter"])
    ]

    for req in requests:
        shard = dispatcher.assign(req)

    results = await dispatcher.dispatch_all()
    print(f"\n{len(results)} results")
    for req_id, text in sorted(results.items())[:4]:
        print(f"  [{req_id}] {text[:60]}...")

asyncio.run(main())

# Expected Token Savings: sharding enables parallel processing at 3x throughput vs single queue
# Environment: high-throughput batch systems, multi-worker inference, distributed agent pools
```

### Option 6: Token Budget with Backpressure and Queue Depth Monitoring

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from collections import deque

async_client = anthropic.AsyncAnthropic()

@dataclass
class QueueMetrics:
    enqueued: int = 0
    processed: int = 0
    rejected: int = 0
    total_wait_ms: float = 0.0
    samples: deque = field(default_factory=lambda: deque(maxlen=20))

    def record_wait(self, wait_ms: float) -> None:
        self.samples.append(wait_ms)
        self.total_wait_ms += wait_ms

    @property
    def avg_wait_ms(self) -> float:
        return sum(self.samples) / max(len(self.samples), 1)

    @property
    def queue_depth(self) -> int:
        return self.enqueued - self.processed - self.rejected

class TokenBudgetController:
    """Token bucket with backpressure — rejects when queue depth exceeds max_depth."""
    def __init__(self, rate_tokens_per_sec: float = 5000, burst: int = 10_000, max_depth: int = 20):
        self.rate = rate_tokens_per_sec
        self.burst = burst
        self.available = float(burst)
        self.last_refill = time.monotonic()
        self.lock = asyncio.Lock()
        self.max_depth = max_depth
        self.metrics = QueueMetrics()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.available = min(self.burst, self.available + elapsed * self.rate)
        self.last_refill = now

    async def try_acquire(self, tokens: int, timeout: float = 5.0) -> bool:
        if self.metrics.queue_depth >= self.max_depth:
            self.metrics.rejected += 1
            return False  # backpressure: queue full

        self.metrics.enqueued += 1
        deadline = time.monotonic() + timeout
        t_enqueue = time.monotonic()

        while time.monotonic() < deadline:
            async with self.lock:
                self._refill()
                if self.available >= tokens:
                    self.available -= tokens
                    wait_ms = (time.monotonic() - t_enqueue) * 1000
                    self.metrics.record_wait(wait_ms)
                    self.metrics.processed += 1
                    return True
            await asyncio.sleep(tokens / self.rate * 0.5)

        self.metrics.rejected += 1
        self.metrics.enqueued -= 1
        return False

controller = TokenBudgetController(rate_tokens_per_sec=3000, burst=6000, max_depth=15)

async def controlled_call(req_id: str, prompt: str, tokens: int = 200) -> str | None:
    acquired = await controller.try_acquire(tokens, timeout=3.0)
    if not acquired:
        print(f"  [{req_id}] REJECTED (backpressure)")
        return None

    resp = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=min(tokens, 256),
        messages=[{"role": "user", "content": prompt}]
    )
    return resp.content[0].text

async def main():
    tasks = [
        controlled_call(f"r{i:02d}", f"What is concept #{i} in databases? One sentence.", tokens=150)
        for i in range(15)
    ]
    results = await asyncio.gather(*tasks)
    ok = sum(1 for r in results if r is not None)
    m = controller.metrics
    print(f"\n{ok}/{len(results)} succeeded | avg_wait={m.avg_wait_ms:.0f}ms | rejected={m.rejected} | depth={m.queue_depth}")

asyncio.run(main())

# Expected Token Savings: backpressure prevents token debt accumulation; sustains rate limit compliance
# Environment: public APIs, multi-tenant services, token-quota-limited deployments
```

## Comparison

| Option | Strategy | Backpressure | Priority | Best For |
|--------|----------|--------------|----------|----------|
| 1 | Static token-budget batches | No | No | Simple bulk processing |
| 2 | Priority queue with budget | No | Yes | Mixed-priority workloads |
| 3 | Sliding window rate limiter | Implicit | No | Continuous ingestion |
| 4 | Adaptive batch sizing | No | No | Self-tuning pipelines |
| 5 | Token-sharded fan-out | No | No | High-throughput parallelism |
| 6 | Token bucket + queue depth | Yes | No | Public API rate compliance |
