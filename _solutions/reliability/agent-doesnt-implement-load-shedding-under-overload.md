---
title: "Agent Doesn't Implement Load Shedding Under Overload"
description: "AI agents accept every incoming request regardless of system load — when upstream traffic spikes, request queues grow unboundedly, latency degrades for all users, and agents exhaust memory or hit rate limits. Without load shedding, a burst crushes the entire system rather than gracefully dropping excess work."
problem_description: |
  An agent serving production traffic has no mechanism to protect itself when incoming request rate exceeds processing capacity. As the queue grows, per-request latency increases linearly. Memory climbs. Downstream rate limits trigger. Eventually the agent crashes or becomes so slow it's functionally unavailable. The right response to overload is to shed load — reject low-priority requests immediately with a 429/503 response and a Retry-After header, so the system stays healthy for the requests it can actually serve. Without load shedding, one traffic spike degrades service for everyone instead of just the excess traffic.
category: reliability
difficulty: intermediate
tags: [load-shedding, overload, backpressure, rate-limiting, resilience]
---

## Solution 1: Queue-Depth Load Shedder

Reject incoming requests when the in-flight queue exceeds a configured depth — the simplest possible load shedder with zero external dependencies.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass


@dataclass
class ShedResult:
    shed: bool
    reason: str | None = None
    queue_depth: int = 0


class QueueDepthLoadShedder:
    def __init__(
        self,
        max_queue_depth: int = 50,
        max_tokens: int = 512,
        model: str = "claude-haiku-4-5-20251001",
    ):
        self._sem = asyncio.Semaphore(max_queue_depth)
        self._in_flight = 0
        self._max_depth = max_queue_depth
        self._shed_count = 0
        self._accept_count = 0
        self.max_tokens = max_tokens
        self.model = model

    def _check(self) -> ShedResult:
        if self._in_flight >= self._max_depth:
            return ShedResult(shed=True, reason="queue_full", queue_depth=self._in_flight)
        return ShedResult(shed=False, queue_depth=self._in_flight)

    async def handle(
        self,
        client: AsyncAnthropic,
        request_id: str,
        system_prompt: str,
        user_message: str,
    ) -> dict:
        check = self._check()
        if check.shed:
            self._shed_count += 1
            return {
                "request_id": request_id,
                "status": "shed",
                "reason": check.reason,
                "queue_depth": check.queue_depth,
                "retry_after": 5,
            }

        self._in_flight += 1
        self._accept_count += 1
        try:
            response = await client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )
            return {
                "request_id": request_id,
                "status": "ok",
                "text": response.content[0].text,
                "queue_depth": self._in_flight,
            }
        except Exception as e:
            return {"request_id": request_id, "status": "error", "error": str(e)}
        finally:
            self._in_flight -= 1

    def stats(self) -> dict:
        total = self._shed_count + self._accept_count
        return {
            "accepted": self._accept_count,
            "shed": self._shed_count,
            "shed_rate": round(self._shed_count / max(total, 1), 3),
            "current_in_flight": self._in_flight,
        }


# Usage
async def main():
    client = AsyncAnthropic()
    shedder = QueueDepthLoadShedder(max_queue_depth=5)

    system_prompt = "Answer in one sentence."
    requests = [f"req_{i:03d}" for i in range(20)]

    tasks = [
        shedder.handle(client, rid, system_prompt, "What is caching?")
        for rid in requests
    ]
    results = await asyncio.gather(*tasks)

    for r in results:
        status = r["status"]
        print(f"[{r['request_id']}] {status}" + (f" q={r.get('queue_depth')}" if status == "ok" else ""))

    print(f"\nStats: {shedder.stats()}")

asyncio.run(main())
```

## Solution 2: Token-Bucket Load Shedder with Priority Tiers

Use a token bucket to rate-limit accepted requests, and assign priority tiers — high-priority requests consume fewer tokens and are shed last when the bucket empties.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass
from enum import IntEnum


class Priority(IntEnum):
    HIGH = 1    # User-facing, interactive
    NORMAL = 2  # Standard API consumers
    LOW = 3     # Background batch jobs


@dataclass
class TokenBucket:
    capacity: float
    refill_rate: float  # tokens per second
    _tokens: float = 0.0
    _last_refill: float = 0.0

    def __post_init__(self):
        self._tokens = self.capacity
        self._last_refill = time.monotonic()

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_rate)
        self._last_refill = now

    def consume(self, tokens: float) -> bool:
        self._refill()
        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        return False

    @property
    def fill_ratio(self) -> float:
        self._refill()
        return self._tokens / self.capacity


PRIORITY_COST = {
    Priority.HIGH: 1.0,
    Priority.NORMAL: 2.0,
    Priority.LOW: 4.0,
}


class PriorityTokenBucketShedder:
    def __init__(
        self,
        capacity: float = 20.0,
        refill_rate: float = 5.0,  # tokens/sec
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 256,
    ):
        self._bucket = TokenBucket(capacity=capacity, refill_rate=refill_rate)
        self.model = model
        self.max_tokens = max_tokens
        self._stats: dict[str, int] = {"accepted": 0, "shed": 0}

    async def handle(
        self,
        client: AsyncAnthropic,
        request_id: str,
        user_message: str,
        priority: Priority = Priority.NORMAL,
        system_prompt: str = "Answer concisely.",
    ) -> dict:
        cost = PRIORITY_COST[priority]
        fill = self._bucket.fill_ratio

        if not self._bucket.consume(cost):
            self._stats["shed"] += 1
            retry_after = cost / self._bucket.refill_rate
            return {
                "request_id": request_id,
                "status": "shed",
                "priority": priority.name,
                "bucket_fill": round(fill, 2),
                "retry_after": round(retry_after, 1),
            }

        self._stats["accepted"] += 1
        try:
            response = await client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )
            return {
                "request_id": request_id,
                "status": "ok",
                "priority": priority.name,
                "bucket_fill": round(self._bucket.fill_ratio, 2),
                "text": response.content[0].text,
            }
        except Exception as e:
            return {"request_id": request_id, "status": "error", "error": str(e)}

    def stats(self) -> dict:
        total = self._stats["accepted"] + self._stats["shed"]
        return {
            **self._stats,
            "shed_rate": round(self._stats["shed"] / max(total, 1), 3),
            "bucket_fill": round(self._bucket.fill_ratio, 3),
        }


# Usage
async def main():
    client = AsyncAnthropic()
    shedder = PriorityTokenBucketShedder(capacity=10.0, refill_rate=2.0)

    priorities = [Priority.HIGH, Priority.NORMAL, Priority.LOW, Priority.NORMAL] * 5
    tasks = [
        shedder.handle(client, f"req_{i:03d}", "What is REST?", p)
        for i, p in enumerate(priorities)
    ]
    results = await asyncio.gather(*tasks)

    for r in results:
        print(f"[{r['request_id']}] {r['status']} priority={r.get('priority')} fill={r.get('bucket_fill')}")

    print(f"\nStats: {shedder.stats()}")

asyncio.run(main())
```

## Solution 3: Adaptive Load Shedder Based on Response Latency

Automatically increase shed rate when observed latency exceeds a target threshold — dynamically protecting SLOs without manual capacity planning.

```python
import asyncio
import time
import statistics
from anthropic import AsyncAnthropic
from collections import deque
from dataclasses import dataclass, field


@dataclass
class LatencyWindow:
    window_size: int = 30
    target_p95_ms: float = 1000.0
    _latencies: deque = field(default_factory=deque)

    def __post_init__(self):
        self._latencies = deque(maxlen=self.window_size)

    def record(self, latency_ms: float):
        self._latencies.append(latency_ms)

    @property
    def p95(self) -> float | None:
        if len(self._latencies) < 5:
            return None
        sorted_lat = sorted(self._latencies)
        idx = int(len(sorted_lat) * 0.95)
        return sorted_lat[min(idx, len(sorted_lat) - 1)]

    @property
    def overload_factor(self) -> float:
        """How much over-target we are: 1.0 = at target, 2.0 = 2x over target."""
        p95 = self.p95
        if p95 is None:
            return 1.0
        return p95 / self.target_p95_ms


class AdaptiveLatencyLoadShedder:
    def __init__(
        self,
        target_p95_ms: float = 800.0,
        max_shed_rate: float = 0.9,
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 256,
    ):
        self._window = LatencyWindow(target_p95_ms=target_p95_ms)
        self._max_shed_rate = max_shed_rate
        self._request_counter = 0
        self.model = model
        self.max_tokens = max_tokens
        self._shed_count = 0
        self._accept_count = 0

    @property
    def current_shed_rate(self) -> float:
        factor = self._window.overload_factor
        if factor <= 1.0:
            return 0.0
        # Linearly ramp shed rate as overload grows
        rate = min((factor - 1.0) * 0.5, self._max_shed_rate)
        return rate

    def _should_shed(self) -> bool:
        rate = self.current_shed_rate
        if rate == 0.0:
            return False
        # Deterministic shedding: shed every N-th request
        self._request_counter += 1
        shed_every = max(1, int(1.0 / rate))
        return self._request_counter % shed_every == 0

    async def handle(
        self,
        client: AsyncAnthropic,
        request_id: str,
        user_message: str,
        system_prompt: str = "Answer in one sentence.",
    ) -> dict:
        if self._should_shed():
            self._shed_count += 1
            p95 = self._window.p95
            return {
                "request_id": request_id,
                "status": "shed",
                "shed_rate": round(self.current_shed_rate, 3),
                "p95_ms": round(p95, 1) if p95 else None,
            }

        start = time.monotonic()
        self._accept_count += 1
        try:
            response = await client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )
            latency_ms = (time.monotonic() - start) * 1000
            self._window.record(latency_ms)
            return {
                "request_id": request_id,
                "status": "ok",
                "latency_ms": round(latency_ms, 1),
                "p95_ms": round(self._window.p95, 1) if self._window.p95 else None,
                "shed_rate": round(self.current_shed_rate, 3),
                "text": response.content[0].text[:60],
            }
        except Exception as e:
            latency_ms = (time.monotonic() - start) * 1000
            self._window.record(latency_ms * 3)  # Penalize errors
            return {"request_id": request_id, "status": "error", "error": str(e)}

    def stats(self) -> dict:
        total = self._accept_count + self._shed_count
        return {
            "accepted": self._accept_count,
            "shed": self._shed_count,
            "shed_rate": round(self._shed_count / max(total, 1), 3),
            "current_shed_rate": round(self.current_shed_rate, 3),
            "p95_ms": round(self._window.p95, 1) if self._window.p95 else None,
        }


# Usage
async def main():
    client = AsyncAnthropic()
    shedder = AdaptiveLatencyLoadShedder(target_p95_ms=600.0)

    tasks = [
        shedder.handle(client, f"req_{i:03d}", f"Question {i}: explain caching.")
        for i in range(30)
    ]
    results = await asyncio.gather(*tasks)

    accepted = [r for r in results if r["status"] == "ok"]
    shed = [r for r in results if r["status"] == "shed"]
    print(f"Accepted: {len(accepted)}, Shed: {len(shed)}")
    print(f"Stats: {shedder.stats()}")

asyncio.run(main())
```

## Solution 4: Probabilistic Load Shedder with Exponential Decay

Use probabilistic shedding with exponentially increasing shed probability as queue depth grows — ensuring fair random selection of which requests to drop rather than strict LIFO.

```python
import asyncio
import math
import random
import time
from anthropic import AsyncAnthropic


class ProbabilisticLoadShedder:
    def __init__(
        self,
        soft_limit: int = 20,
        hard_limit: int = 50,
        decay_factor: float = 2.0,
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 256,
    ):
        """
        soft_limit: queue depth where shedding starts (0% shed rate)
        hard_limit: queue depth where shed rate reaches 100%
        decay_factor: controls curve steepness
        """
        self._soft_limit = soft_limit
        self._hard_limit = hard_limit
        self._decay_factor = decay_factor
        self._in_flight = 0
        self.model = model
        self.max_tokens = max_tokens
        self._stats = {"accepted": 0, "shed": 0}

    def _shed_probability(self) -> float:
        depth = self._in_flight
        if depth <= self._soft_limit:
            return 0.0
        if depth >= self._hard_limit:
            return 1.0
        # Exponential ramp between soft and hard limit
        ratio = (depth - self._soft_limit) / (self._hard_limit - self._soft_limit)
        return 1 - math.exp(-self._decay_factor * ratio)

    async def handle(
        self,
        client: AsyncAnthropic,
        request_id: str,
        user_message: str,
        system_prompt: str = "Answer in one sentence.",
    ) -> dict:
        prob = self._shed_probability()

        if prob > 0 and random.random() < prob:
            self._stats["shed"] += 1
            return {
                "request_id": request_id,
                "status": "shed",
                "shed_prob": round(prob, 3),
                "queue_depth": self._in_flight,
            }

        self._in_flight += 1
        self._stats["accepted"] += 1
        try:
            response = await client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )
            return {
                "request_id": request_id,
                "status": "ok",
                "shed_prob_at_entry": round(prob, 3),
                "text": response.content[0].text[:60],
            }
        except Exception as e:
            return {"request_id": request_id, "status": "error", "error": str(e)}
        finally:
            self._in_flight -= 1

    def stats(self) -> dict:
        total = sum(self._stats.values())
        return {
            **self._stats,
            "shed_rate": round(self._stats["shed"] / max(total, 1), 3),
            "current_shed_prob": round(self._shed_probability(), 3),
            "in_flight": self._in_flight,
        }


# Usage
async def main():
    client = AsyncAnthropic()
    shedder = ProbabilisticLoadShedder(soft_limit=3, hard_limit=10)

    tasks = [
        shedder.handle(client, f"req_{i:03d}", "What is TLS?")
        for i in range(25)
    ]
    results = await asyncio.gather(*tasks)

    for r in results:
        if r["status"] == "shed":
            print(f"[{r['request_id']}] SHED prob={r['shed_prob']}")
        else:
            print(f"[{r['request_id']}] OK")

    print(f"\nStats: {shedder.stats()}")

asyncio.run(main())
```

## Solution 5: Circuit-Breaker-Integrated Load Shedder

Combine load shedding with a circuit breaker — shed excess load normally, but open the circuit entirely when error rate spikes, preventing the agent from hammering a degraded downstream.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from enum import Enum
from collections import deque


class CircuitState(Enum):
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Rejecting all requests
    HALF_OPEN = "half_open"  # Testing recovery


class CircuitBreakerLoadShedder:
    def __init__(
        self,
        max_queue: int = 30,
        error_rate_threshold: float = 0.3,
        window_size: int = 20,
        open_duration: float = 30.0,
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 256,
    ):
        self._max_queue = max_queue
        self._in_flight = 0
        self._circuit = CircuitState.CLOSED
        self._circuit_opened_at: float | None = None
        self._open_duration = open_duration
        self._outcomes: deque = deque(maxlen=window_size)
        self._error_rate_threshold = error_rate_threshold
        self.model = model
        self.max_tokens = max_tokens
        self._stats = {"accepted": 0, "shed_queue": 0, "shed_circuit": 0, "errors": 0}

    @property
    def error_rate(self) -> float:
        if not self._outcomes:
            return 0.0
        return sum(1 for o in self._outcomes if o == "error") / len(self._outcomes)

    def _update_circuit(self):
        if self._circuit == CircuitState.CLOSED:
            if self.error_rate >= self._error_rate_threshold:
                self._circuit = CircuitState.OPEN
                self._circuit_opened_at = time.monotonic()
                print(f"[circuit] OPENED (error_rate={self.error_rate:.2f})")
        elif self._circuit == CircuitState.OPEN:
            elapsed = time.monotonic() - (self._circuit_opened_at or 0)
            if elapsed >= self._open_duration:
                self._circuit = CircuitState.HALF_OPEN
                print("[circuit] HALF_OPEN — testing recovery")
        elif self._circuit == CircuitState.HALF_OPEN:
            if self.error_rate < self._error_rate_threshold / 2:
                self._circuit = CircuitState.CLOSED
                print("[circuit] CLOSED — recovered")
            elif self.error_rate >= self._error_rate_threshold:
                self._circuit = CircuitState.OPEN
                self._circuit_opened_at = time.monotonic()
                print("[circuit] REOPENED — still degraded")

    async def handle(
        self,
        client: AsyncAnthropic,
        request_id: str,
        user_message: str,
        system_prompt: str = "Answer concisely.",
    ) -> dict:
        self._update_circuit()

        # Circuit open: shed all
        if self._circuit == CircuitState.OPEN:
            self._stats["shed_circuit"] += 1
            return {
                "request_id": request_id,
                "status": "shed",
                "reason": "circuit_open",
                "retry_after": self._open_duration,
            }

        # Queue full: shed excess
        if self._in_flight >= self._max_queue:
            self._stats["shed_queue"] += 1
            return {
                "request_id": request_id,
                "status": "shed",
                "reason": "queue_full",
                "queue_depth": self._in_flight,
            }

        self._in_flight += 1
        self._stats["accepted"] += 1
        try:
            response = await client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )
            self._outcomes.append("ok")
            return {
                "request_id": request_id,
                "status": "ok",
                "circuit": self._circuit.value,
                "text": response.content[0].text[:60],
            }
        except Exception as e:
            self._outcomes.append("error")
            self._stats["errors"] += 1
            return {"request_id": request_id, "status": "error", "error": str(e)}
        finally:
            self._in_flight -= 1

    def stats(self) -> dict:
        return {
            **self._stats,
            "circuit_state": self._circuit.value,
            "error_rate": round(self.error_rate, 3),
            "in_flight": self._in_flight,
        }


# Usage
async def main():
    client = AsyncAnthropic()
    shedder = CircuitBreakerLoadShedder(max_queue=10)

    tasks = [
        shedder.handle(client, f"req_{i:03d}", "Explain REST APIs briefly.")
        for i in range(20)
    ]
    results = await asyncio.gather(*tasks)

    ok = sum(1 for r in results if r["status"] == "ok")
    shed = sum(1 for r in results if r["status"] == "shed")
    print(f"OK: {ok}, Shed: {shed}")
    print(f"Stats: {shedder.stats()}")

asyncio.run(main())
```

## Solution 6: Graceful Degradation Load Shedder — Serve Cached or Simplified Responses

Instead of rejecting overloaded requests outright, serve degraded responses (cached, shortened, or templated) — maintaining user experience at reduced quality rather than failing completely.

```python
import asyncio
import hashlib
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field


@dataclass
class CachedResponse:
    text: str
    created_at: float = field(default_factory=time.time)
    hit_count: int = 0


class GracefulDegradationShedder:
    def __init__(
        self,
        max_in_flight: int = 15,
        cache_ttl: float = 300.0,
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 512,
        degraded_max_tokens: int = 64,
    ):
        self._max_in_flight = max_in_flight
        self._in_flight = 0
        self._cache: dict[str, CachedResponse] = {}
        self._cache_ttl = cache_ttl
        self.model = model
        self.max_tokens = max_tokens
        self.degraded_max_tokens = degraded_max_tokens
        self._stats = {"full": 0, "degraded": 0, "cached": 0, "shed": 0}

    def _cache_key(self, system: str, message: str) -> str:
        return hashlib.md5(f"{system}:{message}".encode()).hexdigest()[:12]

    def _get_cached(self, key: str) -> CachedResponse | None:
        entry = self._cache.get(key)
        if entry and time.time() - entry.created_at < self._cache_ttl:
            entry.hit_count += 1
            return entry
        return None

    def _load_level(self) -> str:
        ratio = self._in_flight / self._max_in_flight
        if ratio < 0.6:
            return "normal"
        if ratio < 0.85:
            return "elevated"
        return "critical"

    async def handle(
        self,
        client: AsyncAnthropic,
        request_id: str,
        user_message: str,
        system_prompt: str = "Answer concisely.",
    ) -> dict:
        key = self._cache_key(system_prompt, user_message)
        level = self._load_level()

        # Always serve cache hits regardless of load
        cached = self._get_cached(key)
        if cached:
            self._stats["cached"] += 1
            return {
                "request_id": request_id,
                "status": "cached",
                "text": cached.text,
                "cache_hits": cached.hit_count,
            }

        # Critical load: shed if no cache
        if level == "critical":
            self._stats["shed"] += 1
            return {
                "request_id": request_id,
                "status": "shed",
                "reason": "overload",
                "load_level": level,
                "retry_after": 10,
            }

        self._in_flight += 1
        try:
            # Elevated load: use reduced max_tokens for faster responses
            tokens = self.degraded_max_tokens if level == "elevated" else self.max_tokens
            degraded = level == "elevated"

            response = await client.messages.create(
                model=self.model,
                max_tokens=tokens,
                system=system_prompt + (" Be very brief." if degraded else ""),
                messages=[{"role": "user", "content": user_message}],
            )
            text = response.content[0].text
            self._cache[key] = CachedResponse(text=text)

            stat_key = "degraded" if degraded else "full"
            self._stats[stat_key] += 1
            return {
                "request_id": request_id,
                "status": "ok",
                "degraded": degraded,
                "load_level": level,
                "text": text,
            }
        except Exception as e:
            return {"request_id": request_id, "status": "error", "error": str(e)}
        finally:
            self._in_flight -= 1

    def stats(self) -> dict:
        return {**self._stats, "in_flight": self._in_flight, "cache_size": len(self._cache)}


# Usage
async def main():
    client = AsyncAnthropic()
    shedder = GracefulDegradationShedder(max_in_flight=5)

    # Send 20 concurrent requests — some will be degraded, some cached
    tasks = [
        shedder.handle(client, f"req_{i:03d}", "What is a microservice?" if i % 3 == 0 else f"Question {i}?")
        for i in range(20)
    ]
    results = await asyncio.gather(*tasks)

    for r in results:
        print(f"[{r['request_id']}] {r['status']}" +
              (f" degraded={r.get('degraded')}" if r["status"] == "ok" else ""))

    print(f"\nStats: {shedder.stats()}")

asyncio.run(main())
```

## Comparison

| Approach | Fairness | Complexity | Recovery | User Experience | Best For |
|---|---|---|---|---|---|
| Queue-Depth Shedder | LIFO bias | Very Low | Instant | Hard failure | Simple services, internal APIs |
| Token-Bucket Priority | Priority-fair | Low | Instant | Soft for high-pri | Multi-tenant services |
| Adaptive Latency | Latency-aware | Medium | Automatic | SLO-preserving | Latency-sensitive APIs |
| Probabilistic | Random-fair | Low | Instant | Partially degraded | Fair random shedding |
| Circuit Breaker Integrated | N/A when open | Medium | Timed | Hard failure | Downstream-degradation protection |
| Graceful Degradation | Cache-first | High | Instant | Soft degradation | User-facing consumer apps |
