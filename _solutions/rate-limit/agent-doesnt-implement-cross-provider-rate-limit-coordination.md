---
title: "Agent Doesn't Implement Cross-Provider Rate Limit Coordination"
description: "Coordinate rate limit state across multiple AI providers (Anthropic, OpenAI, Cohere, etc.) so that when one provider throttles you, requests automatically shift to available providers without manual intervention."
difficulty: advanced
category: rate-limit
tags: [rate-limit, multi-provider, load-balancing, failover, resilience]
---

## Problem

Agents using multiple AI providers manage each provider's rate limits independently—or not at all. When Anthropic's API returns a 429, the agent fails rather than routing the request to OpenAI or another available provider. When all providers have capacity, there's no strategy to use them efficiently. Cross-provider coordination turns a collection of independent rate limits into a unified capacity pool.

## Solutions

### Option 1: Least-Loaded Provider Router

Track token usage across providers and route each request to the provider with the most remaining capacity.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

@dataclass
class ProviderBucket:
    name: str
    tokens_per_minute: int
    _used: float = 0.0
    _last_refill: float = field(default_factory=time.monotonic)

    def refill(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        refill_amount = (elapsed / 60.0) * self.tokens_per_minute
        self._used = max(0.0, self._used - refill_amount)
        self._last_refill = now

    def available(self) -> float:
        self.refill()
        return self.tokens_per_minute - self._used

    def consume(self, tokens: int):
        self._used += tokens

    def utilization(self) -> float:
        self.refill()
        return self._used / self.tokens_per_minute

@dataclass
class MultiProviderRouter:
    providers: list[ProviderBucket]
    _call_log: list[dict] = field(default_factory=list)

    def select_provider(self) -> ProviderBucket:
        """Select the provider with most remaining capacity."""
        available = [p for p in self.providers if p.available() > 0]
        if not available:
            # All exhausted — pick least loaded
            return min(self.providers, key=lambda p: p.utilization())
        return max(available, key=lambda p: p.available())

    async def complete(self, prompt: str, tokens_estimate: int = 200) -> tuple[str, str]:
        provider = self.select_provider()
        provider.consume(tokens_estimate)

        # In production: instantiate the right client per provider
        client = AsyncAnthropic()
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=tokens_estimate,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text
        actual_tokens = response.usage.output_tokens
        # Correct the estimate
        provider.consume(actual_tokens - tokens_estimate)

        self._call_log.append({"provider": provider.name, "tokens": actual_tokens})
        return text, provider.name

    def utilization_report(self) -> dict:
        return {
            p.name: {
                "utilization": f"{p.utilization():.0%}",
                "available_tokens": int(p.available()),
            }
            for p in self.providers
        }

async def demo_least_loaded():
    router = MultiProviderRouter(providers=[
        ProviderBucket("anthropic-tier1", tokens_per_minute=40000),
        ProviderBucket("anthropic-tier2", tokens_per_minute=20000),
        ProviderBucket("openai-gpt4o", tokens_per_minute=30000),
    ])

    prompts = [f"What is concept #{i}?" for i in range(8)]

    for prompt in prompts:
        text, provider = await router.complete(prompt, tokens_estimate=100)
        print(f"[{provider}] {prompt}: {text.strip()[:50]}")

    print(f"\nUtilization: {router.utilization_report()}")

asyncio.run(demo_least_loaded())
```

### Option 2: 429-Triggered Automatic Failover

Catch rate limit errors and transparently retry on the next available provider.

```python
import asyncio
import time
from anthropic import AsyncAnthropic, RateLimitError
from dataclasses import dataclass, field

@dataclass
class ProviderState:
    name: str
    model: str
    client: AsyncAnthropic
    throttled_until: float = 0.0
    total_requests: int = 0
    total_throttles: int = 0

    def is_available(self) -> bool:
        return time.monotonic() >= self.throttled_until

    def mark_throttled(self, retry_after_seconds: float = 60.0):
        self.throttled_until = time.monotonic() + retry_after_seconds
        self.total_throttles += 1
        print(f"[Provider:{self.name}] Throttled for {retry_after_seconds:.0f}s")

    def time_until_available(self) -> float:
        return max(0.0, self.throttled_until - time.monotonic())

class FailoverRouter:
    def __init__(self, providers: list[ProviderState]):
        self._providers = providers
        self._request_log: list[tuple[str, bool]] = []  # (provider, success)

    def _available_providers(self) -> list[ProviderState]:
        return [p for p in self._providers if p.is_available()]

    async def complete(self, messages: list[dict], max_tokens: int = 300) -> tuple[str, str]:
        available = self._available_providers()
        if not available:
            waits = [(p.time_until_available(), p) for p in self._providers]
            wait_time, fastest = min(waits, key=lambda x: x[0])
            print(f"[FailoverRouter] All throttled. Waiting {wait_time:.1f}s for {fastest.name}")
            await asyncio.sleep(wait_time)
            available = [fastest]

        for provider in available:
            try:
                response = await provider.client.messages.create(
                    model=provider.model,
                    max_tokens=max_tokens,
                    messages=messages,
                )
                provider.total_requests += 1
                self._request_log.append((provider.name, True))
                return response.content[0].text, provider.name

            except RateLimitError as e:
                # Parse retry-after from error if available
                retry_after = 60.0
                provider.mark_throttled(retry_after)
                self._request_log.append((provider.name, False))
                print(f"[FailoverRouter] {provider.name} rate limited. Trying next provider.")
                continue

            except Exception as e:
                print(f"[FailoverRouter] {provider.name} error: {e}. Trying next.")
                self._request_log.append((provider.name, False))
                continue

        raise RuntimeError("All providers exhausted")

    def stats(self) -> dict:
        return {
            p.name: {
                "requests": p.total_requests,
                "throttles": p.total_throttles,
                "currently_available": p.is_available(),
            }
            for p in self._providers
        }

async def demo_failover():
    providers = [
        ProviderState(
            name="primary",
            model="claude-haiku-4-5-20251001",
            client=AsyncAnthropic(),
        ),
        ProviderState(
            name="secondary",
            model="claude-haiku-4-5-20251001",
            client=AsyncAnthropic(),
        ),
    ]

    router = FailoverRouter(providers)

    # Simulate primary being throttled
    providers[0].mark_throttled(retry_after_seconds=2.0)

    for i in range(5):
        try:
            text, provider = await router.complete(
                [{"role": "user", "content": f"Request {i}: brief answer"}]
            )
            print(f"[{provider}] Request {i}: {text.strip()[:50]}")
        except RuntimeError as e:
            print(f"Request {i} failed: {e}")

        await asyncio.sleep(0.5)

    print(f"\nStats: {router.stats()}")

asyncio.run(demo_failover())
```

### Option 3: Weighted Round-Robin with Capacity-Proportional Distribution

Distribute requests across providers proportional to their rate limits so each is utilized optimally.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
import random

@dataclass
class WeightedProvider:
    name: str
    model: str
    weight: float           # Relative traffic share (e.g., tpm / total_tpm)
    client: AsyncAnthropic
    requests_sent: int = 0

class WeightedRoundRobin:
    def __init__(self, providers: list[WeightedProvider]):
        self._providers = providers
        self._total_weight = sum(p.weight for p in providers)
        self._normalize_weights()

    def _normalize_weights(self):
        for p in self._providers:
            p.weight = p.weight / self._total_weight

    def select(self) -> WeightedProvider:
        """Weighted random selection."""
        roll = random.random()
        cumulative = 0.0
        for provider in self._providers:
            cumulative += provider.weight
            if roll < cumulative:
                return provider
        return self._providers[-1]

    def select_least_used_proportional(self) -> WeightedProvider:
        """Select provider most under its target utilization."""
        total_requests = sum(p.requests_sent for p in self._providers) or 1
        return min(
            self._providers,
            key=lambda p: (p.requests_sent / total_requests) - p.weight
        )

    async def complete(self, messages: list[dict], max_tokens: int = 200) -> tuple[str, str]:
        provider = self.select_least_used_proportional()
        provider.requests_sent += 1

        response = await provider.client.messages.create(
            model=provider.model,
            max_tokens=max_tokens,
            messages=messages,
        )
        return response.content[0].text, provider.name

    def distribution_report(self) -> dict:
        total = sum(p.requests_sent for p in self._providers) or 1
        return {
            p.name: {
                "target_share": f"{p.weight:.0%}",
                "actual_share": f"{p.requests_sent / total:.0%}",
                "requests": p.requests_sent,
            }
            for p in self._providers
        }

async def demo_weighted_distribution():
    # Anthropic gets 60% of traffic (higher TPM), OpenAI gets 40%
    providers = [
        WeightedProvider("anthropic", "claude-haiku-4-5-20251001", weight=0.6,
                         client=AsyncAnthropic()),
        WeightedProvider("anthropic-backup", "claude-haiku-4-5-20251001", weight=0.4,
                         client=AsyncAnthropic()),
    ]

    router = WeightedRoundRobin(providers)

    prompts = [{"role": "user", "content": f"Brief answer #{i}"} for i in range(20)]

    tasks = [router.complete([p]) for p in prompts]
    results = await asyncio.gather(*tasks)

    for text, provider in results[:5]:
        print(f"[{provider}]: {text.strip()[:50]}")

    print(f"\nDistribution report:")
    for name, stats in router.distribution_report().items():
        print(f"  {name}: {stats}")

asyncio.run(demo_weighted_distribution())
```

### Option 4: Shared Rate Limit State via Redis

For multi-process or multi-host agent deployments, coordinate rate limit state through Redis.

```python
import asyncio
import time
import json
from anthropic import AsyncAnthropic
from dataclasses import dataclass

# Simulated Redis (replace with actual aioredis in production)
class FakeRedis:
    _store: dict[str, tuple] = {}  # key -> (value, expires_at)

    async def get(self, key: str) -> str | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at and time.monotonic() > expires_at:
            del self._store[key]
            return None
        return value

    async def set(self, key: str, value: str, ex: float | None = None):
        expires_at = time.monotonic() + ex if ex else None
        self._store[key] = (value, expires_at)

    async def incr(self, key: str) -> int:
        current = await self.get(key)
        new_val = (int(current) if current else 0) + 1
        ttl_remaining = None
        if key in self._store:
            _, expires_at = self._store[key]
            if expires_at:
                ttl_remaining = expires_at - time.monotonic()
        await self.set(key, str(new_val), ex=ttl_remaining or 60.0)
        return new_val

redis = FakeRedis()

@dataclass
class RedisRateLimitCoordinator:
    """Coordinates rate limits across multiple agent instances via Redis."""
    provider_name: str
    requests_per_minute: int
    tokens_per_minute: int
    instance_id: str = "instance-1"

    async def _window_key(self) -> str:
        window = int(time.time() // 60)  # 1-minute windows
        return f"ratelimit:{self.provider_name}:{window}"

    async def can_make_request(self, estimated_tokens: int = 200) -> tuple[bool, str]:
        key = await self._window_key()
        count_key = f"{key}:count"
        token_key = f"{key}:tokens"

        # Get current usage
        current_count_str = await redis.get(count_key)
        current_tokens_str = await redis.get(token_key)
        current_count = int(current_count_str or 0)
        current_tokens = int(current_tokens_str or 0)

        if current_count >= self.requests_per_minute:
            return False, f"Request limit reached ({current_count}/{self.requests_per_minute}/min)"
        if current_tokens + estimated_tokens > self.tokens_per_minute:
            return False, f"Token limit reached ({current_tokens}/{self.tokens_per_minute}/min)"

        return True, "ok"

    async def record_request(self, actual_tokens: int):
        key = await self._window_key()
        await redis.incr(f"{key}:count")
        # Add token count (simplified - real implementation uses INCRBY)
        token_key = f"{key}:tokens"
        current = await redis.get(token_key)
        new_val = (int(current) if current else 0) + actual_tokens
        await redis.set(token_key, str(new_val), ex=60.0)

    async def current_usage(self) -> dict:
        key = await self._window_key()
        count = await redis.get(f"{key}:count") or "0"
        tokens = await redis.get(f"{key}:tokens") or "0"
        return {
            "provider": self.provider_name,
            "requests_used": int(count),
            "requests_limit": self.requests_per_minute,
            "tokens_used": int(tokens),
            "tokens_limit": self.tokens_per_minute,
        }

async def demo_redis_coordination():
    coordinators = [
        RedisRateLimitCoordinator(
            provider_name="anthropic",
            requests_per_minute=50,
            tokens_per_minute=40000,
            instance_id=f"worker-{i}"
        )
        for i in range(3)
    ]

    client = AsyncAnthropic()

    async def worker_task(coordinator: RedisRateLimitCoordinator, request_id: int) -> str:
        allowed, reason = await coordinator.can_make_request(estimated_tokens=200)
        if not allowed:
            return f"[{coordinator.instance_id}] BLOCKED: {reason}"

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=50,
            messages=[{"role": "user", "content": f"Request {request_id}"}]
        )
        await coordinator.record_request(response.usage.output_tokens)
        return f"[{coordinator.instance_id}] ✓ Request {request_id}"

    tasks = [
        worker_task(coordinators[i % 3], i)
        for i in range(9)
    ]
    results = await asyncio.gather(*tasks)
    for r in results:
        print(r)

    usage = await coordinators[0].current_usage()
    print(f"\nShared usage: {usage}")

asyncio.run(demo_redis_coordination())
```

### Option 5: Provider Health Score-Based Routing

Maintain a rolling health score per provider based on latency, error rate, and rate limit hits.

```python
import asyncio
import time
import math
from anthropic import AsyncAnthropic, RateLimitError
from dataclasses import dataclass, field
from collections import deque

@dataclass
class ProviderHealth:
    name: str
    model: str
    window: int = 20

    _latencies: deque = field(default_factory=lambda: deque(maxlen=20))
    _errors: deque = field(default_factory=lambda: deque(maxlen=20))
    _rate_limits: deque = field(default_factory=lambda: deque(maxlen=20))

    def record(self, success: bool, latency_ms: float, rate_limited: bool = False):
        self._latencies.append(latency_ms if success else 10000)
        self._errors.append(0 if success else 1)
        self._rate_limits.append(1 if rate_limited else 0)

    def health_score(self) -> float:
        """0.0 (worst) to 1.0 (best)."""
        if not self._latencies:
            return 0.5  # Unknown = neutral

        avg_latency = sum(self._latencies) / len(self._latencies)
        error_rate = sum(self._errors) / max(len(self._errors), 1)
        rl_rate = sum(self._rate_limits) / max(len(self._rate_limits), 1)

        # Normalize latency: 500ms = 0.9, 2000ms = 0.5, 5000ms = 0.1
        latency_score = max(0.0, 1.0 - (avg_latency - 200) / 5000)
        error_score = 1.0 - error_rate
        rl_score = 1.0 - rl_rate * 2  # Rate limits penalized heavily

        return max(0.0, min(1.0, latency_score * 0.3 + error_score * 0.4 + rl_score * 0.3))

class HealthBasedRouter:
    def __init__(self, providers: list[tuple[str, str]]):  # (name, model)
        self._client = AsyncAnthropic()
        self._health: dict[str, ProviderHealth] = {
            name: ProviderHealth(name=name, model=model)
            for name, model in providers
        }

    def _select_provider(self) -> ProviderHealth:
        """Select highest-health provider with softmax-style randomization."""
        scores = {name: p.health_score() for name, p in self._health.items()}
        # Weighted random selection proportional to health score
        total = sum(scores.values()) or 1
        import random
        roll = random.random() * total
        cumulative = 0.0
        for name, score in scores.items():
            cumulative += score
            if roll <= cumulative:
                return self._health[name]
        return list(self._health.values())[-1]

    async def complete(self, messages: list[dict], max_tokens: int = 200) -> tuple[str, str]:
        provider = self._select_provider()
        start = time.monotonic()

        try:
            response = await self._client.messages.create(
                model=provider.model,
                max_tokens=max_tokens,
                messages=messages,
            )
            latency = (time.monotonic() - start) * 1000
            provider.record(success=True, latency_ms=latency)
            return response.content[0].text, provider.name

        except RateLimitError:
            latency = (time.monotonic() - start) * 1000
            provider.record(success=False, latency_ms=latency, rate_limited=True)
            raise
        except Exception:
            latency = (time.monotonic() - start) * 1000
            provider.record(success=False, latency_ms=latency)
            raise

    def health_report(self) -> dict:
        return {
            name: {
                "health_score": f"{p.health_score():.2f}",
            }
            for name, p in self._health.items()
        }

async def demo_health_routing():
    router = HealthBasedRouter([
        ("anthropic-primary", "claude-haiku-4-5-20251001"),
        ("anthropic-secondary", "claude-haiku-4-5-20251001"),
    ])

    for i in range(10):
        try:
            text, provider = await router.complete(
                [{"role": "user", "content": f"Quick answer #{i}"}]
            )
            print(f"[{provider}] ✓: {text.strip()[:50]}")
        except Exception as e:
            print(f"Error: {e}")

    print(f"\nHealth report: {router.health_report()}")

asyncio.run(demo_health_routing())
```

### Option 6: Priority-Based Provider Cascade

Define a priority order across providers—use the primary unless rate limited, then cascade to secondary, tertiary.

```python
import asyncio
import time
from anthropic import AsyncAnthropic, RateLimitError
from dataclasses import dataclass, field

@dataclass
class CascadeProvider:
    name: str
    model: str
    priority: int               # 1 = highest priority (primary)
    cost_per_1k_tokens: float   # Used for cost tracking
    _client: AsyncAnthropic = field(init=False)
    _throttled_until: float = 0.0
    requests_served: int = 0
    cost_incurred: float = 0.0

    def __post_init__(self):
        self._client = AsyncAnthropic()

    def is_available(self) -> bool:
        return time.monotonic() >= self._throttled_until

    def throttle(self, seconds: float = 60.0):
        self._throttled_until = time.monotonic() + seconds
        print(f"[Cascade] {self.name} throttled for {seconds:.0f}s")

    async def complete(self, messages: list[dict], max_tokens: int) -> str:
        response = await asyncio.wait_for(
            self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                messages=messages,
            ),
            timeout=10.0
        )
        self.requests_served += 1
        tokens_used = (response.usage.input_tokens + response.usage.output_tokens) / 1000
        self.cost_incurred += tokens_used * self.cost_per_1k_tokens
        return response.content[0].text

class ProviderCascade:
    def __init__(self, providers: list[CascadeProvider]):
        self._providers = sorted(providers, key=lambda p: p.priority)

    async def complete(self, messages: list[dict], max_tokens: int = 200) -> tuple[str, str]:
        errors = []
        for provider in self._providers:
            if not provider.is_available():
                remaining = provider._throttled_until - time.monotonic()
                errors.append(f"{provider.name}: throttled ({remaining:.0f}s remaining)")
                continue

            try:
                text = await provider.complete(messages, max_tokens)
                if provider.priority > 1:
                    print(f"[Cascade] Served by {provider.name} (priority {provider.priority})")
                return text, provider.name

            except RateLimitError:
                provider.throttle(seconds=60.0)
                errors.append(f"{provider.name}: rate limited")
                continue

            except asyncio.TimeoutError:
                errors.append(f"{provider.name}: timeout")
                continue

            except Exception as e:
                errors.append(f"{provider.name}: {e}")
                continue

        raise RuntimeError(f"All providers failed: {'; '.join(errors)}")

    def cost_report(self) -> dict:
        return {
            p.name: {
                "priority": p.priority,
                "requests_served": p.requests_served,
                "cost": f"${p.cost_incurred:.4f}",
                "available": p.is_available(),
            }
            for p in self._providers
        }

async def demo_cascade():
    providers = [
        CascadeProvider("anthropic-primary", "claude-haiku-4-5-20251001",
                        priority=1, cost_per_1k_tokens=0.00025),
        CascadeProvider("anthropic-fallback", "claude-haiku-4-5-20251001",
                        priority=2, cost_per_1k_tokens=0.00025),
    ]

    # Simulate primary being throttled
    providers[0].throttle(seconds=3.0)

    cascade = ProviderCascade(providers)

    for i in range(6):
        try:
            text, provider = await cascade.complete(
                [{"role": "user", "content": f"Request {i}"}]
            )
            print(f"[{provider}] Request {i}: {text.strip()[:50]}")
        except RuntimeError as e:
            print(f"Request {i} failed: {e}")
        await asyncio.sleep(0.5)

    import json
    print(f"\nCost report:\n{json.dumps(cascade.cost_report(), indent=2)}")

asyncio.run(demo_cascade())
```

## Comparison

| Approach | Routing Strategy | 429 Handling | State Sharing | Best For |
|---|---|---|---|---|
| Least-Loaded Router | Capacity-based | No auto-switch | In-process | Single-process agents |
| 429-Triggered Failover | Failure-triggered | Automatic switch | In-process | Reliability-first agents |
| Weighted Round-Robin | Proportional | Manual weight tuning | In-process | Balanced cost control |
| Redis-Coordinated | Window-based | Coordinated | Multi-process | Distributed deployments |
| Health Score Routing | Score-based | Auto-downweight | In-process | Adaptive quality routing |
| Priority Cascade | Priority order | Automatic cascade | In-process | Cost-optimized primary |

**Choose 429-Triggered Failover** as the minimum viable implementation—it handles the most common production problem (one provider throttling) with minimal complexity. **Choose Redis-Coordinated** when running multiple agent instances that share a provider's rate limit quota and need to prevent one instance from starving others. **Choose Health Score Routing** when you need to continuously adapt to varying provider performance without manual intervention.
