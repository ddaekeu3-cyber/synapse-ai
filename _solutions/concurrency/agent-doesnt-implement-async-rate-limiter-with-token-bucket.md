---
layout: solution
title: "Agent Doesn't Implement Async Rate Limiter with Token Bucket"
category: concurrency
description: "Agent fires API calls as fast as asyncio allows, hitting 429 rate limit errors under burst load instead of smoothing traffic with a proper token bucket rate limiter."
tags: [concurrency, rate-limiting, token-bucket, asyncio, 429]
---

# Agent Doesn't Implement Async Rate Limiter with Token Bucket

## Problem

Async agents that `await` multiple API calls concurrently with `asyncio.gather()` can burst hundreds of requests per second, immediately triggering 429 rate limit errors. Naive fixes like adding `asyncio.sleep()` between calls serialize work unnecessarily, destroying throughput. The token bucket algorithm provides the right primitive: it allows bursting up to a configured capacity while enforcing a long-term average rate — maintaining maximum throughput without hitting rate limits.

## Solution Options

### Option 1: Simple Async Token Bucket

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

async_client = anthropic.AsyncAnthropic()

@dataclass
class TokenBucket:
    """Async token bucket rate limiter."""
    rate: float        # tokens per second (refill rate)
    capacity: float    # maximum burst size
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self):
        self._tokens = self.capacity
        self._last_refill = time.monotonic()

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._last_refill = now

    async def acquire(self, tokens: float = 1.0):
        """Wait until `tokens` are available, then consume them."""
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                # Calculate wait time until enough tokens accumulate
                deficit = tokens - self._tokens
                wait = deficit / self.rate
                await asyncio.sleep(wait)

# 5 requests/sec with burst of 10
rate_limiter = TokenBucket(rate=5.0, capacity=10.0)

async def rate_limited_call(prompt: str, call_id: int) -> str:
    await rate_limiter.acquire(1.0)
    t0 = time.monotonic()
    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": prompt}],
    )
    elapsed = time.monotonic() - t0
    print(f"[{call_id:02d}] acquired at t={time.monotonic():.2f}s | latency={elapsed*1000:.0f}ms")
    return response.content[0].text

async def main():
    # Simulate 15 concurrent requests — token bucket smooths them to 5/sec
    prompts = [f"What is {i} squared?" for i in range(15)]
    tasks = [rate_limited_call(p, i) for i, p in enumerate(prompts)]
    results = await asyncio.gather(*tasks)
    print(f"\nCompleted {len(results)} calls")

asyncio.run(main())

# Expected Token Savings: Prevents wasted retries on 429 errors; zero API overhead
# Environment: Any async agent making >5 concurrent API calls
```

### Option 2: Weighted Token Bucket (Cost-Aware Rate Limiting)

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

async_client = anthropic.AsyncAnthropic()

@dataclass
class WeightedTokenBucket:
    """Token bucket where each request consumes tokens proportional to its cost."""
    tokens_per_second: float    # Anthropic TPM limit / 60
    burst_tokens: float         # Max burst (e.g., 10,000 tokens)
    _available: float = field(init=False)
    _last_refill: float = field(init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _total_consumed: float = 0.0

    def __post_init__(self):
        self._available = self.burst_tokens
        self._last_refill = time.monotonic()

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._available = min(self.burst_tokens, self._available + elapsed * self.tokens_per_second)
        self._last_refill = now

    async def acquire(self, estimated_tokens: int):
        """Block until enough token-budget is available for this request."""
        async with self._lock:
            while True:
                self._refill()
                if self._available >= estimated_tokens:
                    self._available -= estimated_tokens
                    self._total_consumed += estimated_tokens
                    return
                deficit = estimated_tokens - self._available
                wait = deficit / self.tokens_per_second
                print(f"[RATE LIMITER] Need {estimated_tokens} tokens, have {self._available:.0f}. Waiting {wait:.2f}s...")
                await asyncio.sleep(wait)

def estimate_tokens(prompt: str, max_tokens: int) -> int:
    """Estimate total tokens for a request (input + output estimate)."""
    input_est = len(prompt) // 4
    return input_est + max_tokens

# 40,000 TPM limit → ~667 tokens/sec; burst of 8,000 tokens
limiter = WeightedTokenBucket(tokens_per_second=667, burst_tokens=8000)

@dataclass
class RequestSpec:
    prompt: str
    max_tokens: int
    label: str

async def weighted_limited_call(spec: RequestSpec) -> str:
    estimated = estimate_tokens(spec.prompt, spec.max_tokens)
    await limiter.acquire(estimated)
    print(f"[{spec.label}] Acquired {estimated} token budget")

    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=spec.max_tokens,
        messages=[{"role": "user", "content": spec.prompt}],
    )
    actual = response.usage.input_tokens + response.usage.output_tokens
    print(f"[{spec.label}] Actual tokens: {actual}")
    return response.content[0].text

async def main():
    requests = [
        RequestSpec("What is Python?", 64, "small_1"),
        RequestSpec("Write a 200-word essay on distributed systems.", 300, "large_1"),
        RequestSpec("Say hello.", 16, "tiny_1"),
        RequestSpec("Explain TCP/IP in detail with examples.", 400, "large_2"),
        RequestSpec("What is 2+2?", 16, "tiny_2"),
    ]
    tasks = [weighted_limited_call(r) for r in requests]
    results = await asyncio.gather(*tasks)
    print(f"\nCompleted {len(results)} requests | Total budget consumed: {limiter._total_consumed:.0f} tokens")

asyncio.run(main())

# Expected Token Savings: Prevents 429 retries on heavy bursts; token-proportional scheduling is fairer
# Environment: Agents subject to Anthropic TPM (tokens-per-minute) limits, not just RPM
```

### Option 3: Multi-Tier Rate Limiter (RPM + TPM)

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

async_client = anthropic.AsyncAnthropic()

@dataclass
class AsyncTokenBucket:
    rate: float
    capacity: float
    name: str
    _tokens: float = field(init=False)
    _last: float = field(init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self):
        self._tokens = self.capacity
        self._last = time.monotonic()

    async def acquire(self, cost: float = 1.0):
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                wait = (cost - self._tokens) / self.rate
                print(f"[{self.name}] throttled — waiting {wait:.2f}s (available={self._tokens:.1f})")
                await asyncio.sleep(wait)

class MultiTierRateLimiter:
    """Enforces both RPM and TPM limits simultaneously."""

    def __init__(self, rpm: float, tpm: float, burst_requests: int = 10, burst_tokens: int = 5000):
        self.rpm_bucket = AsyncTokenBucket(rate=rpm / 60, capacity=burst_requests, name="RPM")
        self.tpm_bucket = AsyncTokenBucket(rate=tpm / 60, capacity=burst_tokens, name="TPM")

    async def acquire(self, estimated_tokens: int):
        # Must satisfy BOTH limits simultaneously
        await asyncio.gather(
            self.rpm_bucket.acquire(1),
            self.tpm_bucket.acquire(estimated_tokens),
        )

# 60 RPM, 40,000 TPM (typical Haiku limits)
rate_limiter = MultiTierRateLimiter(rpm=60, tpm=40_000, burst_requests=5, burst_tokens=4000)

async def multi_tier_call(prompt: str, max_tokens: int, label: str) -> str:
    estimated_tokens = len(prompt) // 4 + max_tokens
    await rate_limiter.acquire(estimated_tokens)
    print(f"[{label}] Rate limit acquired (est={estimated_tokens} tokens)")

    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text

async def main():
    # Mix of small and large requests
    calls = [
        ("Translate 'hello' to French.", 16, "tiny"),
        ("Write a detailed summary of microservices architecture patterns.", 256, "large"),
        ("What is 5 * 7?", 8, "tiny"),
        ("Explain the CAP theorem with examples.", 200, "medium"),
        ("Say hi.", 8, "tiny"),
        ("Describe event-driven architecture in 3 paragraphs.", 300, "large"),
    ]

    tasks = [multi_tier_call(p, mt, l) for p, mt, l in calls]
    results = await asyncio.gather(*tasks)
    print(f"\nAll {len(results)} calls completed successfully")

asyncio.run(main())

# Expected Token Savings: Eliminates 429 errors from either RPM or TPM limit violations
# Environment: Production agents with both request and token rate limits to respect simultaneously
```

### Option 4: Sliding Window Rate Limiter

```python
import anthropic
import asyncio
import collections
import time
from dataclasses import dataclass, field

async_client = anthropic.AsyncAnthropic()

@dataclass
class SlidingWindowRateLimiter:
    """
    Sliding window counter — more precise than fixed window,
    avoids the token bucket's 'all burst at window boundary' problem.
    """
    max_requests: int
    window_seconds: float
    _timestamps: collections.deque = field(default_factory=collections.deque, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def _evict_old(self):
        cutoff = time.monotonic() - self.window_seconds
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    async def acquire(self):
        async with self._lock:
            while True:
                self._evict_old()
                if len(self._timestamps) < self.max_requests:
                    self._timestamps.append(time.monotonic())
                    return
                # Wait until the oldest request falls out of the window
                oldest = self._timestamps[0]
                wait = (oldest + self.window_seconds) - time.monotonic()
                if wait > 0:
                    print(f"[SLIDING WINDOW] {len(self._timestamps)}/{self.max_requests} in window — wait {wait:.2f}s")
                    await asyncio.sleep(wait + 0.001)

    @property
    def current_rate(self) -> float:
        self._evict_old()
        return len(self._timestamps) / self.window_seconds

# 10 requests per 2-second sliding window
limiter = SlidingWindowRateLimiter(max_requests=10, window_seconds=2.0)

async def sliding_window_call(prompt: str, req_id: int) -> str:
    await limiter.acquire()
    print(f"[REQ {req_id:02d}] acquired | current rate: {limiter.current_rate:.1f} req/s")

    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=32,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text

async def main():
    tasks = [sliding_window_call(f"Say the number {i}", i) for i in range(20)]
    t0 = time.monotonic()
    results = await asyncio.gather(*tasks)
    elapsed = time.monotonic() - t0
    print(f"\n{len(results)} requests in {elapsed:.1f}s ({len(results)/elapsed:.1f} req/s avg)")

asyncio.run(main())

# Expected Token Savings: No wasted retries; sliding window prevents boundary burst spikes
# Environment: Agents calling rate-limited third-party APIs (not just Anthropic) like web search
```

### Option 5: Adaptive Rate Limiter with 429 Backoff

```python
import anthropic
import asyncio
import time
import random
from dataclasses import dataclass, field

async_client = anthropic.AsyncAnthropic()

@dataclass
class AdaptiveRateLimiter:
    """
    Starts at max rate, backs off on 429, gradually recovers.
    Learns the actual rate limit from response headers or errors.
    """
    initial_rate: float  # req/sec
    min_rate: float = 0.5
    max_rate: float = 10.0
    backoff_factor: float = 0.5
    recovery_factor: float = 1.1
    recovery_interval: float = 30.0

    _current_rate: float = field(init=False)
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)
    _last_recovery: float = field(init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _total_429s: int = 0
    _total_calls: int = 0

    def __post_init__(self):
        self._current_rate = self.initial_rate
        self._tokens = self.initial_rate
        self._last_refill = time.monotonic()
        self._last_recovery = time.monotonic()

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._current_rate, self._tokens + elapsed * self._current_rate)
        self._last_refill = now

        # Gradual recovery
        if now - self._last_recovery > self.recovery_interval:
            new_rate = min(self.max_rate, self._current_rate * self.recovery_factor)
            if new_rate > self._current_rate:
                print(f"[ADAPTIVE] Rate recovering: {self._current_rate:.2f} → {new_rate:.2f} req/s")
                self._current_rate = new_rate
            self._last_recovery = now

    async def acquire(self):
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._current_rate
                await asyncio.sleep(wait)

    def on_rate_limited(self):
        """Call this when a 429 is received to back off."""
        self._total_429s += 1
        new_rate = max(self.min_rate, self._current_rate * self.backoff_factor)
        print(f"[ADAPTIVE] 429 received! Backing off: {self._current_rate:.2f} → {new_rate:.2f} req/s")
        self._current_rate = new_rate
        self._tokens = 0  # Reset burst allowance
        self._last_recovery = time.monotonic()  # Reset recovery timer

    @property
    def stats(self) -> dict:
        return {
            "current_rate": round(self._current_rate, 2),
            "total_calls": self._total_calls,
            "total_429s": self._total_429s,
        }

limiter = AdaptiveRateLimiter(initial_rate=5.0, min_rate=0.5, max_rate=10.0)

async def adaptive_call(prompt: str, call_id: int, simulate_429: bool = False) -> str:
    await limiter.acquire()
    limiter._total_calls += 1

    # Simulate occasional 429s for demonstration
    if simulate_429 and random.random() < 0.15:
        limiter.on_rate_limited()
        await asyncio.sleep(1.0)  # Brief pause before retry
        await limiter.acquire()

    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=32,
        messages=[{"role": "user", "content": prompt}],
    )
    print(f"[{call_id:02d}] OK | rate={limiter._current_rate:.1f} req/s")
    return response.content[0].text

async def main():
    tasks = [adaptive_call(f"Question {i}", i, simulate_429=True) for i in range(12)]
    results = await asyncio.gather(*tasks)
    print(f"\nStats: {limiter.stats}")

asyncio.run(main())

# Expected Token Savings: Minimizes wasted retries by learning and respecting actual limits dynamically
# Environment: Agents operating near rate limits that fluctuate based on server load
```

### Option 6: Distributed Rate Limiter with SQLite Shared State

```python
import anthropic
import asyncio
import sqlite3
import time
from pathlib import Path
from dataclasses import dataclass

async_client = anthropic.AsyncAnthropic()
DB_PATH = Path("/tmp/rate_limiter.db")

def init_rate_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS request_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                worker_id TEXT NOT NULL,
                ts REAL NOT NULL,
                tokens_used INTEGER DEFAULT 0
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON request_log(ts)")
        conn.commit()

init_rate_db()

def count_requests_in_window(window_sec: float = 60.0) -> int:
    cutoff = time.time() - window_sec
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT COUNT(*) FROM request_log WHERE ts > ?", (cutoff,)).fetchone()
    return row[0]

def count_tokens_in_window(window_sec: float = 60.0) -> int:
    cutoff = time.time() - window_sec
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT COALESCE(SUM(tokens_used), 0) FROM request_log WHERE ts > ?", (cutoff,)).fetchone()
    return row[0]

def record_request(worker_id: str, tokens: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO request_log (worker_id, ts, tokens_used) VALUES (?, ?, ?)",
                     (worker_id, time.time(), tokens))
        conn.commit()

def cleanup_old_records(window_sec: float = 60.0):
    cutoff = time.time() - window_sec
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM request_log WHERE ts < ?", (cutoff,))
        conn.commit()

@dataclass
class DistributedRateLimiter:
    max_rpm: int = 60
    max_tpm: int = 40_000
    worker_id: str = "worker_0"
    poll_interval: float = 0.1

    async def acquire(self, estimated_tokens: int):
        while True:
            rpm = count_requests_in_window(60)
            tpm = count_tokens_in_window(60)

            if rpm < self.max_rpm and tpm + estimated_tokens <= self.max_tpm:
                record_request(self.worker_id, estimated_tokens)
                print(f"[DIST-RL] Acquired | RPM: {rpm+1}/{self.max_rpm} | TPM: {tpm+estimated_tokens}/{self.max_tpm}")
                return

            if rpm >= self.max_rpm:
                print(f"[DIST-RL] RPM limit ({rpm}/{self.max_rpm}) — waiting {self.poll_interval}s")
            if tpm + estimated_tokens > self.max_tpm:
                print(f"[DIST-RL] TPM limit ({tpm}/{self.max_tpm}) — waiting {self.poll_interval}s")

            await asyncio.sleep(self.poll_interval)

async def distributed_call(prompt: str, worker_id: str, call_id: int) -> str:
    limiter = DistributedRateLimiter(max_rpm=60, max_tpm=40_000, worker_id=worker_id)
    estimated_tokens = len(prompt) // 4 + 64

    await limiter.acquire(estimated_tokens)

    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text

async def main():
    # Simulate 3 workers sharing the same rate limit via SQLite
    tasks = []
    for call_id in range(9):
        worker = f"worker_{call_id % 3}"
        tasks.append(distributed_call(f"Answer briefly: question {call_id}", worker, call_id))

    results = await asyncio.gather(*tasks)
    cleanup_old_records()
    print(f"\nCompleted {len(results)} calls across 3 workers")

asyncio.run(main())

# Expected Token Savings: Prevents cross-worker 429s in multi-process deployments
# Environment: Multi-worker deployments (gunicorn/uvicorn) sharing a single API key's rate limit
```

## Comparison

| Option | Algorithm | Burst Support | Token-Aware | Distributed | Adaptive | Best For |
|--------|-----------|--------------|------------|-------------|---------|---------|
| 1. Simple Token Bucket | Token bucket | Yes | No | No | No | Single-process basic rate limiting |
| 2. Weighted Token Bucket | Token bucket | Yes | Yes (TPM) | No | No | TPM-limited workloads |
| 3. Multi-Tier RPM+TPM | Dual token bucket | Yes | Yes | No | No | Both RPM and TPM limits |
| 4. Sliding Window | Sliding window | Partial | No | No | No | Precise per-second rate control |
| 5. Adaptive Backoff | Token bucket + backoff | Yes | No | No | Yes | Unknown or variable rate limits |
| 6. Distributed SQLite | Sliding window | Partial | Yes | Yes | No | Multi-worker shared-key deployments |
