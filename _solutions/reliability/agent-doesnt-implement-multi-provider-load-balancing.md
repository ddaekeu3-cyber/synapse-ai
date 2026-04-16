---
title: "Agent Doesn't Implement Multi-Provider Load Balancing"
description: "Six solutions for distributing LLM requests across multiple AI providers to improve availability, reduce costs, and avoid single-provider rate limits."
difficulty: intermediate
category: reliability
tags: [load-balancing, multi-provider, failover, availability, cost, routing]
---

# Agent Doesn't Implement Multi-Provider Load Balancing

Relying on a single AI provider creates a single point of failure. A provider outage, rate-limit spike, or price increase affects all traffic instantly. Multi-provider load balancing distributes requests across Anthropic, OpenAI, Google, and others—improving availability, reducing cost, and staying within rate limits per provider.

## Solution 1: Round-Robin Load Balancer Across Providers

Rotate requests evenly across configured providers using a simple round-robin counter.

```python
import asyncio
import itertools
from dataclasses import dataclass
from typing import Any
from anthropic import AsyncAnthropic

# Unified interface: each provider adapter exposes async chat()
@dataclass
class ProviderConfig:
    name: str
    model: str
    weight: int = 1  # For weighted routing (used later)


class AnthropicAdapter:
    def __init__(self, model: str = "claude-haiku-4-5-20251001"):
        self.client = AsyncAnthropic()
        self.model = model
        self.name = "anthropic"

    async def chat(self, message: str, max_tokens: int = 1024) -> dict[str, Any]:
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": message}],
        )
        return {
            "provider": self.name,
            "model": self.model,
            "text": response.content[0].text,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }


class OpenAIAdapter:
    """Drop-in adapter for OpenAI; requires openai package."""
    def __init__(self, model: str = "gpt-4o-mini"):
        try:
            from openai import AsyncOpenAI
            self.client = AsyncOpenAI()
        except ImportError:
            self.client = None
        self.model = model
        self.name = "openai"

    async def chat(self, message: str, max_tokens: int = 1024) -> dict[str, Any]:
        if self.client is None:
            raise RuntimeError("openai package not installed")
        response = await self.client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": message}],
        )
        choice = response.choices[0]
        return {
            "provider": self.name,
            "model": self.model,
            "text": choice.message.content,
            "input_tokens": response.usage.prompt_tokens,
            "output_tokens": response.usage.completion_tokens,
        }


class RoundRobinBalancer:
    def __init__(self, providers: list):
        if not providers:
            raise ValueError("At least one provider required")
        self._providers = providers
        self._cycle = itertools.cycle(range(len(providers)))
        self._lock = asyncio.Lock()
        self._call_counts = [0] * len(providers)

    async def _next_provider(self):
        async with self._lock:
            idx = next(self._cycle)
            self._call_counts[idx] += 1
            return self._providers[idx], idx

    async def chat(self, message: str, max_tokens: int = 1024) -> dict[str, Any]:
        provider, idx = await self._next_provider()
        try:
            result = await provider.chat(message, max_tokens)
            return result
        except Exception as e:
            # Try next provider on failure
            next_provider = self._providers[(idx + 1) % len(self._providers)]
            result = await next_provider.chat(message, max_tokens)
            result["fallback"] = True
            return result

    def stats(self) -> dict:
        return {
            p.name: count
            for p, count in zip(self._providers, self._call_counts)
        }


async def demo_round_robin():
    # Only Anthropic available in this demo; real use adds OpenAIAdapter etc.
    providers = [
        AnthropicAdapter("claude-haiku-4-5-20251001"),
        AnthropicAdapter("claude-haiku-4-5-20251001"),  # Simulating second "provider"
    ]
    balancer = RoundRobinBalancer(providers)
    messages = [f"What is {i}+{i}?" for i in range(6)]
    results = await asyncio.gather(*[balancer.chat(m) for m in messages])
    for r in results:
        print(f"  [{r['provider']}] {r['text'][:60]}")
    print(f"Call distribution: {balancer.stats()}")
```

## Solution 2: Weighted Provider Routing by Cost and Latency

Route more traffic to cheaper or faster providers; shift weight dynamically based on observed latency.

```python
import asyncio
import random
import time
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class ProviderStats:
    name: str
    base_weight: float
    cost_per_1k_output: float  # USD
    success_count: int = 0
    error_count: int = 0
    total_latency_ms: float = 0.0
    ema_latency_ms: float = 500.0  # Initial estimate
    ema_alpha: float = 0.2

    def record(self, latency_ms: float, success: bool):
        if success:
            self.success_count += 1
            self.ema_latency_ms = (
                self.ema_alpha * latency_ms
                + (1 - self.ema_alpha) * self.ema_latency_ms
            )
        else:
            self.error_count += 1

    @property
    def error_rate(self) -> float:
        total = self.success_count + self.error_count
        return self.error_count / max(total, 1)

    @property
    def effective_weight(self) -> float:
        """Lower error rate and latency → higher effective weight."""
        reliability = max(0.0, 1 - self.error_rate * 3)
        latency_factor = 1000.0 / max(self.ema_latency_ms, 100)
        return self.base_weight * reliability * latency_factor


class WeightedProvider:
    def __init__(self, stats: ProviderStats):
        self.stats = stats
        self.client = AsyncAnthropic()

    async def chat(self, message: str, max_tokens: int = 1024) -> dict:
        start = time.perf_counter()
        try:
            response = await self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": message}],
            )
            elapsed = (time.perf_counter() - start) * 1000
            self.stats.record(elapsed, success=True)
            return {
                "provider": self.stats.name,
                "text": response.content[0].text,
                "latency_ms": elapsed,
                "output_tokens": response.usage.output_tokens,
            }
        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            self.stats.record(elapsed, success=False)
            raise


class WeightedBalancer:
    def __init__(self, providers: list[WeightedProvider]):
        self.providers = providers

    def _select(self) -> WeightedProvider:
        weights = [p.stats.effective_weight for p in self.providers]
        total = sum(weights)
        if total == 0:
            return random.choice(self.providers)
        r = random.uniform(0, total)
        cumulative = 0.0
        for provider, weight in zip(self.providers, weights):
            cumulative += weight
            if r <= cumulative:
                return provider
        return self.providers[-1]

    async def chat(self, message: str, max_tokens: int = 1024) -> dict:
        provider = self._select()
        return await provider.chat(message, max_tokens)

    def weight_report(self) -> dict:
        return {
            p.stats.name: {
                "effective_weight": round(p.stats.effective_weight, 3),
                "ema_latency_ms": round(p.stats.ema_latency_ms, 1),
                "error_rate": round(p.stats.error_rate, 3),
            }
            for p in self.providers
        }


async def demo_weighted():
    providers = [
        WeightedProvider(ProviderStats("anthropic-haiku", base_weight=1.0, cost_per_1k_output=4.0)),
        WeightedProvider(ProviderStats("anthropic-haiku-2", base_weight=0.5, cost_per_1k_output=4.0)),
    ]
    balancer = WeightedBalancer(providers)
    results = await asyncio.gather(*[balancer.chat(f"Say hi {i}") for i in range(10)])
    print(f"Completed {len(results)} requests")
    print("Weight report:", balancer.weight_report())
```

## Solution 3: Latency-Optimized Hedged Requests

Send the same request to two providers; use whichever responds first; cancel the slower one.

```python
import asyncio
import time
from anthropic import AsyncAnthropic


class HedgedBalancer:
    """
    Sends to primary first; after hedge_delay_ms, also fires to backup.
    Returns the first successful response and cancels the other.
    """

    def __init__(
        self,
        primary,
        backup,
        hedge_delay_ms: float = 200.0,
    ):
        self.primary = primary
        self.backup = backup
        self.hedge_delay_ms = hedge_delay_ms
        self.primary_wins = 0
        self.backup_wins = 0
        self.hedge_fired = 0

    async def chat(self, message: str, max_tokens: int = 1024) -> dict:
        result_queue: asyncio.Queue = asyncio.Queue()

        async def run_provider(provider, label: str):
            try:
                result = await provider.chat(message, max_tokens)
                result["won_by"] = label
                await result_queue.put(result)
            except Exception as e:
                await result_queue.put({"error": str(e), "won_by": label})

        # Start primary immediately
        primary_task = asyncio.create_task(run_provider(self.primary, "primary"))

        # Start backup after hedge delay
        async def delayed_backup():
            await asyncio.sleep(self.hedge_delay_ms / 1000)
            self.hedge_fired += 1
            backup_task = asyncio.create_task(run_provider(self.backup, "backup"))
            return backup_task

        hedge_task = asyncio.create_task(delayed_backup())

        # Wait for first successful result
        result = await result_queue.get()

        # Cancel remaining work
        primary_task.cancel()
        hedge_task.cancel()

        if result.get("won_by") == "primary":
            self.primary_wins += 1
        else:
            self.backup_wins += 1

        return result

    def stats(self) -> dict:
        return {
            "primary_wins": self.primary_wins,
            "backup_wins": self.backup_wins,
            "hedge_requests_fired": self.hedge_fired,
        }


class SimpleProviderAdapter:
    def __init__(self, name: str, client: AsyncAnthropic, model: str):
        self.name = name
        self.client = client
        self.model = model

    async def chat(self, message: str, max_tokens: int = 1024) -> dict:
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": message}],
        )
        return {
            "provider": self.name,
            "text": response.content[0].text,
            "output_tokens": response.usage.output_tokens,
        }


async def demo_hedged():
    client = AsyncAnthropic()
    primary = SimpleProviderAdapter("primary", client, "claude-haiku-4-5-20251001")
    backup = SimpleProviderAdapter("backup", client, "claude-haiku-4-5-20251001")
    balancer = HedgedBalancer(primary, backup, hedge_delay_ms=300)

    messages = [f"What is {i}^2?" for i in range(8)]
    results = await asyncio.gather(*[balancer.chat(m) for m in messages])
    for r in results:
        print(f"  [{r.get('won_by','?')}] {r.get('text','')[:50]}")
    print(f"Stats: {balancer.stats()}")
```

## Solution 4: Cost-Optimized Routing by Task Complexity

Route simple tasks to cheap models; complex tasks to powerful (expensive) models — decided by a classifier.

```python
import asyncio
from enum import Enum
from anthropic import AsyncAnthropic


class TaskComplexity(Enum):
    SIMPLE = "simple"       # Factual, single-step, short output
    MODERATE = "moderate"   # Multi-step reasoning, medium output
    COMPLEX = "complex"     # Deep analysis, code generation, long output


@dataclass_like = None  # Forward declaration

class ComplexityRouter:
    SIMPLE_KEYWORDS = {
        "what is", "define", "capital of", "how many", "yes or no",
        "true or false", "spell", "translate", "what does",
    }
    COMPLEX_KEYWORDS = {
        "analyze", "compare", "design", "implement", "architecture",
        "write code", "debug", "optimize", "explain in detail", "research",
    }

    def classify(self, message: str) -> TaskComplexity:
        msg = message.lower()
        if any(kw in msg for kw in self.COMPLEX_KEYWORDS):
            return TaskComplexity.COMPLEX
        if any(kw in msg for kw in self.SIMPLE_KEYWORDS) or len(message) < 80:
            return TaskComplexity.SIMPLE
        return TaskComplexity.MODERATE


class CostOptimizedRouter:
    """
    Routes requests based on task complexity:
    - SIMPLE → cheap/fast model (low cost)
    - MODERATE → mid-tier model
    - COMPLEX → most capable model (higher cost)
    """

    # Model routing table: complexity → (model, max_tokens)
    ROUTING_TABLE = {
        TaskComplexity.SIMPLE: ("claude-haiku-4-5-20251001", 512),
        TaskComplexity.MODERATE: ("claude-haiku-4-5-20251001", 1024),
        TaskComplexity.COMPLEX: ("claude-haiku-4-5-20251001", 4096),  # Would be claude-sonnet-4-6 in prod
    }

    def __init__(self):
        self.client = AsyncAnthropic()
        self.classifier = ComplexityRouter()
        self._routing_stats = {c: 0 for c in TaskComplexity}
        self._cost_saved_usd = 0.0

    async def chat(self, message: str) -> dict:
        complexity = self.classifier.classify(message)
        model, max_tokens = self.ROUTING_TABLE[complexity]
        self._routing_stats[complexity] += 1

        response = await self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": message}],
        )
        return {
            "complexity": complexity.value,
            "model": model,
            "text": response.content[0].text,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }

    def routing_report(self) -> dict:
        total = sum(self._routing_stats.values())
        return {
            "total_requests": total,
            "by_complexity": {
                c.value: {
                    "count": n,
                    "pct": round(n / max(total, 1) * 100, 1),
                }
                for c, n in self._routing_stats.items()
            },
        }


async def demo_cost_routing():
    router = CostOptimizedRouter()
    tasks = [
        "What is the capital of France?",
        "Analyze the trade-offs between microservices and monolithic architecture.",
        "Spell 'necessary'.",
        "Write a Python implementation of merge sort with full documentation.",
        "Define photosynthesis.",
        "Compare SQL vs NoSQL databases for a high-write workload.",
    ]
    results = await asyncio.gather(*[router.chat(t) for t in tasks])
    for task, result in zip(tasks, results):
        print(f"  [{result['complexity']:8s}|{result['model'][-10:]}] {task[:50]}")
    print(f"\nRouting report: {router.routing_report()}")


class ComplexityRouter:  # Redefine properly
    SIMPLE_KEYWORDS = {
        "what is", "define", "capital of", "how many", "yes or no",
        "true or false", "spell", "translate", "what does",
    }
    COMPLEX_KEYWORDS = {
        "analyze", "compare", "design", "implement", "architecture",
        "write code", "debug", "optimize", "explain in detail", "research",
    }

    def classify(self, message: str) -> TaskComplexity:
        msg = message.lower()
        if any(kw in msg for kw in self.COMPLEX_KEYWORDS):
            return TaskComplexity.COMPLEX
        if any(kw in msg for kw in self.SIMPLE_KEYWORDS) or len(message) < 80:
            return TaskComplexity.SIMPLE
        return TaskComplexity.MODERATE
```

## Solution 5: Geographic / Latency-Aware Provider Selection

Ping each provider endpoint and route to the one with lowest observed latency for the caller's region.

```python
import asyncio
import time
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class ProviderEndpoint:
    name: str
    region: str  # e.g., "us-east-1", "eu-west-1"
    ping_latency_ms: float = 999.0  # Updated by health checks
    available: bool = True
    consecutive_errors: int = 0
    MAX_ERRORS = 3


class LatencyAwareBalancer:
    def __init__(self, endpoints: list[ProviderEndpoint], ping_interval: float = 30.0):
        self.endpoints = endpoints
        self.ping_interval = ping_interval
        self.client = AsyncAnthropic()
        self._ping_task: asyncio.Task | None = None

    async def start(self):
        await self._ping_all()  # Initial ping
        self._ping_task = asyncio.create_task(self._ping_loop())

    async def stop(self):
        if self._ping_task:
            self._ping_task.cancel()

    async def _ping_endpoint(self, endpoint: ProviderEndpoint):
        """Measure latency with a minimal LLM call."""
        start = time.perf_counter()
        try:
            await asyncio.wait_for(
                self.client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=1,
                    messages=[{"role": "user", "content": "hi"}],
                ),
                timeout=5.0,
            )
            latency = (time.perf_counter() - start) * 1000
            endpoint.ping_latency_ms = latency
            endpoint.available = True
            endpoint.consecutive_errors = 0
        except Exception:
            endpoint.consecutive_errors += 1
            if endpoint.consecutive_errors >= endpoint.MAX_ERRORS:
                endpoint.available = False
                print(f"[LATENCY] Marking {endpoint.name} unavailable after {endpoint.consecutive_errors} errors")

    async def _ping_all(self):
        await asyncio.gather(*[self._ping_endpoint(ep) for ep in self.endpoints])

    async def _ping_loop(self):
        while True:
            await asyncio.sleep(self.ping_interval)
            await self._ping_all()

    def _select_endpoint(self) -> ProviderEndpoint:
        available = [ep for ep in self.endpoints if ep.available]
        if not available:
            # Fall back to any endpoint if all marked unavailable
            available = self.endpoints
        return min(available, key=lambda ep: ep.ping_latency_ms)

    async def chat(self, message: str, max_tokens: int = 1024) -> dict:
        endpoint = self._select_endpoint()
        start = time.perf_counter()
        try:
            response = await self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": message}],
            )
            elapsed = (time.perf_counter() - start) * 1000
            # Update endpoint's observed latency (EMA)
            endpoint.ping_latency_ms = 0.1 * elapsed + 0.9 * endpoint.ping_latency_ms
            return {
                "endpoint": endpoint.name,
                "region": endpoint.region,
                "latency_ms": elapsed,
                "text": response.content[0].text,
            }
        except Exception as e:
            endpoint.consecutive_errors += 1
            raise

    def latency_report(self) -> list[dict]:
        return sorted(
            [
                {
                    "name": ep.name,
                    "region": ep.region,
                    "ping_latency_ms": round(ep.ping_latency_ms, 1),
                    "available": ep.available,
                }
                for ep in self.endpoints
            ],
            key=lambda x: x["ping_latency_ms"],
        )


async def demo_latency_aware():
    endpoints = [
        ProviderEndpoint("anthropic-us", "us-east-1"),
        ProviderEndpoint("anthropic-eu", "eu-west-1"),
    ]
    balancer = LatencyAwareBalancer(endpoints)
    await balancer.start()

    results = await asyncio.gather(*[balancer.chat(f"Hello {i}") for i in range(6)])
    for r in results:
        print(f"  [{r['endpoint']}|{r['latency_ms']:.0f}ms] {r['text'][:40]}")

    print("\nLatency report:", balancer.latency_report())
    await balancer.stop()
```

## Solution 6: Budget-Constrained Multi-Provider Router

Track spend per provider; switch to a cheaper provider when a provider's daily budget is exhausted.

```python
import asyncio
import time
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class ProviderBudget:
    name: str
    daily_budget_usd: float
    input_cost_per_1m: float   # USD per 1M input tokens
    output_cost_per_1m: float  # USD per 1M output tokens
    _spent_today: float = 0.0
    _day_start: float = field(default_factory=time.time)

    def _reset_if_new_day(self):
        now = time.time()
        if now - self._day_start >= 86400:
            self._spent_today = 0.0
            self._day_start = now

    @property
    def budget_remaining(self) -> float:
        self._reset_if_new_day()
        return max(0.0, self.daily_budget_usd - self._spent_today)

    @property
    def budget_exhausted(self) -> bool:
        return self.budget_remaining <= 0.001

    def record_usage(self, input_tokens: int, output_tokens: int):
        self._reset_if_new_day()
        cost = (
            input_tokens * self.input_cost_per_1m / 1_000_000
            + output_tokens * self.output_cost_per_1m / 1_000_000
        )
        self._spent_today += cost
        return cost

    @property
    def utilization_pct(self) -> float:
        self._reset_if_new_day()
        return round(self._spent_today / max(self.daily_budget_usd, 0.001) * 100, 1)


class BudgetConstrainedRouter:
    def __init__(self, providers: list[ProviderBudget]):
        # Sorted by cost: cheapest first
        self.providers = sorted(providers, key=lambda p: p.output_cost_per_1m)
        self.client = AsyncAnthropic()
        self.total_cost = 0.0

    def _select_provider(self) -> ProviderBudget | None:
        for provider in self.providers:
            if not provider.budget_exhausted:
                return provider
        return None

    async def chat(self, message: str, max_tokens: int = 1024) -> dict:
        provider = self._select_provider()
        if provider is None:
            raise RuntimeError("All provider budgets exhausted for today")

        if provider.utilization_pct > 80:
            print(f"[BUDGET] {provider.name} at {provider.utilization_pct}% — nearing limit")

        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": message}],
        )
        cost = provider.record_usage(
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
        self.total_cost += cost
        return {
            "provider": provider.name,
            "text": response.content[0].text,
            "cost_usd": cost,
            "provider_remaining_usd": provider.budget_remaining,
        }

    def budget_report(self) -> list[dict]:
        return [
            {
                "provider": p.name,
                "budget_usd": p.daily_budget_usd,
                "spent_usd": round(p._spent_today, 4),
                "remaining_usd": round(p.budget_remaining, 4),
                "utilization_pct": p.utilization_pct,
                "exhausted": p.budget_exhausted,
            }
            for p in self.providers
        ]


async def demo_budget_router():
    providers = [
        ProviderBudget("haiku-primary", daily_budget_usd=5.0, input_cost_per_1m=0.80, output_cost_per_1m=4.0),
        ProviderBudget("haiku-secondary", daily_budget_usd=3.0, input_cost_per_1m=0.80, output_cost_per_1m=4.0),
    ]
    router = BudgetConstrainedRouter(providers)
    messages = [f"Explain concept {i} briefly." for i in range(10)]
    results = await asyncio.gather(*[router.chat(m) for m in messages])

    print(f"Total cost: ${router.total_cost:.4f}")
    print("Budget report:")
    for report in router.budget_report():
        print(f"  {report['provider']}: ${report['spent_usd']:.4f} / ${report['budget_usd']} ({report['utilization_pct']}%)")
```

## Comparison Table

| Solution | Selection Strategy | Cost Optimization | Latency Optimization | Failover | Best For |
|---|---|---|---|---|---|
| Round-Robin | Strict rotation | No | No | Next-in-cycle | Even distribution, simple setup |
| Weighted Routing | Error-rate & latency weighted | Partial (cost weight) | Yes (EMA latency) | Weight degradation | Stable multi-provider fleets |
| Hedged Requests | First-to-respond wins | No (double spend) | Yes (tail latency) | Automatic | Ultra-low latency requirements |
| Cost-Optimized | Task complexity routing | Yes | No | No | Mixed-complexity workloads |
| Latency-Aware | Lowest-ping endpoint | No | Yes (ping-based) | Mark unavailable | Geo-distributed deployments |
| Budget-Constrained | Cheapest within budget | Yes | No | Failover on budget exhaustion | Spend-controlled agents |

**Recommended**: Start with **Round-Robin** (Solution 1) for simplicity, then add **Cost-Optimized Routing** (Solution 4) to reduce spend on simple tasks. Use **Weighted Routing** (Solution 2) in production for self-healing behavior as providers degrade. Add **Budget-Constrained** (Solution 6) when operating under strict cost controls.
