---
title: "Agent Doesn't Implement Adaptive Batch Size Tuning"
description: "AI agents use a fixed batch size for API calls, embedding generation, or tool invocations; too small wastes throughput, too large causes timeouts and memory pressure under variable load."
category: performance
difficulty: intermediate
tags: [batching, throughput, adaptive, asyncio, embeddings, rate-limiting, optimization]
---

# Agent Doesn't Implement Adaptive Batch Size Tuning

## Problem

Fixed batch sizes are wrong by definition: the optimal batch size depends on current request rate, API rate limits, network latency, and available memory — all of which change at runtime. A batch of 100 items that works at 3 AM triggers rate limits at 9 AM peak. A batch of 5 items that's safe during spikes wastes 95% throughput during low load. Adaptive batch sizing adjusts continuously based on measured outcomes.

## Solution 1: AIMD Batch Size Controller (Additive Increase, Multiplicative Decrease)

Borrow TCP's congestion control: increase batch size linearly on success, halve it on failure.

```python
import asyncio
import time
from dataclasses import dataclass

@dataclass
class AIMDBatchController:
    min_size: int = 1
    max_size: int = 256
    initial_size: int = 16
    additive_increase: int = 4    # +4 on each success
    multiplicative_decrease: float = 0.5  # halve on failure/timeout

    def __post_init__(self):
        self._current = self.initial_size
        self._success_streak = 0
        self._last_adjusted = time.monotonic()

    @property
    def batch_size(self) -> int:
        return self._current

    def on_success(self, latency_ms: float, target_latency_ms: float = 2000):
        """Call after a successful batch."""
        self._success_streak += 1
        # Increase only if latency is comfortably within target
        if latency_ms < target_latency_ms * 0.8 and self._success_streak >= 3:
            self._current = min(self._max_size(), self._current + self.additive_increase)
            self._success_streak = 0

    def _max_size(self) -> int:
        return self.max_size

    def on_failure(self, reason: str = ""):
        """Call on rate limit, timeout, or OOM."""
        self._success_streak = 0
        self._current = max(self.min_size, int(self._current * self.multiplicative_decrease))

    def on_timeout(self):
        self.on_failure("timeout")

    def on_rate_limit(self):
        # More aggressive decrease on 429
        self._current = max(self.min_size, self._current // 4)

    def __repr__(self):
        return f"AIMDBatchController(current={self._current}, streak={self._success_streak})"

# Usage with embedding batching
async def adaptive_embed_texts(texts: list[str], embed_fn) -> list[list[float]]:
    controller = AIMDBatchController(min_size=4, max_size=200, initial_size=32)
    results: list[list[float]] = [None] * len(texts)
    i = 0

    while i < len(texts):
        batch_size = controller.batch_size
        batch = texts[i: i + batch_size]
        t0 = time.monotonic()
        try:
            embeddings = await asyncio.wait_for(embed_fn(batch), timeout=10.0)
            latency_ms = (time.monotonic() - t0) * 1000
            controller.on_success(latency_ms)
            for j, emb in enumerate(embeddings):
                results[i + j] = emb
            i += len(batch)
        except asyncio.TimeoutError:
            controller.on_timeout()
        except Exception as e:
            if "429" in str(e) or "rate_limit" in str(e).lower():
                controller.on_rate_limit()
                await asyncio.sleep(1.0)
            else:
                controller.on_failure(str(e))

    return results
```

**When to use**: Embedding pipelines, bulk tool calls, any repeated API call with variable load.

---

## Solution 2: Latency-Percentile-Driven Batch Sizing

Monitor p95 latency over a rolling window and shrink/grow batch size to keep p95 within SLO.

```python
import asyncio
import time
from collections import deque

class LatencyGuidedBatcher:
    """Adjusts batch size to keep p95 latency within a target."""

    def __init__(
        self,
        target_p95_ms: float = 3000,
        min_size: int = 1,
        max_size: int = 512,
        window: int = 50,
        adjustment_interval: int = 10,  # adjust every N batches
    ):
        self._target = target_p95_ms
        self._min = min_size
        self._max = max_size
        self._window = window
        self._interval = adjustment_interval
        self._size = min(32, max_size)
        self._latencies: deque = deque(maxlen=window)
        self._batches_since_adjust = 0

    @property
    def batch_size(self) -> int:
        return self._size

    def record_latency(self, latency_ms: float):
        self._latencies.append(latency_ms)
        self._batches_since_adjust += 1
        if self._batches_since_adjust >= self._interval:
            self._adjust()
            self._batches_since_adjust = 0

    def _p95(self) -> float:
        if not self._latencies:
            return 0.0
        s = sorted(self._latencies)
        return s[int(len(s) * 0.95)]

    def _adjust(self):
        p95 = self._p95()
        if p95 == 0:
            return
        ratio = p95 / self._target

        if ratio < 0.7:      # comfortably under target: grow 25%
            self._size = min(self._max, int(self._size * 1.25))
        elif ratio < 0.9:    # slightly under: grow 10%
            self._size = min(self._max, int(self._size * 1.10))
        elif ratio > 1.2:    # over target: shrink 30%
            self._size = max(self._min, int(self._size * 0.70))
        elif ratio > 1.0:    # slightly over: shrink 15%
            self._size = max(self._min, int(self._size * 0.85))

        import logging
        logging.getLogger(__name__).debug(
            "batch_size_adjusted",
            extra={"p95_ms": round(p95, 1), "target_ms": self._target, "new_size": self._size},
        )

# Usage
batcher = LatencyGuidedBatcher(target_p95_ms=2000, min_size=4, max_size=256)

async def process_batch(items: list, handler) -> list:
    size = batcher.batch_size
    t0 = time.monotonic()
    result = await handler(items[:size])
    batcher.record_latency((time.monotonic() - t0) * 1000)
    return result
```

**When to use**: APIs with latency-based SLOs. Keeps batch size optimal without hardcoding.

---

## Solution 3: Token-Count-Aware Batch Sizing for LLM Calls

For LLM APIs, batch size should be bounded by total tokens, not item count — a batch of 10 long documents hits context limits faster than 100 short queries.

```python
import asyncio
from anthropic import AsyncAnthropic
from typing import Callable

client = AsyncAnthropic()

def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return len(text) // 4

class TokenAwareBatcher:
    def __init__(
        self,
        max_batch_tokens: int = 4000,
        max_batch_items: int = 50,
        model_context_limit: int = 200_000,
    ):
        self._max_tokens = max_batch_tokens
        self._max_items = max_batch_items
        self._context_limit = model_context_limit

    def make_batches(self, items: list[str]) -> list[list[str]]:
        """Split items into token-bounded batches."""
        batches = []
        current_batch: list[str] = []
        current_tokens = 0

        for item in items:
            item_tokens = estimate_tokens(item)

            # Start new batch if adding this item would exceed limits
            if current_batch and (
                current_tokens + item_tokens > self._max_tokens
                or len(current_batch) >= self._max_items
            ):
                batches.append(current_batch)
                current_batch = []
                current_tokens = 0

            # Skip single items that exceed the token limit (truncate or log)
            if item_tokens > self._max_tokens:
                item = item[:self._max_tokens * 4]  # truncate to limit
                item_tokens = self._max_tokens

            current_batch.append(item)
            current_tokens += item_tokens

        if current_batch:
            batches.append(current_batch)

        return batches

    async def process_all(self, items: list[str], handler) -> list:
        batches = self.make_batches(items)
        all_results = []
        for batch in batches:
            result = await handler(batch)
            all_results.extend(result if isinstance(result, list) else [result])
        return all_results

# Usage: embed long documents
batcher = TokenAwareBatcher(max_batch_tokens=8000, max_batch_items=20)

async def embed_documents(docs: list[str]) -> list[list[float]]:
    async def embed_batch(batch: list[str]) -> list[list[float]]:
        # Call embedding API with the batch
        await asyncio.sleep(0.1)  # simulate API call
        return [[0.1] * 1536 for _ in batch]

    return await batcher.process_all(docs, embed_batch)
```

**When to use**: Embedding or classification tasks with variable-length inputs. Prevents context window overflow.

---

## Solution 4: Throughput-Maximizing Batch Accumulator with Deadline

Accumulate items until the batch is full OR a deadline passes — whichever comes first — to balance throughput and latency.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, TypeVar

T = TypeVar("T")

@dataclass
class BatchAccumulator:
    max_size: int = 64
    max_wait_ms: float = 50.0   # flush after 50ms even if batch not full
    _items: list = field(default_factory=list)
    _futures: list = field(default_factory=list)
    _flush_task: asyncio.Task | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _flush_fn: Any = None

    async def submit(self, item: Any) -> Any:
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        async with self._lock:
            self._items.append(item)
            self._futures.append(fut)

            if len(self._items) == 1:
                # Start deadline timer
                self._flush_task = asyncio.create_task(
                    self._flush_after_deadline()
                )

            if len(self._items) >= self.max_size:
                # Batch full: flush immediately
                if self._flush_task and not self._flush_task.done():
                    self._flush_task.cancel()
                await self._flush()

        return await fut

    async def _flush_after_deadline(self):
        await asyncio.sleep(self.max_wait_ms / 1000)
        async with self._lock:
            if self._items:
                await self._flush()

    async def _flush(self):
        if not self._items:
            return
        items = self._items[:]
        futures = self._futures[:]
        self._items.clear()
        self._futures.clear()

        try:
            results = await self._flush_fn(items)
            for fut, result in zip(futures, results):
                if not fut.done():
                    fut.set_result(result)
        except Exception as e:
            for fut in futures:
                if not fut.done():
                    fut.set_exception(e)

# Usage: create with a flush function
acc = BatchAccumulator(max_size=64, max_wait_ms=50)

async def do_batch_embed(texts: list[str]) -> list[list[float]]:
    await asyncio.sleep(0.1)  # simulate API call
    return [[0.1] * 1536 for _ in texts]

acc._flush_fn = do_batch_embed

async def embed_one(text: str) -> list[float]:
    return await acc.submit(text)

# Concurrent callers are automatically batched
async def demo():
    results = await asyncio.gather(*[embed_one(f"text {i}") for i in range(200)])
    print(f"Got {len(results)} embeddings via auto-batching")
```

**When to use**: High-concurrency agents where many coroutines independently call the same API. Batching is transparent to callers.

---

## Solution 5: Rate-Limit-Aware Adaptive Batching

Read the API's rate limit headers and dynamically compute the largest batch that fits within the remaining quota.

```python
import asyncio
import time
import aiohttp
from dataclasses import dataclass

@dataclass
class RateLimitState:
    requests_limit: int = 1000
    requests_remaining: int = 1000
    tokens_limit: int = 100_000
    tokens_remaining: int = 100_000
    reset_at: float = 0.0

    def update_from_headers(self, headers: dict):
        self.requests_remaining = int(headers.get("x-ratelimit-remaining-requests", self.requests_remaining))
        self.tokens_remaining   = int(headers.get("x-ratelimit-remaining-tokens", self.tokens_remaining))
        self.requests_limit     = int(headers.get("x-ratelimit-limit-requests", self.requests_limit))
        self.tokens_limit       = int(headers.get("x-ratelimit-limit-tokens", self.tokens_limit))

    @property
    def request_headroom(self) -> float:
        """Fraction of request quota remaining (0-1)."""
        if self.requests_limit == 0:
            return 1.0
        return self.requests_remaining / self.requests_limit

    @property
    def token_headroom(self) -> float:
        if self.tokens_limit == 0:
            return 1.0
        return self.tokens_remaining / self.tokens_limit

class RateLimitAwareBatcher:
    def __init__(self, base_batch_size: int = 32, tokens_per_item: int = 100):
        self._base = base_batch_size
        self._tokens_per_item = tokens_per_item
        self._state = RateLimitState()
        self._lock = asyncio.Lock()

    def optimal_batch_size(self, available_items: int) -> int:
        headroom = min(self._state.request_headroom, self._state.token_headroom)

        if headroom > 0.8:     # plenty of quota: use full batch
            size = self._base
        elif headroom > 0.5:   # moderate quota: half batch
            size = self._base // 2
        elif headroom > 0.2:   # tight quota: quarter batch
            size = max(1, self._base // 4)
        else:                  # near limit: single item
            size = 1

        # Also bound by token budget
        max_by_tokens = max(1, self._state.tokens_remaining // self._tokens_per_item)
        return min(size, available_items, max_by_tokens)

    def update_state(self, response_headers: dict):
        self._state.update_from_headers(response_headers)

async def adaptive_api_caller(items: list[str], api_fn) -> list:
    batcher = RateLimitAwareBatcher(base_batch_size=50, tokens_per_item=200)
    results = []
    i = 0

    while i < len(items):
        size = batcher.optimal_batch_size(len(items) - i)
        batch = items[i: i + size]

        resp_headers, batch_results = await api_fn(batch)
        batcher.update_state(resp_headers)
        results.extend(batch_results)
        i += size

    return results
```

**When to use**: OpenAI/Anthropic embedding APIs that return rate limit headers. Prevents 429s automatically.

---

## Solution 6: Exponential Moving Average Batch Tuner

Use EMA of throughput (items/sec) to find the sweet spot where throughput stops increasing with larger batches.

```python
import asyncio
import time
from collections import deque

class EMABatchTuner:
    """Find the batch size that maximizes throughput via EMA tracking."""

    def __init__(self, min_size: int = 1, max_size: int = 512, alpha: float = 0.3):
        self._min = min_size
        self._max = max_size
        self._alpha = alpha  # EMA smoothing factor
        self._size = 16
        self._ema_throughput: float = 0.0
        self._step_direction: int = 1   # +1 = increasing, -1 = decreasing
        self._prev_throughput: float = 0.0
        self._measurements = 0

    def record(self, items_processed: int, elapsed_s: float):
        throughput = items_processed / max(elapsed_s, 0.001)  # items/sec

        # Update EMA
        if self._measurements == 0:
            self._ema_throughput = throughput
        else:
            self._ema_throughput = self._alpha * throughput + (1 - self._alpha) * self._ema_throughput

        self._measurements += 1

        # Adjust direction every 5 measurements
        if self._measurements % 5 == 0:
            if self._ema_throughput > self._prev_throughput * 1.05:
                # Throughput still improving: keep going in same direction
                pass
            elif self._ema_throughput < self._prev_throughput * 0.95:
                # Throughput declining: reverse direction
                self._step_direction *= -1
            # else: plateau, try the other direction
            else:
                self._step_direction *= -1

            # Apply step
            step = max(1, self._size // 4)
            new_size = self._size + self._step_direction * step
            self._size = max(self._min, min(self._max, new_size))
            self._prev_throughput = self._ema_throughput

    @property
    def batch_size(self) -> int:
        return self._size

    @property
    def throughput(self) -> float:
        return round(self._ema_throughput, 2)

# Usage
tuner = EMABatchTuner(min_size=4, max_size=256)

async def optimized_pipeline(items: list, handler) -> list:
    results = []
    i = 0
    while i < len(items):
        size = tuner.batch_size
        batch = items[i: i + size]
        t0 = time.monotonic()
        batch_results = await handler(batch)
        tuner.record(len(batch), time.monotonic() - t0)
        results.extend(batch_results)
        i += size
    return results
```

**When to use**: Unknown API characteristics where you want to discover the optimal batch size empirically in production.

---

## Comparison

| Solution | Adaptation Signal | Convergence Speed | Rate Limit Aware | Latency SLO | Best For |
|---|---|---|---|---|---|
| AIMD controller | Error rate | Fast on failure | Via on_rate_limit | No | General API batching |
| Latency-percentile | p95 latency | Medium | No | Yes | Latency SLO enforcement |
| Token-count-aware | Token count | Static | No | No | LLM input batching |
| Deadline accumulator | Batch fullness + timer | Immediate | No | Via max_wait | High-concurrency callers |
| Rate-limit-aware | API headers | Immediate | Yes | No | APIs with rate limit headers |
| EMA throughput tuner | Items/sec | Slow (exploratory) | No | No | Unknown API characteristics |

**Rule of thumb**: Use AIMD as baseline. Add token-count awareness for LLM APIs. Use rate-limit headers if available. EMA tuner is for novel backends where you have no prior knowledge.
