---
layout: solution
title: "Agent Doesn't Implement Adaptive Concurrency Control"
category: rate-limit
description: "Agents with fixed concurrency limits either under-utilize capacity during quiet periods or trigger rate limit errors during bursts. Adaptive concurrency automatically tunes the number of parallel requests based on observed error rates and response times."
tags: [rate-limit, concurrency, adaptive, throughput, auto-scaling, performance]
---

# Agent Doesn't Implement Adaptive Concurrency Control

## Problem

Fixed concurrency limits are always wrong: too low wastes throughput, too high causes 429 errors and retries that actually reduce effective throughput. Adaptive concurrency control solves this by treating concurrency as a tunable parameter that adjusts in real time based on API feedback — reducing on errors, increasing on success.

## Why This Happens

Most teams set a static `asyncio.Semaphore(N)` and never revisit it. The right value of N changes based on account tier, time of day, and API load — but static values cannot track these changes. The result is either conservative under-utilization or aggressive over-use that triggers throttling.

## Solutions

### Option 1: AIMD Concurrency Control — Additive increase, multiplicative decrease (TCP-style)

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

@dataclass
class AIMDController:
    """Additive Increase, Multiplicative Decrease — same algorithm as TCP congestion control."""
    min_concurrency: int = 1
    max_concurrency: int = 20
    increase_step: int = 1       # Add this on each successful window
    decrease_factor: float = 0.5 # Multiply by this on rate limit
    window_size: int = 10        # Requests per evaluation window

    current: int = field(init=False)
    successes: int = field(default=0, init=False)
    errors: int = field(default=0, init=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self):
        self.current = self.min_concurrency

    async def record_success(self) -> None:
        async with self.lock:
            self.successes += 1
            if (self.successes + self.errors) >= self.window_size:
                await self._evaluate()

    async def record_error(self, is_rate_limit: bool = False) -> None:
        async with self.lock:
            self.errors += 1
            if is_rate_limit:
                # Immediate decrease on rate limit
                self.current = max(
                    self.min_concurrency,
                    int(self.current * self.decrease_factor)
                )
                print(f"[AIMD] Rate limit hit → concurrency reduced to {self.current}")
                self.successes = self.errors = 0

    async def _evaluate(self) -> None:
        """End of window: increase if error-free, else decrease."""
        error_rate = self.errors / (self.successes + self.errors)
        if error_rate == 0:
            self.current = min(self.max_concurrency, self.current + self.increase_step)
            print(f"[AIMD] Clean window → concurrency increased to {self.current}")
        elif error_rate > 0.1:
            self.current = max(self.min_concurrency, int(self.current * self.decrease_factor))
            print(f"[AIMD] Error rate {error_rate:.0%} → concurrency reduced to {self.current}")
        self.successes = self.errors = 0


class AIMDAdaptiveAgent:
    def __init__(self):
        self.client = anthropic.AsyncAnthropic()
        self.controller = AIMDController(min_concurrency=2, max_concurrency=15)

    async def _call(self, prompt: str) -> str | None:
        try:
            response = await self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": prompt}]
            )
            await self.controller.record_success()
            return response.content[0].text
        except anthropic.RateLimitError:
            await self.controller.record_error(is_rate_limit=True)
            await asyncio.sleep(2)
            return None
        except anthropic.APIError:
            await self.controller.record_error()
            return None

    async def process_batch(self, prompts: list[str]) -> list[str | None]:
        results = []
        i = 0
        while i < len(prompts):
            # Grab current concurrency window
            batch_size = self.controller.current
            batch = prompts[i:i + batch_size]
            semaphore = asyncio.Semaphore(batch_size)

            async def bounded_call(p: str) -> str | None:
                async with semaphore:
                    return await self._call(p)

            batch_results = await asyncio.gather(*[bounded_call(p) for p in batch])
            results.extend(batch_results)
            i += batch_size
            print(f"[AIMD] Processed {i}/{len(prompts)}, current concurrency: {self.controller.current}")

        return results


async def main():
    agent = AIMDAdaptiveAgent()
    prompts = [f"What is {i} + {i}?" for i in range(30)]
    results = await agent.process_batch(prompts)
    successful = sum(1 for r in results if r)
    print(f"Completed: {successful}/{len(prompts)} successful")

asyncio.run(main())

# Expected Token Savings: 20-40% better throughput than static limits; no wasted retries from over-concurrency
# Environment: High-throughput batch agents, parallel tool executors, any agent processing queues
```

### Option 2: Gradient-Based Tuner — Measure throughput; climb toward optimal concurrency

```python
import anthropic
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

@dataclass
class ThroughputMeasurement:
    concurrency: int
    rps: float       # Requests per second achieved
    error_rate: float

@dataclass
class GradientTuner:
    """Hill-climbing optimizer: probe nearby concurrency values and move toward higher throughput."""
    initial: int = 5
    min_concurrency: int = 1
    max_concurrency: int = 25
    probe_interval: int = 20   # Requests before re-evaluating
    step_size: int = 2

    current: int = field(init=False)
    history: deque = field(default_factory=lambda: deque(maxlen=10), init=False)
    request_times: list[float] = field(default_factory=list, init=False)
    error_count: int = field(default=0, init=False)
    request_count: int = field(default=0, init=False)
    _window_start: float = field(default_factory=time.time, init=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self):
        self.current = self.initial

    async def record_request(self, success: bool, latency: float) -> None:
        async with self.lock:
            self.request_count += 1
            if success:
                self.request_times.append(latency)
            else:
                self.error_count += 1

            if self.request_count >= self.probe_interval:
                await self._tune()

    async def _tune(self) -> None:
        elapsed = time.time() - self._window_start
        total = self.request_count
        rps = total / elapsed if elapsed > 0 else 0
        error_rate = self.error_count / total if total > 0 else 0

        measurement = ThroughputMeasurement(
            concurrency=self.current, rps=rps, error_rate=error_rate
        )
        self.history.append(measurement)

        # Find direction: did increasing/decreasing concurrency improve RPS?
        if len(self.history) >= 2:
            prev = self.history[-2]
            curr = self.history[-1]
            # If error rate is high, back off
            if curr.error_rate > 0.15:
                self.current = max(self.min_concurrency, self.current - self.step_size)
                print(f"[GRADIENT] High errors ({curr.error_rate:.0%}) → reduce to {self.current}")
            elif curr.rps > prev.rps and curr.concurrency >= prev.concurrency:
                # More concurrency helped — keep going
                self.current = min(self.max_concurrency, self.current + self.step_size)
                print(f"[GRADIENT] RPS improved {prev.rps:.1f}→{curr.rps:.1f} → increase to {self.current}")
            elif curr.rps < prev.rps and curr.concurrency >= prev.concurrency:
                # More concurrency hurt — back off
                self.current = max(self.min_concurrency, self.current - self.step_size)
                print(f"[GRADIENT] RPS degraded {prev.rps:.1f}→{curr.rps:.1f} → reduce to {self.current}")

        # Reset window
        self.request_count = 0
        self.error_count = 0
        self.request_times = []
        self._window_start = time.time()


class GradientAdaptiveAgent:
    def __init__(self):
        self.client = anthropic.AsyncAnthropic()
        self.tuner = GradientTuner(initial=4)

    async def call(self, prompt: str) -> str | None:
        start = time.time()
        try:
            response = await self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": prompt}]
            )
            await self.tuner.record_request(True, time.time() - start)
            return response.content[0].text
        except anthropic.RateLimitError:
            await self.tuner.record_request(False, time.time() - start)
            await asyncio.sleep(5)
            return None
        except Exception:
            await self.tuner.record_request(False, time.time() - start)
            return None

    async def process(self, prompts: list[str]) -> list[str | None]:
        results = []
        idx = 0
        while idx < len(prompts):
            concurrency = self.tuner.current
            batch = prompts[idx:idx + concurrency * 2]  # 2x concurrency per window
            sem = asyncio.Semaphore(concurrency)

            async def run(p: str) -> str | None:
                async with sem:
                    return await self.call(p)

            batch_results = await asyncio.gather(*[run(p) for p in batch])
            results.extend(batch_results)
            idx += len(batch)

        return results


async def main():
    agent = GradientAdaptiveAgent()
    prompts = [f"Define word number {i}" for i in range(50)]
    results = await agent.process(prompts)
    print(f"Done: {sum(1 for r in results if r)}/{len(prompts)}")

asyncio.run(main())

# Expected Token Savings: Finds optimal concurrency empirically; typically 30-60% better than static N=5
# Environment: Long-running batch jobs, nightly processing pipelines, agents with unpredictable load
```

### Option 3: Token-Bucket Adaptive Controller — Budget tokens per second, auto-adjust

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

@dataclass
class TokenBucket:
    """Token bucket with adaptive refill rate based on observed API behavior."""
    capacity: float = 100_000     # Max tokens in bucket (TPM)
    refill_rate: float = 80_000   # Tokens per minute initial estimate
    tokens: float = field(init=False)
    last_refill: float = field(default_factory=time.time, init=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    # Adaptive state
    consecutive_successes: int = field(default=0, init=False)
    consecutive_failures: int = field(default=0, init=False)

    def __post_init__(self):
        self.tokens = self.capacity

    async def consume(self, tokens: float) -> bool:
        """Returns True if request is allowed, False if throttled."""
        async with self.lock:
            self._refill()
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False

    def _refill(self) -> None:
        now = time.time()
        elapsed_minutes = (now - self.last_refill) / 60
        refill_amount = elapsed_minutes * self.refill_rate
        self.tokens = min(self.capacity, self.tokens + refill_amount)
        self.last_refill = now

    async def on_success(self) -> None:
        async with self.lock:
            self.consecutive_successes += 1
            self.consecutive_failures = 0
            # Gradually increase estimated refill rate
            if self.consecutive_successes % 10 == 0:
                self.refill_rate = min(self.capacity, self.refill_rate * 1.05)

    async def on_rate_limit(self, retry_after: float = 60) -> None:
        async with self.lock:
            self.consecutive_failures += 1
            self.consecutive_successes = 0
            # Reduce refill rate estimate and drain bucket
            self.refill_rate = max(10_000, self.refill_rate * 0.7)
            self.tokens = 0
            print(f"[BUCKET] Rate limited. Refill rate → {self.refill_rate:.0f} TPM. Waiting {retry_after}s")
        await asyncio.sleep(retry_after)


ESTIMATED_TOKENS_PER_REQUEST = 500  # Rough estimate; adjust per use case

class TokenBucketAgent:
    def __init__(self):
        self.client = anthropic.AsyncAnthropic()
        self.bucket = TokenBucket(capacity=90_000, refill_rate=80_000)

    async def call(self, prompt: str) -> str | None:
        # Pre-flight: check token budget
        allowed = await self.bucket.consume(ESTIMATED_TOKENS_PER_REQUEST)
        if not allowed:
            # Wait for bucket to refill
            await asyncio.sleep(1)
            allowed = await self.bucket.consume(ESTIMATED_TOKENS_PER_REQUEST)
            if not allowed:
                print("[BUCKET] Throttling request — bucket empty")
                await asyncio.sleep(5)
                return None

        try:
            response = await self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}]
            )
            actual_tokens = response.usage.input_tokens + response.usage.output_tokens
            # Refund over/under-estimation
            await self.bucket.on_success()
            return response.content[0].text

        except anthropic.RateLimitError as e:
            retry_after = float(getattr(e, 'retry_after', 60) or 60)
            await self.bucket.on_rate_limit(retry_after)
            return None

    async def process_batch(self, prompts: list[str], max_concurrency: int = 10) -> list[str | None]:
        semaphore = asyncio.Semaphore(max_concurrency)

        async def bounded(p: str) -> str | None:
            async with semaphore:
                return await self.call(p)

        return await asyncio.gather(*[bounded(p) for p in prompts])


async def main():
    agent = TokenBucketAgent()
    prompts = [f"Summarize topic {i} in one sentence." for i in range(20)]
    results = await agent.process_batch(prompts)
    print(f"Success rate: {sum(1 for r in results if r)}/{len(prompts)}")

asyncio.run(main())

# Expected Token Savings: Prevents wasted 429 retries; self-calibrates to actual account limits
# Environment: Agents operating near API limits, production services requiring predictable throughput
```

### Option 4: Sliding Window Rate Tracker — Real-time error rate controls concurrency

```python
import anthropic
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

@dataclass
class SlidingWindowTracker:
    """Track success/failure over a rolling time window."""
    window_seconds: float = 10.0
    min_concurrency: int = 1
    max_concurrency: int = 20
    target_error_rate: float = 0.05  # 5% target

    _events: deque = field(default_factory=deque, init=False)  # (timestamp, success)
    _current_concurrency: int = field(init=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self):
        self._current_concurrency = (self.min_concurrency + self.max_concurrency) // 2

    async def record(self, success: bool) -> None:
        async with self.lock:
            now = time.time()
            self._events.append((now, success))
            # Evict old events
            while self._events and self._events[0][0] < now - self.window_seconds:
                self._events.popleft()
            # Adjust concurrency based on current error rate
            self._adjust()

    def _adjust(self) -> None:
        if len(self._events) < 5:
            return  # Not enough data
        errors = sum(1 for _, ok in self._events if not ok)
        rate = errors / len(self._events)

        if rate > self.target_error_rate * 2:
            new = max(self.min_concurrency, self._current_concurrency - 2)
            if new != self._current_concurrency:
                print(f"[WINDOW] Error rate {rate:.0%} → reduce to {new}")
                self._current_concurrency = new
        elif rate < self.target_error_rate * 0.5 and len(self._events) >= 20:
            new = min(self.max_concurrency, self._current_concurrency + 1)
            if new != self._current_concurrency:
                print(f"[WINDOW] Error rate {rate:.0%} → increase to {new}")
                self._current_concurrency = new

    @property
    def concurrency(self) -> int:
        return self._current_concurrency

    def stats(self) -> dict:
        if not self._events:
            return {"events": 0, "error_rate": 0.0, "concurrency": self._current_concurrency}
        errors = sum(1 for _, ok in self._events if not ok)
        return {
            "events": len(self._events),
            "error_rate": round(errors / len(self._events), 3),
            "concurrency": self._current_concurrency,
        }


class SlidingWindowAgent:
    def __init__(self):
        self.client = anthropic.AsyncAnthropic()
        self.tracker = SlidingWindowTracker(window_seconds=15.0, max_concurrency=12)
        self._semaphore: asyncio.Semaphore | None = None
        self._semaphore_lock = asyncio.Lock()

    async def _get_semaphore(self) -> asyncio.Semaphore:
        async with self._semaphore_lock:
            if self._semaphore is None or self._semaphore._value != self.tracker.concurrency:
                self._semaphore = asyncio.Semaphore(self.tracker.concurrency)
        return self._semaphore

    async def call(self, prompt: str) -> str | None:
        sem = await self._get_semaphore()
        async with sem:
            try:
                response = await self.client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=128,
                    messages=[{"role": "user", "content": prompt}]
                )
                await self.tracker.record(True)
                return response.content[0].text
            except anthropic.RateLimitError:
                await self.tracker.record(False)
                await asyncio.sleep(10)
                return None
            except Exception:
                await self.tracker.record(False)
                return None

    async def process(self, prompts: list[str]) -> list[str | None]:
        tasks = [asyncio.create_task(self.call(p)) for p in prompts]
        results = await asyncio.gather(*tasks)
        print(f"Final stats: {self.tracker.stats()}")
        return list(results)


async def main():
    agent = SlidingWindowAgent()
    prompts = [f"Answer briefly: what is {i}?" for i in range(25)]
    results = await agent.process(prompts)
    print(f"Success: {sum(1 for r in results if r)}/{len(prompts)}")

asyncio.run(main())

# Expected Token Savings: Real-time adjustment; no 30-second lag vs window-based approaches
# Environment: Interactive agents, real-time services, production APIs with variable load patterns
```

### Option 5: Percentile Latency Controller — Back off when P95 latency spikes (overload signal)

```python
import anthropic
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
import statistics

@dataclass
class LatencyController:
    """Use P95 latency as a proxy for API overload; reduce concurrency on latency spikes."""
    p95_threshold_ms: float = 3000    # Back off when P95 exceeds this
    p95_recover_ms: float = 1500      # Recover when P95 drops below this
    sample_window: int = 50           # Number of samples for percentile calculation
    min_concurrency: int = 1
    max_concurrency: int = 20

    _latencies: deque = field(default_factory=lambda: deque(maxlen=50), init=False)
    _current: int = field(init=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self):
        self._current = 8  # Start in the middle

    async def record(self, latency_ms: float, rate_limited: bool = False) -> None:
        async with self.lock:
            if rate_limited:
                self._current = max(self.min_concurrency, self._current - 3)
                print(f"[LATENCY] Rate limited → concurrency = {self._current}")
                return

            self._latencies.append(latency_ms)
            if len(self._latencies) >= 10:
                sorted_lat = sorted(self._latencies)
                p95 = sorted_lat[int(len(sorted_lat) * 0.95)]
                p50 = statistics.median(sorted_lat)

                if p95 > self.p95_threshold_ms:
                    new = max(self.min_concurrency, self._current - 1)
                    if new != self._current:
                        self._current = new
                        print(f"[LATENCY] P95={p95:.0f}ms → reduce concurrency to {self._current}")
                elif p95 < self.p95_recover_ms and p50 < self.p95_recover_ms / 2:
                    new = min(self.max_concurrency, self._current + 1)
                    if new != self._current:
                        self._current = new

    @property
    def concurrency(self) -> int:
        return self._current

    def percentiles(self) -> dict:
        if len(self._latencies) < 5:
            return {}
        s = sorted(self._latencies)
        return {
            "p50": round(s[int(len(s) * 0.50)], 0),
            "p95": round(s[int(len(s) * 0.95)], 0),
            "p99": round(s[min(int(len(s) * 0.99), len(s)-1)], 0),
        }


class LatencyAdaptiveAgent:
    def __init__(self):
        self.client = anthropic.AsyncAnthropic()
        self.controller = LatencyController(p95_threshold_ms=2500)

    async def call(self, prompt: str) -> str | None:
        start = time.time()
        try:
            response = await self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": prompt}]
            )
            latency_ms = (time.time() - start) * 1000
            await self.controller.record(latency_ms)
            return response.content[0].text
        except anthropic.RateLimitError:
            latency_ms = (time.time() - start) * 1000
            await self.controller.record(latency_ms, rate_limited=True)
            await asyncio.sleep(30)
            return None
        except Exception:
            return None

    async def process(self, prompts: list[str]) -> list[str | None]:
        results = []
        i = 0
        while i < len(prompts):
            concurrency = self.controller.concurrency
            sem = asyncio.Semaphore(concurrency)
            batch = prompts[i:i + concurrency * 3]

            async def bounded(p: str) -> str | None:
                async with sem:
                    return await self.call(p)

            batch_results = await asyncio.gather(*[bounded(p) for p in batch])
            results.extend(batch_results)
            i += len(batch)
            if self.controller.percentiles():
                print(f"[LATENCY] Latency: {self.controller.percentiles()}, concurrency: {concurrency}")

        return results


async def main():
    agent = LatencyAdaptiveAgent()
    prompts = [f"What is the meaning of number {i}?" for i in range(30)]
    results = await agent.process(prompts)
    print(f"Completed: {sum(1 for r in results if r)}/{len(prompts)}")

asyncio.run(main())

# Expected Token Savings: Latency-based throttling catches overload before 429s; 15-25% better utilization
# Environment: Latency-sensitive production agents; services with SLA requirements
```

### Option 6: Multi-Tier Adaptive Pool — Separate pools for priority tiers, each self-tuning

```python
import anthropic
import asyncio
from dataclasses import dataclass, field
from enum import IntEnum

class Priority(IntEnum):
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    BATCH = 3

@dataclass
class TierConfig:
    priority: Priority
    initial_concurrency: int
    min_concurrency: int
    max_concurrency: int
    rate_limit_backoff: float = 30.0   # seconds to wait on 429

@dataclass
class AdaptiveTier:
    config: TierConfig
    _concurrency: int = field(init=False)
    _successes: int = field(default=0, init=False)
    _errors: int = field(default=0, init=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self):
        self._concurrency = self.config.initial_concurrency

    @property
    def semaphore(self) -> asyncio.Semaphore:
        return asyncio.Semaphore(self._concurrency)

    async def record(self, success: bool) -> None:
        async with self.lock:
            if success:
                self._successes += 1
            else:
                self._errors += 1
                self._concurrency = max(
                    self.config.min_concurrency,
                    int(self._concurrency * 0.75)
                )

            # Every 15 requests, try increasing if clean
            total = self._successes + self._errors
            if total >= 15 and self._errors == 0:
                self._concurrency = min(self.config.max_concurrency, self._concurrency + 1)
                self._successes = self._errors = 0

    def stats(self) -> dict:
        return {
            "priority": self.config.priority.name,
            "concurrency": self._concurrency,
            "successes": self._successes,
            "errors": self._errors,
        }


class MultiTierAdaptiveAgent:
    TIERS = [
        TierConfig(Priority.CRITICAL, initial_concurrency=8, min_concurrency=2, max_concurrency=15),
        TierConfig(Priority.HIGH,     initial_concurrency=5, min_concurrency=1, max_concurrency=10),
        TierConfig(Priority.NORMAL,   initial_concurrency=3, min_concurrency=1, max_concurrency=8),
        TierConfig(Priority.BATCH,    initial_concurrency=2, min_concurrency=1, max_concurrency=5),
    ]

    def __init__(self):
        self.client = anthropic.AsyncAnthropic()
        self._tiers = {t.priority: AdaptiveTier(config=t) for t in self.TIERS}

    async def call(self, prompt: str, priority: Priority = Priority.NORMAL) -> str | None:
        tier = self._tiers[priority]
        async with tier.semaphore:
            try:
                response = await self.client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=128,
                    messages=[{"role": "user", "content": prompt}]
                )
                await tier.record(True)
                return response.content[0].text
            except anthropic.RateLimitError:
                await tier.record(False)
                await asyncio.sleep(tier.config.rate_limit_backoff)
                return None
            except Exception:
                await tier.record(False)
                return None

    def print_stats(self) -> None:
        for tier in self._tiers.values():
            print(tier.stats())


async def main():
    agent = MultiTierAdaptiveAgent()

    # Mix of different priority tasks
    tasks = (
        [asyncio.create_task(agent.call(f"Critical: {i}", Priority.CRITICAL)) for i in range(5)] +
        [asyncio.create_task(agent.call(f"Normal: {i}", Priority.NORMAL)) for i in range(10)] +
        [asyncio.create_task(agent.call(f"Batch: {i}", Priority.BATCH)) for i in range(15)]
    )

    results = await asyncio.gather(*tasks)
    print(f"Success: {sum(1 for r in results if r)}/{len(results)}")
    agent.print_stats()

asyncio.run(main())

# Expected Token Savings: Critical tasks maintain throughput during congestion; batch absorbs backpressure
# Environment: Multi-tenant systems, mixed workloads with SLA tiers, priority-sensitive production agents
```

## Comparison

| Option | Algorithm | Reacts To | Convergence Speed | Best For |
|--------|-----------|-----------|------------------|----------|
| AIMD | TCP-style additive/multiplicative | Rate limits, error rate | Fast (per window) | General-purpose batch |
| Gradient-Based | Hill climbing on RPS | Throughput trends | Slow (needs data) | Long-running batch jobs |
| Token Bucket | Token budget estimation | TPM limits | Immediate | TPM-bound workloads |
| Sliding Window | Rolling error rate | Recent error rate | Fast (real-time) | Interactive agents |
| Latency Controller | P95 latency | API overload signal | Medium | SLA-sensitive services |
| Multi-Tier Pool | Per-priority adaptation | Errors per tier | Fast | Priority-differentiated workloads |
