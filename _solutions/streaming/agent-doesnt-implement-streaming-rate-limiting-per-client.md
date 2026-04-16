---
title: "Agent Doesn't Implement Streaming Rate Limiting Per Client"
description: "Apply per-client rate limits to streaming responses to prevent bandwidth abuse, protect downstream systems, and ensure fair resource allocation across concurrent users."
difficulty: intermediate
category: streaming
tags: [streaming, rate-limiting, fairness, backpressure, resource-management]
---

## Problem

Streaming endpoints send tokens as fast as the model generates them with no per-client throttling. One heavy client can saturate bandwidth, starve other users, or trigger downstream cascades when many clients connect simultaneously. Unlike request-count rate limiting, streaming requires token-level and byte-level throttling that respects the continuous nature of SSE or WebSocket connections.

## Solutions

### Option 1: Token-Bucket Throttler for Stream Chunks

Apply a token bucket to each client that limits how many tokens per second they receive.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client = AsyncAnthropic()

@dataclass
class TokenBucket:
    capacity: float        # Max tokens to burst
    refill_rate: float     # Tokens per second
    tokens: float = field(init=False)
    last_refill: float = field(init=False)

    def __post_init__(self):
        self.tokens = self.capacity
        self.last_refill = time.monotonic()

    def consume(self, amount: float = 1.0) -> float:
        """Returns seconds to wait before consuming `amount` tokens."""
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

        if self.tokens >= amount:
            self.tokens -= amount
            return 0.0
        else:
            deficit = amount - self.tokens
            return deficit / self.refill_rate

async def throttled_stream(client_id: str, prompt: str, tokens_per_second: float = 20.0):
    """Stream response to a client at a maximum token rate."""
    bucket = TokenBucket(capacity=tokens_per_second * 2, refill_rate=tokens_per_second)
    total_tokens = 0
    start = time.monotonic()

    async with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        async for text in stream.text_stream:
            # Each chunk counts as ~1 token (approximate)
            wait = bucket.consume(1.0)
            if wait > 0:
                await asyncio.sleep(wait)

            yield text
            total_tokens += 1

    elapsed = time.monotonic() - start
    actual_rate = total_tokens / elapsed if elapsed > 0 else 0
    print(f"[{client_id}] Streamed {total_tokens} chunks at {actual_rate:.1f}/s "
          f"(limit: {tokens_per_second}/s)")

async def demo_token_bucket():
    async def collect_stream(client_id: str, prompt: str, rate: float) -> str:
        chunks = []
        async for chunk in throttled_stream(client_id, prompt, tokens_per_second=rate):
            chunks.append(chunk)
        return "".join(chunks)

    # Two concurrent clients with different rate limits
    results = await asyncio.gather(
        collect_stream("premium-user", "Explain streaming rate limiting in 3 sentences.", 50.0),
        collect_stream("free-user", "What is a token bucket?", 10.0),
    )

    for result in results:
        print(f"\nResult: {result[:100]}...")

asyncio.run(demo_token_bucket())
```

### Option 2: Concurrent Stream Limiter

Cap the number of simultaneous streaming connections per client.

```python
import asyncio
from anthropic import AsyncAnthropic
from collections import defaultdict

client = AsyncAnthropic()

class StreamLimiter:
    def __init__(self, max_concurrent_per_client: int = 3):
        self._max = max_concurrent_per_client
        self._semaphores: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(self._max)
        )
        self._active: dict[str, int] = defaultdict(int)

    async def stream(self, client_id: str, prompt: str):
        sem = self._semaphores[client_id]

        if sem.locked() and self._active[client_id] >= self._max:
            raise ConnectionRefusedError(
                f"Client {client_id} exceeded {self._max} concurrent streams"
            )

        async with sem:
            self._active[client_id] += 1
            try:
                async with client.messages.stream(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=200,
                    messages=[{"role": "user", "content": prompt}]
                ) as stream:
                    async for text in stream.text_stream:
                        yield text
            finally:
                self._active[client_id] -= 1

    def active_streams(self, client_id: str) -> int:
        return self._active[client_id]

async def demo_concurrent_limit():
    limiter = StreamLimiter(max_concurrent_per_client=2)

    async def run_stream(client_id: str, prompt: str, stream_id: int) -> str:
        try:
            chunks = []
            async for chunk in limiter.stream(client_id, prompt):
                chunks.append(chunk)
            return f"[{client_id}:{stream_id}] OK ({len(chunks)} chunks)"
        except ConnectionRefusedError as e:
            return f"[{client_id}:{stream_id}] BLOCKED: {e}"

    # Try 3 concurrent streams for a client with limit=2
    tasks = [
        run_stream("user-abc", f"Brief answer to question {i}", i)
        for i in range(3)
    ]
    results = await asyncio.gather(*tasks)
    for r in results:
        print(r)

asyncio.run(demo_concurrent_limit())
```

### Option 3: Bandwidth-Aware Byte-Level Throttling

Throttle by bytes per second rather than tokens, useful for binary or variable-length content.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

@dataclass
class BandwidthThrottler:
    bytes_per_second: float
    burst_bytes: float

    def __post_init__(self):
        self._available = self.burst_bytes
        self._last_check = time.monotonic()

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self._last_check
        self._available = min(self.burst_bytes, self._available + elapsed * self.bytes_per_second)
        self._last_check = now

    async def throttle(self, data: bytes) -> bytes:
        """Wait if necessary, then allow this chunk through."""
        self._refill()
        chunk_size = len(data)

        if self._available >= chunk_size:
            self._available -= chunk_size
        else:
            deficit = chunk_size - self._available
            wait_time = deficit / self.bytes_per_second
            await asyncio.sleep(wait_time)
            self._refill()
            self._available -= chunk_size

        return data

class PerClientBandwidthManager:
    def __init__(self):
        # Tier-based limits: bytes per second
        self._tiers = {
            "free":      BandwidthThrottler(bytes_per_second=512, burst_bytes=1024),
            "pro":       BandwidthThrottler(bytes_per_second=4096, burst_bytes=8192),
            "enterprise": BandwidthThrottler(bytes_per_second=32768, burst_bytes=65536),
        }

    def get_throttler(self, tier: str) -> BandwidthThrottler:
        return self._tiers.get(tier, self._tiers["free"])

    async def stream_to_client(self, client_id: str, tier: str, prompt: str):
        throttler = self.get_throttler(tier)
        total_bytes = 0
        start = time.monotonic()

        async with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        ) as stream:
            async for text in stream.text_stream:
                data = text.encode("utf-8")
                await throttler.throttle(data)
                yield data.decode("utf-8")
                total_bytes += len(data)

        elapsed = time.monotonic() - start
        actual_bps = total_bytes / elapsed if elapsed > 0 else 0
        limit_bps = throttler.bytes_per_second
        print(f"[{client_id}/{tier}] {total_bytes}B in {elapsed:.2f}s "
              f"({actual_bps:.0f} B/s, limit: {limit_bps:.0f} B/s)")

async def demo_bandwidth_throttling():
    manager = PerClientBandwidthManager()
    prompt = "Explain how bandwidth throttling works in streaming APIs."

    async def collect(client_id: str, tier: str) -> str:
        chunks = []
        async for chunk in manager.stream_to_client(client_id, tier, prompt):
            chunks.append(chunk)
        return "".join(chunks)

    results = await asyncio.gather(
        collect("user-1", "free"),
        collect("user-2", "pro"),
    )
    for r in results:
        print(f"Response: {r[:80]}...")

asyncio.run(demo_bandwidth_throttling())
```

### Option 4: Sliding Window Request Rate Limiter

Track requests-per-minute per client using a sliding window, rejecting or queuing excess.

```python
import asyncio
import time
from collections import deque
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client = AsyncAnthropic()

@dataclass
class SlidingWindowLimiter:
    max_requests: int       # Max requests in window
    window_seconds: float   # Window duration
    _timestamps: deque = field(default_factory=deque)

    def is_allowed(self) -> tuple[bool, float]:
        """Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        cutoff = now - self.window_seconds

        # Remove expired entries
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

        if len(self._timestamps) < self.max_requests:
            self._timestamps.append(now)
            return True, 0.0
        else:
            # Return when the oldest request will expire
            oldest = self._timestamps[0]
            retry_after = (oldest + self.window_seconds) - now
            return False, max(0.0, retry_after)

class SlidingWindowStreamGateway:
    def __init__(self):
        self._limiters: dict[str, SlidingWindowLimiter] = {}

    def _get_limiter(self, client_id: str, tier: str) -> SlidingWindowLimiter:
        if client_id not in self._limiters:
            limits = {"free": 5, "pro": 30, "enterprise": 200}
            rpm = limits.get(tier, 5)
            self._limiters[client_id] = SlidingWindowLimiter(
                max_requests=rpm,
                window_seconds=60.0
            )
        return self._limiters[client_id]

    async def stream(self, client_id: str, tier: str, prompt: str):
        limiter = self._get_limiter(client_id, tier)
        allowed, retry_after = limiter.is_allowed()

        if not allowed:
            raise Exception(
                f"Rate limit exceeded for {client_id}. "
                f"Retry after {retry_after:.1f}s"
            )

        async with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}]
        ) as stream:
            async for text in stream.text_stream:
                yield text

async def demo_sliding_window():
    gateway = SlidingWindowStreamGateway()

    async def attempt_stream(client_id: str, tier: str, req_num: int) -> str:
        try:
            chunks = []
            async for chunk in gateway.stream(client_id, tier, "Say hello briefly."):
                chunks.append(chunk)
            return f"[{client_id}] Request {req_num}: OK"
        except Exception as e:
            return f"[{client_id}] Request {req_num}: {e}"

    # Free tier allows 5/min — try 7 rapid requests
    results = await asyncio.gather(*[
        attempt_stream("free-user-1", "free", i) for i in range(7)
    ])
    for r in results:
        print(r)

asyncio.run(demo_sliding_window())
```

### Option 5: Priority Queue with Fair Scheduling

Queue streaming requests and serve them in priority order to prevent starvation.

```python
import asyncio
import time
import heapq
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from typing import AsyncIterator

client = AsyncAnthropic()

@dataclass(order=True)
class StreamRequest:
    priority: int               # Lower = higher priority (1=high, 3=low)
    enqueued_at: float = field(compare=True)
    client_id: str = field(compare=False)
    prompt: str = field(compare=False)
    result_queue: asyncio.Queue = field(compare=False, default_factory=asyncio.Queue)

class FairStreamScheduler:
    def __init__(self, max_concurrent: int = 3):
        self._queue: list[StreamRequest] = []
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._worker_task: asyncio.Task | None = None
        self._running = False

    async def start(self):
        self._running = True
        self._worker_task = asyncio.create_task(self._worker())

    async def stop(self):
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()

    async def submit(self, client_id: str, prompt: str, priority: int = 2) -> StreamRequest:
        req = StreamRequest(
            priority=priority,
            enqueued_at=time.monotonic(),
            client_id=client_id,
            prompt=prompt,
        )
        heapq.heappush(self._queue, req)
        return req

    async def _worker(self):
        while self._running:
            if not self._queue:
                await asyncio.sleep(0.01)
                continue

            req = heapq.heappop(self._queue)
            asyncio.create_task(self._serve(req))

    async def _serve(self, req: StreamRequest):
        async with self._semaphore:
            wait_time = time.monotonic() - req.enqueued_at
            print(f"[Scheduler] Serving {req.client_id} (priority={req.priority}, "
                  f"waited={wait_time:.2f}s)")
            try:
                async with client.messages.stream(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=100,
                    messages=[{"role": "user", "content": req.prompt}]
                ) as stream:
                    full_text = await stream.get_final_text()
                    await req.result_queue.put(("ok", full_text))
            except Exception as e:
                await req.result_queue.put(("error", str(e)))

async def demo_fair_scheduling():
    scheduler = FairStreamScheduler(max_concurrent=2)
    await scheduler.start()

    # Submit mix of priorities
    requests = [
        ("enterprise-A", "Tell me a fact.", 1),   # High priority
        ("free-B", "Tell me a fact.", 3),          # Low priority
        ("pro-C", "Tell me a fact.", 2),            # Medium priority
        ("enterprise-D", "Tell me a fact.", 1),    # High priority
        ("free-E", "Tell me a fact.", 3),           # Low priority
    ]

    submitted = []
    for client_id, prompt, priority in requests:
        req = await scheduler.submit(client_id, prompt, priority)
        submitted.append(req)

    # Wait for all results
    for req in submitted:
        status, result = await asyncio.wait_for(req.result_queue.get(), timeout=30.0)
        print(f"[{req.client_id}] {status}: {result[:60]}")

    await scheduler.stop()

asyncio.run(demo_fair_scheduling())
```

### Option 6: Adaptive Rate Limiting Based on Server Load

Dynamically tighten per-client limits when server load spikes.

```python
import asyncio
import time
import psutil
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

@dataclass
class LoadAwareRateLimiter:
    base_rps: float = 10.0      # Requests per second at low load
    min_rps: float = 1.0        # Floor during high load
    high_load_threshold: float = 0.80   # CPU % to start throttling
    critical_load_threshold: float = 0.95

    def current_limit(self) -> float:
        try:
            cpu = psutil.cpu_percent(interval=None) / 100.0
        except Exception:
            cpu = 0.5  # Assume moderate load if psutil unavailable

        if cpu >= self.critical_load_threshold:
            return self.min_rps
        elif cpu >= self.high_load_threshold:
            # Linear interpolation between base and min
            ratio = (cpu - self.high_load_threshold) / (
                self.critical_load_threshold - self.high_load_threshold
            )
            return self.base_rps - ratio * (self.base_rps - self.min_rps)
        else:
            return self.base_rps

class AdaptiveStreamGateway:
    def __init__(self):
        self._limiter = LoadAwareRateLimiter()
        self._client_last_request: dict[str, float] = {}

    def _check_rate(self, client_id: str) -> tuple[bool, float]:
        now = time.monotonic()
        limit = self._limiter.current_limit()
        min_interval = 1.0 / limit

        last = self._client_last_request.get(client_id, 0.0)
        elapsed = now - last

        if elapsed >= min_interval:
            self._client_last_request[client_id] = now
            return True, 0.0
        else:
            return False, min_interval - elapsed

    async def stream(self, client_id: str, prompt: str):
        allowed, retry_after = self._check_rate(client_id)

        if not allowed:
            current_limit = self._limiter.current_limit()
            raise Exception(
                f"Adaptive rate limit: {current_limit:.1f} RPS current. "
                f"Retry in {retry_after:.2f}s"
            )

        async with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}]
        ) as stream:
            current_limit = self._limiter.current_limit()
            async for text in stream.text_stream:
                yield text

async def demo_adaptive_rate_limiting():
    gateway = AdaptiveStreamGateway()

    async def try_stream(client_id: str, req_num: int) -> str:
        try:
            chunks = []
            async for chunk in gateway.stream(client_id, f"Brief response {req_num}"):
                chunks.append(chunk)
            return f"[{client_id}] req {req_num}: OK"
        except Exception as e:
            return f"[{client_id}] req {req_num}: LIMITED ({e})"

    # Simulate burst from 3 clients
    tasks = []
    for client_id in ["user-1", "user-2", "user-3"]:
        for req_num in range(3):
            tasks.append(try_stream(client_id, req_num))
            await asyncio.sleep(0.05)  # Slight stagger

    results = await asyncio.gather(*tasks)
    for r in results:
        print(r)

asyncio.run(demo_adaptive_rate_limiting())
```

## Comparison

| Approach | Throttle Dimension | Fairness | Complexity | Best For |
|---|---|---|---|---|
| Token-Bucket Throttler | Tokens/chunks per second | Per-client | Low | General streaming endpoints |
| Concurrent Stream Limiter | Simultaneous connections | Per-client | Low | WebSocket / long-lived streams |
| Byte-Level Throttling | Bytes per second | Tier-based | Medium | Binary / variable-length streams |
| Sliding Window Limiter | Requests per minute | Per-client | Low | API gateway / REST+SSE |
| Priority Queue Scheduler | Queue position | Priority+FIFO | High | Multi-tenant with SLA tiers |
| Adaptive Rate Limiting | Load-based dynamic | Per-client | Medium | Auto-scaling environments |

**Choose Token-Bucket Throttler** as the default—it handles burstiness gracefully while enforcing a steady-state limit. **Choose Adaptive Rate Limiting** when your service runs on shared infrastructure and needs automatic protection against overload. **Choose Priority Queue Scheduler** when you have enterprise SLA tiers that require guaranteed throughput even under load.
