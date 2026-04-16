---
title: "Agent Doesn't Implement Health-Check-Based Failover"
description: "Continuously probe model endpoints and external dependencies, automatically routing traffic to healthy alternatives when failures are detected."
category: reliability
difficulty: intermediate
tags: [failover, health-check, reliability, asyncio, resilience, routing]
---

# Agent Doesn't Implement Health-Check-Based Failover

## Problem

Agents that hardcode a single model endpoint or external service URL will fail silently or crash when that endpoint becomes unavailable. Production agents need continuous health monitoring with automatic failover to backup endpoints — returning to the primary as soon as it recovers.

---

## Option 1: Simple Round-Robin with Health Probing

```python
import asyncio
import time
import anthropic
from dataclasses import dataclass, field

@dataclass
class Endpoint:
    name: str
    model: str
    healthy: bool = True
    last_checked: float = 0.0
    failure_count: int = 0
    success_count: int = 0

class HealthCheckedRouter:
    def __init__(self, endpoints: list[Endpoint], probe_interval: float = 15.0):
        self.endpoints = endpoints
        self.probe_interval = probe_interval
        self._client = anthropic.AsyncAnthropic()
        self._probe_task: asyncio.Task | None = None

    def healthy_endpoints(self) -> list[Endpoint]:
        return [e for e in self.endpoints if e.healthy]

    def primary(self) -> Endpoint | None:
        healthy = self.healthy_endpoints()
        return healthy[0] if healthy else None

    async def probe(self, endpoint: Endpoint) -> bool:
        try:
            await asyncio.wait_for(
                self._client.messages.create(
                    model=endpoint.model,
                    max_tokens=5,
                    messages=[{"role": "user", "content": "ping"}]
                ),
                timeout=5.0
            )
            endpoint.healthy = True
            endpoint.failure_count = 0
            endpoint.success_count += 1
            return True
        except Exception:
            endpoint.failure_count += 1
            if endpoint.failure_count >= 2:
                endpoint.healthy = False
            return False

    async def _probe_loop(self):
        while True:
            await asyncio.gather(*[self.probe(e) for e in self.endpoints], return_exceptions=True)
            await asyncio.sleep(self.probe_interval)

    async def start(self):
        # Initial probe
        await asyncio.gather(*[self.probe(e) for e in self.endpoints], return_exceptions=True)
        self._probe_task = asyncio.create_task(self._probe_loop())

    async def call(self, messages: list[dict], max_tokens: int = 512) -> str:
        ep = self.primary()
        if ep is None:
            raise RuntimeError("No healthy endpoints available")
        resp = await self._client.messages.create(
            model=ep.model, max_tokens=max_tokens, messages=messages
        )
        return resp.content[0].text

router = HealthCheckedRouter([
    Endpoint(name="primary", model="claude-sonnet-4-6"),
    Endpoint(name="fallback-haiku", model="claude-haiku-4-5-20251001"),
])

async def main():
    await router.start()
    result = await router.call([{"role": "user", "content": "What is 2+2?"}])
    print(f"Response from {router.primary().name}: {result}")
    if router._probe_task:
        router._probe_task.cancel()

asyncio.run(main())
```

---

## Option 2: Priority Failover with Automatic Recovery

```python
import asyncio
import time
import anthropic
from dataclasses import dataclass, field
from enum import Enum

class EndpointState(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    RECOVERING = "recovering"

@dataclass
class ManagedEndpoint:
    name: str
    model: str
    priority: int = 0  # lower = higher priority
    state: EndpointState = EndpointState.HEALTHY
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_failure_at: float = 0.0
    latency_ms: float = 0.0

    FAILURE_THRESHOLD = 3
    RECOVERY_THRESHOLD = 2
    DEGRADED_LATENCY_MS = 3000.0

class PriorityFailoverRouter:
    def __init__(self, endpoints: list[ManagedEndpoint]):
        self.endpoints = sorted(endpoints, key=lambda e: e.priority)
        self._client = anthropic.AsyncAnthropic()
        self._lock = asyncio.Lock()

    def _best_endpoint(self) -> ManagedEndpoint | None:
        candidates = [e for e in self.endpoints if e.state in (EndpointState.HEALTHY, EndpointState.DEGRADED, EndpointState.RECOVERING)]
        if not candidates:
            return None
        # Prefer HEALTHY > RECOVERING > DEGRADED, then by priority
        def score(e: ManagedEndpoint) -> tuple:
            state_score = {EndpointState.HEALTHY: 0, EndpointState.RECOVERING: 1, EndpointState.DEGRADED: 2}
            return (state_score.get(e.state, 3), e.priority)
        return min(candidates, key=score)

    def _record_success(self, ep: ManagedEndpoint, latency_ms: float):
        ep.latency_ms = latency_ms
        ep.consecutive_failures = 0
        ep.consecutive_successes += 1
        if ep.state == EndpointState.RECOVERING and ep.consecutive_successes >= ep.RECOVERY_THRESHOLD:
            ep.state = EndpointState.HEALTHY
            print(f"[FAILOVER] {ep.name} recovered → HEALTHY")
        elif ep.state == EndpointState.HEALTHY and latency_ms > ep.DEGRADED_LATENCY_MS:
            ep.state = EndpointState.DEGRADED
            print(f"[FAILOVER] {ep.name} slow → DEGRADED")

    def _record_failure(self, ep: ManagedEndpoint):
        ep.consecutive_failures += 1
        ep.consecutive_successes = 0
        ep.last_failure_at = time.time()
        if ep.consecutive_failures >= ep.FAILURE_THRESHOLD:
            ep.state = EndpointState.DOWN
            print(f"[FAILOVER] {ep.name} → DOWN after {ep.consecutive_failures} failures")
        else:
            ep.state = EndpointState.DEGRADED

    async def _probe_down_endpoints(self):
        """Periodically probe DOWN endpoints for recovery."""
        while True:
            await asyncio.sleep(30)
            for ep in self.endpoints:
                if ep.state == EndpointState.DOWN:
                    # Attempt a probe
                    try:
                        t0 = time.time()
                        await asyncio.wait_for(
                            self._client.messages.create(model=ep.model, max_tokens=5,
                                messages=[{"role": "user", "content": "ping"}]),
                            timeout=5.0
                        )
                        ep.state = EndpointState.RECOVERING
                        ep.consecutive_successes = 1
                        print(f"[FAILOVER] {ep.name} probe succeeded → RECOVERING")
                    except Exception:
                        pass

    async def call(self, messages: list[dict], max_tokens: int = 512) -> tuple[str, str]:
        """Returns (response_text, endpoint_name_used)."""
        for ep in sorted(self.endpoints, key=lambda e: (e.state == EndpointState.DOWN, e.priority)):
            if ep.state == EndpointState.DOWN:
                continue
            try:
                t0 = time.time()
                resp = await asyncio.wait_for(
                    self._client.messages.create(model=ep.model, max_tokens=max_tokens, messages=messages),
                    timeout=10.0
                )
                latency = (time.time() - t0) * 1000
                async with self._lock:
                    self._record_success(ep, latency)
                return resp.content[0].text, ep.name
            except Exception as e:
                print(f"[FAILOVER] {ep.name} failed: {e}")
                async with self._lock:
                    self._record_failure(ep)

        raise RuntimeError("All endpoints unavailable")

router = PriorityFailoverRouter([
    ManagedEndpoint(name="sonnet", model="claude-sonnet-4-6", priority=0),
    ManagedEndpoint(name="haiku", model="claude-haiku-4-5-20251001", priority=1),
])

async def main():
    asyncio.create_task(router._probe_down_endpoints())
    text, ep_name = await router.call([{"role": "user", "content": "Explain failover in one sentence."}])
    print(f"[{ep_name}]: {text}")

asyncio.run(main())
```

---

## Option 3: Latency-Weighted Endpoint Selection

```python
import asyncio
import time
import anthropic
import random
from dataclasses import dataclass, field
from collections import deque

client = anthropic.AsyncAnthropic()

@dataclass
class LatencyTracker:
    model: str
    name: str
    window: deque = field(default_factory=lambda: deque(maxlen=10))
    error_rate: float = 0.0
    _errors: deque = field(default_factory=lambda: deque(maxlen=20))

    def record_latency(self, ms: float):
        self.window.append(ms)

    def record_error(self):
        self._errors.append(1)
        self.error_rate = sum(self._errors) / len(self._errors)

    def record_success(self):
        self._errors.append(0)
        self.error_rate = sum(self._errors) / len(self._errors)

    def p95_latency(self) -> float:
        if not self.window:
            return 999.0
        sorted_w = sorted(self.window)
        idx = max(0, int(len(sorted_w) * 0.95) - 1)
        return sorted_w[idx]

    def score(self) -> float:
        """Lower is better: penalize high latency and high error rate."""
        return self.p95_latency() * (1 + self.error_rate * 5)

class LatencyWeightedRouter:
    def __init__(self, trackers: list[LatencyTracker]):
        self.trackers = trackers

    def _select(self) -> LatencyTracker:
        """Weighted random selection inversely proportional to score."""
        scores = [t.score() for t in self.trackers]
        # Convert to weights (inverse score)
        weights = [1.0 / max(s, 0.1) for s in scores]
        total = sum(weights)
        r = random.uniform(0, total)
        cumulative = 0.0
        for tracker, weight in zip(self.trackers, weights):
            cumulative += weight
            if r <= cumulative:
                return tracker
        return self.trackers[-1]

    async def call(self, messages: list[dict], max_tokens: int = 512) -> tuple[str, str]:
        tracker = self._select()
        t0 = time.time()
        try:
            resp = await client.messages.create(
                model=tracker.model, max_tokens=max_tokens, messages=messages
            )
            latency_ms = (time.time() - t0) * 1000
            tracker.record_latency(latency_ms)
            tracker.record_success()
            return resp.content[0].text, tracker.name
        except Exception as e:
            tracker.record_error()
            # Fallback to next best
            fallback = min([t for t in self.trackers if t != tracker], key=lambda t: t.score(), default=None)
            if fallback:
                resp = await client.messages.create(
                    model=fallback.model, max_tokens=max_tokens, messages=messages
                )
                return resp.content[0].text, f"{fallback.name}(fallback)"
            raise

router = LatencyWeightedRouter([
    LatencyTracker(name="sonnet", model="claude-sonnet-4-6"),
    LatencyTracker(name="haiku", model="claude-haiku-4-5-20251001"),
])

async def main():
    for i in range(5):
        text, name = await router.call([{"role": "user", "content": f"Count to {i+1}."}])
        print(f"[{name}] {text[:60]}")

asyncio.run(main())
```

---

## Option 4: Active Health Check with Configurable Thresholds

```python
import asyncio
import time
import anthropic
from dataclasses import dataclass, field
import statistics

client = anthropic.AsyncAnthropic()

@dataclass
class HealthConfig:
    probe_interval_s: float = 20.0
    probe_timeout_s: float = 4.0
    mark_down_after_failures: int = 3
    mark_up_after_successes: int = 2
    latency_warning_ms: float = 2500.0

@dataclass
class EndpointHealth:
    name: str
    model: str
    is_up: bool = True
    latencies: list = field(default_factory=list)
    failure_streak: int = 0
    success_streak: int = 0

    def avg_latency(self) -> float:
        return statistics.mean(self.latencies[-10:]) if self.latencies else 0.0

class ActiveHealthMonitor:
    def __init__(self, endpoints: list[EndpointHealth], config: HealthConfig = None):
        self.endpoints = endpoints
        self.config = config or HealthConfig()
        self._task: asyncio.Task | None = None

    async def _probe_one(self, ep: EndpointHealth):
        try:
            t0 = time.time()
            await asyncio.wait_for(
                client.messages.create(model=ep.model, max_tokens=3,
                    messages=[{"role": "user", "content": "ok"}]),
                timeout=self.config.probe_timeout_s
            )
            latency_ms = (time.time() - t0) * 1000
            ep.latencies.append(latency_ms)
            ep.failure_streak = 0
            ep.success_streak += 1
            if not ep.is_up and ep.success_streak >= self.config.mark_up_after_successes:
                ep.is_up = True
                print(f"[HEALTH] {ep.name} is UP (avg={ep.avg_latency():.0f}ms)")
            if latency_ms > self.config.latency_warning_ms:
                print(f"[HEALTH] {ep.name} SLOW: {latency_ms:.0f}ms")
        except Exception as e:
            ep.failure_streak += 1
            ep.success_streak = 0
            if ep.is_up and ep.failure_streak >= self.config.mark_down_after_failures:
                ep.is_up = False
                print(f"[HEALTH] {ep.name} is DOWN: {e}")

    async def start(self):
        # Initial probe
        await asyncio.gather(*[self._probe_one(e) for e in self.endpoints])
        self._task = asyncio.create_task(self._probe_loop())

    async def _probe_loop(self):
        while True:
            await asyncio.sleep(self.config.probe_interval_s)
            await asyncio.gather(*[self._probe_one(e) for e in self.endpoints])

    def best(self) -> EndpointHealth | None:
        up = [e for e in self.endpoints if e.is_up]
        if not up:
            return None
        return min(up, key=lambda e: e.avg_latency() or 9999)

    async def call(self, messages: list[dict], max_tokens: int = 512) -> str:
        ep = self.best()
        if ep is None:
            raise RuntimeError("No healthy endpoints")
        resp = await client.messages.create(model=ep.model, max_tokens=max_tokens, messages=messages)
        return resp.content[0].text

monitor = ActiveHealthMonitor(
    endpoints=[
        EndpointHealth(name="sonnet", model="claude-sonnet-4-6"),
        EndpointHealth(name="haiku", model="claude-haiku-4-5-20251001"),
    ],
    config=HealthConfig(probe_interval_s=30.0, mark_down_after_failures=2)
)

async def main():
    await monitor.start()
    resp = await monitor.call([{"role": "user", "content": "What is health checking?"}])
    print(resp[:200])
    if monitor._task:
        monitor._task.cancel()

asyncio.run(main())
```

---

## Option 5: Model-Tier Failover with Quality Degradation Warning

```python
import asyncio
import anthropic
from dataclasses import dataclass
from enum import Enum

client = anthropic.AsyncAnthropic()

class Tier(Enum):
    BEST = "best"
    GOOD = "good"
    MINIMAL = "minimal"

@dataclass
class TierConfig:
    tier: Tier
    model: str
    max_tokens: int
    quality_note: str

TIERS = [
    TierConfig(Tier.BEST, "claude-sonnet-4-6", 2048, "Full quality"),
    TierConfig(Tier.GOOD, "claude-haiku-4-5-20251001", 1024, "Reduced quality — primary unavailable"),
    TierConfig(Tier.MINIMAL, "claude-haiku-4-5-20251001", 256, "Minimal mode — fallback chain exhausted"),
]

class TieredFailoverAgent:
    def __init__(self):
        self._failed_tiers: set[Tier] = set()
        self._lock = asyncio.Lock()

    async def call(self, messages: list[dict], on_degradation=None) -> dict:
        for tier_cfg in TIERS:
            if tier_cfg.tier in self._failed_tiers:
                continue
            try:
                resp = await asyncio.wait_for(
                    client.messages.create(
                        model=tier_cfg.model,
                        max_tokens=tier_cfg.max_tokens,
                        messages=messages
                    ),
                    timeout=12.0
                )
                if tier_cfg.tier != Tier.BEST and on_degradation:
                    await on_degradation(tier_cfg.quality_note)
                return {
                    "text": resp.content[0].text,
                    "tier": tier_cfg.tier.value,
                    "quality_note": tier_cfg.quality_note,
                }
            except Exception as e:
                print(f"[TIER FAILOVER] {tier_cfg.tier.value} failed: {e}")
                async with self._lock:
                    self._failed_tiers.add(tier_cfg.tier)

        raise RuntimeError("All model tiers failed")

    async def recover_tier(self, tier: Tier):
        """Call when health check confirms a tier is back."""
        async with self._lock:
            self._failed_tiers.discard(tier)
            print(f"[TIER FAILOVER] {tier.value} recovered")

agent = TieredFailoverAgent()

async def main():
    async def warn(msg: str):
        print(f"[QUALITY WARNING] {msg}")

    result = await agent.call(
        [{"role": "user", "content": "Summarize the benefits of model failover."}],
        on_degradation=warn
    )
    print(f"[{result['tier']}] {result['text'][:150]}")

asyncio.run(main())
```

---

## Option 6: Geographic/Regional Failover with Latency Routing

```python
import asyncio
import time
import anthropic
from dataclasses import dataclass, field
import heapq

client = anthropic.AsyncAnthropic()

@dataclass
class Region:
    name: str
    model: str
    priority: int
    is_available: bool = True
    latency_ema_ms: float = 500.0  # exponential moving average
    ema_alpha: float = 0.3

    def update_latency(self, sample_ms: float):
        self.latency_ema_ms = self.ema_alpha * sample_ms + (1 - self.ema_alpha) * self.latency_ema_ms

class GeoFailoverRouter:
    def __init__(self, regions: list[Region]):
        self.regions = regions
        self._client = anthropic.AsyncAnthropic()
        self._lock = asyncio.Lock()

    def _sorted_regions(self) -> list[Region]:
        available = [r for r in self.regions if r.is_available]
        # Sort by (priority, latency_ema) — primary region first, then latency
        return sorted(available, key=lambda r: (r.priority, r.latency_ema_ms))

    async def call(self, messages: list[dict], max_tokens: int = 512) -> dict:
        tried: list[str] = []
        for region in self._sorted_regions():
            tried.append(region.name)
            t0 = time.time()
            try:
                resp = await asyncio.wait_for(
                    self._client.messages.create(
                        model=region.model, max_tokens=max_tokens, messages=messages
                    ),
                    timeout=8.0
                )
                latency = (time.time() - t0) * 1000
                async with self._lock:
                    region.update_latency(latency)
                return {
                    "text": resp.content[0].text,
                    "region": region.name,
                    "latency_ms": round(latency, 1),
                    "tried": tried,
                }
            except asyncio.TimeoutError:
                async with self._lock:
                    region.update_latency(9999)
                    if region.latency_ema_ms > 5000:
                        region.is_available = False
                        print(f"[GEO] {region.name} timed out — marking unavailable")
            except Exception as e:
                print(f"[GEO] {region.name} error: {e}")
                async with self._lock:
                    region.is_available = False

        raise RuntimeError(f"All regions failed. Tried: {tried}")

    async def restore(self, region_name: str):
        async with self._lock:
            for r in self.regions:
                if r.name == region_name:
                    r.is_available = True
                    r.latency_ema_ms = 500.0
                    print(f"[GEO] {region_name} restored")

router = GeoFailoverRouter([
    Region(name="us-primary", model="claude-sonnet-4-6", priority=0),
    Region(name="eu-secondary", model="claude-sonnet-4-6", priority=1),
    Region(name="ap-tertiary", model="claude-haiku-4-5-20251001", priority=2),
])

async def main():
    results = await asyncio.gather(*[
        router.call([{"role": "user", "content": f"Query {i}"}])
        for i in range(3)
    ])
    for r in results:
        print(f"[{r['region']} {r['latency_ms']}ms] {r['text'][:60]}")

asyncio.run(main())
```

---

## Comparison

| Option | Selection Strategy | Recovery | Latency Tracking | Best For |
|--------|------------------|----------|------------------|----------|
| 1 – Round-Robin | First healthy | Probe loop | None | Simple failover |
| 2 – Priority Failover | Priority + state machine | Automatic probe | None | Multi-tier deployments |
| 3 – Latency-Weighted | Inverse-score weighted random | Implicit via success | P95 window | Traffic splitting |
| 4 – Active Health Monitor | Best avg latency | Configurable thresholds | Moving average | Production monitoring |
| 5 – Tier Degradation | Quality tiers | Manual restore | None | Graceful degradation UX |
| 6 – Geo Failover | Priority + EMA latency | EMA threshold restore | EMA | Multi-region deployments |

**Recommendation:** Use Option 4 for general production use — it combines active health probing, configurable thresholds, and latency-aware routing. Add Option 5's quality degradation warnings so users know when they're receiving reduced-quality responses during an incident.
