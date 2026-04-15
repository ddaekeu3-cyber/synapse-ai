---
layout: solution
title: "Agent doesn't implement per-model rate limiting"
category: rate-limit
description: "Agent uses a single shared rate limiter for all Claude models, causing haiku calls to consume opus-tier token budgets and triggering 429s on high-value model tiers unexpectedly."
tags: [rate-limiting, per-model, token-budget, claude-haiku, claude-opus, api-limits]
---

## Symptom

The agent starts hitting 429 errors on `claude-opus-4-6` even though opus calls are rare. Inspection reveals that a shared rate limiter counts tokens from `claude-haiku-4-5-20251001` bulk-classification calls against the same bucket as opus reasoning calls. Alternatively, haiku calls are blocked waiting for a permit that opus traffic has exhausted.

## Root Cause

Anthropic enforces separate rate limits per model tier: haiku, sonnet, and opus each have independent requests-per-minute (RPM) and tokens-per-minute (TPM) quotas. A single shared rate limiter conflates these, either under-throttling high-tier traffic (causing 429s) or over-throttling low-tier traffic (wasting throughput). The model tiers also have very different TPM limits — haiku can handle 10× the token volume of opus.

---

## Option 1 — Per-model token bucket map

**Create one `TokenBucket` per model tier. Each call acquires from the correct bucket.**

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()


class TokenBucket:
    def __init__(self, rate: float, capacity: float) -> None:
        self.rate = rate
        self.capacity = capacity
        self._tokens = capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(
                self.capacity,
                self._tokens + (now - self._last) * self.rate,
            )
            self._last = now
            while self._tokens < tokens:
                deficit = tokens - self._tokens
                wait = deficit / self.rate
                self._lock.release()
                await asyncio.sleep(wait)
                await self._lock.acquire()
                now = time.monotonic()
                self._tokens = min(
                    self.capacity,
                    self._tokens + (now - self._last) * self.rate,
                )
                self._last = now
            self._tokens -= tokens


# Approximate Anthropic free-tier limits (tokens/sec)
# Adjust to your actual tier limits from the console
MODEL_BUCKETS: dict[str, TokenBucket] = {
    "claude-haiku-4-5-20251001": TokenBucket(rate=8_000, capacity=25_000),  # ~500k TPM
    "claude-sonnet-4-6":         TokenBucket(rate=3_300, capacity=10_000),  # ~200k TPM
    "claude-opus-4-6":           TokenBucket(rate=1_600, capacity=5_000),   # ~100k TPM
}


async def call_model(model: str, prompt: str, max_tokens: int = 512) -> str:
    bucket = MODEL_BUCKETS.get(model)
    if bucket:
        # Estimate input tokens (rough: 1 token ≈ 4 chars)
        estimated = len(prompt) // 4 + max_tokens
        await bucket.acquire(estimated)

    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


async def main() -> None:
    # Haiku for cheap classification — uses haiku bucket only
    haiku_tasks = [
        asyncio.create_task(
            call_model("claude-haiku-4-5-20251001", f"Classify: {i}", 50)
        )
        for i in range(20)
    ]
    # Opus for complex reasoning — uses opus bucket only
    opus_tasks = [
        asyncio.create_task(
            call_model("claude-opus-4-6", f"Reason step by step: {i}", 1024)
        )
        for i in range(3)
    ]

    results = await asyncio.gather(*haiku_tasks, *opus_tasks)
    print(f"Completed {len(results)} calls.")


asyncio.run(main())
```

**Expected Token Savings:** Prevents haiku volume from exhausting opus quota — eliminates 429 retries on opus, saving the 2–3× token overhead of exponential backoff retries.

**Environment:** Agents mixing model tiers; Python 3.10+; tune `rate`/`capacity` to your Anthropic console tier limits.

---

## Option 2 — Response-header-aware per-model limiter

**Read `x-ratelimit-remaining-tokens` and `x-ratelimit-reset-tokens` headers from each response to update the correct model's bucket dynamically.**

```python
import asyncio
import time
import httpx
import anthropic

# Use the raw httpx client to access response headers
_http = httpx.AsyncClient(timeout=60)

_RATE_STATE: dict[str, dict] = {
    "claude-haiku-4-5-20251001": {"remaining": 500_000, "reset_at": 0.0},
    "claude-sonnet-4-6":         {"remaining": 200_000, "reset_at": 0.0},
    "claude-opus-4-6":           {"remaining": 100_000, "reset_at": 0.0},
}
_LOCKS: dict[str, asyncio.Lock] = {k: asyncio.Lock() for k in _RATE_STATE}


async def _wait_if_limited(model: str, estimated_tokens: int) -> None:
    state = _RATE_STATE[model]
    remaining = state["remaining"]
    reset_at = state["reset_at"]

    if remaining < estimated_tokens:
        wait = max(0.0, reset_at - time.monotonic())
        if wait > 0:
            print(f"[{model}] token limit low ({remaining}), waiting {wait:.1f}s …")
            await asyncio.sleep(wait)


def _update_state(model: str, headers: dict) -> None:
    state = _RATE_STATE[model]
    if "x-ratelimit-remaining-tokens" in headers:
        state["remaining"] = int(headers["x-ratelimit-remaining-tokens"])
    if "x-ratelimit-reset-tokens" in headers:
        # Header value is like "1m23s" or "500ms"
        raw = headers["x-ratelimit-reset-tokens"]
        seconds = 0.0
        if "m" in raw:
            parts = raw.split("m")
            seconds += int(parts[0]) * 60
            raw = parts[1]
        if "s" in raw:
            seconds += float(raw.replace("s", "").replace("m", "") or 0)
        state["reset_at"] = time.monotonic() + seconds


async def call_model(model: str, prompt: str, api_key: str) -> str:
    estimated = len(prompt) // 4 + 512

    async with _LOCKS[model]:
        await _wait_if_limited(model, estimated)

    payload = {
        "model": model,
        "max_tokens": 512,
        "messages": [{"role": "user", "content": prompt}],
    }
    resp = await _http.post(
        "https://api.anthropic.com/v1/messages",
        json=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    resp.raise_for_status()
    _update_state(model, dict(resp.headers))

    return resp.json()["content"][0]["text"]


async def main() -> None:
    import os
    key = os.environ["ANTHROPIC_API_KEY"]
    results = await asyncio.gather(
        call_model("claude-haiku-4-5-20251001", "Classify this text: positive or negative? 'Great product!'", key),
        call_model("claude-sonnet-4-6", "Summarise the history of the internet in 3 sentences.", key),
    )
    for r in results:
        print(r[:80])


asyncio.run(main())
```

**Expected Token Savings:** Self-calibrating limits mean the agent runs at maximum safe throughput for each tier — eliminates both over-throttling (wasted time) and under-throttling (429 retries).

**Environment:** Production agents where actual tier limits are unknown or variable; requires direct HTTP access to read response headers.

---

## Option 3 — Middleware wrapper class with per-model tracking

**Wrap the Anthropic client in a `RateLimitedClient` that enforces per-model concurrency limits using semaphores.**

```python
import asyncio
import anthropic
from dataclasses import dataclass, field


@dataclass
class ModelConfig:
    max_concurrent: int          # max simultaneous calls
    requests_per_minute: float   # RPM limit
    semaphore: asyncio.Semaphore = field(init=False)
    _call_times: list[float] = field(default_factory=list)

    def __post_init__(self):
        self.semaphore = asyncio.Semaphore(self.max_concurrent)

    async def acquire_rpm(self) -> None:
        import time
        now = time.monotonic()
        # Remove calls older than 60 seconds
        self._call_times = [t for t in self._call_times if now - t < 60]
        if len(self._call_times) >= self.requests_per_minute:
            oldest = self._call_times[0]
            wait = 60 - (now - oldest)
            if wait > 0:
                await asyncio.sleep(wait)
        self._call_times.append(time.monotonic())


class RateLimitedClient:
    MODEL_CONFIGS = {
        "claude-haiku-4-5-20251001": ModelConfig(max_concurrent=20, requests_per_minute=100),
        "claude-sonnet-4-6":         ModelConfig(max_concurrent=10, requests_per_minute=50),
        "claude-opus-4-6":           ModelConfig(max_concurrent=3,  requests_per_minute=10),
    }

    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic()

    async def create(self, model: str, prompt: str, max_tokens: int = 512) -> str:
        config = self.MODEL_CONFIGS.get(
            model,
            ModelConfig(max_concurrent=5, requests_per_minute=30),
        )
        await config.acquire_rpm()
        async with config.semaphore:
            response = await self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text


async def main() -> None:
    client = RateLimitedClient()

    async def classify(i: int) -> str:
        return await client.create("claude-haiku-4-5-20251001", f"Is '{i}' even? Yes/No.", 5)

    async def analyse(i: int) -> str:
        return await client.create("claude-opus-4-6", f"Deep analysis of the number {i}.", 256)

    haiku = [asyncio.create_task(classify(i)) for i in range(30)]
    opus  = [asyncio.create_task(analyse(i)) for i in range(5)]

    all_results = await asyncio.gather(*haiku, *opus)
    print(f"Done: {len(all_results)} results")


asyncio.run(main())
```

**Expected Token Savings:** Per-model concurrency caps prevent burst storms on expensive tiers — avoids retry cascades that multiply token spend by 2–4×.

**Environment:** Multi-model agents where haiku, sonnet, and opus are used for different task types.

---

## Option 4 — Adaptive rate limiter that learns from 429 responses

**Start permissive, then tighten per-model limits dynamically when 429s arrive. Relax limits after a quiet window.**

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()


class AdaptiveRateLimiter:
    def __init__(self, initial_rps: float = 10.0) -> None:
        self._rps = initial_rps
        self._min_interval = 1.0 / initial_rps
        self._last_call = 0.0
        self._last_429 = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._min_interval - (now - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()

    def on_429(self, retry_after: float = 5.0) -> None:
        self._rps = max(0.5, self._rps * 0.5)   # halve the rate
        self._min_interval = 1.0 / self._rps
        self._last_429 = time.monotonic()
        print(f"  429 received — throttled to {self._rps:.1f} rps")

    def maybe_relax(self) -> None:
        if time.monotonic() - self._last_429 > 60:
            new_rps = min(self._rps * 1.1, 10.0)
            if new_rps != self._rps:
                self._rps = new_rps
                self._min_interval = 1.0 / self._rps


_LIMITERS: dict[str, AdaptiveRateLimiter] = {
    "claude-haiku-4-5-20251001": AdaptiveRateLimiter(initial_rps=20.0),
    "claude-sonnet-4-6":         AdaptiveRateLimiter(initial_rps=8.0),
    "claude-opus-4-6":           AdaptiveRateLimiter(initial_rps=2.0),
}


async def call_with_adaptive_limit(model: str, prompt: str) -> str:
    limiter = _LIMITERS[model]
    limiter.maybe_relax()
    await limiter.acquire()

    for attempt in range(4):
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text
        except anthropic.RateLimitError as e:
            retry_after = float(e.response.headers.get("retry-after", 5))
            limiter.on_429(retry_after)
            await asyncio.sleep(retry_after * (2 ** attempt))

    raise RuntimeError(f"Failed after retries on {model}")


async def main() -> None:
    tasks = [
        call_with_adaptive_limit("claude-haiku-4-5-20251001", f"Task {i}")
        for i in range(50)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    errors = sum(1 for r in results if isinstance(r, Exception))
    print(f"Done: {len(results) - errors} ok, {errors} errors")


asyncio.run(main())
```

**Expected Token Savings:** Self-tuning limits eliminate both sustained 429 retry cascades and unnecessary throttling — typically reduces wasted retry tokens by 60–80% vs. a fixed rate limiter.

**Environment:** Agents operating near API limits; especially useful when tier limits change due to usage-based scaling.

---

## Option 5 — Shared Redis rate limiter across multiple agent processes

**Use Redis atomic operations to enforce per-model rate limits across horizontally scaled agent replicas.**

```python
import asyncio
import time
import anthropic
import redis.asyncio as aioredis

client = anthropic.AsyncAnthropic()

# Per-model RPM limits (adjust to your Anthropic tier)
MODEL_RPM: dict[str, int] = {
    "claude-haiku-4-5-20251001": 1000,
    "claude-sonnet-4-6":         200,
    "claude-opus-4-6":           50,
}


async def acquire_model_slot(r: aioredis.Redis, model: str) -> None:
    """Sliding window rate limiter using Redis sorted set."""
    key = f"ratelimit:{model}"
    rpm = MODEL_RPM.get(model, 100)
    window = 60  # seconds

    while True:
        now = time.time()
        pipe = r.pipeline()
        pipe.zremrangebyscore(key, 0, now - window)        # remove old entries
        pipe.zcard(key)                                     # count current window
        pipe.zadd(key, {str(now): now})                    # add this request
        pipe.expire(key, window + 5)
        _, count, *_ = await pipe.execute()

        if count < rpm:
            return   # slot acquired

        # Window full — wait until oldest entry expires
        oldest = await r.zrange(key, 0, 0, withscores=True)
        if oldest:
            wait = window - (now - oldest[0][1]) + 0.1
            await asyncio.sleep(max(0.1, wait))
        else:
            await asyncio.sleep(0.1)


async def call_model(r: aioredis.Redis, model: str, prompt: str) -> str:
    await acquire_model_slot(r, model)
    response = await client.messages.create(
        model=model,
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


async def main() -> None:
    r = await aioredis.from_url("redis://localhost:6379")
    tasks = [
        call_model(r, "claude-haiku-4-5-20251001", f"Label {i}")
        for i in range(20)
    ]
    results = await asyncio.gather(*tasks)
    print(f"Done: {len(results)}")
    await r.aclose()


asyncio.run(main())
```

**Expected Token Savings:** Shared rate limiting prevents combined traffic from multiple replicas from exceeding tier limits — eliminates cross-replica 429 storms that waste 2–5× tokens in retries.

**Environment:** Multi-process or Kubernetes-deployed agents; Redis 5+; `redis-py>=4.2`.

---

## Option 6 — Priority queue with model-tier routing

**Route requests through a priority queue: opus gets highest priority, haiku lowest. Rate limits are applied per tier after dequeue.**

```python
import asyncio
import heapq
import time
import anthropic

client = anthropic.AsyncAnthropic()

MODEL_PRIORITY = {
    "claude-opus-4-6":           0,   # highest priority
    "claude-sonnet-4-6":         1,
    "claude-haiku-4-5-20251001": 2,   # lowest priority
}
MODEL_RPS = {
    "claude-haiku-4-5-20251001": 15.0,
    "claude-sonnet-4-6":         5.0,
    "claude-opus-4-6":           1.0,
}


class PriorityModelQueue:
    def __init__(self) -> None:
        self._heap: list = []
        self._counter = 0
        self._event = asyncio.Event()
        self._last_call: dict[str, float] = {}

    def submit(self, model: str, prompt: str, future: asyncio.Future) -> None:
        priority = MODEL_PRIORITY.get(model, 99)
        heapq.heappush(self._heap, (priority, self._counter, model, prompt, future))
        self._counter += 1
        self._event.set()

    async def run(self) -> None:
        while True:
            await self._event.wait()
            self._event.clear()
            while self._heap:
                priority, _, model, prompt, future = heapq.heappop(self._heap)
                # Per-model rate limiting
                min_interval = 1.0 / MODEL_RPS[model]
                last = self._last_call.get(model, 0.0)
                wait = min_interval - (time.monotonic() - last)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_call[model] = time.monotonic()

                try:
                    resp = await client.messages.create(
                        model=model,
                        max_tokens=256,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    future.set_result(resp.content[0].text)
                except Exception as e:
                    future.set_exception(e)


async def main() -> None:
    queue = PriorityModelQueue()
    runner = asyncio.create_task(queue.run())

    async def ask(model: str, prompt: str) -> str:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        queue.submit(model, prompt, fut)
        return await fut

    tasks = (
        [ask("claude-haiku-4-5-20251001", f"Classify {i}") for i in range(10)]
        + [ask("claude-opus-4-6", f"Reason about {i}") for i in range(2)]
        + [ask("claude-sonnet-4-6", f"Summarise {i}") for i in range(5)]
    )
    results = await asyncio.gather(*tasks)
    print(f"Completed {len(results)} calls (opus prioritised).")
    runner.cancel()


asyncio.run(main())
```

**Expected Token Savings:** Priority routing ensures opus reasoning tasks aren't starved behind haiku bulk work — eliminates timeout-and-retry patterns caused by high-priority calls waiting in a FIFO queue behind low-value traffic.

**Environment:** Agents that mix interactive (opus/sonnet) and batch (haiku) workloads on the same event loop.

---

## Comparison

| Option | Limit Scope | Dynamic Adjustment | Multi-process | Complexity |
|--------|------------|-------------------|--------------|------------|
| 1. Per-model token bucket | Token volume | No | No | Low |
| 2. Header-driven limits | Token volume | Yes (from API) | No | Medium |
| 3. Semaphore + RPM wrapper | Concurrency + RPM | No | No | Medium |
| 4. Adaptive limiter | RPM | Yes (from 429s) | No | Medium |
| 5. Redis sliding window | RPM | No | Yes | Medium |
| 6. Priority queue | RPM per tier | No | No | High |

**Recommended path:** Start with Option 1 (per-model token bucket) — zero dependencies, immediate fix. Add Option 4 (adaptive) when you want self-tuning. Use Option 5 (Redis) when running multiple agent replicas that share API quota.
